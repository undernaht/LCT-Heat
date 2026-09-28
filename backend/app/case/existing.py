"""Влияние новых подключений на существующую сеть (ТЗ §4 п. 6, справочно).

Реконструкция в актуальной модели не выполняется (разъяснение № 14), и во
входе нет ни текущих расходов существующих участков, ни ссылок «следующий
объект к источнику». Но показать, по каким существующим участкам пойдёт
добавочный расход, можно: от каждой точки присоединения — кратчайший путь по
существующей сети к источнику, и на каждом участке пути расход суммируется.
Отношение добавки к пропускной способности Ду — ориентир, где сеть загружена
новыми подключениями сильнее всего; выводом реконструкции это не является.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field as dc_field
from typing import Any

from shapely.geometry import Point

from .model import CaseInput, ExistingEdge
from .network import NODE_CHAMBER_NEW, Network
from .rules import Rules

SNAP_M = 0.5
# Дальше этого от ближайшей вершины сети источник или камера считаются «не на сети»
ATTACH_M = 25.0


@dataclass
class EdgeImpact:
    edge_id: Any
    diameter: int
    length_m: float
    added_flow_tph: float
    capacity_tph: float | None
    tie_ins: list[Any] = dc_field(default_factory=list)   # какие точки присоединения дают расход

    @property
    def share(self) -> float | None:
        if not self.capacity_tph:
            return None
        return self.added_flow_tph / self.capacity_tph


@dataclass
class Impact:
    edges: list[EdgeImpact]
    source_found: bool
    unreached: list[str]            # узлы присоединения, от которых источник не достижим по сети
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_found": self.source_found,
            "unreached_tie_ins": self.unreached,
            "note": self.note,
            "edges": [
                {
                    "edge_id": e.edge_id, "diameter": e.diameter, "length_m": round(e.length_m, 1),
                    "added_flow_tph": round(e.added_flow_tph, 3), "capacity_tph": e.capacity_tph,
                    "share_of_capacity": round(e.share, 3) if e.share is not None else None,
                    "tie_ins": e.tie_ins,
                }
                for e in sorted(self.edges, key=lambda x: -x.added_flow_tph)
            ],
        }


class _Graph:
    """Граф существующей сети: вершины — концы участков (склеены в допуске)."""

    def __init__(self, edges: list[ExistingEdge]):
        self.points: list[tuple[float, float]] = []
        self.adj: dict[int, list[tuple[int, int, float]]] = {}   # v → [(u, edge index, длина)]
        self.edges = edges
        for index, edge in enumerate(edges):
            a = self._vertex(edge.geom.coords[0])
            b = self._vertex(edge.geom.coords[-1])
            self.adj.setdefault(a, []).append((b, index, edge.geom.length))
            self.adj.setdefault(b, []).append((a, index, edge.geom.length))

    def _vertex(self, xy) -> int:
        for i, p in enumerate(self.points):
            if math.dist(p, xy) <= SNAP_M:
                return i
        self.points.append((float(xy[0]), float(xy[1])))
        self.adj.setdefault(len(self.points) - 1, [])
        return len(self.points) - 1

    def nearest_vertex(self, point: Point, *, within_m: float = ATTACH_M) -> int | None:
        """Ближайшая вершина графа, если она не дальше `within_m`."""
        if not self.points:
            return None
        index = min(range(len(self.points)), key=lambda i: math.dist(self.points[i], (point.x, point.y)))
        return index if math.dist(self.points[index], (point.x, point.y)) <= within_m else None

    def attach_point(self, point: Point, *, within_m: float = ATTACH_M) -> int | None:
        """Вершина для точки: существующая вершина в допуске склейки, иначе новая
        вершина на ближайшем участке (источник или камера посреди участка).
        Дальше `within_m` от сети точка к графу не привязывается."""
        vertex = self.nearest_vertex(point, within_m=SNAP_M)
        if vertex is not None or not self.edges:
            return vertex
        index = min(range(len(self.edges)), key=lambda i: self.edges[i].geom.distance(point))
        edge = self.edges[index]
        if edge.geom.distance(point) > within_m:
            return None
        along = edge.geom.project(point)
        a = self._vertex(edge.geom.coords[0])
        b = self._vertex(edge.geom.coords[-1])
        proj = edge.geom.interpolate(along)
        self.points.append((proj.x, proj.y))
        vertex = len(self.points) - 1
        halves = [(a, index, along), (b, index, edge.geom.length - along)]
        self.adj[vertex] = list(halves)
        for end, _, length in halves:
            self.adj[end].append((vertex, index, length))
        return vertex

    def path_edges(self, start: int, goal: int) -> list[int] | None:
        """Индексы участков на кратчайшем пути (Дейкстра по длине)."""
        dist = {start: 0.0}
        prev: dict[int, tuple[int, int]] = {}
        heap = [(0.0, start)]
        while heap:
            d, v = heapq.heappop(heap)
            if v == goal:
                break
            if d > dist.get(v, math.inf):
                continue
            for u, edge_index, length in self.adj.get(v, []):
                nd = d + length
                if nd < dist.get(u, math.inf):
                    dist[u] = nd
                    prev[u] = (v, edge_index)
                    heapq.heappush(heap, (nd, u))
        if goal not in dist:
            return None
        result = []
        v = goal
        while v != start:
            v, edge_index = prev[v]
            result.append(edge_index)
        return result


def analyse(case: CaseInput, net: Network, rules: Rules, flow_by_oks_node: dict[str, float]) -> Impact:
    if case.source is None or not case.network:
        return Impact([], False, [], "источник или существующая сеть отсутствуют — путь к источнику не строится")

    graph = _Graph(case.network)
    source_vertex = graph.attach_point(case.source)
    if source_vertex is None:
        return Impact([], False, [], f"источник дальше {ATTACH_M:.0f} м от существующей сети — "
                                     "путь к источнику не строится")
    added: dict[int, EdgeImpact] = {}
    unreached: list[str] = []
    # Номера новых камер — как их нумерует выгрузка (output.variant_features): «камера k» ↔ v*_chamber_k
    chamber_no = {n.id: k for k, n in enumerate((n for n in net.nodes.values() if n.kind == NODE_CHAMBER_NEW), start=1)}

    for root in net.tie_in_nodes():
        flows = net.subtree_flow(root.id, flow_by_oks_node)
        total = sum(flows[s.id] for s in net.incident(root.id))
        if total <= 0:
            continue
        # Подпись точки присоединения: существующая камера — её id, новая — участок,
        # на котором стоит (с id узла: на одном участке может быть несколько врезок)
        label = (f"камера {root.ref}" if root.existing is not None
                 else f"участок {root.on_edge.id}, новая камера {chamber_no.get(root.id, '?')}"
                 if root.on_edge is not None else str(root.id))
        # Точка присоединения — вершина графа: существующая камера на конце участка
        # или новая камера посреди участка (вершина на участке, обе половины ведут
        # к его концам и считаются тем же участком)
        start = graph.attach_point(Point(*root.point))
        path = graph.path_edges(start, source_vertex) if start is not None else None
        if path is None:
            unreached.append(label)
            continue
        for index in dict.fromkeys(path):
            edge = case.network[index]
            item = added.get(index)
            if item is None:
                cap = rules.diameter(edge.diameter).capacity_tph if rules.has_diameter(edge.diameter) else None
                item = added[index] = EdgeImpact(edge.id, edge.diameter, edge.geom.length, 0.0, cap)
            item.added_flow_tph += total
            item.tie_ins.append(label)

    return Impact(
        edges=list(added.values()), source_found=source_vertex is not None, unreached=unreached,
        note="добавочный расход по пути к источнику; текущие расходы существующей сети во входе не заданы, "
             "реконструкция в актуальной модели не выполняется (разъяснение № 14)",
    )
