"""Синтетические проверочные наборы для конкурсной модели кейса.

Конкурсный файл содержит ограничения только трёх типов (oks, water, railway),
а проверочный набор организаторов может принести любой тип таблицы 2:
дороги, трамвай, газопроводы, кабели, парки, социальные и запрещённые
территории. Узнать об ошибке в правиле для дорог на защите — поздно, поэтому
солвер заранее прогоняется на наборах, где такие объекты стоят осмысленно:
поперёк вероятных маршрутов, а не где попало.

Размещение — чистые функции от геометрии входного файла. Маршруты «точка →
сеть» оцениваются собственным грубым поиском по сетке (независимым от
солвера: он нужен только чтобы понять, где идёт улица), а ограничения ставятся
относительно них: «посреди коридора на пути точки к сети, от стены до
стены». Координаты в градусах нигде не зашиты, поэтому генератор работает и
на другом файле той же структуры.

Использование:
    python data/case_synthetic.py --scenario roads
    python data/case_synthetic.py --all
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from shapely import affinity
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient
from shapely.ops import linemerge, nearest_points, substring, unary_union
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.geo import crs  # noqa: E402
from app.geo.raster import Grid, burn_mask  # noqa: E402

DEFAULT_INPUT = ROOT / "ресурсы" / "распаковано" / "ТЗ" / "Датасет скорректированный.geojson"
CACHE_DIR = Path(__file__).resolve().parent / "cache" / "case"
METRIC_CRS = "EPSG:32637"

# Таблица 2 приложения: отступы, по которым оценивается проходимость коридора.
OKS_CLEARANCE = 5.0          # Ду < 500
BLOCK_CLEARANCE = 1.0        # парк, вода, ж/д, социальный объект, запрещённая территория
BLOCK_TYPES = frozenset({"park", "social_area", "prohibited_site", "water", "railway"})

WALL_GAP = 0.5               # зазор между новым объектом и полигоном ОКС
ROUTE_RESOLUTION = 1.0       # шаг сетки оценки маршрутов, м
ROUTE_END_MARGIN = 25.0      # ближе к концам маршрута объекты не ставятся
SPAN_MAX_HALF = 80.0         # дальше этого поперечник коридора не ищется

SCENARIOS = ("roads", "utilities", "tram", "blocks", "mixed", "strings", "big", "unreachable")


# ---------------------------------------------------------------------------
# Вход
# ---------------------------------------------------------------------------


@dataclass
class District:
    """Конкурсный файл: исходные features дословно плюс геометрия в метрах."""

    header: dict[str, Any]
    raw: list[dict[str, Any]]
    geoms: list[BaseGeometry]
    metric_crs: str = METRIC_CRS
    _base_routes: RouteEstimate | None = field(default=None, repr=False)

    @classmethod
    def load(cls, path: Path) -> District:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        header = {k: v for k, v in data.items() if k != "features"}
        raw = data["features"]
        geoms = [crs.to_metric(shape(f["geometry"]), METRIC_CRS) for f in raw]
        return cls(header=header, raw=raw, geoms=geoms)

    # --- выборки ---------------------------------------------------------

    def props(self, i: int) -> dict[str, Any]:
        return self.raw[i]["properties"]

    def indices(self, object_type: str, restriction_type: str | None = None) -> list[int]:
        out = []
        for i, f in enumerate(self.raw):
            p = f["properties"]
            if p.get("object_type") != object_type:
                continue
            if restriction_type is not None and p.get("restriction_type") != restriction_type:
                continue
            out.append(i)
        return out

    @cached_property
    def points(self) -> dict[Any, Point]:
        return {self.props(i)["id"]: self.geoms[i] for i in self.indices("oks_connection_point")}

    @cached_property
    def flows(self) -> dict[Any, float]:
        return {self.props(i)["id"]: float(self.props(i)["flow_tph"])
                for i in self.indices("oks_connection_point")}

    @cached_property
    def oks_indices(self) -> list[int]:
        return self.indices("restriction", "oks")

    @cached_property
    def oks_union(self) -> BaseGeometry:
        return unary_union([self.geoms[i] for i in self.oks_indices])

    @cached_property
    def block_geoms(self) -> list[BaseGeometry]:
        return [self.geoms[i] for i in self.indices("restriction")
                if self.props(i).get("restriction_type") in BLOCK_TYPES]

    @cached_property
    def walls(self) -> BaseGeometry:
        """Куда новым объектам нельзя: ОКС и существующие запретные полигоны с зазором."""
        return unary_union([self.oks_union, *self.block_geoms]).buffer(WALL_GAP)

    @cached_property
    def _wall_parts(self) -> list[Polygon]:
        return _polygons(self.walls)

    @cached_property
    def _wall_tree(self) -> STRtree:
        return STRtree(self._wall_parts)

    def walls_near(self, geom: BaseGeometry) -> BaseGeometry:
        """Стены в окрестности объекта — вычитать всю карту из каждой полосы слишком долго.

        Части уже не пересекаются (это компоненты одного объединения), поэтому
        собираются в MultiPolygon без повторного объединения — оно в разы дороже.
        """
        idx = self._wall_tree.query(geom)
        if len(idx) == 0:
            return Polygon()
        parts = [self._wall_parts[i] for i in idx]
        return parts[0] if len(parts) == 1 else MultiPolygon(parts)

    def touches_walls(self, geom: BaseGeometry) -> bool:
        """Объект залез в зазор к ОКС или запретному полигону (касание края зазора — норма)."""
        near = self.walls_near(geom)
        return not near.is_empty and geom.distance(near.buffer(-0.05)) <= 0.0

    @cached_property
    def network_geoms(self) -> list[LineString]:
        return [self.geoms[i] for i in self.indices("heat_network")]

    @cached_property
    def network_union(self) -> BaseGeometry:
        return unary_union(self.network_geoms)

    @cached_property
    def max_numeric_id(self) -> int:
        ids = [f["properties"]["id"] for f in self.raw]
        return max((int(v) for v in ids if isinstance(v, (int, float))), default=0)

    # --- собственный полигон и точка выхода --------------------------------

    def own_polygon_index(self, point: Point) -> int | None:
        """Полигон ОКС, содержащий точку (или ближайший в пределах 0,5 м)."""
        best, best_d = None, 0.5
        for i in self.oks_indices:
            d = self.geoms[i].distance(point)
            if d < best_d:
                best, best_d = i, d
                if d == 0.0:
                    break
        return best

    def own_polygon(self, point: Point) -> Polygon | None:
        """Часть собственного объекта, в которой лежит точка."""
        i = self.own_polygon_index(point)
        if i is None:
            return None
        parts = _polygons(self.geoms[i])
        return min(parts, key=lambda g: g.distance(point))

    def approach_point(self, pid: Any) -> Point:
        """Начало маршрута: снаружи собственного полигона, за полосой отступа (§2.2)."""
        return next(self.approach_points(pid), self.points[pid])

    def approach_points(self, pid: Any, step: float = 4.0) -> Iterator[Point]:
        """Кандидаты на выход из здания по возрастанию расстояния от точки.

        Первый — от ближайшей границы (§2.2); остальные — запасные на случай,
        когда ближайшая граница смотрит в замкнутый двор (§9.1 спецификации).
        Ленивый: проверка каждого кандидата стоит дорого, а нужен обычно первый.
        """
        p = self.points[pid]
        own = self.own_polygon(p)
        if own is None:
            yield p
            return
        # Собственный объект — весь MultiPolygon (§9.2 спецификации): у объекта из
        # десятков частей выход не должен упираться в соседнюю часть.
        whole = self.geoms[self.own_polygon_index(p)]
        ring = own.exterior
        samples = [nearest_points(ring, p)[0]]
        samples += [Point(c) for c in ring.coords]
        samples += [ring.interpolate(t) for t in np.arange(0.0, ring.length, step)]
        samples.sort(key=lambda b: b.distance(p))
        clear = OKS_CLEARANCE + 1.0
        for b in samples:
            if b.distance(p) > 1e-6:
                n = _unit((b.x - p.x, b.y - p.y))
            else:
                n = _outward_normal(own, b)
            # У Г-образного здания луч из точки может идти вдоль второго крыла:
            # тогда выход отодвигается, пока не выйдет из полосы отступа.
            for reach in (clear + 1.0, clear + 2.0, clear + 4.0, clear + 6.0):
                t = Point(b.x + n[0] * reach, b.y + n[1] * reach)
                if whole.contains(t) or LineString([b, t]).crosses(whole):
                    break
                if whole.distance(t) >= clear:
                    yield t
                    break

    def base_routes(self) -> RouteEstimate:
        """Маршруты без новых ограничений — общие для всех сценариев."""
        if self._base_routes is None:
            self._base_routes = estimate_routes(self)
        return self._base_routes


def _polygons(geom: BaseGeometry) -> list[Polygon]:
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon)]


def _unit(v: tuple[float, float]) -> tuple[float, float]:
    n = math.hypot(v[0], v[1])
    return (v[0] / n, v[1] / n) if n > 0 else (1.0, 0.0)


def _rotate(v: tuple[float, float], deg: float) -> tuple[float, float]:
    a = math.radians(deg)
    return (v[0] * math.cos(a) - v[1] * math.sin(a), v[0] * math.sin(a) + v[1] * math.cos(a))


def _outward_normal(poly: Polygon, at: Point) -> tuple[float, float]:
    """Нормаль к контуру наружу — для точки, лежащей ровно на границе."""
    ring = poly.exterior
    s = ring.project(at)
    a = ring.interpolate(max(0.0, s - 0.5))
    b = ring.interpolate(min(ring.length, s + 0.5))
    t = _unit((b.x - a.x, b.y - a.y))
    n = (-t[1], t[0])
    probe = Point(at.x + n[0] * 0.3, at.y + n[1] * 0.3)
    return n if not poly.contains(probe) else (-n[0], -n[1])


def _direction_at(line: LineString, s: float, half: float = 1.5) -> tuple[float, float]:
    a = line.interpolate(max(0.0, s - half))
    b = line.interpolate(min(line.length, s + half))
    return _unit((b.x - a.x, b.y - a.y))


def _straight_direction(line: LineString, s: float, half: float = 15.0,
                        tolerance: float = 2.0) -> tuple[float, float] | None:
    """Направление коридора в точке маршрута — или None, если маршрут здесь изгибается.

    Объект «поперёк маршрута» на изломе — это объект неизвестно подо что: угол
    пересечения зависит от того, какой из двух отрезков считать направлением.
    """
    s0, s1 = max(0.0, s - half), min(line.length, s + half)
    if s1 - s0 < 2 * half - 1e-6:
        return None
    a, b = line.interpolate(s0), line.interpolate(s1)
    chord = LineString([a, b])
    if substring(line, s0, s1).hausdorff_distance(chord) > tolerance:
        return None
    return _unit((b.x - a.x, b.y - a.y))


# ---------------------------------------------------------------------------
# Оценка маршрутов: Дейкстра по сетке от всей сети сразу
# ---------------------------------------------------------------------------


@dataclass
class RouteEstimate:
    """Грубые маршруты «точка → сеть» с учётом только запретных ограничений."""

    grid: Grid
    routes: dict[Any, LineString]
    unreachable: list[Any]


def _grid_graph(free: np.ndarray, step: float) -> csr_matrix:
    h, w = free.shape
    idx = np.arange(h * w, dtype=np.int64).reshape(h, w)
    rows, cols, weights = [], [], []
    diag = step * math.sqrt(2.0)
    for dr, dc, cost in ((0, 1, step), (1, 0, step), (1, 1, diag), (1, -1, diag)):
        c0, c1 = max(0, -dc), w - max(0, dc)
        mask = free[0:h - dr, c0:c1] & free[dr:h, c0 + dc:c1 + dc]
        rows.append(idx[0:h - dr, c0:c1][mask])
        cols.append(idx[dr:h, c0 + dc:c1 + dc][mask])
        weights.append(np.full(int(mask.sum()), cost))
    n = h * w
    return csr_matrix(
        (np.concatenate(weights), (np.concatenate(rows), np.concatenate(cols))), shape=(n, n)
    )


def _nearest_free_cell(free: np.ndarray, row: int, col: int, radius: int) -> int | None:
    h, w = free.shape
    r0, r1 = max(0, row - radius), min(h, row + radius + 1)
    c0, c1 = max(0, col - radius), min(w, col + radius + 1)
    window = free[r0:r1, c0:c1]
    if not window.any():
        return None
    rr, cc = np.nonzero(window)
    k = int(np.argmin((rr + r0 - row) ** 2 + (cc + c0 - col) ** 2))
    return (rr[k] + r0) * w + (cc[k] + c0)


def estimate_routes(
    district: District,
    extra_obstacles: Iterable[BaseGeometry] = (),
    resolution: float = ROUTE_RESOLUTION,
) -> RouteEstimate:
    """Для каждой точки — кратчайший обход ОКС и запретных полигонов до сети.

    Это не решение задачи, а карта улиц глазами трассировщика: по ней видно,
    каким коридором точка скорее всего пойдёт к сети.
    """
    bounds = unary_union([district.network_union, *district.points.values()]).bounds
    grid = Grid.covering(bounds, resolution, margin=150.0)
    # Полугабарит пары Ду100–150 — 0,25–0,33 м; растр по центрам ячеек, иначе
    # проход шириной в два габарита между стеной и парком считается закрытым.
    obstacles = [district.oks_union.buffer(OKS_CLEARANCE + 0.35)]
    obstacles += [g.buffer(BLOCK_CLEARANCE + 0.35)
                  for g in [*district.block_geoms, *extra_obstacles]]
    free = ~burn_mask(grid, obstacles, all_touched=False)
    targets = burn_mask(grid, district.network_geoms) & free
    graph = _grid_graph(free, resolution)
    sources = np.flatnonzero(targets.ravel())
    dist, pred, _ = dijkstra(
        graph, directed=False, indices=sources, min_only=True, return_predecessors=True
    )

    routes: dict[Any, LineString] = {}
    unreachable: list[Any] = []
    for pid in district.points:
        start, cell = None, None
        for candidate in district.approach_points(pid):
            row, col = grid.rowcol(candidate.x, candidate.y)
            cell = _nearest_free_cell(free, row, col, radius=int(2 / resolution) + 1)
            if cell is not None and np.isfinite(dist[cell]):
                start = candidate
                break
        if start is None or cell is None:
            unreachable.append(pid)
            continue
        chain = []
        cur = cell
        while cur >= 0:
            chain.append(cur)
            cur = pred[cur]
        coords = [(start.x, start.y)]
        coords += [grid.xy(i // grid.width, i % grid.width) for i in chain]
        end = nearest_points(district.network_union, Point(coords[-1]))[0]
        coords.append((end.x, end.y))
        routes[pid] = LineString(coords).simplify(2.5)
    return RouteEstimate(grid=grid, routes=routes, unreachable=unreachable)


# ---------------------------------------------------------------------------
# Размещение относительно маршрутов
# ---------------------------------------------------------------------------


@dataclass
class Span:
    """Свободный поперечник коридора через точку маршрута."""

    line: LineString
    cut_start: bool      # конец упёрся в стену (а не в предел длины)
    cut_end: bool
    anchor: Point
    axis: tuple[float, float]

    @property
    def spans_corridor(self) -> bool:
        return self.cut_start and self.cut_end


@dataclass
class Placement:
    geom: BaseGeometry
    pid: Any
    s: float
    span: Span | None = None
    width: float | None = None


class Placer:
    """Ставит объекты поперёк или вдоль оценённых маршрутов, не задевая ОКС.

    Уже размещённое запоминается, чтобы объекты одного сценария не ложились
    друг на друга случайно — только там, где это задумано.
    """

    def __init__(self, district: District, routes: dict[Any, LineString]):
        self.d = district
        self.routes = routes
        self.placed: list[BaseGeometry] = []
        self.network_zone = district.network_union.buffer(4.0)
        ends = [Point(r.coords[0]) for r in routes.values()]
        ends += [Point(r.coords[-1]) for r in routes.values()]
        # Выходы из зданий и места присоединения — ничьи: объект на них проверял бы
        # не правило таблицы 2, а финальный участок §2.2.
        self.route_ends = unary_union(ends) if ends else Point()

    # --- выбор маршрутов и кандидатов --------------------------------------

    def routes_by_length(self, min_length: float = 0.0, exclude: Iterable[Any] = ()) -> list[Any]:
        skip = set(exclude)
        ids = [pid for pid, r in self.routes.items() if r.length >= min_length and pid not in skip]
        return sorted(ids, key=lambda pid: -self.routes[pid].length)

    def candidates(self, pid: Any, *, prefer: str = "middle", step: float = 4.0,
                   skip: Iterable[float] = ()) -> list[float]:
        """Точки маршрута, где можно ставить объект: подальше от концов."""
        length = self.routes[pid].length
        lo, hi = ROUTE_END_MARGIN, length - ROUTE_END_MARGIN
        if hi <= lo:
            return []
        skipped = set(skip)
        values = [float(v) for v in np.arange(lo, hi + 1e-9, step) if float(v) not in skipped]
        if prefer == "middle":
            values.sort(key=lambda s: abs(s - length / 2))
        elif prefer == "end":
            values.sort(key=lambda s: -s)
        elif prefer == "start":
            values.sort()
        return values

    def routes_hit(self, geom: BaseGeometry, margin: float = 2.0) -> set[Any]:
        zone = geom.buffer(margin)
        return {pid for pid, r in self.routes.items() if r.intersects(zone)}

    # --- геометрические примитивы -----------------------------------------

    def free_span(
        self, anchor: Point, axis: tuple[float, float], max_half: float = SPAN_MAX_HALF
    ) -> Span | None:
        ux, uy = _unit(axis)
        a = (anchor.x - ux * max_half, anchor.y - uy * max_half)
        b = (anchor.x + ux * max_half, anchor.y + uy * max_half)
        probe = LineString([a, b])
        pieces = _lines(probe.difference(self.d.walls_near(probe)))
        if not pieces:
            return None
        piece = min(pieces, key=lambda g: g.distance(anchor))
        if piece.distance(anchor) > 1e-6:
            return None
        coords = list(piece.coords)
        if Point(coords[0]).distance(Point(a)) > Point(coords[-1]).distance(Point(a)):
            coords.reverse()
        piece = LineString(coords)
        cut_start = Point(coords[0]).distance(Point(a)) > 1e-6
        cut_end = Point(coords[-1]).distance(Point(b)) > 1e-6
        return Span(piece, cut_start, cut_end, anchor, (ux, uy))

    def span_across(self, pid: Any, s: float, *, skew: float = 0.0,
                    max_half: float = SPAN_MAX_HALF) -> Span | None:
        route = self.routes[pid]
        anchor = route.interpolate(s)
        d = _straight_direction(route, s)
        if d is None:
            return None
        axis = _rotate((-d[1], d[0]), skew)
        return self.free_span(anchor, axis, max_half)

    def strip_from_span(self, span: Span, width: float, *, shorten_end: float = 0.0) -> Polygon | None:
        """Полоса заданной ширины вдоль поперечника, обрезанная по стенам.

        `shorten_end` оставляет у дальней стены проход такой ширины (от самого
        ОКС, а не от зазора): полоса не доходит до неё, а обрезается её буфером.
        """
        line = span.line
        poly = line.buffer(width / 2.0, cap_style="flat")
        poly = poly.difference(self.d.walls_near(poly))
        if shorten_end > 0:
            if line.length <= shorten_end + 1.0 or not span.cut_end:
                return None
            end = Point(line.coords[-1])
            end_walls = [w for w in _polygons(self.d.walls_near(end.buffer(1.0)))
                         if w.distance(end) < 0.1]
            if not end_walls:
                return None
            poly = poly.difference(unary_union(end_walls).buffer(shorten_end - WALL_GAP))
        return _part_containing(poly, span.anchor)

    def median_road(self, span: Span, width: float, hole: float) -> Polygon | None:
        """Дорога с разделительной полосой: полигон с дыркой, не доходящей до стен."""
        outer = self.strip_from_span(span, width)
        if outer is None:
            return None
        for end_margin in (6.0, 9.0, 12.0, 16.0):
            line = span.line
            if line.length <= 2 * end_margin + 4.0:
                return None
            axis = substring(line, end_margin, line.length - end_margin)
            median = axis.buffer(hole / 2.0, cap_style="flat")
            if not median.within(outer.buffer(-0.5)):
                continue
            road = outer.difference(median)
            if isinstance(road, Polygon) and len(road.interiors) == 1:
                return road
        return None

    def line_along(self, pid: Any, *, length: float = 45.0, offset: float = 0.6,
                   min_straight: float = 40.0) -> LineString | None:
        """Линия, идущая вдоль самого длинного прямого куска маршрута."""
        route = self.routes[pid]
        coords = list(route.coords)
        best = None
        for a, b in zip(coords[:-1], coords[1:]):
            seg = LineString([a, b])
            if seg.length >= min_straight and (best is None or seg.length > best.length):
                best = seg
        if best is None:
            return None
        take = min(length, best.length - 10.0)
        mid = best.length / 2.0
        piece = substring(best, mid - take / 2.0, mid + take / 2.0)
        line = piece.offset_curve(offset)
        if line.is_empty or self.d.touches_walls(line):
            return None
        return LineString(line.coords)

    def line_before_tie_in(self, pid: Any, *, offset: float = 7.0,
                           half: float = 40.0) -> LineString | None:
        """Линия параллельно сети со стороны подхода: маршрут пересечёт её перед врезкой."""
        route = self.routes[pid]
        end = Point(route.coords[-1])
        near = [g for g in self.d.network_geoms if g.distance(end) < 60.0]
        merged = _lines(linemerge(unary_union(near)))
        if not merged:
            return None
        axis = min(merged, key=lambda g: g.distance(end))
        s = axis.project(end)
        sub = substring(axis, max(0.0, s - half), min(axis.length, s + half))
        if sub.length < 20.0:
            return None
        before = route.interpolate(max(0.0, route.length - 8.0))
        d = _direction_at(sub, sub.project(end))
        side = (before.x - end.x) * -d[1] + (before.y - end.y) * d[0]
        line = sub.offset_curve(offset if side > 0 else -offset, join_style="mitre")
        pieces = _lines(line.difference(self.d.walls_near(line)))
        if not pieces:
            return None
        line = min(pieces, key=lambda g: g.distance(end))
        if line.length < 20.0 or not line.intersects(route):
            return None
        return line

    # --- приёмка --------------------------------------------------------

    def acceptable(self, geom: BaseGeometry | None, pid: Any, *, avoid_network: bool = True,
                   clearance: float = 12.0, end_margin: float = 15.0,
                   must_cross: bool = True) -> bool:
        """Не в ОКС, не на сети, не на уже размещённом, и действительно на маршруте."""
        if geom is None or geom.is_empty or not geom.is_valid:
            return False
        if self.d.touches_walls(geom):
            return False
        if avoid_network and geom.intersects(self.network_zone):
            return False
        if any(geom.intersects(p.buffer(clearance)) for p in self.placed):
            return False
        route = self.routes[pid]
        if must_cross and not geom.intersects(route):
            return False
        if end_margin > 0 and geom.intersects(self.route_ends.buffer(end_margin)):
            return False
        return True

    def place(self, pid: Any, make: Callable[[float], BaseGeometry | None], *,
              prefer: str = "middle", skip: Iterable[float] = (),
              **accept: Any) -> Placement | None:
        for s in self.candidates(pid, prefer=prefer, skip=skip):
            geom = make(s)
            if self.acceptable(geom, pid, **accept):
                self.placed.append(geom)
                return Placement(geom, pid, s)
        return None

    def place_strip(self, pid: Any, width: float, *, skew: float = 0.0, hole: float | None = None,
                    shorten_end: float = 0.0, prefer: str = "middle",
                    max_half: float = SPAN_MAX_HALF, skip: Iterable[float] = ()
                    ) -> Placement | None:
        """Полоса поперёк маршрута от стены до стены (дорога, трамвай, парк…)."""
        spans: dict[float, Span] = {}

        def make(s: float) -> BaseGeometry | None:
            span = self.span_across(pid, s, skew=skew, max_half=max_half)
            if span is None or not span.spans_corridor:
                return None
            if hole is not None:
                poly = self.median_road(span, width, hole)
            else:
                poly = self.strip_from_span(span, width, shorten_end=shorten_end)
            if poly is None or not _crosses_once(poly, self.routes[pid]):
                return None
            spans[s] = span
            return poly

        result = self.place(pid, make, prefer=prefer, skip=skip)
        if result is not None:
            result.span = spans[result.s]
            result.width = width
        return result

    def place_line_across(self, pid: Any, *, prefer: str = "middle",
                          max_half: float = SPAN_MAX_HALF) -> Placement | None:
        spans: dict[float, Span] = {}

        def make(s: float) -> BaseGeometry | None:
            span = self.span_across(pid, s, max_half=max_half)
            if span is None or not span.spans_corridor:
                return None
            spans[s] = span
            return span.line

        result = self.place(pid, make, prefer=prefer)
        if result is not None:
            result.span = spans[result.s]
        return result

    def place_along_street(self, *, width: float | None, min_route: float = 60.0,
                           max_half: float = 120.0, exclude: Iterable[Any] = (),
                           clearance: float = 12.0) -> Placement | None:
        """Объект вдоль улицы, которую маршрут пересекает.

        Улица — это длинный поперечник, упирающийся в здания с обеих сторон;
        поперечник в чистое поле улицей не считается.
        """
        best: tuple[float, Any, float, Span] | None = None
        for pid in self.routes_by_length(min_route, exclude=exclude):
            for s in self.candidates(pid, prefer="middle"):
                span = self.span_across(pid, s, max_half=max_half)
                if span is None or span.line.length < 50.0:
                    continue
                # Длинный поперечник у самого выхода из здания — это двор, а не улица.
                length = self.routes[pid].length
                depth = min(1.0, s / 80.0, (length - s) / 80.0)
                score = span.line.length * (1.0 if span.spans_corridor else 0.3) * depth
                if best is not None and score <= best[0]:
                    continue
                geom = span.line if width is None else self.strip_from_span(span, width)
                if geom is None or not self.acceptable(geom, pid, clearance=clearance):
                    continue
                if width is not None and not _crosses_once(geom, self.routes[pid]):
                    continue
                best = (score, pid, s, span)
        if best is None:
            return None
        _, pid, s, span = best
        geom = span.line if width is None else self.strip_from_span(span, width)
        self.placed.append(geom)
        return Placement(geom, pid, s, span, width)


def _lines(geom: BaseGeometry) -> list[LineString]:
    if geom.is_empty:
        return []
    if isinstance(geom, LineString):
        return [geom]
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, LineString) and not g.is_empty]


def _part_containing(geom: BaseGeometry, anchor: Point) -> Polygon | None:
    parts = _polygons(geom)
    if not parts:
        return None
    part = min(parts, key=lambda g: g.distance(anchor))
    return part if part.distance(anchor) < 1e-6 and part.is_valid else None


def _crosses_once(poly: Polygon, route: LineString) -> bool:
    """Маршрут проходит объект насквозь одним куском — иначе это не «поперёк».

    Дырка (разделительная полоса) не в счёт: смотрится внешний контур.
    """
    inside = route.intersection(Polygon(poly.exterior))
    return isinstance(inside, LineString) and not inside.is_empty


def _offset_line(line: LineString, distance: float) -> LineString | None:
    out = line.offset_curve(distance, join_style="mitre")
    pieces = _lines(out)
    if not pieces:
        return None
    return max(pieces, key=lambda g: g.length)


# ---------------------------------------------------------------------------
# Сценарии
# ---------------------------------------------------------------------------


@dataclass
class Scenario:
    name: str
    features: list[dict[str, Any]]
    new_geoms: list[tuple[str, BaseGeometry]] = field(default_factory=list)   # (тип, метры)
    routes: dict[Any, LineString] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    header: dict[str, Any] = field(default_factory=dict)

    def to_geojson(self) -> dict[str, Any]:
        data = dict(self.header)
        data["type"] = "FeatureCollection"
        data["name"] = f"case_{self.name}"
        data["features"] = self.features
        return data


class Builder:
    """Накапливает новые объекты сценария и раздаёт им id, не пересекающиеся с исходными."""

    def __init__(self, district: District, name: str, routes: dict[Any, LineString]):
        self.d = district
        self.scenario = Scenario(
            name=name, features=copy.deepcopy(district.raw), routes=routes,
            header=copy.deepcopy(district.header),
        )
        self.next_free = district.max_numeric_id + 1

    def next_id(self) -> int:
        value = self.next_free
        self.next_free += 1
        return value

    def add_restriction(self, rtype: str, geom: BaseGeometry, note: str = "") -> int:
        if not geom.is_valid or geom.is_empty:
            raise ValueError(f"невалидная геометрия для {rtype}: {note}")
        fid = self.next_id()
        self.scenario.features.append(feature(
            {"id": fid, "object_type": "restriction", "restriction_type": rtype}, geom,
        ))
        self.scenario.new_geoms.append((rtype, geom))
        if note:
            self.scenario.notes.append(f"{rtype} #{fid}: {note}")
        return fid


def feature(props: dict[str, Any], metric_geom: BaseGeometry) -> dict[str, Any]:
    wgs = crs.to_wgs84(metric_geom, METRIC_CRS)
    if isinstance(wgs, Polygon):
        wgs = orient(wgs)
    return {"type": "Feature", "properties": props, "geometry": mapping(wgs)}


ROAD_SPECS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("прямая, 10 м", {"width": 10.0}),
    ("косая: ось под 30° к маршруту, 9 м", {"width": 9.0, "skew": 60.0}),
    ("с разделительной полосой 8 + 7 + 8 м", {"width": 23.0, "hole": 7.0}),
    ("прямая, 13 м", {"width": 13.0}),
)


def add_roads(builder: Builder, placer: Placer, specs=ROAD_SPECS,
              min_route: float = 80.0) -> list[Placement]:
    """Дороги поперёк коридоров: каждая — на самом длинном ещё не перекрытом маршруте."""
    out: list[Placement] = []
    covered: set[Any] = set()
    for label, spec in specs:
        for pid in placer.routes_by_length(min_route, exclude=covered):
            placed = placer.place_strip(pid, **spec)
            if placed is None:
                continue
            hit = placer.routes_hit(placed.geom)
            covered |= hit
            builder.add_restriction(
                "road", placed.geom,
                f"{label}; маршрут точки {pid}, {placed.s:.0f} м от выхода; "
                f"на пути точек {_fmt_ids(hit)}",
            )
            out.append(placed)
            break
    return out


def scenario_roads(district: District) -> Scenario:
    est = district.base_routes()
    placer = Placer(district, est.routes)
    builder = Builder(district, "roads", est.routes)
    add_roads(builder, placer)
    return builder.scenario


def add_utilities(builder: Builder, placer: Placer, *, across: int = 2,
                  exclude: Iterable[Any] = ()) -> None:
    covered: set[Any] = set(exclude)
    kinds = ("gas_pipeline", "power_cable")
    # Поперёк коридоров — на самых длинных маршрутах.
    n = 0
    for pid in placer.routes_by_length(80.0):
        if n >= across:
            break
        if pid in covered:
            continue
        placed = placer.place_line_across(pid)
        if placed is None:
            continue
        hit = placer.routes_hit(placed.geom)
        covered |= hit
        builder.add_restriction(
            kinds[n % 2], placed.geom,
            f"поперёк коридора; маршрут точки {pid}, {placed.s:.0f} м; на пути точек {_fmt_ids(hit)}",
        )
        n += 1
    # Вдоль вероятного маршрута: проверка запрета следования в полосе отступа.
    for pid in placer.routes_by_length(80.0):
        line = placer.line_along(pid)
        if line is None or not placer.acceptable(line, pid, must_cross=False):
            continue
        placer.placed.append(line)
        builder.add_restriction(
            "power_cable", line,
            f"вдоль маршрута точки {pid} на {line.length:.0f} м (смещение 0,6 м)",
        )
        break
    # Поперёк подхода к сети — прямо перед врезкой самого короткого маршрута.
    for pid in sorted(placer.routes, key=lambda k: placer.routes[k].length):
        if placer.routes[pid].length < 20.0:
            continue
        line = placer.line_before_tie_in(pid)
        if line is None or not placer.acceptable(line, pid, avoid_network=False, end_margin=0.0):
            continue
        placer.placed.append(line)
        builder.add_restriction(
            "gas_pipeline", line,
            f"параллельно сети в 7 м, перед врезкой маршрута точки {pid}; "
            f"на пути точек {_fmt_ids(placer.routes_hit(line))}",
        )
        break
    # Ещё один поперёк, ближе к сети — на маршруте, который пока никто не трогал.
    for pid in placer.routes_by_length(80.0, exclude=covered):
        placed = placer.place_line_across(pid, prefer="end")
        if placed is None:
            continue
        builder.add_restriction(
            "gas_pipeline", placed.geom,
            f"поперёк коридора у сети; маршрут точки {pid}, {placed.s:.0f} м; "
            f"на пути точек {_fmt_ids(placer.routes_hit(placed.geom))}",
        )
        break


def scenario_utilities(district: District) -> Scenario:
    est = district.base_routes()
    placer = Placer(district, est.routes)
    builder = Builder(district, "utilities", est.routes)
    add_utilities(builder, placer)
    return builder.scenario


def add_tram(builder: Builder, placer: Placer, *, with_line: bool = True) -> None:
    placed = placer.place_along_street(width=6.5)
    if placed is not None:
        builder.add_restriction(
            "tram_tracks", placed.geom,
            f"полигон 6,5 м вдоль улицы; маршрут точки {placed.pid}, {placed.s:.0f} м; "
            f"на пути точек {_fmt_ids(placer.routes_hit(placed.geom))}",
        )
    if with_line:
        hit = placer.routes_hit(placed.geom) if placed is not None else set()
        placed = placer.place_along_street(width=None, exclude=hit, clearance=30.0)
        if placed is not None:
            builder.add_restriction(
                "tram_tracks", placed.geom,
                f"линия вдоль улицы; маршрут точки {placed.pid}, {placed.s:.0f} м; "
                f"на пути точек {_fmt_ids(placer.routes_hit(placed.geom))}",
            )


def scenario_tram(district: District) -> Scenario:
    est = district.base_routes()
    placer = Placer(district, est.routes)
    builder = Builder(district, "tram", est.routes)
    add_tram(builder, placer)
    return builder.scenario


BLOCK_SPECS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("park", {"width": 30.0, "prefer": "middle"}),
    ("prohibited_site", {"width": 20.0, "prefer": "end"}),
    ("social_area", {"width": 25.0, "prefer": "middle", "shorten_end": 9.0}),
)


def add_blocks(builder: Builder, placer: Placer) -> list[BaseGeometry]:
    """Запретные полигоны в коридорах: перекрыть, но не запереть ни одну точку.

    После каждого объекта маршруты пересчитываются с ним как с препятствием:
    если кто-то стал недостижим — объект переставляется дальше по маршруту.
    """
    blocks: list[BaseGeometry] = []
    covered: set[Any] = set()
    base_len = {pid: r.length for pid, r in placer.routes.items()}
    for rtype, spec in BLOCK_SPECS:
        spec = dict(spec)
        prefer = spec.pop("prefer")
        done = False
        for pid in placer.routes_by_length(80.0, exclude=covered):
            tried: set[float] = set()
            for _attempt in range(4):
                placed = placer.place_strip(pid, prefer=prefer, skip=tried, **spec)
                if placed is None:
                    break
                est = estimate_routes(placer.d, extra_obstacles=[*blocks, placed.geom])
                if est.unreachable:
                    placer.placed.remove(placed.geom)
                    tried.add(placed.s)
                    continue
                blocks.append(placed.geom)
                hit = placer.routes_hit(placed.geom)
                covered |= hit
                detours = [f"{k}: {base_len[k]:.0f}→{est.routes[k].length:.0f} м"
                           for k in sorted(hit, key=str) if k in est.routes]
                builder.add_restriction(
                    rtype, placed.geom,
                    f"{'частично' if spec.get('shorten_end') else 'полностью'} перекрывает "
                    f"коридор маршрута точки {pid}, {placed.s:.0f} м; обход: {', '.join(detours)}",
                )
                done = True
                break
            if done:
                break
    return blocks


def scenario_blocks(district: District) -> Scenario:
    est = district.base_routes()
    placer = Placer(district, est.routes)
    builder = Builder(district, "blocks", est.routes)
    blocks = add_blocks(builder, placer)
    after = estimate_routes(district, extra_obstacles=blocks)
    builder.scenario.routes = after.routes
    return builder.scenario


def scenario_mixed(district: District) -> Scenario:
    est = district.base_routes()
    placer = Placer(district, est.routes)
    builder = Builder(district, "mixed", est.routes)
    blocks = add_blocks(builder, placer)

    # Дальше всё ставится относительно маршрутов в обход запретов.
    after = estimate_routes(district, extra_obstacles=blocks)
    builder.scenario.routes = after.routes
    placer = Placer(district, after.routes)
    placer.placed.extend(blocks)

    roads = add_roads(builder, placer, specs=(ROAD_SPECS[0], ROAD_SPECS[2]))
    if roads:
        add_overlay(builder, placer, roads[0])
    under_roads = set().union(*(placer.routes_hit(r.geom) for r in roads)) if roads else set()
    add_utilities(builder, placer, across=1, exclude=under_roads)
    add_tram(builder, placer, with_line=False)
    return builder.scenario


def add_overlay(builder: Builder, placer: Placer, road: Placement) -> None:
    """Газопровод внутри дороги и кабель вплотную к ней: наложение спецпроходов."""
    span = road.span
    assert span is not None and road.width is not None
    width = road.width
    gas = _offset_line(span.line, -1.5)
    if gas is not None:
        gas_in = road.geom.buffer(-0.3).intersection(gas)
        pieces = _lines(gas_in)
        if pieces:
            gas = max(pieces, key=lambda g: g.length)
            placer.placed.append(gas)
            builder.add_restriction(
                "gas_pipeline", gas,
                f"внутри дороги маршрута точки {road.pid}, в 1,5 м от её оси: "
                f"спецпроход газопровода целиком внутри спецпрохода дороги",
            )
    cable = _offset_line(span.line, width / 2.0 + 2.5)
    if cable is not None:
        pieces = _lines(cable.difference(placer.d.walls_near(cable)))
        pieces = [g for g in pieces if g.intersects(placer.routes[road.pid])]
        if pieces:
            cable = max(pieces, key=lambda g: g.length)
            placer.placed.append(cable)
            builder.add_restriction(
                "power_cable", cable,
                "в 2,5 м от края той же дороги: зоны ±2 м и «3 м за границей» "
                "накладываются частично",
            )


def scenario_strings(district: District) -> Scenario:
    """Те же данные, но всё, что может прийти «не так», приходит не так."""
    builder = Builder(district, "strings", {})
    feats = builder.scenario.features
    notes = builder.scenario.notes
    for f in feats:
        f["properties"]["id"] = f"obj-{f['properties']['id']}"

    point_feats = [f for f in feats if f["properties"]["object_type"] == "oks_connection_point"]
    for f in point_feats[:2]:
        v = f["properties"]["flow_tph"]
        f["properties"]["flow_tph"] = str(v).replace(".", ",")
        notes.append(f"{f['properties']['id']}: flow_tph = {f['properties']['flow_tph']!r}")

    # Сдвиги — только у точек в односвязных полигонах без дырок, чтобы сдвиг
    # наружу не попал в соседнее здание.
    movable = []
    for f in point_feats:
        p = crs.to_metric(shape(f["geometry"]), METRIC_CRS)
        i = district.own_polygon_index(p)
        if i is None:
            continue
        parts = _polygons(district.geoms[i])
        if len(parts) != 1 or parts[0].interiors:
            continue
        b = nearest_points(parts[0].exterior, p)[0]
        n = _unit((b.x - p.x, b.y - p.y))
        outside = Point(b.x + n[0] * 1.0, b.y + n[1] * 1.0)
        if district.oks_union.distance(outside) < 0.9:
            continue
        movable.append((f, p, b, n, i))
    untouched = [m for m in movable if m[0] not in point_feats[:2]]
    movable = untouched if len(untouched) >= 3 else movable
    if len(movable) < 3:
        raise ValueError("не нашлось трёх точек в простых полигонах для сценария strings")

    f, p, b, n, _ = movable[0]
    f["geometry"] = mapping(crs.to_wgs84(b, METRIC_CRS))
    notes.append(f"{f['properties']['id']}: сдвинута ровно на границу полигона "
                 f"(на {b.distance(p):.2f} м)")
    f, p, b, n, _ = movable[1]
    out = Point(b.x + n[0] * 0.3, b.y + n[1] * 0.3)
    f["geometry"] = mapping(crs.to_wgs84(out, METRIC_CRS))
    notes.append(f"{f['properties']['id']}: сдвинута на 0,3 м наружу от границы")
    f, _, _, _, i = movable[2]
    dup = copy.deepcopy(feats[i])
    dup["properties"]["id"] = f"obj-{builder.next_id()}"
    feats.append(dup)
    builder.scenario.new_geoms.append(("oks", district.geoms[i]))
    notes.append(f"{dup['properties']['id']}: дубль полигона {feats[i]['properties']['id']} "
                 f"(в нём точка {f['properties']['id']})")
    return builder.scenario


def scenario_big(district: District, shift_gap: float = 50.0) -> Scenario:
    """Район 2×2: копии со сдвигом, сети связаны прямыми участками Ду500."""
    builder = Builder(district, "big", {})
    minx, miny, maxx, maxy = unary_union(district.geoms).bounds
    dx, dy = maxx - minx + shift_gap, maxy - miny + shift_gap
    feats: list[dict[str, Any]] = []
    all_ids = [f["properties"]["id"] for f in district.raw]
    numeric = all(isinstance(v, (int, float)) for v in all_ids)
    id_step = 10 ** len(str(district.max_numeric_id)) if numeric else 0

    copies = ((0, 0), (1, 0), (0, 1), (1, 1))
    endpoints: dict[tuple[int, int], list[Point]] = {}
    oks_all: list[BaseGeometry] = []
    for k, (ix, iy) in enumerate(copies):
        off = (ix * dx, iy * dy)
        ends: list[Point] = []
        for f, g in zip(district.raw, district.geoms):
            p = f["properties"]
            if k > 0 and p["object_type"] == "source":
                continue          # источник один: остальные копии питаются через перемычки
            if k == 0:
                feats.append(copy.deepcopy(f))
                moved = g
            else:
                moved = affinity.translate(g, *off)
                nf = copy.deepcopy(f)
                nf["properties"]["id"] = (p["id"] + k * id_step) if numeric else f"{p['id']}-c{k}"
                nf["geometry"] = mapping(crs.to_wgs84(moved, METRIC_CRS))
                feats.append(nf)
            if p["object_type"] == "heat_network":
                ends.append(Point(moved.coords[0]))
                ends.append(Point(moved.coords[-1]))
            if p.get("restriction_type") == "oks":
                oks_all.append(moved)
        endpoints[(ix, iy)] = ends
    builder.scenario.features = feats
    builder.next_free = district.max_numeric_id + 4 * id_step
    obstacles = STRtree(oks_all)

    for a, b in (((0, 0), (1, 0)), ((0, 0), (0, 1)), ((0, 1), (1, 1))):
        link, crossings = _shortest_link(endpoints[a], endpoints[b], obstacles)
        fid = builder.next_id() if numeric else f"link-{a}-{b}"
        feats.append(feature({"id": fid, "object_type": "heat_network", "diameter": 500}, link))
        builder.scenario.new_geoms.append(("heat_network", link))
        builder.scenario.notes.append(
            f"перемычка Ду500 #{fid}: копия {a} → {b}, {link.length:.0f} м"
            f"{'' if crossings == 0 else f', пересекает {crossings} полигонов ОКС'}"
        )
    builder.scenario.notes.append(
        f"сдвиг копий: {dx:.0f} м на восток, {dy:.0f} м на север; объектов {len(feats)}"
    )
    return builder.scenario


def _shortest_link(a: list[Point], b: list[Point], obstacles: STRtree) -> tuple[LineString, int]:
    """Кратчайшая прямая между концами участков двух копий, по возможности мимо ОКС.

    Если без пересечений не выходит — та, что задевает меньше всего зданий.
    """
    pairs = sorted(((p.distance(q), p, q) for p in a for q in b), key=lambda t: t[0])
    best: tuple[int, LineString] | None = None
    for _, p, q in pairs:
        line = LineString([p, q])
        crossings = len(obstacles.query(line, predicate="intersects"))
        if crossings == 0:
            return line, 0
        if best is None or crossings < best[0]:
            best = (crossings, line)
    assert best is not None
    return best[1], best[0]


def scenario_unreachable(district: District) -> Scenario:
    """Кольцо запрещённой территории вокруг самого «свободного» полигона с точкой."""
    est = district.base_routes()
    builder = Builder(district, "unreachable", est.routes)
    best_pid, best_score = None, -1.0
    for pid, p in district.points.items():
        i = district.own_polygon_index(p)
        if i is None:
            continue
        own = district.geoms[i]
        others = unary_union([district.geoms[j] for j in district.oks_indices if j != i])
        score = min(own.distance(others), own.distance(district.network_union))
        if score > best_score:
            best_pid, best_score = pid, score
    if best_pid is None:
        raise ValueError("нет точки с собственным полигоном")
    own = district.geoms[district.own_polygon_index(district.points[best_pid])]
    hull = own.convex_hull
    ring = hull.buffer(12.0).difference(hull.buffer(3.0))
    after = estimate_routes(district, extra_obstacles=[ring])
    if best_pid not in after.unreachable:
        raise ValueError(f"кольцо вокруг точки {best_pid} не сделало её недостижимой")
    builder.scenario.routes = after.routes
    builder.add_restriction(
        "prohibited_site", ring,
        f"кольцо 3–12 м вокруг полигона точки {best_pid} (до соседей {best_score:.1f} м); "
        f"без маршрута остались точки {_fmt_ids(after.unreachable)}",
    )
    return builder.scenario


BUILDERS: dict[str, Callable[[District], Scenario]] = {
    "roads": scenario_roads,
    "utilities": scenario_utilities,
    "tram": scenario_tram,
    "blocks": scenario_blocks,
    "mixed": scenario_mixed,
    "strings": scenario_strings,
    "big": scenario_big,
    "unreachable": scenario_unreachable,
}


def build(name: str, district: District) -> Scenario:
    return BUILDERS[name](district)


def _fmt_ids(ids: Iterable[Any]) -> str:
    return ", ".join(str(v) for v in sorted(ids, key=lambda v: (str(type(v)), v)))


# ---------------------------------------------------------------------------
# Вывод: GeoJSON и картинка
# ---------------------------------------------------------------------------

COLORS = {
    "road": "#3d3d3d",
    "tram_tracks": "#7b2cbf",
    "gas_pipeline": "#e07b00",
    "power_cable": "#d0007a",
    "park": "#2a9d2a",
    "social_area": "#1f6fd0",
    "prohibited_site": "#c1121f",
    "water": "#8fd3ff",
    "railway": "#a0522d",
    "oks": "#b0b0b0",
    "heat_network": "#e00000",
}


def write(scenario: Scenario, out_dir: Path = CACHE_DIR) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    geojson_path = out_dir / f"{scenario.name}.geojson"
    with open(geojson_path, "w", encoding="utf-8") as fh:
        json.dump(scenario.to_geojson(), fh, ensure_ascii=False)
    png_path = out_dir / f"{scenario.name}.png"
    render(scenario, png_path)
    return geojson_path, png_path


def _poly_patch(poly: Polygon, **kw):
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path as MplPath

    poly = orient(poly)
    verts, codes = [], []
    for ring in (poly.exterior, *poly.interiors):
        pts = list(ring.coords)
        verts += pts
        codes += [MplPath.MOVETO] + [MplPath.LINETO] * (len(pts) - 2) + [MplPath.CLOSEPOLY]
    return PathPatch(MplPath(verts, codes), **kw)


def render(scenario: Scenario, path: Path,
           extent: tuple[float, float, float, float] | None = None) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    feats = scenario.features
    geoms = [crs.to_metric(shape(f["geometry"]), METRIC_CRS) for f in feats]
    focus = [g for (_, g) in scenario.new_geoms] + list(scenario.routes.values())
    focus += [g for f, g in zip(feats, geoms)
              if f["properties"]["object_type"] in ("oks_connection_point", "heat_network")]
    minx, miny, maxx, maxy = extent or unary_union(focus).buffer(40.0).bounds
    w, h = maxx - minx, maxy - miny
    scale = 16.0 / max(w, h)
    fig, ax = plt.subplots(figsize=(max(6.0, w * scale), max(6.0, h * scale)))

    for f, g in zip(feats, geoms):
        p = f["properties"]
        t = p["object_type"]
        if t == "restriction":
            rt = p.get("restriction_type")
            color = COLORS.get(rt, "#c8c8c8") if rt in ("oks", "water", "railway") else None
            if color is None:
                continue
            for poly in _polygons(g):
                ax.add_patch(_poly_patch(poly, fc=color, ec="#707070", lw=0.3, alpha=0.7))
        elif t == "heat_network":
            xs, ys = zip(*g.coords)
            ax.plot(xs, ys, color=COLORS["heat_network"], lw=1.8, zorder=3)
        elif t == "heat_chamber":
            ax.plot(g.x, g.y, "s", color="#8b0000", ms=5, zorder=4)
        elif t == "oks_connection_point":
            ax.plot(g.x, g.y, "o", color="#1a8a1a", ms=6, zorder=5)
            ax.annotate(str(p["id"]), (g.x, g.y), xytext=(3, 3), textcoords="offset points",
                        fontsize=7, color="#1a8a1a", weight="bold")
        elif t == "source":
            ax.plot(g.x, g.y, "*", color="#ff9900", ms=14, zorder=5)

    for pid, r in scenario.routes.items():
        xs, ys = zip(*r.coords)
        ax.plot(xs, ys, "--", color="#4aa3df", lw=0.8, alpha=0.8, zorder=2)

    for rtype, g in scenario.new_geoms:
        color = COLORS.get(rtype, "#000000")
        if isinstance(g, (Polygon, MultiPolygon)):
            for poly in _polygons(g):
                ax.add_patch(_poly_patch(poly, fc=color, ec=color, lw=1.0, alpha=0.45, zorder=6))
        else:
            for line in _lines(g):
                xs, ys = zip(*line.coords)
                ax.plot(xs, ys, color=color, lw=2.5, zorder=6)
        c = g.representative_point()
        ax.annotate(rtype, (c.x, c.y), xytext=(4, 4), textcoords="offset points", fontsize=8,
                    color=color, weight="bold", zorder=7,
                    bbox={"boxstyle": "round,pad=0.15", "fc": "white", "ec": color, "alpha": 0.8})

    ax.set_xlim(minx, maxx)
    ax.set_ylim(miny, maxy)
    ax.set_aspect("equal")
    ax.set_title(f"case_{scenario.name}: {len(scenario.new_geoms)} новых объектов", fontsize=10)
    ax.tick_params(labelsize=6)
    fig.savefig(path, dpi=110, bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--scenario", choices=SCENARIOS, action="append", default=[])
    parser.add_argument("--all", action="store_true", help="все сценарии")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--out", type=Path, default=CACHE_DIR)
    args = parser.parse_args(argv)
    names = list(SCENARIOS) if args.all else args.scenario
    if not names:
        parser.error("укажите --scenario или --all")

    district = District.load(args.input)
    for name in names:
        scenario = build(name, district)
        geojson_path, png_path = write(scenario, args.out)
        print(f"[{name}] {geojson_path.name}, {png_path.name}: "
              f"{len(scenario.features)} объектов, новых {len(scenario.new_geoms)}")
        for note in scenario.notes:
            print(f"    {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
