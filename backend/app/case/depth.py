"""Режим с учётом глубины (§5 приложения, §7 спецификации).

Земля условно плоская, обычная глубина до верха габарита 3,0 м. Глубину
приходится менять только у пересечений с подземными линиями: газопровод
(верх на 2,8 м), кабель (2,7 м), существующая теплосеть (3,0 м). Проход над
объектом почти всегда возможен и остаётся не глубже 3,0 м — значит Kгл = 1 и
стоимость не растёт; проход под объектом нужен, лишь когда сверху не хватает
места до минимальных 0,7 м.

Профиль — ломаная: полка постоянной глубины над/под объектом, спуски и
подъёмы с уклоном не круче 0,10, возврат на 3,0 м. Между близкими
препятствиями изменённая глубина сохраняется (нижняя огибающая). Участок
делится техническими узлами в точках излома профиля, и каждому куску
записываются `depth_start`/`depth_end`.

Дороги и трамвай глубины не меняют: под ними верх габарита должен быть не
выше 1,0/1,2 м, а обычные 3,0 м это выполняют.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any

from .field import CaseField, SpecialZone
from .network import NODE_TECH, Network, Segment
from .rules import Rules

# Минимальная половина полки постоянной глубины, если у объекта нет полей
FLAT_EXTRA_M = 0.5


@dataclass
class DepthConstraint:
    d: float                 # положение вдоль участка, м
    top_depth_m: float       # требуемая глубина верха габарита на полке
    flat_half_m: float       # половина длины полки
    deeper: bool             # True — проход под объектом (глубже 3,0)
    zone: SpecialZone


@dataclass
class DepthReport:
    constraints: list[tuple[str, DepthConstraint]] = dc_field(default_factory=list)
    issues: list[str] = dc_field(default_factory=list)
    max_depth_m: float = 0.0
    min_depth_m: float = 0.0
    pieces_split: int = 0

    @property
    def ok(self) -> bool:
        return not self.issues


# --- требования у пересечений --------------------------------------------------


def _object_depth(zone: SpecialZone, rules: Rules) -> tuple[float, float] | None:
    """(глубина верха, высота габарита) объекта или None, если глубина не задана."""
    rule = rules.restriction(zone.type)
    if rule.depth is None or rule.depth.get("kind") != "above_or_below":
        return None
    if rule.gauge_by_diameter:
        height = rules.diameter(zone.object_du).height_m if zone.object_du and rules.has_diameter(zone.object_du) else 0.5
    else:
        height = rule.gauge_height_m
    top = rule.gauge_top_depth_m if rule.gauge_top_depth_m is not None else float(rules.depth["normal_top_depth_m"])
    return top, height


def constraint_for(zone: SpecialZone, d: float, du: int, rules: Rules) -> DepthConstraint | None:
    spec = _object_depth(zone, rules)
    if spec is None:
        return None
    obj_top, obj_height = spec
    clearance = float(rules.restriction(zone.type).depth["clearance_m"])
    own_height = rules.diameter(du).height_m
    normal = float(rules.depth["normal_top_depth_m"])
    minimum = float(rules.depth["min_top_depth_m"])
    # Полка совпадает со спецучастком (± поле вдоль трассы): тогда изломы профиля
    # ложатся ровно в его техузлы, и спецпроход остаётся одним прямым участком
    flat_half = max(rules.restriction(zone.type).margin_m, zone.margin_m, FLAT_EXTRA_M)

    above = obj_top - clearance - own_height          # верх нашей трубы при проходе сверху
    if above >= minimum - 1e-9:
        # сверху: не глубже обычной глубины — Kгл = 1
        return DepthConstraint(d=d, top_depth_m=min(above, normal), flat_half_m=flat_half, deeper=False, zone=zone)
    below = obj_top + obj_height + clearance
    return DepthConstraint(d=d, top_depth_m=below, flat_half_m=flat_half, deeper=True, zone=zone)


def _crossings_on_segment(segment: Segment, field: CaseField) -> list[tuple[SpecialZone, float]]:
    """Точки пересечения участка с подземными линиями (вдоль участка, м)."""
    line = segment.line
    result = []
    for zone in field.specials:
        if zone.type not in ("gas_pipeline", "power_cable", "heat_network"):
            continue
        if not zone.geom.intersects(line):
            continue
        inter = line.intersection(zone.geom)
        points = inter.geoms if hasattr(inter, "geoms") else [inter]
        for p in points:
            if p.geom_type != "Point":
                continue
            d = float(line.project(p))
            # точка присоединения к сети — не пересечение
            if zone.type == "heat_network" and (d <= 0.5 or d >= line.length - 0.5):
                continue
            result.append((zone, d))
    return result


# --- профиль -----------------------------------------------------------------------


def _envelope(constraints: list[DepthConstraint], length: float, end_depths: tuple[float, float],
              rules: Rules) -> tuple[list[tuple[float, float]], str | None]:
    """Ломаная (d, глубина) по огибающей ограничений. None во втором элементе — всё хорошо."""
    normal = float(rules.depth["normal_top_depth_m"])
    slope = float(rules.depth["max_slope"])
    shallow = [c for c in constraints if not c.deeper]
    deep = [c for c in constraints if c.deeper]
    if shallow and deep:
        return [(0.0, normal), (length, normal)], "на участке и проход сверху, и проход снизу — профиль не строится"

    def shallow_profile(d: float) -> float:
        value = normal
        for c in shallow:
            value = min(value, c.top_depth_m + slope * max(0.0, abs(d - c.d) - c.flat_half_m))
        value = min(value, end_depths[0] + slope * d, end_depths[1] + slope * (length - d))
        return value

    def deep_profile(d: float) -> float:
        value = normal
        for c in deep:
            value = max(value, c.top_depth_m - slope * max(0.0, abs(d - c.d) - c.flat_half_m))
        value = max(value, end_depths[0] - slope * d, end_depths[1] - slope * (length - d))
        return value

    profile = deep_profile if deep else shallow_profile
    # Огибающая — кусочно-линейная с уклонами 0 и ±0,10; её изломы (в том
    # числе пересечения двух встречных спусков) находятся по смене наклона на
    # мелкой сетке — так не нужно перебирать пары ограничений.
    step = 0.05
    count = max(2, int(round(length / step)) + 1)
    ds = [min(length, i * step) for i in range(count)]
    zs = [profile(d) for d in ds]
    points: list[tuple[float, float]] = [(0.0, round(zs[0], 3))]
    for i in range(1, len(ds) - 1):
        before = (zs[i] - zs[i - 1]) / (ds[i] - ds[i - 1] or 1e-9)
        after = (zs[i + 1] - zs[i]) / (ds[i + 1] - ds[i] or 1e-9)
        if abs(after - before) > 1e-3:
            points.append((round(ds[i], 3), round(zs[i], 3)))
    points.append((length, round(zs[-1], 3)))
    # изломы ближе 0,3 м друг к другу — это дискретизация, а не профиль
    cleaned: list[tuple[float, float]] = [points[0]]
    for p in points[1:]:
        if p[0] - cleaned[-1][0] < 0.3 and p is not points[-1]:
            continue
        cleaned.append(p)
    return cleaned, None


def apply(net: Network, field: CaseField) -> DepthReport:
    """Назначить глубины и разрезать участки в изломах профиля."""
    rules = field.rules
    normal = float(rules.depth["normal_top_depth_m"])
    report = DepthReport(max_depth_m=normal, min_depth_m=normal)

    # 1. ограничения по участкам
    per_segment: dict[str, list[DepthConstraint]] = {}
    for segment in net.segments.values():
        constraints = []
        for zone, d in _crossings_on_segment(segment, field):
            c = constraint_for(zone, d, segment.du, rules)
            if c is not None:
                constraints.append(c)
                report.constraints.append((segment.id, c))
        per_segment[segment.id] = constraints

    # 2. глубины в узлах: если полка близко к узлу, профиль не успевает вернуться
    #    к 3,0 м, и соседний участок продолжает спуск/подъём через узел
    slope = float(rules.depth["max_slope"])
    node_depth: dict[str, float] = {n: normal for n in net.nodes}
    for segment in net.segments.values():
        for c in per_segment[segment.id]:
            for node_id, dist in ((segment.start, c.d), (segment.end, segment.length - c.d)):
                ramp = slope * max(0.0, dist - c.flat_half_m)
                if c.deeper:
                    node_depth[node_id] = max(node_depth[node_id], c.top_depth_m - ramp)
                else:
                    node_depth[node_id] = min(node_depth[node_id], c.top_depth_m + ramp)

    # 3. профиль и деление
    for segment in list(net.segments.values()):
        constraints = per_segment.get(segment.id, [])
        ends = (node_depth[segment.start], node_depth[segment.end])
        if not constraints and ends == (normal, normal):
            segment.depth_start = segment.depth_end = normal
            segment.k_depth = 1.0
            continue
        profile, problem = _envelope(constraints, segment.length, ends, rules)
        if problem:
            report.issues.append(f"{segment.id}: {problem}")
            segment.depth_start = segment.depth_end = normal
            continue
        _split_by_profile(net, segment, profile, rules, report)

    for segment in net.segments.values():
        for depth in (segment.depth_start, segment.depth_end):
            if depth is not None:
                report.max_depth_m = max(report.max_depth_m, depth)
                report.min_depth_m = min(report.min_depth_m, depth)
    return report


def _split_by_profile(net: Network, segment: Segment, profile: list[tuple[float, float]], rules: Rules,
                      report: DepthReport) -> None:
    """Разрезать участок в точках излома и записать глубины кускам."""
    line = segment.line
    current = segment
    for (d0, z0), (d1, z1) in zip(profile, profile[1:]):
        if d1 >= line.length - 0.02:
            _assign_depth(current, z0, z1, rules)
            break
        cut = line.interpolate(d1)
        start_id = current.start
        node = net.split_segment(current, (cut.x, cut.y), NODE_TECH)
        halves = net.incident(node.id)
        first = next(s for s in halves if start_id in (s.start, s.end))
        rest = next(s for s in halves if s is not first)
        _assign_depth(first, z0, z1, rules)
        report.pieces_split += 1
        current = rest


def _assign_depth(segment: Segment, z0: float, z1: float, rules: Rules) -> None:
    segment.depth_start = round(z0, 3)
    segment.depth_end = round(z1, 3)
    # §5 приложения: на спуске/подъёме Kгл — среднее арифметическое в концах
    segment.k_depth = round((rules.k_depth(z0) + rules.k_depth(z1)) / 2.0, 6)


def summary(report: DepthReport) -> dict[str, Any]:
    return {
        "crossings_with_depth": len(report.constraints),
        "below_object": sum(1 for _, c in report.constraints if c.deeper),
        "above_object": sum(1 for _, c in report.constraints if not c.deeper),
        "max_top_depth_m": report.max_depth_m,
        "min_top_depth_m": report.min_depth_m,
        "pieces_split": report.pieces_split,
        "issues": report.issues,
    }


def profile_points(segment: Segment) -> list[tuple[float, float]]:
    """Для интерфейса: (пикет, глубина) по участку."""
    if segment.depth_start is None or segment.depth_end is None:
        return []
    return [(0.0, segment.depth_start), (segment.length, segment.depth_end)]

