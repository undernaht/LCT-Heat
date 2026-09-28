"""Сборка AreaModel из слоёв GeoJSON.

Единственное место, кроме export, где происходит перепроецирование.

Ничто здесь не бросает исключений на кривых данных: всё, что не удалось
прочитать, попадает в `ImportReport` и доходит до API. Раньше отсутствие поля
`id` давало голый 500, а `MultiPolygon` — молча пустой слой; и то и другое
означало, что сервис нельзя показать никому, кроме себя.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.base import BaseGeometry

from ..config import get_config
from ..domain.enums import BuildingUse, HeatNodeKind, Laying, SurfaceClass, UtilityKind
from ..domain.models import (
    AreaModel,
    Building,
    Greenery,
    HeatEdge,
    HeatLoad,
    HeatNetwork,
    HeatNode,
    Parcel,
    Railway,
    Road,
    SurfacePatch,
    TempSchedule,
    Utility,
    WaterBody,
)
from ..geo import crs, elevation
from ..hydraulics import diameters
from .geometry import geometries_of, is_valid_metric, properties_of, reproject
from .report import ImportReport

WGS84 = "EPSG:4326"


# --- Метаданные района ------------------------------------------------------


def read_meta(area_dir: Path) -> dict[str, Any]:
    path = area_dir / "meta.json"
    if not path.exists():
        raise FileNotFoundError(f"нет meta.json в {area_dir}")
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_crs(meta: dict[str, Any]) -> tuple[str, str]:
    """Исходная и метрическая системы координат.

    `bbox` в meta.json исторически писался как [min_lat, min_lon, max_lat,
    max_lon], тогда как RFC 7946 требует [minlon, minlat, maxlon, maxlat].
    Определить порядок по значениям нельзя: для Москвы и широта, и долгота
    меньше 90. Поэтому порядок объявляется явно полем `bbox_order`, а лучший
    вариант — сразу записать выбранную метрическую проекцию в `metric_crs`,
    и тогда порядок вообще перестаёт влиять на расчёт.
    """
    source = str(meta.get("crs") or WGS84)

    metric = meta.get("metric_crs")
    if metric:
        return source, str(metric)

    bbox = meta.get("bbox")
    if not bbox or len(bbox) != 4:
        raise ValueError("в meta.json нет ни metric_crs, ни корректного bbox")

    if str(meta.get("bbox_order", "latlon")).lower() == "lonlat":
        min_lon, min_lat, max_lon, max_lat = bbox
    else:
        min_lat, min_lon, max_lat, max_lon = bbox

    if source != WGS84:
        # bbox задан в исходной проекции — переводим в градусы, чтобы выбрать зону
        lon_min, lat_min = crs.point_to_wgs84_from(min_lon, min_lat, source)
        lon_max, lat_max = crs.point_to_wgs84_from(max_lon, max_lat, source)
        min_lon, min_lat, max_lon, max_lat = lon_min, lat_min, lon_max, lat_max

    return source, crs.utm_epsg_for_bbox(min_lon, min_lat, max_lon, max_lat)


# --- Чтение слоёв -----------------------------------------------------------


def _read(area_dir: Path, name: str, report: ImportReport, *, required: bool) -> list[dict]:
    path = area_dir / f"{name}.geojson"
    if not path.exists():
        if required:
            report.add(name, "missing_file", "файл слоя отсутствует")
        return []

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        report.add(name, "bad_value", f"файл не читается как JSON: {error}")
        return []

    if isinstance(payload, dict) and payload.get("type") == "Feature":
        return [payload]                      # одиночный Feature — тоже валидный GeoJSON
    if not isinstance(payload, dict) or "features" not in payload:
        report.add(name, "bad_value", "не FeatureCollection и не Feature")
        return []

    features = payload.get("features") or []
    if not features:
        report.add(name, "empty", "слой пуст")
    return features


def _require(props: dict, key: str, layer: str, report: ImportReport) -> Any | None:
    value = props.get(key)
    if value is None:
        report.add(layer, "missing_field", f"нет обязательного поля «{key}»")
        return None
    return value


def _enum(
    factory: Callable[[str], Any], value: Any, layer: str, field: str,
    report: ImportReport, default: Any = None,
) -> Any:
    try:
        return factory(str(value))
    except (ValueError, KeyError):
        report.add(layer, "bad_value", f"неизвестное значение «{field}» = {value!r}")
        return default


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def road_geometry(highway: str) -> dict[str, Any]:
    table = get_config().costs["road_geometry"]
    return table.get(highway, table["residential"])


LANDUSE_SURFACE: dict[str, SurfaceClass] = {
    "grass": SurfaceClass.LAWN,
    "meadow": SurfaceClass.LAWN,
    "village_green": SurfaceClass.LAWN,
    "recreation_ground": SurfaceClass.LAWN,
    "forest": SurfaceClass.LAWN,
    "industrial": SurfaceClass.GROUND,
}
LEISURE_SURFACE: dict[str, SurfaceClass] = {
    "park": SurfaceClass.LAWN,
    "garden": SurfaceClass.LAWN,
    "pitch": SurfaceClass.LAWN,
}


def _surface_of(props: dict[str, Any]) -> SurfaceClass:
    leisure = props.get("leisure")
    if leisure and leisure in LEISURE_SURFACE:
        return LEISURE_SURFACE[leisure]
    landuse = props.get("landuse")
    if landuse and landuse in LANDUSE_SURFACE:
        return LANDUSE_SURFACE[landuse]
    return SurfaceClass.GROUND


# --- Основная загрузка ------------------------------------------------------


def load_area(area_dir: Path) -> AreaModel:
    meta = read_meta(area_dir)
    source_crs, metric_crs = resolve_crs(meta)

    report = ImportReport(source_crs=source_crs, metric_crs=metric_crs)
    area = AreaModel(crs=metric_crs)
    area.import_report = report

    def each(layer: str, required: bool = True):
        """Итератор «геометрия в метрах + свойства» с учётом Multi-* и null."""
        for feature in _read(area_dir, layer, report, required=required):
            props = properties_of(feature)
            found = False
            for geom in geometries_of(feature):
                projected = reproject(geom, source_crs, metric_crs)
                if not is_valid_metric(projected):
                    report.add(layer, "bad_geometry",
                               "геометрия выродилась при перепроецировании — "
                               "проверьте объявленную систему координат")
                    continue
                found = True
                yield projected, props
            if not found:
                report.add(layer, "bad_geometry", "объект без пригодной геометрии")

    # --- Здания: существующие и перспективные ---
    for layer, perspective in (("buildings_load", False), ("gen_perspective", True)):
        loaded = 0
        for geom, props in each(layer, required=(layer == "buildings_load")):
            if not isinstance(geom, Polygon):
                report.add(layer, "bad_geometry", f"ожидался полигон, получен {geom.geom_type}")
                continue
            identifier = _require(props, "id", layer, report)
            if identifier is None:
                continue

            use = _enum(BuildingUse, props.get("use", BuildingUse.OTHER.value),
                        layer, "use", report, BuildingUse.OTHER)
            load = None
            if props.get("q_total_gcal_h") is not None:
                load = HeatLoad(
                    heating=_num(props.get("q_heating_gcal_h")) or 0.0,
                    dhw_max=_num(props.get("q_dhw_gcal_h")) or 0.0,
                    source="estimated",
                )
            area.buildings.append(
                Building(
                    id=str(identifier),
                    geom=geom,
                    use=use,
                    floors=_int(props.get("floors")),
                    height_m=_num(props.get("height_m") or props.get("height")),
                    built_year=_int(props.get("built_year")),
                    is_perspective=bool(props.get("is_perspective", perspective)),
                    load=load,
                    tags={"name": str(props["name"])} if props.get("name") else {},
                )
            )
            loaded += 1
        report.count(layer, loaded)

    # --- Тепловая сеть ---
    network = HeatNetwork()
    loaded = 0
    for geom, props in each("gen_heat_nodes", required=False):
        if not isinstance(geom, Point):
            continue
        identifier = _require(props, "id", "gen_heat_nodes", report)
        kind = _enum(HeatNodeKind, props.get("kind", HeatNodeKind.JUNCTION.value),
                     "gen_heat_nodes", "kind", report, HeatNodeKind.JUNCTION)
        if identifier is None:
            continue
        network.nodes.append(
            HeatNode(
                id=str(identifier), geom=geom, kind=kind, name=props.get("name"),
                capacity_gcal_h=_num(props.get("capacity_gcal_h")),
                reserve_gcal_h=_num(props.get("reserve_gcal_h")),
                head_available_m=_num(props.get("head_available_m")),
                static_head_m=_num(props.get("static_head_m")),
            )
        )
        loaded += 1
    report.count("gen_heat_nodes", loaded)

    loaded = 0
    for geom, props in each("gen_heat_edges"):
        if not isinstance(geom, LineString):
            continue
        identifier = _require(props, "id", "gen_heat_edges", report)
        du = _int(_require(props, "du_mm", "gen_heat_edges", report))
        if identifier is None or du is None:
            continue
        schedule = TempSchedule.parse(str(props.get("schedule", "130/70")))
        capacity = _num(props.get("capacity_gcal_h"))
        if capacity is None:
            # Пропускная способность раньше бралась только из атрибута, и при его
            # отсутствии резерв обнулялся — то есть любой участок без этого поля
            # считался запертым. Считаем её сами по Ду и графику.
            capacity = diameters.capacity_gcal_h(du, schedule, is_branch=False)
            report.add("gen_heat_edges", "computed",
                       "пропускная способность не задана, рассчитана по Ду и графику")

        network.edges.append(
            HeatEdge(
                id=str(identifier), geom=geom, du_mm=du,
                laying=_enum(Laying, props.get("laying", Laying.CHANNEL.value),
                             "gen_heat_edges", "laying", report, Laying.CHANNEL),
                schedule=schedule,
                node_a=props.get("node_a"), node_b=props.get("node_b"),
                load_gcal_h=_num(props.get("load_gcal_h")) or 0.0,
                capacity_gcal_h=capacity,
                is_main=bool(props.get("is_main")),
            )
        )
        loaded += 1
    report.count("gen_heat_edges", loaded)
    area.heat = network

    # --- Чужие сети ---
    loaded = 0
    for geom, props in each("gen_utilities", required=False):
        if not isinstance(geom, LineString):
            continue
        kind = _enum(UtilityKind, props.get("kind"), "gen_utilities", "kind", report)
        if kind is None:
            continue
        area.utilities.append(
            Utility(
                id=str(props.get("id") or f"u_{loaded}"), geom=geom, kind=kind,
                pressure_class=props.get("pressure_class"),
                voltage_kv=_num(props.get("voltage_kv")),
                depth_m=_num(props.get("depth_m")),
                diameter_mm=_int(props.get("diameter_mm")),
            )
        )
        loaded += 1
    report.count("gen_utilities", loaded)

    # --- Улицы ---
    loaded = 0
    for geom, props in each("roads"):
        if not isinstance(geom, LineString) or geom.length < 1.0:
            continue
        highway = str(props.get("highway", "residential"))
        spec = road_geometry(highway)
        area.roads.append(
            Road(
                id=f"r_{props.get('osm_id', loaded)}", geom=geom,
                surface_class=SurfaceClass(spec["surface"]),
                width_m=_num(props.get("width")) or spec["width_m"],
                name=props.get("name"),
                requires_closed_crossing=bool(spec["closed_crossing"]),
            )
        )
        loaded += 1
    report.count("roads", loaded)

    # --- Железные дороги ---
    loaded = 0
    for geom, props in each("railways", required=False):
        if not isinstance(geom, LineString):
            continue
        # Тег electrified раньше читался и не использовался, из-за чего к
        # электрифицированной дороге применялся отступ 4,0 м вместо 10,75 м.
        electrified = props.get("electrified")
        area.railways.append(
            Railway(
                id=f"rw_{props.get('osm_id', loaded)}", geom=geom,
                gauge_mm=_int(props.get("gauge")) or 1520,
                electrified=bool(electrified) and str(electrified).lower() != "no",
            )
        )
        loaded += 1
    report.count("railways", loaded)

    # --- Вода ---
    loaded = 0
    for geom, props in each("water", required=False):
        if isinstance(geom, Polygon):
            area.water.append(
                WaterBody(id=f"w_{props.get('osm_id', loaded)}", geom=geom,
                          name=props.get("name"))
            )
            loaded += 1
    report.count("water", loaded)

    # --- Зелёные насаждения ---
    loaded = 0
    for geom, props in each("greenery", required=False):
        points: list[BaseGeometry]
        if isinstance(geom, LineString):
            # Ряд деревьев — стволы через 6 м: норматив меряет до ствола
            step = 6.0
            count = max(2, int(geom.length / step) + 1)
            points = [geom.interpolate(geom.length * i / (count - 1)) for i in range(count)]
        elif isinstance(geom, Point):
            points = [geom]
        else:
            continue

        for index, point in enumerate(points):
            area.greenery.append(
                Greenery(id=f"g_{props.get('osm_id', loaded)}_{index}", geom=point,
                         is_tree=bool(props.get("is_tree", True)))
            )
            loaded += 1
    report.count("greenery", loaded)

    # --- Землепользование ---
    loaded = 0
    for geom, props in each("landuse", required=False):
        if not isinstance(geom, Polygon):
            continue
        area.surfaces.append(
            SurfacePatch(id=f"s_{props.get('osm_id', loaded)}", geom=geom,
                         surface_class=_surface_of(props))
        )
        loaded += 1
    report.count("landuse", loaded)

    # --- Участки ---
    loaded = 0
    for geom, props in each("gen_parcels", required=False):
        if not isinstance(geom, Polygon):
            continue
        area.parcels.append(
            Parcel(
                id=str(props.get("id") or f"p_{loaded}"), geom=geom,
                cadastral_number=props.get("cadastral_number"),
                is_public=bool(props.get("is_public")),
                is_protected_zone=bool(props.get("is_protected_zone")),
                prohibits_laying=bool(props.get("prohibits_laying")),
            )
        )
        loaded += 1
    report.count("gen_parcels", loaded)

    # --- Рельеф ---
    #
    # Отдельный слой: GeoTIFF, а не GeoJSON. Без него сервис работает, только
    # продольный профиль недоступен и уклон не проверяется.
    area.terrain = elevation.load(area_dir, metric_crs)
    if area.terrain.available:
        report.count("elevation", int(area.terrain.values.size))
    else:
        report.add("elevation", "empty",
                   "рельеф не приложен: профиль и проверка уклона недоступны")

    return area
