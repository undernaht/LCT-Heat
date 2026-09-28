"""Построенная сеть: узлы, участки, разбиение, потоки.

Узлы — точки подключения, камеры (новые и существующие) и технические узлы.
Участок — ломаная между двумя узлами; повороты — её внутренние вершины.
Ду и стоимости назначаются позже (diameters.py, costing.py); здесь — топология.
"""

from __future__ import annotations

import copy
import math
from collections import defaultdict
from dataclasses import dataclass, field as dc_field, replace
from typing import Any, Iterator

from shapely.geometry import LineString, Point

from .model import ExistingChamber, ExistingEdge

Point2 = tuple[float, float]

NODE_OKS = "oks"
NODE_CHAMBER_NEW = "chamber_new"
NODE_CHAMBER_EXISTING = "chamber_existing"
NODE_TECH = "technical_node"

# Разрез ближе этого к вершине ломаной прилипает к ней: иначе остаётся
# микроотрезок, который ломает угол в узле (валидатор: 0,3 м → 91°)
SNAP_TO_VERTEX_M = 1.0


@dataclass
class Node:
    id: str
    point: Point2
    kind: str
    ref: Any = None                         # исходный id (точка ОКС / существующая камера)
    on_edge: ExistingEdge | None = None     # новая камера на существующем участке
    existing: ExistingChamber | None = None
    tie_ins: int = 0                        # врезок в существующую камеру
    existing_connections: int = 0           # примыканий существующей сети в этой точке (§9.4)
    approach_rule: str = ""                 # nearest | fallback — для точек подключения
    note: str = ""

    @property
    def is_tie_in_point(self) -> bool:
        return self.kind in (NODE_CHAMBER_NEW, NODE_CHAMBER_EXISTING) and (
            self.on_edge is not None or self.existing is not None
        )


@dataclass
class Segment:
    id: str
    points: list[Point2]                    # от start к end
    start: str
    end: str
    laying: str = "base"                    # base | special
    special_types: list[str] = dc_field(default_factory=list)
    special_ids: list[Any] = dc_field(default_factory=list)
    k_special: float = 1.0
    flow_tph: float = 0.0
    du: int = 0
    depth_start: float | None = None
    depth_end: float | None = None
    k_depth: float = 1.0
    cost: float = 0.0
    step: int = 0                           # шаг построения (номер подключаемой точки), см. Network.step

    @property
    def length(self) -> float:
        return sum(math.dist(a, b) for a, b in zip(self.points, self.points[1:]))

    @property
    def line(self) -> LineString:
        return LineString(self.points)

    def other(self, node_id: str) -> str:
        return self.end if node_id == self.start else self.start


class Network:
    """Дерево (лес) новых участков с узлами."""

    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.segments: dict[str, Segment] = {}
        self._counter: dict[str, int] = defaultdict(int)
        self._adjacent: dict[str, set[str]] = defaultdict(set)   # узел → id участков при нём
        # Шаг построения: строитель увеличивает его на каждую точку; участки
        # помнят свой шаг, чтобы локальный поиск видел, что появилось после ветки
        self.step = 0

    # --- создание ---

    def new_id(self, prefix: str) -> str:
        self._counter[prefix] += 1
        return f"{prefix}_{self._counter[prefix]}"

    def add_node(self, point: Point2, kind: str, **kwargs: Any) -> Node:
        prefix = {NODE_OKS: "oks", NODE_CHAMBER_NEW: "ch", NODE_CHAMBER_EXISTING: "ech", NODE_TECH: "tn"}[kind]
        node = Node(id=self.new_id(prefix), point=point, kind=kind, **kwargs)
        self.nodes[node.id] = node
        return node

    def add_segment(self, points: list[Point2], start: str, end: str, **kwargs: Any) -> Segment:
        assert math.dist(points[0], self.nodes[start].point) < 1e-3, "начало не в узле start"
        assert math.dist(points[-1], self.nodes[end].point) < 1e-3, "конец не в узле end"
        kwargs.setdefault("step", self.step)
        segment = Segment(id=self.new_id("seg"), points=list(points), start=start, end=end, **kwargs)
        self.segments[segment.id] = segment
        self._adjacent[start].add(segment.id)
        self._adjacent[end].add(segment.id)
        return segment

    # --- снимок и удаление (локальный поиск перестраивает ветки и откатывается) ---

    def snapshot(self) -> "NetworkSnapshot":
        """Копия узлов и участков; ссылки на объекты входа (участки, камеры) общие."""
        return NetworkSnapshot(
            nodes={k: copy.copy(n) for k, n in self.nodes.items()},
            segments={
                k: replace(s, points=list(s.points), special_types=list(s.special_types),
                           special_ids=list(s.special_ids))
                for k, s in self.segments.items()
            },
            counter=dict(self._counter), step=self.step,
            adjacent={k: set(v) for k, v in self._adjacent.items() if v},
        )

    def restore(self, snap: "NetworkSnapshot") -> None:
        self.nodes = snap.nodes
        self.segments = snap.segments
        self._counter = defaultdict(int, snap.counter)
        self.step = snap.step
        self._adjacent = defaultdict(set, {k: set(v) for k, v in snap.adjacent.items()})

    def remove_segment(self, segment_id: str) -> None:
        segment = self.segments.pop(segment_id)
        self._adjacent[segment.start].discard(segment_id)
        self._adjacent[segment.end].discard(segment_id)

    def remove_node(self, node_id: str) -> None:
        assert not self.incident(node_id), "узел ещё держит участки"
        del self.nodes[node_id]
        self._adjacent.pop(node_id, None)

    def merge_through(self, node_id: str) -> Segment:
        """Убрать проходной узел (степень 2): два участка сливаются в один."""
        first, second = self.incident(node_id)
        a = first.other(node_id)
        b = second.other(node_id)
        head = first.points if first.end == node_id else list(reversed(first.points))
        tail = second.points if second.start == node_id else list(reversed(second.points))
        common = dict(laying=first.laying, special_types=list(first.special_types),
                      special_ids=list(first.special_ids), k_special=first.k_special,
                      step=min(first.step, second.step))
        self.remove_segment(first.id)
        self.remove_segment(second.id)
        self.remove_node(node_id)
        return self.add_segment(_collapse_collinear(_dedupe(head + tail[1:])), a, b, **common)

    # --- топология ---

    def incident(self, node_id: str) -> list[Segment]:
        ids = self._adjacent.get(node_id)
        if not ids:
            return []
        # в порядке создания участков (seg_N) — детерминированно, как обход всех участков
        return [self.segments[i] for i in sorted(ids, key=_segment_order)]

    def degree(self, node_id: str) -> int:
        return len(self.incident(node_id))

    def connections_used(self, node: Node) -> int:
        """Занятые примыкания камеры по §2.2 приложения."""
        used = self.degree(node.id)
        if node.kind == NODE_CHAMBER_NEW and node.on_edge is not None:
            used += node.existing_connections  # §9.4: концы участков + 2, если внутри участка
        if node.kind == NODE_CHAMBER_EXISTING and node.existing is not None:
            used += node.existing.connections
        return used

    def free_connections(self, node: Node, max_connections: int) -> int:
        return max_connections - self.connections_used(node)

    def split_segment(self, segment: Segment, point: Point2, kind: str = NODE_CHAMBER_NEW, **kwargs: Any) -> Node:
        """Разрезать участок в точке на его ломаной; вернуть новый узел.

        Если точка ближе SNAP_TO_VERTEX_M к внутренней вершине ломаной, узел
        ставится в саму вершину — без микроотрезка.
        """
        line = segment.line
        distance = line.project(Point(point))
        snapped = line.interpolate(distance)
        snapped_pt = (snapped.x, snapped.y)

        # Точка совпадает с концом — новый узел не нужен
        for node_id in (segment.start, segment.end):
            if math.dist(self.nodes[node_id].point, snapped_pt) < 0.05:
                return self.nodes[node_id]

        vertex_index = nearest_interior_vertex(segment.points, snapped_pt, SNAP_TO_VERTEX_M)
        if vertex_index is not None:
            snapped_pt = segment.points[vertex_index]
            before = segment.points[: vertex_index + 1]
            after = segment.points[vertex_index:]
        else:
            before = []
            after = []
            acc = 0.0
            placed = False
            for a, b in zip(segment.points, segment.points[1:]):
                seg_len = math.dist(a, b)
                if not placed:
                    before.append(a)
                    if acc + seg_len >= distance - 1e-9:
                        before.append(snapped_pt)
                        after.append(snapped_pt)
                        placed = True
                else:
                    after.append(a)
                acc += seg_len
            after.append(segment.points[-1])
            if not placed:
                before.append(snapped_pt)
                after = [snapped_pt, segment.points[-1]]

        node = self.add_node(snapped_pt, kind, **kwargs)
        self.remove_segment(segment.id)
        common = dict(laying=segment.laying, special_types=list(segment.special_types),
                      special_ids=list(segment.special_ids), k_special=segment.k_special,
                      step=segment.step)
        self.add_segment(_dedupe(before), segment.start, node.id, **common)
        self.add_segment(_dedupe(after), node.id, segment.end, **common)
        return node

    # --- обход ---

    def tie_in_nodes(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.is_tie_in_point]

    def oks_nodes(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.kind == NODE_OKS]

    def components(self) -> list[set[str]]:
        seen: set[str] = set()
        result = []
        for start in self.nodes:
            if start in seen:
                continue
            comp = {start}
            stack = [start]
            while stack:
                current = stack.pop()
                for s in self.incident(current):
                    other = s.other(current)
                    if other not in comp:
                        comp.add(other)
                        stack.append(other)
            seen |= comp
            result.append(comp)
        return result

    def oriented_from_root(self, root_id: str) -> Iterator[tuple[Segment, str, str]]:
        """Обход компоненты от узла присоединения: (участок, ближний к корню узел, дальний)."""
        seen = {root_id}
        stack = [root_id]
        while stack:
            current = stack.pop()
            for s in self.incident(current):
                other = s.other(current)
                if other in seen:
                    continue
                seen.add(other)
                yield s, current, other
                stack.append(other)

    def subtree_flow(self, root_id: str, oks_flow: dict[str, float]) -> dict[str, float]:
        """Расход каждого участка компоненты = сумма расходов точек дальше от корня."""
        children: dict[str, list[tuple[Segment, str]]] = defaultdict(list)
        order: list[tuple[Segment, str, str]] = list(self.oriented_from_root(root_id))
        for s, near, far in order:
            children[near].append((s, far))
        flow_of_node: dict[str, float] = {}

        def visit(node_id: str) -> float:
            total = oks_flow.get(node_id, 0.0)
            for s, far in children[node_id]:
                total += visit(far)
            flow_of_node[node_id] = total
            return total

        visit(root_id)
        return {s.id: flow_of_node[far] for s, _, far in order}

    def paths_to_root(self, root_id: str) -> dict[str, list[Segment]]:
        """Для каждого узла компоненты — список участков от него до корня."""
        parent: dict[str, tuple[Segment, str]] = {}
        for s, near, far in self.oriented_from_root(root_id):
            parent[far] = (s, near)
        paths: dict[str, list[Segment]] = {}
        for node_id in parent:
            path = []
            current = node_id
            while current in parent:
                s, near = parent[current]
                path.append(s)
                current = near
            paths[node_id] = path
        return paths

    def total_length(self) -> float:
        return sum(s.length for s in self.segments.values())


def _segment_order(segment_id: str) -> int:
    return int(segment_id.rsplit("_", 1)[1])


@dataclass
class NetworkSnapshot:
    nodes: dict[str, Node]
    segments: dict[str, Segment]
    counter: dict[str, int]
    step: int = 0
    adjacent: dict[str, set[str]] = dc_field(default_factory=dict)


def nearest_interior_vertex(points: list[Point2], target: Point2, tolerance: float) -> int | None:
    """Индекс внутренней вершины ломаной ближе tolerance к точке, либо None."""
    best: tuple[float, int] | None = None
    for index in range(1, len(points) - 1):
        d = math.dist(points[index], target)
        if d <= tolerance and (best is None or d < best[0]):
            best = (d, index)
    return best[1] if best else None


def snap_point_to_line(points: list[Point2], target: Point2, tolerance: float) -> Point2:
    """Точка на ломаной: близкая вершина, иначе проекция."""
    index = nearest_interior_vertex(points, target, tolerance)
    if index is not None:
        return points[index]
    line = LineString(points)
    p = line.interpolate(line.project(Point(target)))
    return (p.x, p.y)


def _collapse_collinear(points: list[Point2], tolerance_deg: float = 0.5) -> list[Point2]:
    """Убрать вершины на прямой: точка бывшей проходной камеры на прямом стволе."""
    if len(points) < 3:
        return points
    result = [points[0]]
    for prev, cur, nxt in zip(points, points[1:], points[2:]):
        ax, ay = cur[0] - prev[0], cur[1] - prev[1]
        bx, by = nxt[0] - cur[0], nxt[1] - cur[1]
        na, nb = math.hypot(ax, ay), math.hypot(bx, by)
        if na < 1e-9 or nb < 1e-9:
            continue
        cos = max(-1.0, min(1.0, (ax * bx + ay * by) / (na * nb)))
        if math.degrees(math.acos(cos)) > tolerance_deg:
            result.append(cur)
    result.append(points[-1])
    return result


def _dedupe(points: list[Point2]) -> list[Point2]:
    result: list[Point2] = []
    for p in points:
        if not result or math.dist(result[-1], p) > 1e-6:
            result.append(p)
    return result
