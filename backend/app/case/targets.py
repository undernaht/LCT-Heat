"""Цель маршрута для точки подключения (§2.3 приложения, §9.1–9.2 спецификации).

Точка подключения лежит внутри своего полигона ОКС, а полигон с отступом —
запрет. Приложение разрешает один финальный прямой участок от ближайшей к
точке границы полигона до самой точки, и на этот участок отступ к собственному
полигону не действует. Значит маршрут снаружи должен закончиться там, где
продолжение отрезка «точка → ближайшая граница» выходит из зоны отступа, —
это и есть цель поиска.

Кандидатов несколько, по возрастанию расстояния до границы: ближайшая граница
может упираться в соседа, смотреть в замкнутый двор, снова входить в здание
(вогнутый контур) или требовать разворота круче 90° — тогда берётся следующая
(§9.1, запасной путь; помечается `approach_rule = fallback`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import nearest_points, unary_union
from shapely.strtree import STRtree

from .field import CaseField
from .model import CaseInput, OksPoint, polygon_parts

Point2 = tuple[float, float]

# Минимальный вынос цели за границу зоны отступа и предел поиска свободной
# ячейки вдоль луча: в узком коридоре (2–3 м между зонами) фиксированные
# 1,5 м попадали бы в чужую зону, и ближайшая граница отвергалась зря.
EXIT_EXTRA_M = 0.75
EXIT_SEARCH_M = 6.0
# Шаг перебора точек контура
BOUNDARY_SAMPLE_STEP_M = 4.0
MAX_CANDIDATES = 8
# §9.2: точка «на стене» — не дальше этого снаружи полигона всё ещё его
ON_BOUNDARY_TOLERANCE_M = 0.5
# Дальше этого вдоль луча зона отступа не может продолжаться (отступ ≤ 9 м + габарит)
MAX_RAY_M = 40.0


@dataclass
class ApproachTarget:
    oks: OksPoint
    target: Point                  # цель поиска — снаружи зоны отступа
    boundary: Point | None         # точка на контуре полигона (B); None — точка не в полигоне
    direction: Point2 | None       # единичный вектор наружу вдоль финального участка
    final_leg: LineString | None   # прямой участок target → точка подключения
    own_ids: frozenset[Any]        # собственные объекты ОКС (все содержащие точку)
    is_nearest: bool = True
    note: str = ""

    @property
    def approach_rule(self) -> str:
        return "nearest" if self.is_nearest else "fallback"


def _own_restrictions(oks: OksPoint, case: CaseInput) -> list:
    """Все объекты ОКС, которым принадлежит точка (§9.2: по внешнему кольцу)."""
    polygons = [r for r in case.restrictions if r.type == "oks"]
    inside = []
    near = []
    for r in polygons:
        for part in polygon_parts(r.geom):
            shell = Polygon(part.exterior)
            if shell.contains(oks.geom) or shell.exterior.distance(oks.geom) < 1e-6:
                inside.append(r)
                break
            if shell.exterior.distance(oks.geom) <= ON_BOUNDARY_TOLERANCE_M:
                near.append(r)
    if inside:
        return inside
    return near[:1]


def _outward_direction(shell: Polygon, point: Point, boundary: Point) -> Point2:
    dx, dy = boundary.x - point.x, boundary.y - point.y
    norm = math.hypot(dx, dy)
    if norm > 1e-3:
        return dx / norm, dy / norm
    # Точка на границе: направление — наружу от полигона по нормали к ребру
    ring = shell.exterior
    d = ring.project(boundary)
    ahead = ring.interpolate(min(d + 0.5, ring.length))
    behind = ring.interpolate(max(d - 0.5, 0.0))
    tx, ty = ahead.x - behind.x, ahead.y - behind.y
    tn = math.hypot(tx, ty) or 1.0
    nx, ny = ty / tn, -tx / tn
    probe = Point(boundary.x + nx * 0.5, boundary.y + ny * 0.5)
    if shell.contains(probe):
        nx, ny = -nx, -ny
    return nx, ny


def _exit_point(boundary: Point, direction: Point2, own_zone: BaseGeometry, own_body: BaseGeometry,
                field: CaseField | None = None, foreign: "_Foreign | None" = None) -> Point | None:
    """Первая точка луча из B наружу вне зоны отступа, до которой луч не входит в тело здания.

    §9.1 (a): у вогнутого контура луч может снова войти в здание раньше, чем
    выйдет из зоны, — тогда эта граница не годится. За зоной берётся первая
    свободная и достижимая ячейка сетки (не дальше EXIT_SEARCH_M): так узкий
    коридор между зонами двух зданий не отвергает ближайшую границу.
    """
    ux, uy = direction
    distance = 0.5
    while distance < MAX_RAY_M:
        candidate = Point(boundary.x + ux * distance, boundary.y + uy * distance)
        probe = LineString([(boundary.x + ux * 0.05, boundary.y + uy * 0.05), candidate])
        if own_body.intersects(probe):
            return None
        if not own_zone.intersects(candidate):
            break
        distance += 0.5
    else:
        return None

    exit_d = distance + EXIT_EXTRA_M
    extra = 0.0
    while extra <= EXIT_SEARCH_M:
        d = exit_d + extra
        target = Point(boundary.x + ux * d, boundary.y + uy * d)
        tail = LineString([(boundary.x + ux * 0.05, boundary.y + uy * 0.05), target])
        if own_body.intersects(tail):
            return None
        if field is None:
            return target
        row, col = field.grid.rowcol(target.x, target.y)
        cell_ok = field.grid.contains(target.x, target.y) and not field.blocked[row, col] and field.reachable[row, col]
        leg_ok = foreign is None or not foreign.intersects(LineString([target, (boundary.x, boundary.y)]))
        if cell_ok and leg_ok:
            return target
        extra += 0.5
    return None


class _Foreign:
    """Запреты чужих объектов: проверка отрезка без объединения всего района."""

    def __init__(self, field: CaseField, own_ids: frozenset[Any]):
        items = [(rid, geom) for rid, geom in field.blocked_by_id.items() if rid not in own_ids]
        self.ids = [rid for rid, _ in items]
        self.tree = STRtree([geom for _, geom in items]) if items else None
        self.geoms = [geom for _, geom in items]
        self.extra = field.extra_blocked

    def intersects(self, geom: BaseGeometry) -> bool:
        if self.tree is not None:
            for index in self.tree.query(geom, predicate="intersects"):
                if self.geoms[int(index)].intersects(geom):
                    return True
        return any(g.intersects(geom) for g in self.extra)


def _usable(target: Point, leg: LineString, field: CaseField, foreign: _Foreign) -> bool:
    """Финальный участок не задевает чужие запреты, цель проходима и не в кармане."""
    if not field.grid.contains(target.x, target.y):
        return False
    row, col = field.grid.rowcol(target.x, target.y)
    if field.blocked[row, col] or not field.reachable[row, col]:
        return False
    return not foreign.intersects(leg)


def approach_candidates(oks: OksPoint, case: CaseInput, field: CaseField) -> list[ApproachTarget]:
    own = _own_restrictions(oks, case)
    if not own:
        return [ApproachTarget(oks=oks, target=oks.geom, boundary=None, direction=None,
                               final_leg=None, own_ids=frozenset(), note="точка вне полигонов ОКС")]
    own_ids = frozenset(r.id for r in own)
    own_body = unary_union([r.geom for r in own])
    own_zone = unary_union([field.blocked_by_id[r.id] for r in own if r.id in field.blocked_by_id])
    foreign = _Foreign(field, own_ids)

    # Контур для выхода — внешние кольца всех частей собственных объектов (§9.2)
    shells = [Polygon(part.exterior) for r in own for part in polygon_parts(r.geom)]
    boundary_union = unary_union([s.exterior for s in shells])
    _, nearest = nearest_points(oks.geom, boundary_union)

    points: list[Point] = [nearest]
    for shell in shells:
        ring = shell.exterior
        samples = int(max(8, ring.length // BOUNDARY_SAMPLE_STEP_M))
        points += [ring.interpolate(i / samples, normalized=True) for i in range(samples)]
        points += [Point(c) for c in ring.coords[:-1]]
    points.sort(key=lambda p: p.distance(oks.geom))

    result: list[ApproachTarget] = []
    seen_dirs: list[Point2] = []
    for index, boundary in enumerate(points):
        shell = min(shells, key=lambda s: s.exterior.distance(boundary))
        direction = _outward_direction(shell, oks.geom, boundary)
        if any(abs(direction[0] - d[0]) + abs(direction[1] - d[1]) < 0.12 for d in seen_dirs):
            continue
        target = _exit_point(boundary, direction, own_zone, own_body, field, foreign)
        if target is None:
            continue
        leg = LineString([target, oks.geom])
        if not _usable(target, leg, field, foreign):
            continue
        seen_dirs.append(direction)
        result.append(ApproachTarget(
            oks=oks, target=target, boundary=boundary, direction=direction, final_leg=leg,
            own_ids=own_ids, is_nearest=(index == 0),
            note="" if index == 0 else "ближайшая граница недостижима — взята следующая",
        ))
        if len(result) >= MAX_CANDIDATES:
            break

    if not field.rules.approach_fallback_lenient:
        result = [t for t in result if t.is_nearest]

    if not result:
        direction = _outward_direction(shells[0], oks.geom, nearest)
        target = Point(nearest.x + direction[0] * EXIT_EXTRA_M, nearest.y + direction[1] * EXIT_EXTRA_M)
        result.append(ApproachTarget(
            oks=oks, target=target, boundary=nearest, direction=direction,
            final_leg=LineString([target, oks.geom]), own_ids=own_ids,
            note="ни одна точка контура не достижима",
        ))
    return result


def approach_targets(case: CaseInput, field: CaseField) -> list[list[ApproachTarget]]:
    """Список кандидатов по каждой точке подключения, в порядке точек входа."""
    return [approach_candidates(o, case, field) for o in case.oks]
