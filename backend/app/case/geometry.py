"""Векторная геометрия трассы: спрямление, углы, изломы.

Растровый путь — «лесенка» с угловой ошибкой до 22,5°. Приложение требует
прямых участков без необоснованных изломов и поворотов не круче 90°, поэтому
путь спрямляется по вектору: вершина убирается, если прямая между соседями
не задевает запретов, не пересекает другие ветки и не дороже по полю.
"""

from __future__ import annotations

import math

from shapely.geometry import LineString
from shapely.geometry.base import BaseGeometry
from shapely.prepared import PreparedGeometry, prep

from ..routing.straighten import collapse_collinear, segment_cost
from .field import CaseField

Point2 = tuple[float, float]


def turn_angle_deg(a: Point2, b: Point2, c: Point2) -> float:
    """Изменение направления в вершине b, градусы (0 — прямо)."""
    v1 = (b[0] - a[0], b[1] - a[1])
    v2 = (c[0] - b[0], c[1] - b[1])
    n1, n2 = math.hypot(*v1), math.hypot(*v2)
    if n1 < 1e-9 or n2 < 1e-9:
        return 0.0
    cos = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
    return math.degrees(math.acos(cos))


def turn_sign(a: Point2, b: Point2, c: Point2) -> int:
    cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
    return 1 if cross > 1e-9 else -1 if cross < -1e-9 else 0


# §9.7 спецификации: два новых участка вне общего узла не ближе (w₁+w₂)/2 + 1 м;
# берём запас под Ду400 — 2,4 м. У самого узла присоединения сближение неизбежно.
MIN_SEPARATION_M = 2.4
JOIN_APPROACH_M = 4.0


class Obstacles:
    """Что прямой отрезок не должен пересекать, кроме собственных концов, и к чему
    не должен прижиматься ближе MIN_SEPARATION_M вне окрестности присоединения."""

    def __init__(self, blocked: BaseGeometry, lines: list[BaseGeometry], *, separation_m: float = MIN_SEPARATION_M):
        self.blocked: PreparedGeometry = prep(blocked)
        self.lines: list[PreparedGeometry] = [prep(line) for line in lines if not line.is_empty]
        self.separation_m = separation_m

    def segment_ok(self, a: Point2, b: Point2, *, allow_touch_at_end: bool = True) -> bool:
        seg = LineString([a, b])
        if self.blocked.intersects(seg):
            return False
        for line in self.lines:
            if line.intersects(seg):
                if not allow_touch_at_end:
                    return False
                # Допускается только касание концом отрезка (точка присоединения)
                inter = line.context.intersection(seg)
                if inter.is_empty:
                    continue
                if inter.geom_type != "Point":
                    return False
                if inter.distance(seg.boundary) > 1e-6:
                    return False
            # Сближение: часть отрезка дальше JOIN_APPROACH_M от его концов не
            # должна подходить к чужой линии ближе separation_m
            if self.separation_m > 0 and line.context.distance(seg) < self.separation_m:
                core = seg.difference(seg.boundary.buffer(JOIN_APPROACH_M))
                if not core.is_empty and line.context.distance(core) < self.separation_m:
                    return False
        return True


def straighten(
    field: CaseField,
    points: list[Point2],
    obstacles: Obstacles,
    *,
    epsilon: float = 0.02,
    max_passes: int = 16,
    fixed: set[int] | None = None,
) -> list[Point2]:
    """Жадное удаление вершин. Концы и вершины из `fixed` неприкосновенны."""
    result = collapse_collinear(points)
    for _ in range(max_passes):
        changed = False
        index = 1
        while index < len(result) - 1:
            before, middle, after = result[index - 1], result[index], result[index + 1]
            if fixed and _is_fixed(middle, fixed_points=fixed):
                index += 1
                continue
            if not obstacles.segment_ok(before, after):
                index += 1
                continue
            direct = segment_cost(field, before, after)
            if math.isfinite(direct):
                through = segment_cost(field, before, middle) + segment_cost(field, middle, after)
                if direct <= through * (1.0 + epsilon):
                    del result[index]
                    changed = True
                    continue
            index += 1
        if not changed:
            break
    return result


def _is_fixed(point: Point2, fixed_points: set) -> bool:
    return any(math.dist(point, f) < 1e-6 for f in fixed_points)


def soften_sharp_turns(
    points: list[Point2], obstacles: Obstacles, *, max_turn_deg: float = 89.9, chord_m: float = 4.0
) -> list[Point2]:
    """Поворот круче 90° разбить на два, срезав угол на chord_m по обеим сторонам."""
    result = list(points)
    index = 1
    while index < len(result) - 1:
        a, b, c = result[index - 1], result[index], result[index + 1]
        if turn_angle_deg(a, b, c) <= max_turn_deg + 1e-6:
            index += 1
            continue
        la, lc = math.dist(a, b), math.dist(b, c)
        d = min(chord_m, la * 0.45, lc * 0.45)
        if d < 0.5:
            index += 1
            continue
        p = (b[0] + (a[0] - b[0]) * d / la, b[1] + (a[1] - b[1]) * d / la)
        q = (b[0] + (c[0] - b[0]) * d / lc, b[1] + (c[1] - b[1]) * d / lc)
        if obstacles.segment_ok(p, q):
            result[index : index + 1] = [p, q]
            index += 2
        else:
            index += 1
    return result


def geometry_issues(points: list[Point2], *, min_segment_m: float, step_detect_m: float) -> list[str]:
    """Проверка §2.1 приложения и §9.7 спецификации на готовой ломаной."""
    issues: list[str] = []
    for i in range(1, len(points) - 1):
        angle = turn_angle_deg(points[i - 1], points[i], points[i + 1])
        if angle > 90.0 + 1e-6:
            issues.append(f"поворот {angle:.0f}° в вершине {i}")
    for i, (a, b) in enumerate(zip(points, points[1:])):
        if math.dist(a, b) < min_segment_m and 0 < i < len(points) - 2:
            issues.append(f"отрезок {math.dist(a, b):.1f} м между вершинами {i} и {i + 1}")
    for i in range(1, len(points) - 2):
        s1 = turn_sign(points[i - 1], points[i], points[i + 1])
        s2 = turn_sign(points[i], points[i + 1], points[i + 2])
        if s1 and s2 and s1 != s2 and math.dist(points[i], points[i + 1]) < step_detect_m:
            a1 = turn_angle_deg(points[i - 1], points[i], points[i + 1])
            a2 = turn_angle_deg(points[i], points[i + 1], points[i + 2])
            if a1 > 20 and a2 > 20:
                issues.append(f"ступенька {math.dist(points[i], points[i + 1]):.1f} м у вершины {i}")
    return issues


def remove_short_segments(
    points: list[Point2], obstacles: Obstacles, *, min_segment_m: float = 3.0,
    max_turn_deg: float = 89.9, keep_first: bool = True,
) -> list[Point2]:
    """Убрать вершины, дающие отрезки короче min_segment_m, если прямая допустима.

    Спрямление по стоимости может оставить огрызок в 1–2 м у границы дорогой
    зоны; для приложения это «мелкий излом». Вершина убирается без оглядки на
    стоимость, лишь бы отрезок не задевал запретов и углы остались ≤ 90°.
    """
    result = list(points)
    changed = True
    while changed and len(result) > 2:
        changed = False
        for i in range(len(result) - 1):
            if math.dist(result[i], result[i + 1]) >= min_segment_m:
                continue
            # кандидаты на удаление: внутренние концы короткого отрезка
            for victim in (i + 1, i):
                if victim == 0 or victim == len(result) - 1:
                    continue
                if keep_first and victim == 1 and i == 0:
                    pass
                a, c = result[victim - 1], result[victim + 1]
                if not obstacles.segment_ok(a, c):
                    continue
                trial = result[:victim] + result[victim + 1:]
                ok = all(
                    turn_angle_deg(trial[j - 1], trial[j], trial[j + 1]) <= max_turn_deg + 1e-6
                    for j in range(max(1, victim - 1), min(len(trial) - 1, victim + 1))
                )
                if ok:
                    result = trial
                    changed = True
                    break
            if changed:
                break
    return result
