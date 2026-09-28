"""Трассировщик: одна волна от здания, выбор точки врезки, спрямление.

Ключевое решение (docs/04-algorithm.md §5): волна Дейкстры запускается ОТ ЗДАНИЯ,
а не от точек врезки. Поле стоимости изотропно, поэтому один проход даёт стоимость
до всех точек существующей сети сразу — включая врезку в середину любого участка.
Мультистарт от кандидатов дал бы то же самое, но не позволил бы учесть
индивидуальную стоимость каждого кандидата: у мультистарта все источники стартуют
с нулевой стоимостью.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field as dc_field

import numpy as np
from shapely.geometry import LineString, Point
from shapely.ops import nearest_points, substring
from skimage.graph import MCP_Geometric

from ..compliance import report as compliance
from ..compliance.rules import RuleContext
from ..costfield.build import (
    CostField,
    add_blocked_zone,
    aoi_bounds,
    build_cost_field,
    earthwork_rate,
    pipe_rate,
)
from ..config import get_config
from ..domain.enums import ComplianceStatus, HeatNodeKind, Laying
from ..domain.models import AreaModel, Building, HeatEdge, TempSchedule
from ..economics import estimate as economics
from ..geo.raster import Grid
from ..hydraulics import diameters
from . import profile as profile_module
from . import straighten as straighten_module

DEFAULT_RESOLUTION_M = 1.0
DEFAULT_RADIUS_M = 600.0
AOI_MARGIN_M = 60.0
HEAD_SEARCH_LIMIT = 12
"""Сколько кандидатов врезки перебрать в поисках достаточного напора."""

MAX_REPAIR_PASSES = 2
"""Сколько раз пересчитать трассу, запретив места нарушений нормоконтроля."""

REPAIR_MARGIN_M = 15.0
"""Запас вокруг нарушившего участка при локальной блокировке коридора."""


@dataclass
class TapCandidate:
    """Кандидат на точку врезки со всеми составляющими стоимости."""

    edge_id: str
    node_name: str | None
    point: Point
    du_mm: int
    route_rub: float
    tap_rub: float
    reconstruction_rub: float
    total_rub: float
    reserve_gcal_h: float
    head_available_m: float | None
    accepted: bool = True
    rejected_reason: str | None = None
    source_shortfall: str | None = None                # не хватает мощности источника
    examined: int = 0                                  # сколько всего рассмотрено
    alternatives: list["TapCandidate"] = dc_field(default_factory=list)

    @property
    def needs_reconstruction(self) -> bool:
        return self.reconstruction_rub > 0.0


@dataclass
class RouteSolution:
    profile: str
    geometry: LineString
    length_m: float
    du_mm: int
    laying: Laying
    schedule: str
    q_gcal_h: float
    hydraulics: diameters.RouteHydraulics
    connection: TapCandidate
    cost: economics.CostBreakdown
    risk: economics.ApprovalRisk
    compliance: compliance.ComplianceReport
    # Вертикальная трассировка. Имя не `profile`: так называется вариант расчёта.
    vertical: profile_module.RouteProfile | None = None
    candidates: list[TapCandidate] = dc_field(default_factory=list)
    diameter_explanation: list[str] = dc_field(default_factory=list)
    field_notes: dict = dc_field(default_factory=dict)

    @property
    def rejected_candidates(self) -> list[TapCandidate]:
        return [c for c in self.candidates if not c.accepted]


# --- Подготовка -------------------------------------------------------------


def nearest_edge(area: AreaModel, geom) -> HeatEdge | None:
    if not area.heat.edges:
        return None
    return min(area.heat.edges, key=lambda e: e.geom.distance(geom))


def entry_point_for(building: Building, area: AreaModel, offset_m: float = 2.0) -> Point:
    """Точка ввода: ближайшая к сети грань здания, вынесенная наружу контура."""
    if building.entry_point is not None:
        return building.entry_point

    edge = nearest_edge(area, building.geom)
    target = edge.geom if edge else building.geom.centroid

    on_building, on_target = nearest_points(building.geom.exterior, target)
    dx, dy = on_target.x - on_building.x, on_target.y - on_building.y
    length = math.hypot(dx, dy)
    if length < 1e-6:
        centroid = building.geom.centroid
        dx, dy = on_building.x - centroid.x, on_building.y - centroid.y
        length = max(math.hypot(dx, dy), 1e-6)

    return Point(on_building.x + dx / length * offset_m, on_building.y + dy / length * offset_m)


def nearest_free_cell(field: CostField, row: int, col: int, max_radius: int = 30) -> tuple[int, int]:
    """Ближайшая проходимая ячейка — точка ввода может попасть в буфер соседа."""
    if math.isfinite(field.cost[row, col]):
        return row, col

    height, width = field.grid.shape
    for radius in range(1, max_radius + 1):
        for dr in range(-radius, radius + 1):
            for dc in range(-radius, radius + 1):
                if max(abs(dr), abs(dc)) != radius:
                    continue
                r, c = row + dr, col + dc
                if 0 <= r < height and 0 <= c < width and math.isfinite(field.cost[r, c]):
                    return r, c

    raise ValueError("вокруг точки ввода нет ни одной проходимой ячейки")


# --- Кандидаты врезки -------------------------------------------------------


def reconstruction_cost(edge: HeatEdge, extra_load_gcal_h: float) -> tuple[float, int | None]:
    """Стоимость перекладки участка, если резерва не хватает.

    Возвращает (стоимость, новый Ду). (0, None) — реконструкция не нужна.
    """
    if edge.reserve_gcal_h >= extra_load_gcal_h:
        return 0.0, None

    required = edge.load_gcal_h + extra_load_gcal_h
    choice = diameters.select_diameter(required, edge.schedule, is_branch=False)
    if not choice.selected or choice.selected.du_mm <= edge.du_mm:
        return 0.0, None

    new_du = choice.selected.du_mm
    costs = get_config().costs["reconstruction"]
    multiplier = get_config().costs["surface_multiplier"]["yard"]
    rate = (
        pipe_rate(new_du, edge.laying)
        + earthwork_rate(new_du) * costs["surface_multiplier_default"] * multiplier / 2.0
        + costs["dismantling_per_m"]
    )
    return rate * edge.geom.length, new_du


def collect_candidates(
    area: AreaModel,
    field: CostField,
    cumulative: np.ndarray,
    q_gcal_h: float,
    *,
    capacity_margin: float = 1.0,
) -> list[TapCandidate]:
    """Все точки существующей сети, куда дотянулась волна, с полной стоимостью."""
    grid = field.grid
    node_names = {node.id: node.name for node in area.heat.nodes}
    node_head = {node.id: node.head_available_m for node in area.heat.nodes}
    candidates: list[TapCandidate] = []

    # Резерв мощности источника. Поле HeatNode.reserve_gcal_h загружалось и не
    # читалось ни одной решающей функцией, хотя docs/01 §3 объявляет «резерв
    # источника ≥ Q» ограничением постановки. Проверяем, когда источник один:
    # при нескольких без графа сети нельзя сказать, какой питает участок.
    sources = [n for n in area.heat.nodes if n.kind is HeatNodeKind.SOURCE]
    source_shortfall: str | None = None
    if len(sources) == 1 and sources[0].reserve_gcal_h is not None:
        if sources[0].reserve_gcal_h < q_gcal_h:
            source_shortfall = (
                f"резерв источника {sources[0].name or sources[0].id} — "
                f"{sources[0].reserve_gcal_h:.2f} Гкал/ч, требуется {q_gcal_h:.2f}"
            )

    for edge in area.heat.edges:
        if not edge.geom.intersects(_grid_box(grid)):
            continue

        best_cost = math.inf
        best_point: Point | None = None
        steps = max(2, int(edge.geom.length / grid.resolution) + 1)
        for i in range(steps):
            point = edge.geom.interpolate(edge.geom.length * i / (steps - 1))
            if not grid.contains(point.x, point.y):
                continue
            row, col = grid.rowcol(point.x, point.y)
            value = float(cumulative[row, col])
            if value < best_cost:
                best_cost, best_point = value, point

        if best_point is None or not math.isfinite(best_cost):
            continue

        head = node_head.get(edge.node_a) or node_head.get(edge.node_b)
        name = node_names.get(edge.node_a) or node_names.get(edge.node_b)
        required = q_gcal_h * capacity_margin
        recon_rub, _ = reconstruction_cost(edge, required)
        tap_rub = economics.tap_rate(edge.du_mm)

        candidates.append(
            TapCandidate(
                edge_id=edge.id,
                node_name=name,
                point=best_point,
                du_mm=edge.du_mm,
                route_rub=best_cost,
                tap_rub=tap_rub,
                reconstruction_rub=recon_rub,
                total_rub=best_cost + tap_rub + recon_rub,
                reserve_gcal_h=edge.reserve_gcal_h,
                head_available_m=head,
                source_shortfall=source_shortfall,
            )
        )

    return sorted(candidates, key=lambda c: c.total_rub)


def _grid_box(grid: Grid):
    from shapely.geometry import box

    return box(grid.min_x, grid.min_y, grid.max_x, grid.max_y)


# --- Основной расчёт --------------------------------------------------------


def solve(
    area: AreaModel,
    building: Building,
    *,
    profile: str = "min_cost",
    laying: Laying = Laying.CHANNEL,
    resolution: float = DEFAULT_RESOLUTION_M,
    radius_m: float = DEFAULT_RADIUS_M,
    prepared_field: CostField | None = None,
) -> RouteSolution | None:
    """Найти трассу подключения здания. None — задача неразрешима."""
    if building.load is None or building.load.total <= 0:
        return None

    q = building.load.total
    reference_edge = nearest_edge(area, building.geom)
    schedule = reference_edge.schedule if reference_edge else TempSchedule(130, 70)

    choice = diameters.select_diameter(q, schedule, is_branch=True)
    if not choice.selected:
        return None
    du_mm = choice.selected.du_mm

    field = prepared_field or build_cost_field(
        area,
        Grid.covering(aoi_bounds(area, building, radius_m), resolution, margin=AOI_MARGIN_M),
        du_mm=du_mm,
        laying=laying,
        target_building=building,
        profile=profile,
    )

    entry = entry_point_for(building, area)

    # Нормоконтроль не только сообщает о нарушениях, но и управляет поиском:
    # если трасса пошла вдоль чужой сети, это место запрещается и она ищется
    # заново. Скалярное поле стоимости само по себе следование запретить не может
    # (docs/04-algorithm.md §4.3), поэтому запрет ставится постфактум и точечно.
    repair_notes: list[str] = []
    for repair_pass in range(MAX_REPAIR_PASSES + 1):
        attempt = _solve_once(area, building, field, entry, q, schedule, du_mm, laying, profile)
        if attempt is None:
            return None

        best, line, hydraulics, protocol = attempt
        if repair_pass == MAX_REPAIR_PASSES:
            break

        zones = blocking_zones(area, line, protocol, field.context)
        if not zones:
            break

        repair_notes.append(f"проход {repair_pass + 1}: запрещено зон {len(zones)}")
        for zone in zones:
            field = add_blocked_zone(field, zone)

    # Вертикальная трассировка: профиль строится по найденному плану трассы,
    # и уже он определяет объём выемки — то есть половину земляных работ.
    route_profile = profile_module.for_route(
        area, line, du_mm=du_mm, laying=laying, schedule=schedule
    )

    cost = economics.estimate_route(
        area, field, line, tap_rub=best.tap_rub,
        reconstruction_rub=best.reconstruction_rub, profile=route_profile,
    )
    risk = economics.approval_risk(area, line, needs_reconstruction=best.needs_reconstruction)

    return RouteSolution(
        profile=profile,
        geometry=line,
        length_m=line.length,
        du_mm=du_mm,
        laying=laying,
        schedule=str(schedule),
        q_gcal_h=q,
        hydraulics=hydraulics,
        connection=best,
        cost=cost,
        risk=risk,
        compliance=protocol,
        vertical=route_profile,
        candidates=best.alternatives,
        diameter_explanation=choice.explain(),
        field_notes=field.notes | {
            "grid": f"{field.grid.width}×{field.grid.height} @ {field.grid.resolution} м",
            "candidates_examined": best.examined,
            "profile": profile,
            "repairs": repair_notes,
        },
    )


def _solve_once(
    area: AreaModel,
    building: Building,
    field: CostField,
    entry: Point,
    q: float,
    schedule: TempSchedule,
    du_mm: int,
    laying: Laying,
    profile: str,
) -> tuple[TapCandidate, LineString, diameters.RouteHydraulics, compliance.ComplianceReport] | None:
    """Один проход: волна, выбор точки врезки, трасса, нормоконтроль."""
    start = nearest_free_cell(field, *field.grid.rowcol(entry.x, entry.y))

    mcp = MCP_Geometric(field.cost)
    cumulative, _ = mcp.find_costs([start])

    margin = float(get_config().costs["profiles"].get(profile, {}).get("capacity_margin", 1.0))
    candidates = collect_candidates(area, field, cumulative, q, capacity_margin=margin)
    if not candidates:
        return None

    # Кандидаты перебираются по возрастанию стоимости, пока не найдётся тот, где
    # хватает напора. Слепо брать самый дешёвый нельзя: располагаемый напор падает
    # по мере удаления от источника, и ближайшая дешёвая врезка часто оказывается
    # самой слабой. Отвергнутые попадают в объяснение «почему не она».
    best: TapCandidate | None = None
    line: LineString | None = None
    hydraulics: diameters.RouteHydraulics | None = None
    fallback: tuple[TapCandidate, LineString, diameters.RouteHydraulics] | None = None

    for candidate in candidates[:HEAD_SEARCH_LIMIT]:
        try:
            raster_path = mcp.traceback(field.grid.rowcol(candidate.point.x, candidate.point.y))
        except ValueError:
            candidate.accepted = False
            candidate.rejected_reason = "недостижима"
            continue

        points = straighten_module.straighten(
            field, [field.grid.xy(row, col) for row, col in raster_path]
        )
        points = straighten_module.orthogonalize(
            field, points, [road.geom for road in area.roads]
        )
        if len(points) < 2:
            # Раньше такой кандидат уходил в UI без пометки: счётчик отвергнутых
            # не отличал «прошёл проверку» от «проверить не удалось».
            candidate.accepted = False
            candidate.rejected_reason = "не удалось построить трассу до этой точки"
            continue

        trial_line = LineString(points)
        trial = diameters.check_route(
            q, schedule, trial_line.length,
            head_available_m=candidate.head_available_m, is_branch=True,
        )
        if trial is None:
            candidate.accepted = False
            candidate.rejected_reason = (
                "не подобран диаметр: нагрузка не проходит ни по одному Ду из ряда"
            )
            continue

        if trial.head_ok:
            best, line, hydraulics = candidate, trial_line, trial
            break

        candidate.accepted = False
        if not trial.head_known:
            # Отсутствие данных — не то же самое, что отказ: такой кандидат
            # годится как запасной, но выдавать его молча за проверенный нельзя.
            candidate.rejected_reason = (
                "располагаемый напор в точке врезки неизвестен, проверка не выполнена"
            )
        else:
            candidate.rejected_reason = (
                f"не хватает напора: нужно {trial.head_m:.1f} м на трассу "
                f"плюс {trial.head_required_at_consumer_m:.0f} м на ИТП, "
                f"есть {candidate.head_available_m:.1f} м"
            )
        if fallback is None:
            fallback = (candidate, trial_line, trial)

    if best is None:
        if fallback is None:
            return None
        # Ни одна точка не проходит по напору — показываем лучшую и говорим об этом
        best, line, hydraulics = fallback

    best.examined = len(candidates)
    best.alternatives = candidates
    protocol = compliance.check_route(area, line, field.context, target_building=building)
    return best, line, hydraulics, protocol


def context_for(du_mm: int, laying: Laying) -> RuleContext:
    return RuleContext.from_config(laying, du_mm)


def target_geometries(area: AreaModel) -> dict[str, object]:
    """id объекта → его геометрия. Нужно, чтобы по нарушению найти виновника."""
    lookup: dict[str, object] = {}
    for utility in area.utilities:
        lookup[utility.id] = utility.geom
    for building in area.buildings:
        lookup[building.id] = building.geom
    for railway in area.railways:
        lookup[railway.id] = railway.geom
    for road in area.roads:
        lookup[road.name or road.id] = road.geom.buffer(road.width_m / 2.0)
    return lookup


def blocking_zones(
    area: AreaModel,
    line: LineString,
    protocol: compliance.ComplianceReport,
    ctx: RuleContext,
) -> list[object]:
    """Зоны, которые нужно запретить, чтобы убрать следование вдоль чужой сети.

    Блокируется не весь коридор объекта, а его кусок вокруг того места, где
    нарушение фактически произошло: иначе сеть станет непересекаемой вообще.
    """
    lookup = target_geometries(area)
    zones: list[object] = []

    for check in protocol.checks:
        if check.status is not ComplianceStatus.FAIL or check.kind != "horizontal":
            continue
        geometry = lookup.get(check.target_id)
        if geometry is None:
            continue

        # окрестность нарушившего участка трассы
        start = max(0.0, check.start_m - REPAIR_MARGIN_M)
        end = min(line.length, check.end_m + REPAIR_MARGIN_M)
        neighbourhood = substring(line, start, end).buffer(
            check.required_m + REPAIR_MARGIN_M
        )
        zone = geometry.buffer(check.required_m).intersection(neighbourhood)
        if not zone.is_empty:
            zones.append(zone)

    return zones
