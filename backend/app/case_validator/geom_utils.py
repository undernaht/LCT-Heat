"""Мелкая планиметрия: углы поворота, направления, разбор составных геометрий.

Углы считаются через atan2(|cross|, dot), а не через acos: у почти прямых и
почти обратных направлений acos теряет точность, а допуски здесь — сотые
градуса.
"""

from __future__ import annotations

import math
from typing import Iterable

from shapely.geometry import LineString, LinearRing, Point, Polygon
from shapely.geometry.base import BaseGeometry

XY = tuple[float, float]


def turn_angle(p0: XY, p1: XY, p2: XY) -> float:
    """Угол поворота в вершине p1, градусы: 0 — прямо, 180 — разворот."""
    ax, ay = p1[0] - p0[0], p1[1] - p0[1]
    bx, by = p2[0] - p1[0], p2[1] - p1[1]
    return vectors_angle((ax, ay), (bx, by))


def vectors_angle(a: XY, b: XY) -> float:
    """Угол между направлениями (0..180)."""
    cross = a[0] * b[1] - a[1] * b[0]
    dot = a[0] * b[0] + a[1] * b[1]
    return math.degrees(math.atan2(abs(cross), dot))


def turn_sign(p0: XY, p1: XY, p2: XY) -> int:
    ax, ay = p1[0] - p0[0], p1[1] - p0[1]
    bx, by = p2[0] - p1[0], p2[1] - p1[1]
    cross = ax * by - ay * bx
    return (cross > 0) - (cross < 0)


def lines_angle(a: XY, b: XY) -> float:
    """Острый угол между прямыми (0..90) — для угла пересечения."""
    cross = abs(a[0] * b[1] - a[1] * b[0])
    dot = abs(a[0] * b[0] + a[1] * b[1])
    return math.degrees(math.atan2(cross, dot))


def dist(a: XY, b: XY) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def line_parts(geom: BaseGeometry) -> list[LineString]:
    """Все линейные части составной геометрии (точки отбрасываются)."""
    if geom.is_empty:
        return []
    if isinstance(geom, (LineString, LinearRing)):
        return [LineString(geom.coords)] if len(geom.coords) >= 2 else []
    parts: list[LineString] = []
    for g in getattr(geom, "geoms", []):
        parts.extend(line_parts(g))
    return parts


def point_parts(geom: BaseGeometry) -> list[Point]:
    if geom.is_empty:
        return []
    if isinstance(geom, Point):
        return [geom]
    parts: list[Point] = []
    for g in getattr(geom, "geoms", []):
        parts.extend(point_parts(g))
    return parts


def polygon_parts(geom: BaseGeometry) -> list[Polygon]:
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    parts: list[Polygon] = []
    for g in getattr(geom, "geoms", []):
        parts.extend(polygon_parts(g))
    return parts


def exterior_polygons(geom: BaseGeometry) -> list[Polygon]:
    """Части полигона по внешнему кольцу — без дырок (§9.2 спецификации)."""
    return [Polygon(p.exterior) for p in polygon_parts(geom)]


def boundary_lines(geom: BaseGeometry) -> list[LineString]:
    """Границы: кольца полигонов или сами линии."""
    polys = polygon_parts(geom)
    if polys:
        rings: list[LineString] = []
        for p in polys:
            rings.append(LineString(p.exterior.coords))
            rings.extend(LineString(r.coords) for r in p.interiors)
        return rings
    return line_parts(geom)


def local_direction(lines: Iterable[LineString], at: Point) -> XY | None:
    """Направление ближайшего к точке ребра среди линий."""
    best: tuple[float, XY] | None = None
    for line in lines:
        coords = list(line.coords)
        for a, b in zip(coords, coords[1:]):
            if a == b:
                continue
            d = LineString([a, b]).distance(at)
            if best is None or d < best[0]:
                best = (d, (b[0] - a[0], b[1] - a[1]))
    return best[1] if best else None


def direction_at(line: LineString, chainage: float, ahead: float = 0.05) -> XY:
    """Направление полилинии в точке с данным пикетом."""
    length = line.length
    c0 = max(0.0, min(length, chainage - ahead))
    c1 = max(0.0, min(length, chainage + ahead))
    if c1 - c0 < 1e-9:
        c0, c1 = (0.0, min(length, 2 * ahead)) if chainage < ahead else (max(0.0, length - 2 * ahead), length)
    p0 = line.interpolate(c0)
    p1 = line.interpolate(c1)
    return (p1.x - p0.x, p1.y - p0.y)


def cumulative(coords: list[XY]) -> list[float]:
    acc = [0.0]
    for a, b in zip(coords, coords[1:]):
        acc.append(acc[-1] + dist(a, b))
    return acc
