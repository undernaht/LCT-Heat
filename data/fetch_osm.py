"""Выгрузка городской подложки из OpenStreetMap.

Здания, дороги, ж/д, вода, зелень, покрытия — это реальные данные, и они
бесплатны. Всё, чего в открытом доступе нет (тепловые сети, водопровод,
канализация, газ, кабели), достраивает generate.py.

Топология улиц берётся через общие идентификаторы узлов (`out body; >; out skel`),
а не через геометрию: пересечения улиц должны быть настоящими узлами графа,
иначе сгенерированная сеть развалится на несвязные куски.

Использование:
    python data/fetch_osm.py --name presnya --bbox 55.755 37.560 55.775 37.590
    python data/fetch_osm.py --name presnya --preset presnya
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.geo.crs import utm_epsg_for_bbox  # noqa: E402

CACHE_DIR = Path(__file__).resolve().parent / "cache"
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
USER_AGENT = "lct2026-heat-network/0.1 (hackathon project)"

# Готовые районы Москвы для демо: (min_lat, min_lon, max_lat, max_lon)
PRESETS: dict[str, tuple[float, float, float, float]] = {
    # Пресня: плотная застройка, магистрали, ж/д — богатый набор препятствий
    "presnya": (55.7550, 37.5600, 55.7750, 37.5900),
    # Хамовники: смешанная застройка, набережная, парк
    "khamovniki": (55.7250, 37.5650, 55.7420, 37.5950),
    # Небольшой кусок для быстрых прогонов
    "small": (55.7600, 37.5700, 55.7680, 37.5820),
}

ROAD_CLASSES = [
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "unclassified", "residential", "living_street", "service", "pedestrian",
]

QUERY_TEMPLATE = """
[out:json][timeout:300];
(
  way({bbox})[building];
  way({bbox})[highway~"^({roads})$"];
  way({bbox})[railway~"^(rail|light_rail|tram|subway|narrow_gauge)$"];
  way({bbox})[natural=water];
  way({bbox})[waterway=riverbank];
  way({bbox})[leisure~"^(park|garden|pitch)$"];
  way({bbox})[landuse~"^(grass|forest|meadow|village_green|recreation_ground|industrial)$"];
  node({bbox})[natural=tree];
  way({bbox})[natural=tree_row];
);
out body;
>;
out skel qt;
"""


# --- Overpass ---------------------------------------------------------------


def run_query(bbox: tuple[float, float, float, float]) -> dict[str, Any]:
    query = QUERY_TEMPLATE.format(
        bbox="{:.6f},{:.6f},{:.6f},{:.6f}".format(*bbox),
        roads="|".join(ROAD_CLASSES),
    )
    last_error: Exception | None = None

    for endpoint in OVERPASS_ENDPOINTS:
        for attempt in range(3):
            try:
                print(f"  Overpass: {endpoint} (попытка {attempt + 1})", flush=True)
                request = urllib.request.Request(
                    endpoint,
                    data=query.encode("utf-8"),
                    headers={"User-Agent": USER_AGENT},
                )
                with urllib.request.urlopen(request, timeout=300) as response:
                    return json.load(response)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                print(f"  не вышло: {type(exc).__name__}: {exc}", flush=True)
                time.sleep(5 * (attempt + 1))

    raise RuntimeError(f"Overpass недоступен: {last_error}")


# --- Разбор -----------------------------------------------------------------


def index_elements(
    payload: dict[str, Any],
) -> tuple[dict[int, tuple[float, float]], list[dict], list[dict]]:
    """Узлы по id, список линий и список ОТДЕЛЬНО СТОЯЩИХ узлов с тегами.

    Деревья в OSM — это узлы, а не линии, поэтому их приходится собирать
    отдельно: обычные узлы приходят без тегов и нужны только как геометрия.
    """
    nodes: dict[int, tuple[float, float]] = {}
    ways: list[dict] = []
    tagged_nodes: list[dict] = []

    for element in payload.get("elements", []):
        if element["type"] == "node":
            nodes[element["id"]] = (element["lon"], element["lat"])
            if element.get("tags"):
                tagged_nodes.append(element)
        elif element["type"] == "way":
            ways.append(element)

    return nodes, ways, tagged_nodes


def coords_of(way: dict, nodes: dict[int, tuple[float, float]]) -> list[tuple[float, float]]:
    return [nodes[ref] for ref in way.get("nodes", []) if ref in nodes]


def is_closed(way: dict) -> bool:
    refs = way.get("nodes", [])
    return len(refs) >= 4 and refs[0] == refs[-1]


def feature(geometry: dict, properties: dict) -> dict:
    return {"type": "Feature", "geometry": geometry, "properties": properties}


def classify(way: dict, nodes: dict[int, tuple[float, float]]) -> tuple[str, dict] | None:
    """Определить слой и свойства объекта. None — объект нам не нужен."""
    tags = way.get("tags", {})
    line = coords_of(way, nodes)
    if len(line) < 2:
        return None

    if "building" in tags:
        if not is_closed(way):
            return None
        return "buildings", {
            "osm_id": way["id"],
            "building": tags.get("building"),
            "levels": tags.get("building:levels"),
            "height": tags.get("height"),
            "start_date": tags.get("start_date"),
            "name": tags.get("name"),
            "addr": tags.get("addr:housenumber"),
            "shop": tags.get("shop"),
            "office": tags.get("office"),
            "amenity": tags.get("amenity"),
        }

    if "highway" in tags:
        return "roads", {
            "osm_id": way["id"],
            # Топология: узлы нужны, чтобы собрать граф улиц с настоящими перекрёстками
            "nodes": way.get("nodes", []),
            "highway": tags["highway"],
            "name": tags.get("name"),
            "surface": tags.get("surface"),
            "lanes": tags.get("lanes"),
            "width": tags.get("width"),
            "tunnel": tags.get("tunnel"),
            "bridge": tags.get("bridge"),
        }

    if "railway" in tags:
        return "railways", {
            "osm_id": way["id"],
            "railway": tags["railway"],
            "gauge": tags.get("gauge"),
            "electrified": tags.get("electrified"),
        }

    if tags.get("natural") == "tree_row":
        return "greenery", {
            "osm_id": way["id"],
            "kind": "tree_row",
            "is_tree": True,
        }

    if tags.get("natural") == "water" or tags.get("waterway") == "riverbank":
        if not is_closed(way):
            return None
        return "water", {"osm_id": way["id"], "name": tags.get("name")}

    if "leisure" in tags or "landuse" in tags:
        if not is_closed(way):
            return None
        return "landuse", {
            "osm_id": way["id"],
            "leisure": tags.get("leisure"),
            "landuse": tags.get("landuse"),
        }

    return None


def to_layers(payload: dict[str, Any]) -> dict[str, dict]:
    nodes, ways, tagged_nodes = index_elements(payload)
    layers: dict[str, list[dict]] = {
        "buildings": [], "roads": [], "railways": [], "water": [], "landuse": [],
        "greenery": [],
    }

    for way in ways:
        result = classify(way, nodes)
        if result is None:
            continue
        layer, properties = result
        line = coords_of(way, nodes)

        if layer in ("buildings", "water", "landuse"):
            ring = line if line[0] == line[-1] else [*line, line[0]]
            geometry = {"type": "Polygon", "coordinates": [ring]}
        else:
            geometry = {"type": "LineString", "coordinates": line}

        layers[layer].append(feature(geometry, properties))

    # Отдельно стоящие деревья — это точки, а не линии
    for node in tagged_nodes:
        tags = node.get("tags", {})
        if tags.get("natural") != "tree":
            continue
        layers["greenery"].append(
            feature(
                {"type": "Point", "coordinates": list(nodes[node["id"]])},
                {
                    "osm_id": node["id"],
                    "kind": "tree",
                    "is_tree": True,
                    "genus": tags.get("genus") or tags.get("species"),
                },
            )
        )

    return {
        name: {"type": "FeatureCollection", "features": features}
        for name, features in layers.items()
    }


# --- CLI --------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Выгрузка городской подложки из OSM")
    parser.add_argument("--name", required=True, help="имя района (каталог в data/cache/)")
    parser.add_argument("--bbox", nargs=4, type=float, metavar=("MIN_LAT", "MIN_LON", "MAX_LAT", "MAX_LON"))
    parser.add_argument("--preset", choices=sorted(PRESETS), help="готовый район Москвы")
    parser.add_argument("--force", action="store_true", help="перекачать, даже если кэш есть")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.bbox:
        bbox = tuple(args.bbox)
    elif args.preset:
        bbox = PRESETS[args.preset]
    elif args.name in PRESETS:
        bbox = PRESETS[args.name]
    else:
        print("Нужен --bbox или --preset (или --name из числа пресетов)", file=sys.stderr)
        return 2

    out_dir = CACHE_DIR / args.name
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / "osm_raw.json"

    print(f"Район «{args.name}», bbox {bbox}")

    if raw_path.exists() and not args.force:
        print(f"  кэш найден: {raw_path.name} (--force чтобы перекачать)")
        payload = json.loads(raw_path.read_text(encoding="utf-8"))
    else:
        payload = run_query(bbox)
        raw_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        print(f"  сохранено: {raw_path.name} ({raw_path.stat().st_size / 1e6:.1f} МБ)")

    layers = to_layers(payload)
    for name, collection in layers.items():
        path = out_dir / f"{name}.geojson"
        path.write_text(json.dumps(collection, ensure_ascii=False), encoding="utf-8")
        print(f"  {name:12s} {len(collection['features']):5d} объектов → {path.name}")

    # Метрическая проекция пишется явно: иначе её приходится выводить из bbox,
    # а порядок координат в bbox определить по значениям нельзя — для Москвы
    # и широта, и долгота меньше 90. Из-за этого стандартный порядок RFC 7946
    # молча давал не ту зону UTM и ошибку площади около 4 %.
    min_lat, min_lon, max_lat, max_lon = bbox
    metric_crs = utm_epsg_for_bbox(min_lon, min_lat, max_lon, max_lat)

    meta = {
        "name": args.name,
        "bbox": [min_lon, min_lat, max_lon, max_lat],
        "bbox_order": "lonlat",          # RFC 7946
        "crs": "EPSG:4326",
        "metric_crs": metric_crs,
        "source": "OpenStreetMap © участники OpenStreetMap, ODbL",
        "counts": {name: len(c["features"]) for name, c in layers.items()},
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
