"""Превращение растрового пути в проектную трассу.

Путь по сетке — «лесенка» с угловой ошибкой до 22,5° (8-связность). Спрямление
убирает и её, и дискретизационный шум, поэтому городить 16-связность не нужно.
"""

from __future__ import annotations

import math

from ..costfield.build import CostField

Point2 = tuple[float, float]


def segment_cost(field: CostField, start: Point2, end: Point2) -> float:
    """Интеграл стоимости вдоль отрезка, ₽. inf — отрезок задевает запрет."""
    length = math.dist(start, end)
    if length == 0.0:
        return 0.0

    steps = max(2, int(math.ceil(length / (field.grid.resolution * 0.5))) + 1)
    step_length = length / (steps - 1)

    total = 0.0
    previous: float | None = None
    for i in range(steps):
        t = i / (steps - 1)
        x = start[0] + (end[0] - start[0]) * t
        y = start[1] + (end[1] - start[1]) * t
        value = field.value_at(x, y)
        if not math.isfinite(value):
            return math.inf
        if previous is not None:
            total += (previous + value) / 2.0 * step_length
        previous = value

    return total


def path_cost(field: CostField, points: list[Point2]) -> float:
    return sum(segment_cost(field, a, b) for a, b in zip(points, points[1:]))


def collapse_collinear(points: list[Point2], tolerance: float = 1e-6) -> list[Point2]:
    """Убрать промежуточные вершины на прямых участках."""
    if len(points) < 3:
        return list(points)

    result = [points[0]]
    for previous, current, following in zip(points, points[1:], points[2:]):
        cross = (current[0] - previous[0]) * (following[1] - previous[1]) - (
            current[1] - previous[1]
        ) * (following[0] - previous[0])
        if abs(cross) > tolerance:
            result.append(current)
    result.append(points[-1])
    return result


def dominant_bearing(lines: list, near: Point2, radius_m: float = 200.0) -> float:
    """Господствующее направление застройки, радианы в диапазоне [0, π/4).

    Проектные трассы идут параллельно улицам и фасадам, а не под произвольным
    углом. Направление берётся из азимутов ближайших улиц, взвешенных по длине,
    и сводится по модулю 45°: сетка кварталов симметрична, «север-юг» и
    «запад-восток» для нас одно и то же семейство направлений.
    """
    from shapely.geometry import Point as ShapelyPoint

    probe = ShapelyPoint(near)
    sin_sum = 0.0
    cos_sum = 0.0

    for line in lines:
        if line.distance(probe) > radius_m:
            continue
        coords = list(line.coords)
        for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
            length = math.hypot(x2 - x1, y2 - y1)
            if length < 1.0:
                continue
            angle = math.atan2(y2 - y1, x2 - x1) % (math.pi / 4)
            # усредняем углы через единичный вектор учетверённого угла,
            # иначе 1° и 44° дадут бессмысленное среднее 22°
            sin_sum += length * math.sin(8 * angle)
            cos_sum += length * math.cos(8 * angle)

    if sin_sum == 0.0 and cos_sum == 0.0:
        return 0.0
    return (math.atan2(sin_sum, cos_sum) / 8.0) % (math.pi / 4)


def _snap_bearing(bearing: float, base: float, tolerance: float) -> float | None:
    """Ближайшее направление сетки base + k·45°, если отклонение в пределах допуска."""
    step = math.pi / 4
    offset = bearing - base
    k = round(offset / step)
    target = base + k * step
    deviation = abs((bearing - target + math.pi) % (2 * math.pi) - math.pi)
    return target if deviation <= tolerance else None


def _line_through(point: Point2, bearing: float) -> tuple[float, float, float]:
    """Прямая ax + by = c через точку с заданным азимутом."""
    a = -math.sin(bearing)
    b = math.cos(bearing)
    return a, b, a * point[0] + b * point[1]


def _intersect(
    first: tuple[float, float, float], second: tuple[float, float, float]
) -> Point2 | None:
    a1, b1, c1 = first
    a2, b2, c2 = second
    det = a1 * b2 - a2 * b1
    if abs(det) < 1e-6:          # почти параллельны — пересечение не определено
        return None
    return ((c1 * b2 - c2 * b1) / det, (a1 * c2 - a2 * c1) / det)


ORTHO_PASSES = 3
"""Сколько раз прогнать выравнивание: за один проход оно не сходится."""

ORTHO_TOLERANCES_DEG = (22.0, 14.0, 8.0)
"""Допуски привязки, от смелого к осторожному.

Пробуются по очереди: если разворот на 22° увёл трассу в запретную зону или
заметно её удорожил, пробуется более щадящий. Так диагональная трасса получает
хотя бы частичное выравнивание вместо полного отказа.
"""


def orthogonalize(
    field: CostField,
    points: list[Point2],
    roads: list,
    *,
    epsilon: float = 0.06,
) -> list[Point2]:
    """Привязать направления участков трассы к сетке кварталов.

    Проектные трассы идут параллельно улицам и фасадам, а не под произвольным
    углом: так их проще строить, согласовывать и обслуживать. Каждый участок
    разворачивается к ближайшему направлению `base + k·45°`, вершины
    пересчитываются как пересечения соседних прямых, концы закреплены.

    Допуск подбирается по убыванию: результат принимается целиком, потому что
    наполовину развёрнутая трасса хуже и исходной, и выровненной.
    """
    if len(points) < 3:
        return points

    base = dominant_bearing(roads, points[len(points) // 2])
    budget = path_cost(field, points) * (1.0 + epsilon)
    current = points

    # Один проход не сходится: откат проблемной вершины меняет соседние прямые,
    # и следующий проход выравнивает то, что в предыдущем пришлось оставить.
    for _ in range(ORTHO_PASSES):
        improved = current
        for tolerance_deg in ORTHO_TOLERANCES_DEG:
            candidate = _orthogonalize_once(
                current, base, math.radians(tolerance_deg), field
            )
            if len(candidate) < 2:
                continue
            after = path_cost(field, candidate)
            if math.isfinite(after) and after <= budget:
                improved = candidate
                break
        if improved == current:
            break
        current = improved

    return current


MAX_VERTEX_SHIFT_M = 25.0
"""Дальше этого вершину не двигаем: выравнивание не должно перекраивать трассу."""


def _repair(
    field: CostField, candidate: list[Point2], original: list[Point2], passes: int = 3
) -> list[Point2]:
    """Откатить те вершины, из-за которых участок попал в запретную зону.

    Без этого одна неудачная вершина отбрасывала выравнивание всей трассы: на
    замерах именно так терялись две трассы из трёх — вершина уезжала в буфер
    соседнего дома, стоимость становилась бесконечной, и результат отклонялся
    целиком.
    """
    for _ in range(passes):
        broken = False
        for index in range(1, len(candidate) - 1):
            if candidate[index] == original[index]:
                continue
            left = segment_cost(field, candidate[index - 1], candidate[index])
            right = segment_cost(field, candidate[index], candidate[index + 1])
            if not (math.isfinite(left) and math.isfinite(right)):
                candidate[index] = original[index]
                broken = True
        if not broken:
            break
    return candidate


def _orthogonalize_once(
    points: list[Point2], base: float, tolerance: float, field: CostField
) -> list[Point2]:
    lines: list[tuple[float, float, float]] = []
    for index, (start, end) in enumerate(zip(points, points[1:])):
        bearing = math.atan2(end[1] - start[1], end[0] - start[0])
        target = _snap_bearing(bearing, base, tolerance)
        if target is None:
            target = bearing                       # длинную диагональ не ломаем

        if index == 0:
            anchor = start                          # точка ввода закреплена
        elif index == len(points) - 2:
            anchor = end                            # точка врезки закреплена
        else:
            anchor = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
        lines.append(_line_through(anchor, target))

    result: list[Point2] = [points[0]]
    for index in range(len(lines) - 1):
        crossing = _intersect(lines[index], lines[index + 1])
        # если прямые почти параллельны или вершина уехала слишком далеко,
        # оставляем исходную — лучше не тронуть, чем испортить
        original = points[index + 1]
        if crossing is None or math.dist(crossing, original) > MAX_VERTEX_SHIFT_M:
            result.append(original)
        else:
            result.append(crossing)
    result.append(points[-1])

    result = _repair(field, result, list(points))
    return collapse_collinear(result)


def straighten(
    field: CostField,
    points: list[Point2],
    *,
    epsilon: float = 0.02,
    max_passes: int = 12,
) -> list[Point2]:
    """Жадное удаление вершин, пока прямой отрезок остаётся допустимым.

    Вершина убирается, если прямая между соседями не задевает запретных зон и
    не дороже исходного участка более чем на `epsilon`.
    """
    result = collapse_collinear(points)

    for _ in range(max_passes):
        changed = False
        index = 1
        while index < len(result) - 1:
            before, middle, after = result[index - 1], result[index], result[index + 1]
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
