"""Спецпроходы (§4 приложения): пересечения дорог, трамвая, газа, кабелей и
существующей сети без врезки.

Растровый поиск даёт лишь примерно перпендикулярное пересечение. Здесь по
готовой ломаной каждого участка:

1. находятся пересечения с объектами спецпроходных типов;
2. для каждого строится спецучасток — один прямой отрезок, покрывающий объект
   плюс поля, отсчитанные ВДОЛЬ ТРАССЫ (3 м для дороги/трамвая, 2 м для линий);
3. проверяется прямолинейность и угол (≥ 45° у дорог/трамвая в обеих точках);
4. если пересечение кривое или косое — оно перестраивается: через объект
   проводится прямая хорда под допустимым углом, а соседние вершины
   переподключаются к её концам;
5. участок делится техническими узлами на границах спецучастков и их
   наложений; фрагментам назначаются laying=special и наибольший Kспец.

Подход к месту присоединения — исключение (§9.9 спецификации): концевая
часть участка, заканчивающегося в узле присоединения, спецпроходом через
«свою» существующую линию не считается.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field as dc_field
from typing import Any

from shapely.geometry import LineString, Point
from shapely.geometry.base import BaseGeometry
from shapely.ops import substring

from .field import CaseField, SpecialZone
from .geometry import Obstacles, turn_angle_deg
from .network import NODE_CHAMBER_EXISTING, NODE_CHAMBER_NEW, NODE_TECH, Network, Segment
from .rules import Rules

Point2 = tuple[float, float]

# Допуск на «прямолинейность» спецучастка: суммарный поворот внутри интервала
STRAIGHT_TOLERANCE_DEG = 1.0
# Перебор направлений хорды при перестройке пересечения
CHORD_ANGLES_DEG = (0, 10, -10, 20, -20, 30, -30, 40, -40)
# Для линейных объектов без заданного угла — стремимся к перпендикуляру (§9.8)
LINE_CROSSING_MIN_ANGLE_DEG = 80.0


@dataclass
class Crossing:
    zone: SpecialZone
    d_start: float           # начало спецучастка вдоль ломаной, м
    d_end: float             # конец
    entry_angle_deg: float   # угол к границе/линии в точке входа
    exit_angle_deg: float


@dataclass
class SpecialIssue:
    segment_id: str
    restriction_id: Any
    type: str
    detail: str
    at: Point2


@dataclass
class SpecialReport:
    crossings: list[tuple[str, Crossing]] = dc_field(default_factory=list)
    rebuilt: list[str] = dc_field(default_factory=list)
    issues: list[SpecialIssue] = dc_field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


# --- геометрия пересечений -----------------------------------------------------


def _polyline_distance(line: LineString, point: Point) -> float:
    return float(line.project(point))


def _angle_between_deg(direction: Point2, along: Point2) -> float:
    """Угол между направлением трассы и направлением границы/линии, 0–90°."""
    dot = abs(direction[0] * along[0] + direction[1] * along[1])
    n1 = math.hypot(*direction) or 1.0
    n2 = math.hypot(*along) or 1.0
    cos = max(-1.0, min(1.0, dot / (n1 * n2)))
    return math.degrees(math.acos(cos))


def _direction_at(line: LineString, d: float) -> Point2:
    a = line.interpolate(max(0.0, d - 0.5))
    b = line.interpolate(min(line.length, d + 0.5))
    return (b.x - a.x, b.y - a.y)


def _boundary_direction(boundary: BaseGeometry, point: Point) -> Point2:
    """Направление границы объекта (кольца или линии) в ближайшей к точке позиции."""
    parts = boundary.geoms if hasattr(boundary, "geoms") else [boundary]
    best = min(parts, key=lambda g: g.distance(point))
    d = best.project(point)
    a = best.interpolate(max(0.0, d - 0.5))
    b = best.interpolate(min(best.length, d + 0.5))
    return (b.x - a.x, b.y - a.y)


def _object_boundary(zone: SpecialZone) -> BaseGeometry:
    g = zone.geom
    if g.geom_type in ("Polygon", "MultiPolygon"):
        return g.boundary
    return g


def find_crossings(line: LineString, zone: SpecialZone, rules: Rules) -> list[Crossing]:
    """Все пересечения ломаной с объектом: интервалы вдоль ломаной."""
    geom = zone.geom
    if not geom.intersects(line):
        return []
    boundary = _object_boundary(zone)
    result: list[Crossing] = []
    margin = zone.margin_m

    if geom.geom_type in ("Polygon", "MultiPolygon"):
        inside = line.intersection(geom)
        pieces = inside.geoms if hasattr(inside, "geoms") else [inside]
        for piece in pieces:
            if piece.is_empty or piece.geom_type != "LineString" or piece.length < 0.05:
                continue
            d0 = _polyline_distance(line, Point(piece.coords[0]))
            d1 = _polyline_distance(line, Point(piece.coords[-1]))
            d0, d1 = min(d0, d1), max(d0, d1)
            entry = _angle_between_deg(_direction_at(line, d0), _boundary_direction(boundary, line.interpolate(d0)))
            exit_ = _angle_between_deg(_direction_at(line, d1), _boundary_direction(boundary, line.interpolate(d1)))
            result.append(Crossing(zone, d0 - margin, d1 + margin, entry, exit_))
    else:
        inter = line.intersection(geom)
        points = inter.geoms if hasattr(inter, "geoms") else [inter]
        for p in points:
            if p.is_empty:
                continue
            if p.geom_type != "Point":
                # наложение вдоль линии — не пересечение, а следование; ловится отдельно
                continue
            d = _polyline_distance(line, p)
            angle = _angle_between_deg(_direction_at(line, d), _boundary_direction(boundary, p))
            result.append(Crossing(zone, d - margin, d + margin, angle, angle))
    return result


def _is_straight(line: LineString, d0: float, d1: float) -> bool:
    piece = substring(line, max(0.0, d0), min(line.length, d1))
    coords = list(piece.coords)
    if len(coords) <= 2:
        return True
    total = sum(turn_angle_deg(a, b, c) for a, b, c in zip(coords, coords[1:], coords[2:]))
    return total <= STRAIGHT_TOLERANCE_DEG


def _required_angle(zone: SpecialZone, rules: Rules) -> float:
    if zone.min_angle_deg is not None:
        return max(rules.min_crossing_angle_deg, zone.min_angle_deg)
    if zone.geom.geom_type.endswith("LineString"):
        return LINE_CROSSING_MIN_ANGLE_DEG
    return 0.0


# --- перестройка пересечения ---------------------------------------------------


def rebuild_crossing(
    points: list[Point2], crossing: Crossing, obstacles: Obstacles, rules: Rules,
) -> list[Point2] | None:
    """Заменить кривое/косое пересечение прямой хордой под допустимым углом.

    Ищется прямая через середину пересечения, повёрнутая относительно текущего
    направления на один из углов; хорда обрезается по объекту и удлиняется на
    поля; предыдущая и следующая вершины ломаной подключаются к её концам.
    """
    line = LineString(points)
    zone = crossing.zone
    margin = zone.margin_m
    mid_d = (crossing.d_start + crossing.d_end) / 2.0
    mid = line.interpolate(min(max(mid_d, 0.0), line.length))
    base_dir = _direction_at(line, mid_d)
    bn = math.hypot(*base_dir) or 1.0
    base_dir = (base_dir[0] / bn, base_dir[1] / bn)

    # Вершины ломаной до и после интервала
    cum = [0.0]
    for a, b in zip(points, points[1:]):
        cum.append(cum[-1] + math.dist(a, b))
    before = [i for i, d in enumerate(cum) if d < crossing.d_start - 1e-6]
    after = [i for i, d in enumerate(cum) if d > crossing.d_end + 1e-6]
    if not before or not after:
        return None
    i_prev, i_next = before[-1], after[0]
    prev_pt, next_pt = points[i_prev], points[i_next]

    boundary = _object_boundary(zone)
    required = _required_angle(zone, rules)
    limit = rules.max_turn_deg
    is_polygon = zone.geom.geom_type in ("Polygon", "MultiPolygon")

    for angle in CHORD_ANGLES_DEG:
        rad = math.radians(angle)
        dx = base_dir[0] * math.cos(rad) - base_dir[1] * math.sin(rad)
        dy = base_dir[0] * math.sin(rad) + base_dir[1] * math.cos(rad)
        span = 200.0
        probe = LineString([(mid.x - dx * span, mid.y - dy * span), (mid.x + dx * span, mid.y + dy * span)])
        if is_polygon:
            inter = probe.intersection(zone.geom)
            pieces = [g for g in (inter.geoms if hasattr(inter, "geoms") else [inter])
                      if not g.is_empty and g.geom_type == "LineString"]
            if not pieces:
                continue
            chord = min(pieces, key=lambda g: g.distance(mid))
            c0, c1 = chord.coords[0], chord.coords[-1]
        else:
            inter = probe.intersection(zone.geom)
            pts = [g for g in (inter.geoms if hasattr(inter, "geoms") else [inter]) if g.geom_type == "Point"]
            if not pts:
                continue
            p = min(pts, key=lambda g: g.distance(mid))
            c0 = c1 = (p.x, p.y)
        # ориентируем хорду по направлению движения
        if (c1[0] - c0[0]) * dx + (c1[1] - c0[1]) * dy < 0:
            c0, c1 = c1, c0
        a = (c0[0] - dx * margin, c0[1] - dy * margin)
        b = (c1[0] + dx * margin, c1[1] + dy * margin)

        entry = _angle_between_deg((dx, dy), _boundary_direction(boundary, Point(c0)))
        exit_ = _angle_between_deg((dx, dy), _boundary_direction(boundary, Point(c1)))
        if min(entry, exit_) < required - 1e-6:
            continue
        if math.dist(prev_pt, a) < 0.5 or math.dist(b, next_pt) < 0.5:
            continue
        if turn_angle_deg(prev_pt, a, b) > limit or turn_angle_deg(a, b, next_pt) > limit:
            continue
        if i_prev > 0 and turn_angle_deg(points[i_prev - 1], prev_pt, a) > limit:
            continue
        if i_next < len(points) - 1 and turn_angle_deg(b, next_pt, points[i_next + 1]) > limit:
            continue
        if not (obstacles.segment_ok(prev_pt, a) and obstacles.segment_ok(a, b) and obstacles.segment_ok(b, next_pt)):
            continue
        return points[: i_prev + 1] + [a, b] + points[i_next:]
    return None


# --- применение к сети ---------------------------------------------------------


def _tie_in_exempt(segment: Segment, net: Network, zone: SpecialZone, crossing: Crossing, line_length: float) -> bool:
    """§9.9: подход к узлу присоединения через «свою» существующую линию."""
    if zone.type != "heat_network":
        return False
    for node_id, at_end in ((segment.start, crossing.d_start <= 0.5), (segment.end, crossing.d_end >= line_length - 0.5)):
        node = net.nodes[node_id]
        if not at_end:
            continue
        if node.kind == NODE_CHAMBER_NEW and node.on_edge is not None and node.on_edge.id == zone.restriction_id:
            return True
        if node.kind == NODE_CHAMBER_EXISTING and node.existing is not None:
            if node.existing.geom.distance(zone.geom) <= 0.5:
                return True
    return False


def apply(net: Network, field: CaseField) -> SpecialReport:
    """Найти, выправить и оформить спецпроходы на всех участках сети."""
    report = SpecialReport()
    for segment in list(net.segments.values()):
        others = [s.line for s in net.segments.values() if s.id != segment.id]
        obstacles = Obstacles(field.blocked_geom, [field.network_geom, *others])
        _process_segment(net, field, segment, obstacles, report)
    return report


def _process_segment(net: Network, field: CaseField, segment: Segment, obstacles: Obstacles, report: SpecialReport) -> None:
    rules = field.rules
    points = list(segment.points)

    # 1. выправить кривые/косые пересечения (несколько проходов: перестройка сдвигает соседние)
    for _ in range(4):
        line = LineString(points)
        changed = False
        for zone in field.specials:
            for crossing in find_crossings(line, zone, rules):
                if _tie_in_exempt(segment, net, zone, crossing, line.length):
                    continue
                required = _required_angle(zone, rules)
                straight = _is_straight(line, crossing.d_start, crossing.d_end)
                angle_ok = min(crossing.entry_angle_deg, crossing.exit_angle_deg) >= required - 1e-6
                if straight and angle_ok:
                    continue
                rebuilt = rebuild_crossing(points, crossing, obstacles, rules)
                if rebuilt is None:
                    report.issues.append(SpecialIssue(
                        segment.id, zone.restriction_id, zone.type,
                        f"пересечение {'кривое' if not straight else ''}"
                        f"{' и ' if not straight and not angle_ok else ''}"
                        f"{'под углом ' + format(min(crossing.entry_angle_deg, crossing.exit_angle_deg), '.0f') + '°' if not angle_ok else ''}"
                        " — перестроить не удалось",
                        (line.interpolate(max(0.0, min(line.length, (crossing.d_start + crossing.d_end) / 2))).x,
                         line.interpolate(max(0.0, min(line.length, (crossing.d_start + crossing.d_end) / 2))).y),
                    ))
                    continue
                points = rebuilt
                report.rebuilt.append(f"{segment.id}: {zone.type} {zone.restriction_id}")
                changed = True
                break
            if changed:
                break
        if not changed:
            break

    if points != segment.points:
        segment.points = points

    # 2. интервалы спецпроходов на итоговой ломаной
    line = LineString(points)
    intervals: list[Crossing] = []
    for zone in field.specials:
        for crossing in find_crossings(line, zone, rules):
            if _tie_in_exempt(segment, net, zone, crossing, line.length):
                continue
            intervals.append(crossing)
            report.crossings.append((segment.id, crossing))
    if not intervals:
        return

    # 3. точки деления: границы всех интервалов (обрезанные по участку)
    cuts = sorted({0.0, line.length} | {
        min(max(d, 0.0), line.length) for c in intervals for d in (c.d_start, c.d_end)
    })
    fragments: list[tuple[float, float]] = [(a, b) for a, b in zip(cuts, cuts[1:]) if b - a > 0.02]

    # 4. разрезать участок в точках деления техузлами и назначить параметры
    current = segment
    for a, b in fragments:
        covering = [c for c in intervals if c.d_start - 1e-6 <= a and b <= c.d_end + 1e-6]
        if b < line.length - 0.02:
            cut_point = line.interpolate(b)
            start_id = current.start
            new_node = net.split_segment(current, (cut_point.x, cut_point.y), NODE_TECH)
            halves = net.incident(new_node.id)
            first = next(s for s in halves if start_id in (s.start, s.end))
            rest = next(s for s in halves if s is not first)
            _assign(first, covering)
            current = rest
        else:
            _assign(current, covering)


def _assign(segment: Segment, covering: list[Crossing]) -> None:
    if not covering:
        segment.laying = "base"
        segment.k_special = 1.0
        segment.special_types = []
        segment.special_ids = []
        return
    segment.laying = "special"
    segment.k_special = max(c.zone.k_special for c in covering)
    segment.special_types = sorted({c.zone.type for c in covering})
    segment.special_ids = [c.zone.restriction_id for c in covering]


def following_violations(net: Network, field: CaseField) -> list[SpecialIssue]:
    """§9.9: обычный участок в полосе отступа спецпроходного объекта — нарушение.

    Возвращает места, которые солвер должен запретить и перестроить трассу.
    """
    issues: list[SpecialIssue] = []
    for segment in net.segments.values():
        if segment.laying == "special":
            continue
        line = segment.line
        for zone in field.specials:
            if not zone.band.intersects(line):
                continue
            inside = line.intersection(zone.band)
            pieces = inside.geoms if hasattr(inside, "geoms") else [inside]
            for piece in pieces:
                if piece.is_empty or piece.geom_type != "LineString":
                    continue
                # концевая часть подхода к присоединению — исключение
                d0 = line.project(Point(piece.coords[0]))
                d1 = line.project(Point(piece.coords[-1]))
                touches_end = min(d0, d1) <= 0.5 or max(d0, d1) >= line.length - 0.5
                if touches_end and _end_is_tie_in(segment, net, zone):
                    continue
                if piece.length < 1.0:
                    continue
                mid = piece.interpolate(0.5, normalized=True)
                issues.append(SpecialIssue(
                    segment.id, zone.restriction_id, zone.type,
                    f"следование вдоль в полосе отступа на {piece.length:.1f} м", (mid.x, mid.y),
                ))
    return issues


def _end_is_tie_in(segment: Segment, net: Network, zone: SpecialZone) -> bool:
    if zone.type != "heat_network":
        return False
    for node_id in (segment.start, segment.end):
        node = net.nodes[node_id]
        if node.kind == NODE_CHAMBER_NEW and node.on_edge is not None and node.on_edge.id == zone.restriction_id:
            return True
        if node.kind == NODE_CHAMBER_EXISTING and node.existing is not None and node.existing.geom.distance(zone.geom) <= 0.5:
            return True
    return False

