"""Входные данные кейса: разбор GeoJSON по §1 приложения.

Правила разбора:
* идентификаторы сохраняются как есть — строка остаётся строкой, число числом,
  потому что в выходе ссылки должны совпадать с входом по значению и типу;
* геометрия сразу переводится в EPSG:32637, модель наружу метрическая;
* объекты с ошибками не роняют разбор, а попадают в протокол — молчаливая
  потеря объекта хуже отказа.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Iterable

from shapely.geometry import LineString, MultiPolygon, Point, Polygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from ..geo import crs
from .rules import Rules, get_rules

Id = str | int | float


@dataclass
class ExistingEdge:
    id: Id
    geom: LineString
    diameter: int


@dataclass
class ExistingChamber:
    id: Id
    geom: Point
    connections: int = 0          # занятых примыканий по §2.2 спецификации
    raw_coords: list[float] | None = None   # входные координаты WGS 84 — в выход копируются побитово


@dataclass
class OksPoint:
    id: Id
    geom: Point
    flow_tph: float
    polygon_id: Id | None = None  # полигон ОКС, внутри которого лежит точка
    raw_coords: list[float] | None = None


@dataclass
class Restriction:
    id: Id
    geom: BaseGeometry
    type: str                     # канонический тип по таблице 2
    raw_type: str
    properties: dict[str, Any] = dc_field(default_factory=dict)


@dataclass
class Issue:
    level: str                    # error | warning | info
    where: str
    detail: str
    count: int = 1


@dataclass
class CaseInput:
    calc_crs: str
    source: Point | None
    source_id: Id | None
    network: list[ExistingEdge]
    chambers: list[ExistingChamber]
    oks: list[OksPoint]
    restrictions: list[Restriction]
    unknown_restrictions: list[Restriction]
    issues: list[Issue]
    name: str = ""

    # --- индексы ---

    def restrictions_of(self, canonical: str) -> list[Restriction]:
        return [r for r in self.restrictions if r.type == canonical]

    def oks_by_id(self, oks_id: Id) -> OksPoint | None:
        return next((o for o in self.oks if o.id == oks_id), None)

    def polygon(self, polygon_id: Id) -> Restriction | None:
        return next((r for r in self.restrictions if r.id == polygon_id), None)

    @property
    def total_flow_tph(self) -> float:
        return sum(o.flow_tph for o in self.oks)

    def bounds(self) -> tuple[float, float, float, float]:
        geoms: list[BaseGeometry] = [e.geom for e in self.network]
        geoms += [c.geom for c in self.chambers] + [o.geom for o in self.oks]
        geoms += [r.geom for r in self.restrictions]
        if self.source is not None:
            geoms.append(self.source)
        xs0, ys0, xs1, ys1 = zip(*(g.bounds for g in geoms))
        return min(xs0), min(ys0), max(xs1), max(ys1)

    def stats(self) -> dict[str, Any]:
        by_type = Counter(r.type for r in self.restrictions)
        return {
            "source": self.source is not None,
            "network_edges": len(self.network),
            "network_length_m": round(sum(e.geom.length for e in self.network), 1),
            "chambers": len(self.chambers),
            "oks_points": len(self.oks),
            "total_flow_tph": round(self.total_flow_tph, 3),
            "restrictions": dict(by_type),
            "unknown_restrictions": len(self.unknown_restrictions),
            "issues": len(self.issues),
        }


# --- Разбор -----------------------------------------------------------------


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def _explode(geom: BaseGeometry) -> Iterable[BaseGeometry]:
    """Части составной геометрии: многочастный участок сети (MultiLineString из
    рабочих систем) разбирается на линии с общим id; многоугольники не трогаются."""
    if hasattr(geom, "geoms") and not isinstance(geom, MultiPolygon):
        for g in geom.geoms:
            yield from _explode(g)
    else:
        yield geom


def load_case(path: Path | str, *, rules: Rules | None = None, name: str | None = None) -> CaseInput:
    with Path(path).open(encoding="utf-8") as fh:
        data = json.load(fh)
    return parse_case(data, rules=rules, name=name or Path(path).stem)


def parse_case(data: dict[str, Any], *, rules: Rules | None = None, name: str = "") -> CaseInput:
    rules = rules or get_rules()
    calc_crs = str(rules.raw["meta"]["calc_crs"])
    input_crs = str(rules.raw["meta"]["input_crs"])

    issues: list[Issue] = []
    counts: Counter[str] = Counter()

    def issue(level: str, where: str, detail: str) -> None:
        key = (level, where, detail)
        counts[key] += 1
        if counts[key] == 1:
            issues.append(Issue(level, where, detail))
        else:
            next(i for i in issues if (i.level, i.where, i.detail) == key).count += 1

    if data.get("type") != "FeatureCollection":
        issues.append(Issue("error", "root", f"ожидался FeatureCollection, получен {data.get('type')!r}"))
        features: list[dict[str, Any]] = []
    else:
        features = data.get("features") or []

    source: Point | None = None
    source_id: Id | None = None
    network: list[ExistingEdge] = []
    chambers: list[ExistingChamber] = []
    oks: list[OksPoint] = []
    restrictions: list[Restriction] = []
    unknown: list[Restriction] = []

    for index, feature in enumerate(features):
        props = feature.get("properties") or {}
        object_type = props.get("object_type")
        raw_geom = feature.get("geometry")
        where = f"feature[{index}]"

        if object_type not in {"source", "heat_network", "heat_chamber", "oks_connection_point", "restriction"}:
            issue("warning", where, f"неизвестный object_type {object_type!r} — пропущен")
            continue
        if not raw_geom:
            issue("error", f"{object_type}", "объект без геометрии — пропущен")
            continue
        oid = props.get("id")
        if oid is None:
            issue("error", f"{object_type}", "объект без id — пропущен")
            continue

        raw_point = list(raw_geom.get("coordinates", [])) if raw_geom.get("type") == "Point" else None
        try:
            geom = shape(raw_geom)
        except Exception as exc:  # noqa: BLE001 — любая ошибка разбора геометрии
            issue("error", f"{object_type} {oid}", f"геометрия не разбирается: {exc}")
            continue
        if geom.is_empty:
            issue("error", f"{object_type} {oid}", "пустая геометрия — пропущен")
            continue
        try:
            geom = crs.project(geom, input_crs, calc_crs)
        except Exception as exc:  # noqa: BLE001
            issue("error", f"{object_type} {oid}", f"не перепроецируется: {exc}")
            continue

        if object_type == "source":
            if source is not None:
                issue("warning", "source", "источников больше одного; используется первый")
                continue
            if not isinstance(geom, Point):
                geom = geom.centroid
                issue("warning", f"source {oid}", "источник не точка — взят центроид")
            source, source_id = geom, oid

        elif object_type == "heat_network":
            diameter = _num(props.get("diameter"))
            if diameter is None:
                issue("error", f"heat_network {oid}", "нет diameter — участок пропущен")
                continue
            if not rules.has_diameter(int(diameter)):
                issue("warning", f"heat_network {oid}", f"Ду{int(diameter)} нет в таблице 1")
            for part in _explode(geom):
                if isinstance(part, LineString) and part.length > 0:
                    network.append(ExistingEdge(id=oid, geom=part, diameter=int(diameter)))
                else:
                    issue("error", f"heat_network {oid}", f"ожидалась линия, получено {part.geom_type}")

        elif object_type == "heat_chamber":
            if not isinstance(geom, Point):
                geom = geom.centroid
                issue("warning", f"heat_chamber {oid}", "камера не точка — взят центроид")
            chambers.append(ExistingChamber(id=oid, geom=geom, raw_coords=raw_point))

        elif object_type == "oks_connection_point":
            flow = _num(props.get("flow_tph"))
            if flow is None or flow <= 0:
                issue("error", f"oks_connection_point {oid}", "нет flow_tph — точка пропущена")
                continue
            if not isinstance(geom, Point):
                geom = geom.centroid
                issue("warning", f"oks_connection_point {oid}", "точка подключения не точка — взят центроид")
            oks.append(OksPoint(id=oid, geom=geom, flow_tph=flow, raw_coords=raw_point))

        elif object_type == "restriction":
            raw_type = props.get("restriction_type")
            canonical = rules.canonical_type(raw_type)
            if not geom.is_valid:
                fixed = geom.buffer(0)
                if fixed.is_valid and not fixed.is_empty:
                    geom = fixed
                    issue("info", f"restriction {oid}", "невалидный полигон исправлен buffer(0)")
                else:
                    issue("error", f"restriction {oid}", "невалидная геометрия — пропущено")
                    continue
            item = Restriction(
                id=oid, geom=geom, type=canonical or str(raw_type), raw_type=str(raw_type),
                properties={k: v for k, v in props.items() if k not in {"id", "object_type", "restriction_type"}},
            )
            if canonical is None:
                unknown.append(item)
                issue("warning", "restriction", f"тип {raw_type!r} не в таблице 2 — в обязательной проверке не участвует")
            else:
                restrictions.append(item)

    if source is None:
        issues.append(Issue("warning", "source", "источник не найден — на расчёт не влияет"))
    if not network:
        issues.append(Issue("error", "heat_network", "существующая сеть отсутствует — подключаться некуда"))
    if not oks:
        issues.append(Issue("error", "oks_connection_point", "нет ни одной точки подключения"))

    case = CaseInput(
        calc_crs=calc_crs, source=source, source_id=source_id, network=network,
        chambers=chambers, oks=oks, restrictions=restrictions,
        unknown_restrictions=unknown, issues=issues, name=name,
    )
    _attach_chambers(case, rules)
    _attach_polygons(case)
    return case


# --- Топология ---------------------------------------------------------------


def _attach_chambers(case: CaseInput, rules: Rules) -> None:
    """Сколько примыканий у каждой существующей камеры (§9.12 спецификации).

    Участок примыкает, если его конец ближе допуска. Участок, проходящий
    через камеру насквозь без разрыва, занимает два примыкания.
    """
    tol = rules.attach_tolerance_m
    for chamber in case.chambers:
        count = 0
        for edge in case.network:
            ends = [Point(edge.geom.coords[0]), Point(edge.geom.coords[-1])]
            touching = sum(1 for end in ends if end.distance(chamber.geom) <= tol)
            if touching:
                count += touching
            elif edge.geom.distance(chamber.geom) <= tol:
                count += 2
        chamber.connections = count


def _attach_polygons(case: CaseInput) -> None:
    """Какой полигон ОКС содержит точку подключения (§9.11: вложенные — наименьший)."""
    polygons = [r for r in case.restrictions if r.type == "oks"]
    if not polygons:
        return
    tree = STRtree([r.geom for r in polygons])
    for point in case.oks:
        candidates = [
            polygons[i] for i in tree.query(point.geom, predicate="intersects")
        ]
        if not candidates:
            # Точка на границе полигона может не «intersects» из-за округления
            near = [r for r in polygons if r.geom.distance(point.geom) < 0.05]
            candidates = near
        if candidates:
            point.polygon_id = min(candidates, key=lambda r: r.geom.area).id
        else:
            case.issues.append(Issue(
                "warning", f"oks_connection_point {point.id}",
                "точка не внутри полигона ОКС — цель без финального участка",
            ))


def polygon_parts(geom: BaseGeometry) -> list[Polygon]:
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    return []
