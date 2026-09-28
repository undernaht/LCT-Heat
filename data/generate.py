"""Генератор слоёв, которых нет в открытом доступе.

Городская подложка (здания, улицы, ж/д, вода) — реальная, из OSM. Инженерные
сети закрыты, поэтому достраиваются процедурно, но правдоподобно: магистрали
от источника вдоль улиц, ЦТП в центрах кварталов, попутные водопровод,
канализация, газ и кабели в тех же уличных коридорах.

Ключевое: сгенерированная теплосеть **гидравлически состоятельна**. Диаметры
подбираются тем же кодом (`app.hydraulics`), что и в основном расчёте, по
агрегированной нагрузке ниже по течению, а располагаемый напор в каждом узле
считается вычитанием фактических потерь от источника. Значит и фильтр кандидатов
врезки, и сценарий «нужна реконструкция» работают на настоящих числах, а не на
случайных подписях.

Воспроизводимо: фиксированный seed → тот же датасет.

Использование:
    python data/generate.py --name small
    python data/generate.py --name small --seed 42
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import defaultdict
from heapq import heappop, heappush
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from shapely.geometry import LineString, MultiLineString, Point, Polygon, mapping, shape
from shapely.ops import unary_union
from shapely.strtree import STRtree

from app.config import get_config  # noqa: E402
from app.domain.enums import BuildingUse, HeatNodeKind, Laying, UtilityKind  # noqa: E402
from app.domain.models import Building, TempSchedule  # noqa: E402
from app.geo import crs, elevation  # noqa: E402
from app.ingest.area import resolve_crs  # noqa: E402
from app.hydraulics import diameters, flow  # noqa: E402
from app.loads import estimate as loads  # noqa: E402

CACHE_DIR = Path(__file__).resolve().parent / "cache"

SCHEDULE = TempSchedule(130, 70)
# Располагаемый напор на выводе источника (разность подающего и обратного).
# Было 40 м — при таком напоре на периферии оставалось 9 м, и после честной
# проверки «трасса + 10 м на ИТП» подключиться было нельзя нигде. Для районной
# котельной, питающей сеть в несколько километров, реальная величина — 60–100 м.
SOURCE_HEAD_M = 80.0
# Сверх требования невскипания при рабочей температуре: запас на подключения
# выше существующей сети. Меньше нельзя — иначе верх самой сети уже вскипает.
STATIC_LEVEL_RESERVE_M = 8.0
QUARTER_CELL_M = 250.0        # размер ячейки для расстановки ЦТП
MIN_QUARTER_LOAD = 0.3        # Гкал/ч — ниже ЦТП не ставим
# Отступ теплосети от кромки проезжей части: 1,5 м по СП 124.13330 (табл. А.3,
# до бортового камня) плюс запас на габарит конструкции.
HEAT_CORRIDOR_CLEARANCE_M = 3.0

# Квартал перспективной застройки: несколько корпусов рядом. Нужен, чтобы
# групповому подключению было что делить — разнесённым объектам общий коридор
# почти ничего не даёт.
QUARTER_BUILDINGS = 3
QUARTER_SPACING_M = (55.0, 160.0)

# Классы улиц, вдоль которых идут сети, и ширина проезжей части
ROAD_WIDTH_M: dict[str, float] = {
    "motorway": 22.0, "trunk": 20.0, "primary": 18.0, "secondary": 14.0,
    "tertiary": 11.0, "unclassified": 8.0, "residential": 7.0,
    "living_street": 5.5, "service": 4.5, "pedestrian": 4.0,
}
MAJOR_ROADS = {"motorway", "trunk", "primary", "secondary"}
MINOR_ROADS = {"residential", "living_street", "unclassified", "service"}

# Правила прокладки попутных сетей: смещение от оси улицы и где они бывают.
# Разные смещения создают реальные параллельные коридоры — именно те конфликты,
# которые должен разрешать трассировщик.
UTILITY_RULES: list[dict[str, Any]] = [
    {"kind": UtilityKind.WATER, "offset": -3.5, "roads": "all_but_service", "depth": 2.5,
     "diameter": 200},
    {"kind": UtilityKind.SEWER, "offset": 2.5, "roads": "all_but_service", "depth": 3.5,
     "diameter": 300},
    {"kind": UtilityKind.STORM, "offset": 5.0, "roads": "not_minor_only", "depth": 2.0,
     "diameter": 400},
    {"kind": UtilityKind.GAS, "offset": -6.5, "roads": "minor", "depth": 1.6},
    {"kind": UtilityKind.POWER, "offset": 4.0, "roads": "all", "depth": 0.8},
    {"kind": UtilityKind.COMM, "offset": -5.5, "roads": "major", "depth": 0.9},
]


# --- Ввод-вывод -------------------------------------------------------------


def load_layer(area_dir: Path, name: str) -> list[dict]:
    path = area_dir / f"{name}.geojson"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))["features"]


def write_layer(area_dir: Path, name: str, features: list[dict]) -> None:
    path = area_dir / f"{name}.geojson"
    path.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"  {name:22s} {len(features):5d} объектов → {path.name}")


def to_feature(geom, metric_crs: str, properties: dict) -> dict:
    return {
        "type": "Feature",
        "geometry": mapping(crs.to_wgs84(geom, metric_crs)),
        "properties": properties,
    }


# --- Граф улиц --------------------------------------------------------------


class StreetGraph:
    """Граф улиц с настоящими перекрёстками (общие узлы OSM)."""

    def __init__(self) -> None:
        self.pos: dict[int, tuple[float, float]] = {}
        self.adj: dict[int, list[tuple[int, float, str]]] = defaultdict(list)
        self.edge_class: dict[tuple[int, int], str] = {}

    def add_edge(self, a: int, b: int, pa, pb, road_class: str) -> None:
        length = math.dist(pa, pb)
        if length <= 0:
            return
        self.pos[a], self.pos[b] = pa, pb
        self.adj[a].append((b, length, road_class))
        self.adj[b].append((a, length, road_class))
        self.edge_class[(min(a, b), max(a, b))] = road_class

    def dijkstra(self, source: int, weight_of=None) -> tuple[dict[int, float], dict[int, int]]:
        """Кратчайшие пути от источника. weight_of(length, road_class) -> вес."""
        weight_of = weight_of or (lambda length, _cls: length)
        dist: dict[int, float] = {source: 0.0}
        prev: dict[int, int] = {}
        queue: list[tuple[float, int]] = [(0.0, source)]
        visited: set[int] = set()

        while queue:
            d, node = heappop(queue)
            if node in visited:
                continue
            visited.add(node)
            for neighbour, length, road_class in self.adj[node]:
                if neighbour in visited:
                    continue
                candidate = d + weight_of(length, road_class)
                if candidate < dist.get(neighbour, math.inf):
                    dist[neighbour] = candidate
                    prev[neighbour] = node
                    heappush(queue, (candidate, neighbour))

        return dist, prev

    def nearest_node(self, point: tuple[float, float], within: set[int] | None = None) -> int | None:
        pool = within if within is not None else self.pos.keys()
        best, best_d = None, math.inf
        for node in pool:
            d = math.dist(self.pos[node], point)
            if d < best_d:
                best, best_d = node, d
        return best


def build_street_graph(roads: list[dict], metric_crs: str) -> StreetGraph:
    graph = StreetGraph()
    for feat in roads:
        props = feat["properties"]
        refs = props.get("nodes") or []
        line = crs.to_metric(shape(feat["geometry"]), metric_crs)
        coords = list(line.coords)
        if len(refs) != len(coords):
            continue                      # геометрия и топология разошлись — пропускаем
        road_class = props.get("highway", "residential")
        for i in range(len(coords) - 1):
            graph.add_edge(refs[i], refs[i + 1], coords[i], coords[i + 1], road_class)
    return graph


def largest_component(graph: StreetGraph) -> set[int]:
    """Наибольшая связная компонента: сети должны быть одним куском."""
    seen: set[int] = set()
    best: set[int] = set()
    for start in graph.pos:
        if start in seen:
            continue
        stack, component = [start], set()
        while stack:
            node = stack.pop()
            if node in component:
                continue
            component.add(node)
            stack.extend(n for n, _, _ in graph.adj[node] if n not in component)
        seen |= component
        if len(component) > len(best):
            best = component
    return best


# --- Здания и нагрузки ------------------------------------------------------


def build_buildings(features: list[dict], metric_crs: str) -> list[Building]:
    result: list[Building] = []
    for i, feat in enumerate(features):
        geom = crs.to_metric(shape(feat["geometry"]), metric_crs)
        if not isinstance(geom, Polygon) or geom.area < 20.0:
            continue

        props = feat["properties"]
        tags = {k: str(v) for k, v in props.items() if v is not None}
        if props.get("building"):
            tags["building"] = str(props["building"])

        levels = props.get("levels")
        height = props.get("height")
        start_date = props.get("start_date")

        result.append(
            Building(
                id=f"b_{props.get('osm_id', i)}",
                geom=geom,
                use=loads.classify_use(tags),
                floors=_as_int(levels),
                height_m=_as_float(height),
                built_year=_as_int(start_date[:4] if isinstance(start_date, str) else None),
                tags=tags,
            )
        )
    return result


def _as_int(value: Any) -> int | None:
    try:
        return int(float(str(value).split(";")[0]))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(str(value).split(";")[0].replace("m", "").strip())
    except (TypeError, ValueError):
        return None


# --- Размещение источника и ЦТП ---------------------------------------------

def place_source(graph: StreetGraph, buildings: list[Building], component: set[int]) -> int:
    """Источник — на периферии: узел, наиболее удалённый от центра масс застройки."""
    if not buildings:
        return next(iter(component))
    cx = sum(b.geom.centroid.x for b in buildings) / len(buildings)
    cy = sum(b.geom.centroid.y for b in buildings) / len(buildings)
    return max(component, key=lambda n: math.dist(graph.pos[n], (cx, cy)))


def place_ctp(
    graph: StreetGraph, buildings: list[Building], component: set[int]
) -> dict[int, float]:
    """ЦТП по кварталам: сетка QUARTER_CELL_M, узел графа у центра масс нагрузки.

    Возвращает {узел графа: суммарная нагрузка квартала, Гкал/ч}.
    """
    cells: dict[tuple[int, int], list[Building]] = defaultdict(list)
    for building in buildings:
        if building.load is None or building.load.total <= 0:
            continue
        centroid = building.geom.centroid
        key = (int(centroid.x // QUARTER_CELL_M), int(centroid.y // QUARTER_CELL_M))
        cells[key].append(building)

    result: dict[int, float] = defaultdict(float)
    for group in cells.values():
        total = sum(b.load.total for b in group)
        if total < MIN_QUARTER_LOAD:
            continue
        weight = sum(b.load.total for b in group)
        cx = sum(b.geom.centroid.x * b.load.total for b in group) / weight
        cy = sum(b.geom.centroid.y * b.load.total for b in group) / weight
        node = graph.nearest_node((cx, cy), within=component)
        if node is not None:
            result[node] += total

    return dict(result)


# --- Построение тепловой сети ----------------------------------------------


def build_heat_tree(
    graph: StreetGraph, source: int, consumers: dict[int, float]
) -> tuple[dict[tuple[int, int], float], set[int]]:
    """Дерево магистралей от источника до всех ЦТП с агрегированной нагрузкой.

    Магистрали тяготеют к крупным улицам — вес ребра занижается для главных
    дорог, как это и бывает в реальных схемах теплоснабжения.
    """
    preference = {cls: (0.75 if cls in MAJOR_ROADS else 1.0) for cls in ROAD_WIDTH_M}
    _, prev = graph.dijkstra(source, lambda length, cls: length * preference.get(cls, 1.0))

    edge_load: dict[tuple[int, int], float] = defaultdict(float)
    used_nodes: set[int] = {source}

    for consumer, load in consumers.items():
        if consumer not in prev and consumer != source:
            continue                       # недостижим — пропускаем
        node = consumer
        used_nodes.add(node)
        guard = 0
        while node != source and node in prev and guard < 10_000:
            parent = prev[node]
            edge_load[(min(node, parent), max(node, parent))] += load
            used_nodes.add(parent)
            node = parent
            guard += 1

    return dict(edge_load), used_nodes


def offset_heat_geometry(
    graph: StreetGraph, source: int, edge_load: dict[tuple[int, int], float]
) -> dict[int, tuple[float, float]]:
    """Сместить теплосеть с оси улицы в технический коридор.

    Сеть по оси улицы означает, что любая точка врезки оказывается посреди
    проезжей части, а подход к ней обязан идти по бортовому камню — и
    нормоконтроль честно ловит нарушение 1,5 м, которого в реальности нет:
    существующие сети проложены в коридоре за кромкой, а не под осью.

    Топология сохраняется точно: смещаются узлы, а не отрезки, поэтому рёбра
    остаются теми же парами узлов. Сторона смещения выбирается по направлению
    «от источника», иначе на соседних рёбрах перпендикуляр менял бы знак и сеть
    складывалась бы зигзагом.
    """
    adjacency: dict[int, list[int]] = defaultdict(list)
    for a, b in edge_load:
        adjacency[a].append(b)
        adjacency[b].append(a)

    parent: dict[int, int] = {}
    order: list[int] = [source]
    visited = {source}
    stack = [source]
    while stack:
        node = stack.pop()
        for neighbour in adjacency[node]:
            if neighbour in visited:
                continue
            parent[neighbour] = node
            visited.add(neighbour)
            order.append(neighbour)
            stack.append(neighbour)

    def half_width(node: int) -> float:
        widths = [
            ROAD_WIDTH_M.get(graph.edge_class.get((min(node, n), max(node, n)), ""), 7.0)
            for n in adjacency[node]
        ]
        return max(widths) / 2.0 if widths else 3.5

    result: dict[int, tuple[float, float]] = {}
    for node in order:
        anchor = parent.get(node)
        if anchor is None:
            anchor = adjacency[node][0] if adjacency[node] else None
            if anchor is None:
                result[node] = graph.pos[node]
                continue
            dx = graph.pos[anchor][0] - graph.pos[node][0]
            dy = graph.pos[anchor][1] - graph.pos[node][1]
        else:
            dx = graph.pos[node][0] - graph.pos[anchor][0]
            dy = graph.pos[node][1] - graph.pos[anchor][1]

        length = math.hypot(dx, dy)
        if length < 1e-6:
            result[node] = graph.pos[node]
            continue

        shift = half_width(node) + HEAT_CORRIDOR_CLEARANCE_M
        x, y = graph.pos[node]
        result[node] = (x - dy / length * shift, y + dx / length * shift)

    return result


def size_and_head(
    graph: StreetGraph,
    source: int,
    edge_load: dict[tuple[int, int], float],
) -> tuple[dict[tuple[int, int], int], dict[int, float]]:
    """Диаметры по нагрузке и располагаемый напор в каждом узле.

    Напор считается вычитанием фактических потерь от источника — тем же
    расчётом, что и в основном модуле. Поэтому дальние узлы честно оказываются
    «слабыми», и фильтр кандидатов врезки на них срабатывает по делу.
    """
    du_by_edge: dict[tuple[int, int], int] = {}
    for key, load in edge_load.items():
        choice = diameters.select_diameter(load, SCHEDULE, is_branch=False)
        du_by_edge[key] = choice.selected.du_mm if choice.selected else 500

    # Обход дерева от источника: напор убывает по мере удаления
    head: dict[int, float] = {source: SOURCE_HEAD_M}
    adjacency: dict[int, list[int]] = defaultdict(list)
    for a, b in edge_load:
        adjacency[a].append(b)
        adjacency[b].append(a)

    stack = [source]
    visited = {source}
    while stack:
        node = stack.pop()
        for neighbour in adjacency[node]:
            if neighbour in visited:
                continue
            key = (min(node, neighbour), max(node, neighbour))
            length = math.dist(graph.pos[node], graph.pos[neighbour])
            state = diameters.flow_state(edge_load[key], SCHEDULE, du_by_edge[key])
            dp = flow.route_pressure_loss_pa(state.r_pa_m, length)
            head[neighbour] = max(0.0, head[node] - flow.pressure_to_head_m(dp))
            visited.add(neighbour)
            stack.append(neighbour)

    return du_by_edge, head


# --- Попутные сети ----------------------------------------------------------


def offset_line(line: LineString, distance: float) -> LineString | None:
    """Смещение оси улицы. Вырожденные результаты отбрасываем."""
    try:
        result = line.offset_curve(distance)
    except Exception:
        return None
    if result.is_empty:
        return None
    if isinstance(result, MultiLineString):
        parts = [p for p in result.geoms if p.length > 1.0]
        if not parts:
            return None
        result = max(parts, key=lambda p: p.length)
    return result if isinstance(result, LineString) and result.length > 1.0 else None


def road_matches(rule_scope: str, road_class: str) -> bool:
    if rule_scope == "all":
        return True
    if rule_scope == "all_but_service":
        return road_class not in ("service", "pedestrian")
    if rule_scope == "major":
        return road_class in MAJOR_ROADS
    if rule_scope == "minor":
        return road_class in MINOR_ROADS
    if rule_scope == "not_minor_only":
        return road_class not in ("service", "pedestrian", "living_street")
    return False


def generate_utilities(
    roads: list[dict], metric_crs: str, rng: random.Random
) -> list[tuple[LineString, dict]]:
    """Попутные сети вдоль целых улиц (а не отдельных рёбер графа).

    Важно, что линия непрерывна на всю улицу: иначе трассировщик находил бы
    разрывы между сегментами и «просачивался» сквозь коридор чужой сети —
    артефакт генерации, а не свойство реальности.
    """
    result: list[tuple[LineString, dict]] = []

    for feat in roads:
        road_class = feat["properties"].get("highway", "residential")
        line = crs.to_metric(shape(feat["geometry"]), metric_crs)
        if not isinstance(line, LineString) or line.length < 15.0:
            continue

        for rule in UTILITY_RULES:
            if not road_matches(rule["roads"], road_class):
                continue
            jitter = rng.uniform(-0.8, 0.8)
            offset = offset_line(line, rule["offset"] + jitter)
            if offset is None:
                continue

            props: dict[str, Any] = {
                "kind": rule["kind"].value,
                "depth_m": round(rule["depth"] + rng.uniform(-0.2, 0.2), 2),
                "along_road": feat["properties"].get("osm_id"),
            }
            if rule["kind"] is UtilityKind.GAS:
                props["pressure_class"] = "0.3-0.6" if road_class in MAJOR_ROADS else "<=0.3"
            if rule["kind"] is UtilityKind.POWER:
                props["voltage_kv"] = 10.0 if road_class in MAJOR_ROADS else 0.4
            if rule.get("diameter"):
                props["diameter_mm"] = rule["diameter"]

            result.append((offset, props))

    return result


# --- Участки ----------------------------------------------------------------


def place_perspective_buildings(
    buildings: list[Building],
    heat_lines: list[LineString],
    water: list[Polygon],
    bounds: tuple[float, float, float, float],
    rng: random.Random,
    count: int = 3,
) -> list[tuple[Polygon, dict]]:
    """Перспективные здания — сценарий, а не случайная точка.

    Замер по существующей застройке показал: прямая «здание → ближайшая сеть»
    проходит нормоконтроль в 75 % случаев, потому что сгенерированная сеть идёт
    почти по каждой улице. Значит подключать надо не существующее здание, а
    объект на свободном участке, удалённый от сети, — как и бывает при
    техприсоединении новой застройки.

    Площадка выбирается так, чтобы задача была содержательной: удалена от сети,
    свободна от застройки, и между ней и сетью есть препятствия.
    """
    if not heat_lines:
        return []

    heat_index = STRtree(heat_lines)
    building_index = STRtree([b.geom for b in buildings]) if buildings else None
    water_index = STRtree(water) if water else None

    half = 15.0                     # 30×30 м ≈ 900 м² пятна застройки
    min_x, min_y, max_x, max_y = bounds
    candidates: list[tuple[float, Point, float]] = []

    x = min_x + 40.0
    while x < max_x - 40.0:
        y = min_y + 40.0
        while y < max_y - 40.0:
            point = Point(x, y)
            y += 25.0

            footprint = point.buffer(half + 12.0, cap_style=3)
            if building_index is not None:
                if any(buildings[i].geom.intersects(footprint) for i in building_index.query(footprint)):
                    continue
            if water_index is not None:
                if any(water[i].intersects(footprint) for i in water_index.query(footprint)):
                    continue

            nearest = heat_lines[heat_index.nearest(point)]
            distance = point.distance(nearest)
            if not 120.0 <= distance <= 400.0:
                continue

            # Чем больше препятствий между площадкой и сетью, тем интереснее задача
            corridor = LineString([point, nearest.interpolate(nearest.project(point))]).buffer(6.0)
            obstacles = 0
            if building_index is not None:
                obstacles = sum(
                    1 for i in building_index.query(corridor) if buildings[i].geom.intersects(corridor)
                )

            score = obstacles * 60.0 + distance
            candidates.append((score, point, distance))
        x += 25.0

    candidates.sort(key=lambda item: -item[0])

    def make(point: Point, distance: float, score: float, ident: str, name: str,
             floors: int) -> tuple[Polygon, dict]:
        return (
            point.buffer(half, cap_style=3),
            {
                "id": ident,
                "name": name,
                "use": BuildingUse.RESIDENTIAL.value,
                "floors": floors,
                "built_year": 2026,
                "is_perspective": True,
                "distance_to_network_m": round(distance, 1),
                "obstacles_on_straight_line": max(0, int((score - distance) // 60)),
                "_x": point.x,
                "_y": point.y,
            },
        )

    chosen: list[tuple[Polygon, dict]] = []

    # --- Одиночные площадки: разнесены, каждая — самостоятельный сценарий ---
    for score, point, distance in candidates:
        if any(point.distance(Point(p["_x"], p["_y"])) < 250.0 for _, p in chosen):
            continue
        index = len(chosen) + 1
        chosen.append(make(point, distance, score, f"persp_{index}",
                           f"Перспективное здание №{index}", (9, 14)[index % 2]))
        if len(chosen) >= count:
            break

    # --- Квартал застройки: несколько корпусов рядом ---
    #
    # Без него групповое подключение нечего показывать: разнесённым на 250 м
    # объектам делить коридор почти незачем, и экономия выходит около 5 %.
    # А квартал из нескольких корпусов — типичный случай техприсоединения,
    # и вот там общий коридор действительно окупается.
    taken = [Point(p["_x"], p["_y"]) for _, p in chosen]
    for score, anchor, distance in candidates:
        if any(anchor.distance(p) < 250.0 for p in taken):
            continue

        block = [(score, anchor, distance)]
        for other_score, point, other_distance in candidates:
            if len(block) >= QUARTER_BUILDINGS:
                break
            gap = anchor.distance(point)
            if not QUARTER_SPACING_M[0] <= gap <= QUARTER_SPACING_M[1]:
                continue
            if any(point.distance(p) < QUARTER_SPACING_M[0] for _, p, _ in block):
                continue
            block.append((other_score, point, other_distance))

        if len(block) < QUARTER_BUILDINGS:
            continue

        for index, (block_score, point, block_distance) in enumerate(block, start=1):
            chosen.append(make(point, block_distance, block_score, f"kvartal_{index}",
                               f"Квартал застройки, корпус {index}", (17, 12, 9, 14)[index % 4]))
        break

    return chosen


def generate_parcels(buildings: list[Building], rng: random.Random) -> list[tuple[Polygon, dict]]:
    """Участки — по кластерам застройки. Часть помечается частной собственностью."""
    if not buildings:
        return []

    merged = unary_union([b.geom.buffer(12.0) for b in buildings])
    polygons = list(merged.geoms) if merged.geom_type == "MultiPolygon" else [merged]

    result: list[tuple[Polygon, dict]] = []
    for i, poly in enumerate(polygons):
        shrunk = poly.buffer(-4.0)
        if shrunk.is_empty or shrunk.geom_type != "Polygon" or shrunk.area < 200:
            continue
        is_public = rng.random() < 0.45
        result.append(
            (
                shrunk,
                {
                    "id": f"p_{i}",
                    "cadastral_number": f"77:01:{i:07d}:{rng.randint(1, 99):02d}",
                    "is_public": is_public,
                    "is_protected_zone": (not is_public) and rng.random() < 0.12,
                    "prohibits_laying": False,
                },
            )
        )
    return result


# --- Сборка -----------------------------------------------------------------


def generate(area: str, seed: int) -> None:
    area_dir = CACHE_DIR / area
    if not area_dir.exists():
        raise SystemExit(f"Нет данных района: {area_dir}. Сначала запусти fetch_osm.py")

    meta = json.loads((area_dir / "meta.json").read_text(encoding="utf-8"))
    # Проекция берётся тем же кодом, что и в сервисе: разбирать bbox руками
    # в каждом скрипте — прямой путь к разъезжающимся зонам UTM.
    _, metric_crs = resolve_crs(meta)
    rng = random.Random(seed)

    print(f"Район «{area}», проекция {metric_crs}, seed {seed}")

    road_features = load_layer(area_dir, "roads")
    building_features = load_layer(area_dir, "buildings")

    # --- Здания и нагрузки ---
    buildings = build_buildings(building_features, metric_crs)
    loads.estimate_all(buildings)
    heated = [b for b in buildings if b.load and b.load.total > 0]
    total_load = sum(b.load.total for b in heated)
    print(f"  зданий {len(buildings)}, отапливаемых {len(heated)}, "
          f"суммарная нагрузка {total_load:.1f} Гкал/ч")

    # --- Граф улиц ---
    graph = build_street_graph(road_features, metric_crs)
    component = largest_component(graph)
    print(f"  граф улиц: {len(graph.pos)} узлов, наибольшая компонента {len(component)}")
    if len(component) < 10:
        raise SystemExit("Слишком маленький связный граф улиц — возьми район побольше")

    # --- Тепловая сеть ---
    source = place_source(graph, heated, component)
    consumers = place_ctp(graph, heated, component)
    edge_load, _ = build_heat_tree(graph, source, consumers)
    if not edge_load:
        raise SystemExit("Не удалось построить дерево теплосети")
    du_by_edge, head = size_and_head(graph, source, edge_load)
    heat_pos = offset_heat_geometry(graph, source, edge_load)

    print(f"  теплосеть: источник в узле {source}, ЦТП {len(consumers)}, "
          f"участков {len(edge_load)}")

    # --- Статический уровень ---
    # Систему заполняют так, чтобы в верхней точке давление превышало давление
    # вскипания при температуре подачи. Отсюда и берётся уровень: верхняя отметка
    # сети плюс норматив невскипания плюс запас на будущие подключения.
    # Без DEM его не выдумываем — проверки давления просто останутся без ввода.
    terrain = elevation.load(area_dir, metric_crs)
    static_level: float | None = None
    if terrain.available:
        net_nodes = {node for edge in edge_load for node in edge}
        marks = [terrain.at(*heat_pos[node]) for node in sorted(net_nodes)]
        marks = [m for m in marks if not math.isnan(m)]
        if marks:
            non_boiling = get_config().normatives["vertical_design"]["non_boiling_head_m"]
            required = float(
                non_boiling.get(str(int(SCHEDULE.supply_c)), non_boiling["default"])
            )
            static_level = max(marks) + required + STATIC_LEVEL_RESERVE_M
            print(f"  рельеф: отметки сети {min(marks):.1f}–{max(marks):.1f} м, "
                  f"статический уровень {static_level:.1f} м "
                  f"(невскипание {required:.0f} + запас {STATIC_LEVEL_RESERVE_M:.0f})")

    # --- Узлы ---
    node_features: list[dict] = []
    degree: dict[int, int] = defaultdict(int)
    for a, b in edge_load:
        degree[a] += 1
        degree[b] += 1

    for node, deg in degree.items():
        if node == source:
            kind, name = HeatNodeKind.SOURCE, "Котельная №1"
        elif node in consumers:
            kind, name = HeatNodeKind.CTP, f"ЦТП-{node % 1000:03d}"
        elif deg > 2:
            kind, name = HeatNodeKind.JUNCTION, f"УТ-{node % 1000:03d}"
        else:
            kind, name = HeatNodeKind.CHAMBER, f"ТК-{node % 1000:03d}"

        props: dict[str, Any] = {
            "id": f"hn_{node}",
            "kind": kind.value,
            "name": name,
            "head_available_m": round(head.get(node, 0.0), 2),
        }
        if kind is HeatNodeKind.SOURCE:
            props["capacity_gcal_h"] = round(sum(consumers.values()) * 1.35, 3)
            props["reserve_gcal_h"] = round(sum(consumers.values()) * 0.35, 3)
            if static_level is not None:
                props["static_head_m"] = round(static_level, 1)
        node_features.append(to_feature(Point(heat_pos[node]), metric_crs, props))

    # --- Участки сети ---
    edge_features: list[dict] = []
    for (a, b), load in sorted(edge_load.items()):
        du = du_by_edge[(a, b)]
        capacity = diameters.capacity_gcal_h(du, SCHEDULE, is_branch=False)
        line = LineString([heat_pos[a], heat_pos[b]])
        edge_features.append(
            to_feature(
                line,
                metric_crs,
                {
                    "id": f"he_{a}_{b}",
                    "node_a": f"hn_{a}",
                    "node_b": f"hn_{b}",
                    "du_mm": du,
                    "laying": Laying.CHANNEL.value,
                    "schedule": str(SCHEDULE),
                    "load_gcal_h": round(load, 4),
                    "capacity_gcal_h": round(capacity, 4),
                    "reserve_gcal_h": round(max(0.0, capacity - load), 4),
                    "is_main": du >= 200,
                    "length_m": round(line.length, 1),
                },
            )
        )

    # --- Перспективные здания: сценарий подключения ---
    water_polys = [
        geom
        for geom in (crs.to_metric(shape(f["geometry"]), metric_crs) for f in load_layer(area_dir, "water"))
        if isinstance(geom, Polygon)
    ]
    heat_lines = [LineString([heat_pos[a], heat_pos[b]]) for a, b in edge_load]
    xs = [graph.pos[n][0] for n in component]
    ys = [graph.pos[n][1] for n in component]
    aoi_bounds = (min(xs), min(ys), max(xs), max(ys))

    perspective_features: list[dict] = []
    for geom, props in place_perspective_buildings(
        buildings, heat_lines, water_polys, aoi_bounds, rng, count=2
    ):
        props = {k: v for k, v in props.items() if not k.startswith("_")}
        candidate = Building(
            id=props["id"],
            geom=geom,
            use=BuildingUse.RESIDENTIAL,
            floors=props["floors"],
            built_year=props["built_year"],
            is_perspective=True,
        )
        candidate.load = loads.estimate(candidate)
        props |= {
            "footprint_m2": round(geom.area, 1),
            "total_area_m2": round(loads.total_area_m2(candidate), 1),
            "q_heating_gcal_h": round(candidate.load.heating, 4),
            "q_dhw_gcal_h": round(candidate.load.dhw_max, 4),
            "q_total_gcal_h": round(candidate.load.total, 4),
        }
        perspective_features.append(to_feature(geom, metric_crs, props))

    if perspective_features:
        print("  перспективные здания:")
        for feat in perspective_features:
            p = feat["properties"]
            print(f"    {p['id']}: {p['floors']} эт., Q = {p['q_total_gcal_h']:.3f} Гкал/ч, "
                  f"до сети {p['distance_to_network_m']:.0f} м, "
                  f"зданий на прямой {p['obstacles_on_straight_line']}")

    # --- Попутные сети и участки ---
    utilities = generate_utilities(road_features, metric_crs, rng)
    utility_features = [
        to_feature(geom, metric_crs, {"id": f"u_{i}", **props})
        for i, (geom, props) in enumerate(utilities)
    ]
    parcel_features = [
        to_feature(geom, metric_crs, props) for geom, props in generate_parcels(buildings, rng)
    ]

    # --- Здания с нагрузкой ---
    building_features_out = [
        to_feature(
            b.geom,
            metric_crs,
            {
                "id": b.id,
                "use": b.use.value,
                "floors": loads.floors_of(b),
                "built_year": b.built_year,
                "footprint_m2": round(b.geom.area, 1),
                "total_area_m2": round(loads.total_area_m2(b), 1),
                "q_heating_gcal_h": round(b.load.heating, 4) if b.load else 0.0,
                "q_dhw_gcal_h": round(b.load.dhw_max, 4) if b.load else 0.0,
                "q_total_gcal_h": round(b.load.total, 4) if b.load else 0.0,
                "name": b.tags.get("name"),
            },
        )
        for b in buildings
    ]

    print("\nСлои:")
    write_layer(area_dir, "gen_heat_nodes", node_features)
    write_layer(area_dir, "gen_heat_edges", edge_features)
    write_layer(area_dir, "gen_utilities", utility_features)
    write_layer(area_dir, "gen_parcels", parcel_features)
    write_layer(area_dir, "gen_perspective", perspective_features)
    write_layer(area_dir, "buildings_load", building_features_out)

    du_histogram: dict[int, int] = defaultdict(int)
    for du in du_by_edge.values():
        du_histogram[du] += 1

    summary = {
        "area": area,
        "seed": seed,
        "crs": metric_crs,
        "schedule": str(SCHEDULE),
        "buildings_total": len(buildings),
        "buildings_heated": len(heated),
        "total_load_gcal_h": round(total_load, 3),
        "heat_edges": len(edge_features),
        "heat_nodes": len(node_features),
        "ctp": len(consumers),
        "du_histogram": {str(k): v for k, v in sorted(du_histogram.items())},
        "head_min_m": round(min(head.values()), 2) if head else None,
        "head_max_m": round(max(head.values()), 2) if head else None,
        "utilities": len(utility_features),
        "parcels": len(parcel_features),
        "perspective": [
            {
                "id": f["properties"]["id"],
                "q_total_gcal_h": f["properties"]["q_total_gcal_h"],
                "distance_to_network_m": f["properties"]["distance_to_network_m"],
                "obstacles_on_straight_line": f["properties"]["obstacles_on_straight_line"],
            }
            for f in perspective_features
        ],
    }
    (area_dir / "generated_meta.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n  диаметры: {dict(sorted(du_histogram.items()))}")
    print(f"  напор в узлах: {summary['head_min_m']}–{summary['head_max_m']} м вод. ст.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Генератор слоёв инженерных сетей")
    parser.add_argument("--name", required=True, help="имя района в data/cache/")
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args()

    generate(args.name, args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
