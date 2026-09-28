"""Оценка трассы, нарисованной или поправленной руками.

Автоматический оптимум — не приговор. Инженер знает то, чего нет в данных:
здесь через год снесут павильон, здесь собственник не пустит, а вот тут вдоль
забора копать удобнее. Поэтому трассу нужно уметь поправить и тут же получить
все те же числа, что и для расчётной: диаметр, потери, смету, плату и протокол
нормоконтроля — плюс честную разницу с оптимумом.

Проверяется ровно то же самое, теми же модулями. Никаких поблажек ручной
трассе: если она задевает запретную зону, об этом будет сказано.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field as dc_field

from shapely.geometry import LineString, Point

from ..compliance import report as compliance
from ..costfield.build import CostField, aoi_bounds, build_cost_field
from ..domain.enums import Laying
from ..domain.models import AreaModel, Building, HeatEdge, TempSchedule
from ..economics import estimate as economics
from ..economics import fee as fee_module
from ..geo.raster import Grid
from ..hydraulics import diameters
from . import profile as profile_module
from .solver import (
    AOI_MARGIN_M,
    DEFAULT_RADIUS_M,
    DEFAULT_RESOLUTION_M,
    entry_point_for,
    nearest_edge,
    reconstruction_cost,
)

START_TOLERANCE_M = 25.0
"""Насколько начало трассы может отстоять от здания."""

TAP_TOLERANCE_M = 15.0
"""Насколько конец трассы может отстоять от существующей сети."""

MIN_LENGTH_M = 5.0


@dataclass
class GeometryIssue:
    kind: str          # detached_start | detached_end | crosses_blocked | self_intersect | degenerate
    detail: str
    at_m: float | None = None


@dataclass
class ManualEvaluation:
    """Всё, что известно про нарисованную трассу."""

    length_m: float
    du_mm: int
    laying: Laying
    schedule: str
    q_gcal_h: float
    hydraulics: diameters.RouteHydraulics
    cost: economics.CostBreakdown
    risk: economics.ApprovalRisk
    protocol: compliance.ComplianceReport
    vertical: profile_module.RouteProfile
    fee_total_with_vat_rub: float
    fee_method: str
    tap_edge_id: str | None = None
    tap_node_name: str | None = None
    tap_distance_m: float | None = None
    reconstruction_rub: float = 0.0
    issues: list[GeometryIssue] = dc_field(default_factory=list)

    # Сравнение с расчётной трассой — заполняется вызывающим, если она известна
    reference_length_m: float | None = None
    reference_cost_rub: float | None = None
    reference_risk: float | None = None

    @property
    def admissible(self) -> bool:
        """Годится ли трасса к дальнейшей работе."""
        blocking = {"detached_start", "detached_end", "crosses_blocked", "degenerate"}
        return (
            not any(issue.kind in blocking for issue in self.issues)
            and self.protocol.status.value != "fail"
        )

    @property
    def cost_delta_rub(self) -> float | None:
        if self.reference_cost_rub is None:
            return None
        return self.cost.total_rub - self.reference_cost_rub

    @property
    def cost_delta_share(self) -> float | None:
        if not self.reference_cost_rub:
            return None
        return self.cost_delta_rub / self.reference_cost_rub


# --- Проверки геометрии -----------------------------------------------------


def _self_intersects(line: LineString) -> bool:
    """Самопересечение: труба не может проходить сквозь себя."""
    return not line.is_simple


def _blocked_span(field: CostField, line: LineString) -> float:
    """Сколько метров трассы лежит в запретной зоне."""
    step = field.grid.resolution / 2.0
    count = max(2, int(line.length / step) + 1)
    blocked = 0
    for index in range(count):
        point = line.interpolate(line.length * index / (count - 1))
        row, col = field.grid.rowcol(point.x, point.y)
        if field.blocked[row, col]:
            blocked += 1
    return blocked / count * line.length


def check_geometry(
    field: CostField, area: AreaModel, building: Building, line: LineString
) -> tuple[list[GeometryIssue], HeatEdge | None, float | None]:
    """Проверить саму ломаную: где начинается, где кончается, что задевает."""
    issues: list[GeometryIssue] = []

    if line.length < MIN_LENGTH_M or len(line.coords) < 2:
        issues.append(GeometryIssue("degenerate", "трасса короче пяти метров"))
        return issues, None, None

    if _self_intersects(line):
        issues.append(
            GeometryIssue("self_intersect", "трасса пересекает саму себя")
        )

    # Начало — у здания
    start = Point(line.coords[0])
    to_building = building.geom.distance(start)
    if to_building > START_TOLERANCE_M:
        issues.append(
            GeometryIssue(
                "detached_start",
                f"начало трассы в {to_building:.0f} м от здания "
                f"(допустимо до {START_TOLERANCE_M:.0f} м)",
                at_m=0.0,
            )
        )

    # Конец — на существующей сети
    end = Point(line.coords[-1])
    edge = nearest_edge(area, end)
    distance = edge.geom.distance(end) if edge else None
    if edge is None:
        issues.append(GeometryIssue("detached_end", "в районе нет тепловых сетей"))
    elif distance is not None and distance > TAP_TOLERANCE_M:
        issues.append(
            GeometryIssue(
                "detached_end",
                f"конец трассы в {distance:.0f} м от существующей сети "
                f"(допустимо до {TAP_TOLERANCE_M:.0f} м)",
                at_m=round(line.length, 1),
            )
        )

    # Запретные зоны — контуры зданий с нормативным буфером, вода
    blocked = _blocked_span(field, line)
    if blocked > field.grid.resolution:
        issues.append(
            GeometryIssue(
                "crosses_blocked",
                f"{blocked:.0f} м трассы проходит по запретной зоне: "
                "контур здания с нормативным отступом, водный объект "
                "или участок с запретом прокладки",
            )
        )

    return issues, edge, distance


# --- Основная оценка --------------------------------------------------------


def evaluate(
    area: AreaModel,
    building: Building,
    line: LineString,
    *,
    laying: Laying = Laying.CHANNEL,
    resolution: float = DEFAULT_RESOLUTION_M,
    radius_m: float = DEFAULT_RADIUS_M,
    profile: str = "min_cost",
) -> ManualEvaluation | None:
    """Посчитать всё для нарисованной трассы. None — у здания нет нагрузки."""
    if building.load is None or building.load.total <= 0:
        return None

    q = building.load.total
    reference_edge = nearest_edge(area, building.geom)
    schedule = reference_edge.schedule if reference_edge else TempSchedule(130, 70)

    # Диаметр подбирается по фактической длине нарисованной трассы
    choice = diameters.select_diameter(q, schedule, is_branch=True)
    if not choice.selected:
        return None
    du_mm = choice.selected.du_mm

    grid = Grid.covering(
        aoi_bounds(area, building, radius_m), resolution, margin=AOI_MARGIN_M
    )
    field = build_cost_field(
        area, grid, du_mm=du_mm, laying=laying,
        target_building=building, profile=profile,
    )

    issues, edge, tap_distance = check_geometry(field, area, building, line)

    head_available = None
    reconstruction = 0.0
    tap_rub = 0.0
    if edge is not None:
        node_head = {node.id: node.head_available_m for node in area.heat.nodes}
        head_available = node_head.get(edge.node_a) or node_head.get(edge.node_b)
        reconstruction, _ = reconstruction_cost(edge, q)
        tap_rub = economics.tap_rate(edge.du_mm)

    hydraulics = diameters.check_route(
        q, schedule, line.length, head_available_m=head_available, is_branch=True
    )
    if hydraulics is None:
        return None

    # Нарисованная вручную трасса тоже требует профиля: без него её смета была
    # бы систематически ниже расчётной и правка казалась бы выгоднее, чем есть.
    vertical = profile_module.for_route(
        area, line, du_mm=du_mm, laying=laying, schedule=schedule
    )

    cost = economics.estimate_route(
        area, field, line, tap_rub=tap_rub,
        reconstruction_rub=reconstruction, profile=vertical,
    )
    risk = economics.approval_risk(area, line, needs_reconstruction=reconstruction > 0)
    protocol = compliance.check_route(
        area, line, field.context, target_building=building
    )
    fee = fee_module.calculate(q, {du_mm: line.length}, laying)

    return ManualEvaluation(
        length_m=round(line.length, 1),
        du_mm=du_mm,
        laying=laying,
        schedule=str(schedule),
        q_gcal_h=q,
        hydraulics=hydraulics,
        cost=cost,
        risk=risk,
        protocol=protocol,
        vertical=vertical,
        fee_total_with_vat_rub=fee.total_with_vat_rub,
        fee_method=fee.method,
        tap_edge_id=edge.id if edge else None,
        tap_node_name=_node_name(area, edge),
        tap_distance_m=round(tap_distance, 1) if tap_distance is not None else None,
        reconstruction_rub=reconstruction,
        issues=issues,
    )


def _node_name(area: AreaModel, edge: HeatEdge | None) -> str | None:
    if edge is None:
        return None
    names = {node.id: node.name for node in area.heat.nodes}
    return names.get(edge.node_a) or names.get(edge.node_b)


def snap_to_building(area: AreaModel, building: Building, line: LineString) -> LineString:
    """Подтянуть начало трассы к точке ввода, если оно рядом.

    Рисуя мышью, попасть точно в грань здания невозможно, а отрыв на полтора
    метра — не ошибка проектировщика, а промах курсора.
    """
    entry = entry_point_for(building, area)
    coords = list(line.coords)
    if math.dist(coords[0], (entry.x, entry.y)) <= START_TOLERANCE_M:
        coords[0] = (entry.x, entry.y)
    return LineString(coords)


def snap_to_network(area: AreaModel, line: LineString) -> LineString:
    """Подтянуть конец трассы к ближайшей точке существующей сети."""
    end = Point(line.coords[-1])
    edge = nearest_edge(area, end)
    if edge is None or edge.geom.distance(end) > TAP_TOLERANCE_M:
        return line
    snapped = edge.geom.interpolate(edge.geom.project(end))
    coords = list(line.coords)
    coords[-1] = (snapped.x, snapped.y)
    return LineString(coords)
