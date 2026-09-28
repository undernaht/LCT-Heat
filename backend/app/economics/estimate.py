"""Смета трассы и риск согласований.

Стоимость самой прокладки берётся интегралом поля стоимости вдоль готовой
векторной трассы — то есть ровно тем же числом, которое оптимизировал солвер.
Узлы, врезка и реконструкция добавляются отдельными статьями.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field

import numpy as np
from shapely.geometry import LineString, Point

from ..compliance import rules
from ..config import get_config
from ..costfield.build import CostField, SURFACE_ORDER, _interp_by_du
from ..domain.models import AreaModel


@dataclass
class Crossing:
    """Фактическое пересечение преграды на векторной трассе."""

    kind: str                    # utility_crossing | hdd_road | hdd_rail | puncture
    target: str                  # что пересекаем
    length_m: float
    cost_rub: float


@dataclass
class CostBreakdown:
    total_rub: float
    laying_rub: float            # труба + земляные работы по покрытиям
    crossings_rub: float         # пересечения, поштучно по фактической геометрии
    tap_rub: float
    chambers_rub: float
    reconstruction_rub: float
    heat_loss_rub: float         # дисконтированные потери за срок службы
    excavation_extra_rub: float = 0.0   # выемка сверх минимальной из-за рельефа
    length_by_surface_m: dict[str, float] = dc_field(default_factory=dict)
    crossings: list[Crossing] = dc_field(default_factory=list)
    chambers_count: int = 0
    search_penalty_rub: float = 0.0   # вес поиска, В СМЕТУ НЕ ВХОДИТ

    def as_rows(self) -> list[tuple[str, float]]:
        """Статьи для таблицы в UI, по убыванию вклада."""
        rows = [
            ("Прокладка (труба, земляные работы, покрытия)", self.laying_rub),
            ("Дополнительная выемка из-за рельефа", self.excavation_extra_rub),
            ("Пересечения преград", self.crossings_rub),
            ("Врезка в существующую сеть", self.tap_rub),
            ("Тепловые камеры", self.chambers_rub),
            ("Реконструкция существующего участка", self.reconstruction_rub),
            ("Тепловые потери за срок службы (дисконт.)", self.heat_loss_rub),
        ]
        return sorted((row for row in rows if row[1] > 0), key=lambda r: -r[1])


@dataclass
class ApprovalRisk:
    score: float
    foreign_parcels: int
    roadway_opening_m: float
    protected_zones: int
    closed_crossings: int
    needs_reconstruction: bool

    def as_rows(self) -> list[tuple[str, float]]:
        return [
            ("Чужих участков пересечено", self.foreign_parcels),
            ("Вскрытие проезжей части, м", round(self.roadway_opening_m)),
            ("Пересечений ЗОУИТ", self.protected_zones),
            ("Закрытых переходов", self.closed_crossings),
        ]


def _sample_points(line: LineString, step: float) -> np.ndarray:
    count = max(2, int(np.ceil(line.length / step)) + 1)
    distances = np.linspace(0.0, line.length, count)
    return np.array([[p.x, p.y] for p in (line.interpolate(d) for d in distances)]), (
        line.length / (count - 1)
    )


MERGE_GAP_M = 30.0
"""Пересечения одного типа ближе этого расстояния друг к другу — один переход.

Двухпутная железная дорога или улица с разделительной полосой приходят из OSM
несколькими линиями. Считать по ним отдельные ГНБ с отдельной мобилизацией —
завышение сметы: в жизни это один прокол под всем полотном.
"""


def _spans(line: LineString, corridor) -> list[tuple[float, float, LineString]]:
    """Положение вхождений трассы в коридор, м от начала трассы."""
    intersection = line.intersection(corridor)
    if intersection.is_empty:
        return []

    pieces = (
        [intersection]
        if isinstance(intersection, LineString)
        else [g for g in getattr(intersection, "geoms", []) if isinstance(g, LineString)]
    )

    result: list[tuple[float, float, LineString]] = []
    for piece in pieces:
        start = line.project(Point(piece.coords[0]))
        end = line.project(Point(piece.coords[-1]))
        result.append((min(start, end), max(start, end), piece))
    return result


def _merge(items: list[tuple[float, float, str]]) -> list[tuple[float, float, list[str]]]:
    """Слить близкие интервалы вдоль трассы в один переход."""
    if not items:
        return []

    merged: list[tuple[float, float, list[str]]] = []
    for start, end, target in sorted(items):
        if merged and start - merged[-1][1] <= MERGE_GAP_M:
            previous_start, previous_end, targets = merged[-1]
            if target not in targets:
                targets.append(target)
            merged[-1] = (previous_start, max(previous_end, end), targets)
        else:
            merged.append((start, end, [target]))
    return merged


def count_crossings(area: AreaModel, line: LineString, field: CostField) -> list[Crossing]:
    """Пересечения преград по фактической геометрии трассы.

    Считается ИМЕННО ЗДЕСЬ, а не интегралом надбавок из поля стоимости: надбавки
    поля — веса поиска, они завышены относительно реальных расценок ради нужного
    поведения трассировщика (docs/04-algorithm.md §4.3). Смета обязана быть
    защитимой перед сметчиком, поэтому пересечения считаются поштучно.
    """
    setup = get_config().costs["crossing_setup"]
    per_m = get_config().costs["crossing_per_m"]
    result: list[Crossing] = []

    rail_spans: list[tuple[float, float, str]] = []
    for railway in area.railways:
        corridor = railway.geom.buffer(railway.corridor_width_m / 2.0)
        for start, end, piece in _spans(line, corridor):
            # Заход в полосу отвода — ещё не пересечение путей. Закрытый переход
            # нужен, только если трасса реально идёт под рельсами; иначе это
            # вопрос нормативного отступа, и им занимается нормоконтроль.
            if not piece.intersects(railway.geom):
                continue
            rail_spans.append((start, end, railway.id))

    for start, end, targets in _merge(rail_spans):
        length = end - start
        title = f"ж/д, путей: {len(targets)}" if len(targets) > 1 else f"ж/д {targets[0]}"
        result.append(
            Crossing("hdd_rail", title, round(length, 1),
                     setup["hdd_rail"] + per_m["hdd_rail"] * length)
        )

    road_spans: list[tuple[float, float, str]] = []
    for road in area.roads:
        if not road.requires_closed_crossing:
            continue
        corridor = road.geom.buffer(road.width_m / 2.0)
        for start, end, _piece in _spans(line, corridor):
            road_spans.append((start, end, road.name or road.id))

    for start, end, targets in _merge(road_spans):
        length = end - start
        title = targets[0] if len(targets) == 1 else f"{targets[0]} (+{len(targets) - 1})"
        result.append(
            Crossing("hdd_road", title, round(length, 1),
                     setup["hdd_road"] + per_m["hdd_road"] * length)
        )

    utility_hits = sum(1 for utility in area.utilities if utility.geom.intersects(line))
    if utility_hits:
        result.append(
            Crossing("utility_crossing", f"чужих сетей: {utility_hits}", 0.0,
                     setup["utility_crossing"] * utility_hits)
        )

    # Деревья, попавшие в нормативную зону: снос с компенсационной стоимостью.
    # Норматив берётся из того же профиля, что и в нормоконтроле.
    tree_rule = rules.horizontal("TREE", field.context)
    if tree_rule and area.greenery:
        felled = sum(
            1 for green in area.greenery
            if green.is_tree and green.geom.distance(line) < tree_rule.min_m
        )
        if felled:
            result.append(
                Crossing("tree_removal", f"деревьев под снос: {felled}", 0.0,
                         setup["tree_removal"] * felled)
            )

    return result


def integrate(field: CostField, line: LineString) -> tuple[float, float, dict[str, float]]:
    """Интеграл стоимости вдоль трассы: (всего, из них надбавки, длина по покрытиям)."""
    step = field.grid.resolution / 2.0
    points, actual_step = _sample_points(line, step)

    total = 0.0
    penalty_total = 0.0
    by_surface: dict[str, float] = {}

    previous_cost: float | None = None
    previous_penalty: float | None = None

    for x, y in points:
        row, col = field.grid.rowcol(float(x), float(y))
        cost = float(field.cost[row, col])
        penalty = float(field.penalty[row, col])
        if not np.isfinite(cost):
            # Трасса задевает запретную зону — считаем по базовой ставке и
            # оставляем разбираться нормоконтролю, а не молча занижаем смету.
            cost = field.base_rub_per_m + penalty

        if previous_cost is not None:
            total += (previous_cost + cost) / 2.0 * actual_step
            penalty_total += (previous_penalty + penalty) / 2.0 * actual_step
        previous_cost, previous_penalty = cost, penalty

        surface = SURFACE_ORDER[int(field.surface_code[row, col])].value
        by_surface[surface] = by_surface.get(surface, 0.0) + actual_step

    return total, penalty_total, {k: round(v, 1) for k, v in by_surface.items()}


def chamber_rate(du_mm: int) -> float:
    return _interp_by_du(get_config().costs["nodes"]["chamber_by_du"], du_mm)


def tap_rate(du_mm: int) -> float:
    return _interp_by_du(get_config().costs["nodes"]["tap_in_by_du"], du_mm)


def heat_loss_cost(du_mm: int, length_m: float) -> float:
    """Дисконтированная стоимость тепловых потерь за срок службы."""
    opex = get_config().costs["opex"]
    w_per_m = _interp_by_du(opex["heat_loss_w_per_m"], du_mm)
    gcal_year = w_per_m * length_m * 8400 * 0.86e-6
    annual_rub = gcal_year * opex["heat_tariff_rub_per_gcal"]

    rate = float(opex["discount_rate"])
    years = int(opex["lifetime_years"])
    if rate <= 0:
        return annual_rub * years
    annuity = (1 - (1 + rate) ** -years) / rate
    return annual_rub * annuity


def excavation_extra(profile, du_mm: int, laying, length_m: float) -> float:
    """Стоимость выемки сверх той, что заложена в базовую ставку.

    Базовая ставка земляных работ считает траншею минимальной глубины. Там, где
    ради уклона трубу заглубляют ниже минимума, грунта вынимается больше — и это
    отдельная статья, а не размазанная поправка.
    """
    from ..routing import profile as profile_module

    if profile is None or not profile.available or length_m <= 0:
        return 0.0

    burial = profile_module.burial_depth_m(laying)
    height = profile_module.construction_height_m(du_mm, laying)
    width = profile_module.trench_width_m(du_mm, laying)

    reference = (burial + height) * width * length_m
    extra = max(0.0, profile.excavation_m3 - reference)
    rate = float(get_config().costs["terrain"]["excavation_extra_rub_per_m3"])
    return extra * rate


def estimate_route(
    area: AreaModel,
    field: CostField,
    line: LineString,
    *,
    tap_rub: float = 0.0,
    reconstruction_rub: float = 0.0,
    profile=None,
) -> CostBreakdown:
    integrated_rub, search_penalty_rub, by_surface = integrate(field, line)
    # Из интеграла вычитаем веса поиска: в смету идут только труба и земляные работы.
    laying_rub = integrated_rub - search_penalty_rub

    crossings = count_crossings(area, line, field)
    crossings_rub = sum(crossing.cost_rub for crossing in crossings)

    interval = float(get_config().costs["nodes"]["chamber_interval_m"])
    chambers = max(1, int(line.length // interval))
    chambers_rub = chambers * chamber_rate(field.du_mm)

    heat_loss_rub = heat_loss_cost(field.du_mm, line.length)
    extra_rub = excavation_extra(profile, field.du_mm, field.laying, line.length)

    return CostBreakdown(
        excavation_extra_rub=extra_rub,
        total_rub=(
            laying_rub + crossings_rub + tap_rub + chambers_rub
            + reconstruction_rub + heat_loss_rub + extra_rub
        ),
        laying_rub=laying_rub,
        crossings_rub=crossings_rub,
        tap_rub=tap_rub,
        chambers_rub=chambers_rub,
        reconstruction_rub=reconstruction_rub,
        heat_loss_rub=heat_loss_rub,
        length_by_surface_m=by_surface,
        crossings=crossings,
        chambers_count=chambers,
        search_penalty_rub=search_penalty_rub,
    )


def approval_risk(
    area: AreaModel, line: LineString, *, needs_reconstruction: bool = False
) -> ApprovalRisk:
    weights = get_config().costs["approval_risk_weights"]

    foreign = sum(
        1 for parcel in area.parcels
        if not parcel.is_public and parcel.geom.intersects(line)
    )
    protected = sum(
        1 for parcel in area.parcels if parcel.is_protected_zone and parcel.geom.intersects(line)
    )

    roadway_m = 0.0
    closed_crossings = 0
    for road in area.roads:
        if not road.requires_closed_crossing:
            continue
        corridor = road.geom.buffer(road.width_m / 2.0)
        if not corridor.intersects(line):
            continue
        closed_crossings += 1
        roadway_m += corridor.intersection(line).length

    score = (
        weights["foreign_parcels"] * foreign
        + weights["roadway_opening_m"] * roadway_m
        + weights["protected_zones"] * protected
        + weights["closed_crossings"] * closed_crossings
        + (weights["reconstruction"] if needs_reconstruction else 0.0)
    )

    return ApprovalRisk(
        score=round(score, 2),
        foreign_parcels=foreign,
        roadway_opening_m=round(roadway_m, 1),
        protected_zones=protected,
        closed_crossings=closed_crossings,
        needs_reconstruction=needs_reconstruction,
    )
