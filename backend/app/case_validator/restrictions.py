"""Пространственные ограничения (таблица 2) и специальные проходы (§4).

Габарит новой сети — ось ± w(Ду)/2, поэтому все расстояния считаются от оси
и сравниваются с «отступ + w/2 + полугабарит объекта». Спецпроход
проверяется не по участкам, а по «пробегу» — цепочке специальных участков
через технические узлы: поля ±2/±3 м отсчитываются вдоль трассы от точек
пересечения, и один пробег может обслуживать несколько наложенных зон.
Существующая тепловая сеть — такой же спецпроходный объект (разъяснение № 10),
кроме линии, на которой стоит камера присоединения.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shapely.geometry import LineString, Point
from shapely.geometry.base import BaseGeometry

from app.case_validator.findings import Findings
from app.case_validator.geom_utils import (
    XY,
    boundary_lines,
    cumulative,
    direction_at,
    exterior_polygons,
    line_parts,
    lines_angle,
    local_direction,
    point_parts,
    polygon_parts,
    turn_angle,
)
from app.case_validator.model import IdKey, InputData, InputLine, Restriction, Segment
from app.case_validator.rules import RestrictionRule, Rules
from app.case_validator.topology import Node, VariantGraph

EPS = 1e-3            # метрический допуск сравнения расстояний
COVER_TOL = 0.01      # допуск покрытия поля спецучастка, м
ANGLE_TOL = 0.05      # допуск углов, градусы
STRAIGHT_TOL = 0.05   # излом внутри спецпрохода, градусы
EXTRA_TOL = 0.05      # лишняя длина спецучастка вне зон, м
ON_WALL_M = 0.5       # §9.2: точка на границе/чуть снаружи принадлежит полигону


@dataclass
class SpecialObject:
    id: Any
    key: tuple[str, Any]
    rtype: str
    core: BaseGeometry
    rule: RestrictionRule
    half_gauge: float
    line: InputLine | None = None   # для существующей сети — сама линия

    @property
    def is_polygon(self) -> bool:
        return bool(polygon_parts(self.core))

    @property
    def label(self) -> str:
        return f"{self.rtype} {self.id!r}"


@dataclass
class Zone:
    obj: SpecialObject
    start: float
    end: float
    pieces: list[tuple[float, float]] = field(default_factory=list)  # пикеты входа/выхода


@dataclass
class SpecialRun:
    segments: list[Segment]
    coords: list[XY]
    line: LineString
    chainage: dict[IdKey, tuple[float, float]]
    joints: list[tuple[float, Node]]
    nodes: list[Node]   # концевые узлы пробега
    zones: list[Zone] = field(default_factory=list)

    def segment_at(self, c: float) -> Segment:
        for seg in self.segments:
            c0, c1 = self.chainage[seg.key]
            if c0 - COVER_TOL <= c <= c1 + COVER_TOL:
                return seg
        return self.segments[-1]


@dataclass
class SegmentInfo:
    k_special: float = 1.0
    crossed: frozenset = frozenset()
    run: SpecialRun | None = None


# --- объекты ----------------------------------------------------------------


def special_objects(inp: InputData, rules: Rules) -> list[SpecialObject]:
    objects: list[SpecialObject] = []
    for r in inp.restrictions:
        rule = rules.restriction(r.rtype)
        if rule is None or not rule.is_special:
            continue
        objects.append(SpecialObject(r.id, ("r", r.key), r.rtype, r.geom, rule, rules.half_gauge(rule)))
    hn_rule = rules.restriction("heat_network")
    if hn_rule is not None:
        for line in inp.lines.values():
            objects.append(SpecialObject(line.id, ("hn", line.key), "heat_network", line.geom, hn_rule,
                                         rules.half_gauge(hn_rule, line.diameter), line))
    return objects


def block_objects(inp: InputData, rules: Rules) -> list[tuple[Restriction, RestrictionRule]]:
    result = []
    for r in inp.restrictions:
        rule = rules.restriction(r.rtype)
        if rule is not None and rule.is_block:
            result.append((r, rule))
    return result


def own_polygons(inp: InputData) -> dict[IdKey, list[Restriction]]:
    """Собственные полигоны точек подключения (§2.2 приложения, §9.2 спецификации)."""
    oks_restrictions = [r for r in inp.restrictions if r.rtype == "oks"]
    own: dict[IdKey, list[Restriction]] = {}
    for key, point in inp.oks.items():
        inside = [r for r in oks_restrictions if any(p.covers(point.geom) for p in exterior_polygons(r.geom))]
        if not inside:
            inside = [r for r in oks_restrictions if r.geom.distance(point.geom) <= ON_WALL_M]
        own[key] = inside
    return own


# --- пробеги спецпроходов ------------------------------------------------------


def build_runs(graph: VariantGraph) -> list[SpecialRun]:
    assigned: set[IdKey] = set()
    runs: list[SpecialRun] = []

    def next_special(node: Node, seg: Segment) -> Segment | None:
        if node.kind != "tech" or node.degree != 2:
            return None
        other = node.segments[0] if node.segments[1] is seg else node.segments[1]
        if other is seg or not other.is_special or other.key in assigned:
            return None
        return other

    for seg in sorted(graph.variant.usable_segments, key=lambda s: s.index):
        if not seg.is_special or seg.key in assigned:
            continue
        assigned.add(seg.key)
        chain = [seg]
        coords = seg.coords
        joints_nodes: list[Node] = []
        # вперёд от конца
        node = graph.node_of(seg.end_ref)
        cur = seg
        while (nxt := next_special(node, cur)) is not None:
            assigned.add(nxt.key)
            chain.append(nxt)
            joints_nodes.append(node)
            coords += nxt.coords_from(node.key)[1:]
            node = graph.node_of(nxt.other_ref(node.key))
            cur = nxt
        end_node = node
        # назад от начала
        node = graph.node_of(seg.start_ref)
        cur = seg
        while (prv := next_special(node, cur)) is not None:
            assigned.add(prv.key)
            chain.insert(0, prv)
            joints_nodes.insert(0, node)
            coords = prv.coords_from(node.key)[::-1][:-1] + coords
            node = graph.node_of(prv.other_ref(node.key))
            cur = prv
        start_node = node
        acc = cumulative(coords)
        chainage: dict[IdKey, tuple[float, float]] = {}
        pos = 0
        for s in chain:
            n = len(s.coords)
            chainage[s.key] = (acc[pos], acc[pos + n - 1])
            pos += n - 1
        joints = [(chainage[chain[i + 1].key][0], joints_nodes[i]) for i in range(len(joints_nodes))]
        runs.append(SpecialRun(chain, coords, LineString(coords), chainage, joints, [start_node, end_node]))
    return runs


def _crossing_pieces(run: SpecialRun, obj: SpecialObject, attach_tol: float) -> tuple[list[tuple[float, float]], list[float]]:
    """Пикеты пересечения пробега с объектом; для линий — точки, для полигонов — интервалы."""
    inter = run.line.intersection(obj.core)
    pieces: list[tuple[float, float]] = []
    overlaps: list[float] = []
    if inter.is_empty:
        return pieces, overlaps
    for part in line_parts(inter):
        if part.length <= EPS:
            continue
        cs = [run.line.project(Point(c)) for c in part.coords]
        pieces.append((min(cs), max(cs)))
        if not obj.is_polygon:
            overlaps.append(part.length)
    if not obj.is_polygon:
        skip = [Point(n.xy) for n in run.nodes if obj.line is not None and n.is_attachment
                and any(ln.key == obj.line.key for ln in n.near_lines)]
        for pt in point_parts(inter):
            if any(pt.distance(s) <= attach_tol for s in skip):
                continue
            c = run.line.project(pt)
            pieces.append((c, c))
    pieces.sort()
    return pieces, overlaps


def compute_zones(runs: list[SpecialRun], objects: list[SpecialObject], rules: Rules, f: Findings) -> None:
    for run in runs:
        vid = run.segments[0].variant_id
        for obj in objects:
            if obj.core.distance(run.line) > obj.rule.margin_m + EPS:
                continue
            pieces, overlaps = _crossing_pieces(run, obj, rules.attach_tol_m)
            for length in overlaps:
                f.error("special.overlap_line",
                        f"специальный участок накладывается на линию {obj.label} на {length:.2f} м — пересечение должно быть точечным",
                        variant_id=vid, object_id=run.segments[0].id, actual=round(length, 3))
            for c_in, c_out in pieces:
                zone = Zone(obj, c_in - obj.rule.margin_m, c_out + obj.rule.margin_m, [(c_in, c_out)])
                merged = False
                for existing in run.zones:
                    if existing.obj is obj and existing.start <= zone.end + EPS and zone.start <= existing.end + EPS:
                        existing.start = min(existing.start, zone.start)
                        existing.end = max(existing.end, zone.end)
                        existing.pieces.append((c_in, c_out))
                        merged = True
                        break
                if not merged:
                    run.zones.append(zone)


def segment_infos(graph: VariantGraph, runs: list[SpecialRun]) -> dict[IdKey, SegmentInfo]:
    infos = {s.key: SegmentInfo() for s in graph.variant.usable_segments}
    for run in runs:
        for seg in run.segments:
            c0, c1 = run.chainage[seg.key]
            hits = [z for z in run.zones if min(c1, z.end) - max(c0, z.start) > EPS]
            infos[seg.key] = SegmentInfo(
                k_special=max((z.obj.rule.k_special for z in hits), default=1.0),
                crossed=frozenset(z.obj.key for z in hits),
                run=run,
            )
    return infos


def check_runs(runs: list[SpecialRun], rules: Rules, f: Findings) -> None:
    for run in runs:
        vid = run.segments[0].variant_id
        length = run.line.length
        acc = cumulative(run.coords)
        turns = [
            (acc[i], turn_angle(run.coords[i - 1], run.coords[i], run.coords[i + 1]))
            for i in range(1, len(run.coords) - 1)
        ]
        joint_at = {round(c, 6): node for c, node in run.joints}
        for zone in run.zones:
            obj = zone.obj
            margin = obj.rule.margin_m
            c_in = min(p[0] for p in zone.pieces)
            c_out = max(p[1] for p in zone.pieces)
            if zone.start < -COVER_TOL:
                f.error("special.coverage",
                        f"спецпроход через {obj.label}: поле перед объектом {c_in:.2f} м вместо {margin} м (вдоль трассы)",
                        variant_id=vid, object_id=run.segment_at(c_in).id, expected=margin, actual=round(c_in, 3))
            if zone.end > length + COVER_TOL:
                f.error("special.coverage",
                        f"спецпроход через {obj.label}: поле за объектом {length - c_out:.2f} м вместо {margin} м (вдоль трассы)",
                        variant_id=vid, object_id=run.segment_at(c_out).id, expected=margin, actual=round(length - c_out, 3))
            for c, angle in turns:
                if zone.start + COVER_TOL < c < zone.end - COVER_TOL and angle > STRAIGHT_TOL:
                    node = joint_at.get(round(c, 6))
                    f.error("special.not_straight",
                            f"спецпроход через {obj.label} не прямой: излом {angle:.2f}° на пикете {c:.2f} м",
                            variant_id=vid, object_id=node.id if node else run.segment_at(c).id,
                            expected=f"≤ {STRAIGHT_TOL}°", actual=round(angle, 3))
            if obj.rule.min_angle_deg is not None:
                boundaries = boundary_lines(obj.core)
                for piece in zone.pieces:
                    for c in sorted(set(piece)):
                        at = run.line.interpolate(c)
                        edge = local_direction(boundaries, at)
                        if edge is None:
                            continue
                        angle = lines_angle(direction_at(run.line, c), edge)
                        if angle < obj.rule.min_angle_deg - ANGLE_TOL:
                            f.error("special.angle",
                                    f"угол пересечения {obj.label} на пикете {c:.2f} м: {angle:.1f}°",
                                    variant_id=vid, object_id=run.segment_at(c).id,
                                    expected=f"≥ {obj.rule.min_angle_deg}°", actual=round(angle, 2))
        # части пробега вне всех зон
        for seg in run.segments:
            c0, c1 = run.chainage[seg.key]
            covered = _covered_length(c0, c1, [(z.start, z.end) for z in run.zones])
            extra = (c1 - c0) - covered
            if covered <= EPS:
                f.warning("special.no_crossing",
                          "специальный участок не пересекает ни одного объекта таблицы 2 — Kспец не обоснован",
                          variant_id=vid, object_id=seg.id)
            elif extra > EXTRA_TOL:
                f.warning("special.extra_length",
                          f"специальный участок выходит за границы зон пересечения на {extra:.2f} м — Kспец применён к лишней длине",
                          variant_id=vid, object_id=seg.id, actual=round(float(extra), 3))


def _covered_length(c0: float, c1: float, intervals: list[tuple[float, float]]) -> float:
    clipped = sorted((max(c0, a), min(c1, b)) for a, b in intervals if min(c1, b) > max(c0, a))
    total = 0.0
    cur_a = cur_b = None
    for a, b in clipped:
        if cur_b is None or a > cur_b:
            if cur_b is not None:
                total += cur_b - cur_a
            cur_a, cur_b = a, b
        else:
            cur_b = max(cur_b, b)
    if cur_b is not None:
        total += cur_b - cur_a
    return total


# --- отступы -------------------------------------------------------------------


def _checked_geometry(seg: Segment, exempt_start: bool, exempt_end: bool) -> LineString | None:
    coords = seg.coords
    if exempt_start:
        coords = coords[1:]
    if exempt_end:
        coords = coords[:-1]
    return LineString(coords) if len(coords) >= 2 else None


def check_block_restrictions(graph: VariantGraph, inp: InputData, rules: Rules,
                             own: dict[IdKey, list[Restriction]], f: Findings) -> None:
    vid = graph.variant.variant_id
    blocks = block_objects(inp, rules)
    for seg in graph.variant.usable_segments:
        row = rules.row(seg.diameter)
        if row is None:
            continue
        half = row.width_m / 2.0
        own_start = {r.key for r in own.get(seg.start_ref.key, [])} if seg.start_ref.kind == "oks" else set()
        own_end = {r.key for r in own.get(seg.end_ref.key, [])} if seg.end_ref.kind == "oks" else set()
        for r, rule in blocks:
            clearance = rules.oks_clearance(seg.diameter) if r.rtype == "oks" else rule.clearance_m
            if r.geom.distance(seg.geom) >= clearance + half - EPS:
                continue
            geom = _checked_geometry(seg, r.key in own_start, r.key in own_end)
            if geom is None:
                continue
            d = r.geom.distance(geom)
            if d < half - EPS:
                f.error("restriction.intersection",
                        f"участок пересекает ограничение {r.rtype} {r.id!r} (пересечение запрещено)",
                        variant_id=vid, object_id=seg.id, expected="нет пересечения",
                        actual=f"ось в {d:.2f} м при полугабарите {half:.3f} м")
            elif d - half < clearance - EPS:
                f.error("restriction.clearance",
                        f"отступ от габарита до ограничения {r.rtype} {r.id!r}: {d - half:.2f} м",
                        variant_id=vid, object_id=seg.id, expected=f"≥ {clearance} м", actual=round(d - half, 3))


def _exempt_nodes(seg: Segment, obj: SpecialObject, graph: VariantGraph, infos: dict[IdKey, SegmentInfo]) -> list[Node]:
    nodes: list[Node] = []
    for ref in (seg.start_ref, seg.end_ref):
        node = graph.node_of(ref)
        exempt = False
        if obj.line is not None and node.is_attachment and any(ln.key == obj.line.key for ln in node.near_lines):
            exempt = True
        if any(o.key != seg.key and obj.key in infos[o.key].crossed for o in node.segments):
            exempt = True
        if exempt:
            nodes.append(node)
    return nodes


def check_special_clearances(graph: VariantGraph, objects: list[SpecialObject], infos: dict[IdKey, SegmentInfo],
                             rules: Rules, f: Findings) -> None:
    vid = graph.variant.variant_id
    for seg in graph.variant.usable_segments:
        row = rules.row(seg.diameter)
        if row is None:
            continue
        half = row.width_m / 2.0
        crossed = infos[seg.key].crossed
        for obj in objects:
            if obj.key in crossed:
                continue
            band_r = obj.rule.clearance_m + half + obj.half_gauge
            if obj.core.distance(seg.geom) >= band_r - EPS:
                continue
            exempt = _exempt_nodes(seg, obj, graph, infos)
            band = obj.core.buffer(band_r)
            parts = [p for p in line_parts(seg.geom.intersection(band)) if p.length > EPS]
            for part in parts:
                node = next((n for n in exempt if part.distance(Point(n.xy)) <= EPS), None)
                d = obj.core.distance(part)
                gap = d - half - obj.half_gauge
                if node is None:
                    if d < half + obj.half_gauge - EPS:
                        f.error("restriction.crossing_without_special",
                                f"участок ({seg.laying_method}) пересекает {obj.label} вне специального прохода",
                                variant_id=vid, object_id=seg.id, expected="laying_method=special с полями",
                                actual=f"расстояние между осями {d:.2f} м")
                    else:
                        f.error("restriction.along",
                                f"участок идёт вдоль {obj.label} в полосе отступа: между габаритами {gap:.2f} м "
                                f"(разрешено только пересечение спецпроходом)",
                                variant_id=vid, object_id=seg.id, expected=f"≥ {obj.rule.clearance_m} м", actual=round(gap, 3))
                    continue
                crossing = part.intersection(obj.core)
                if not crossing.is_empty and crossing.distance(Point(node.xy)) > rules.attach_tol_m:
                    f.error("restriction.crossing_without_special",
                            f"участок пересекает {obj.label} вне узла {node.id!r}",
                            variant_id=vid, object_id=seg.id)
                    continue
                limit = 2.0 * max(band_r - obj.core.distance(Point(node.xy)), 0.0) + EXTRA_TOL
                if part.length > limit + EPS:
                    f.warning("restriction.long_approach",
                              f"подход к узлу {node.id!r} проходит {part.length:.2f} м в полосе отступа {obj.label} "
                              f"(ожидается близкий к перпендикуляру подход, не длиннее {limit:.2f} м)",
                              variant_id=vid, object_id=seg.id, expected=f"≤ {limit:.2f}", actual=round(part.length, 3))
