"""Подключение группы зданий общим коридором.

Для квартала перспективной застройки задача перестаёт быть «трасса от здания до
сети» и становится задачей Штейнера: общий коридор от точки врезки, от которого
расходятся вводы. Сумма отдельных подключений заметно дороже — каждое тянет свою
трубу по одному и тому же проезду.

Точное решение задачи Штейнера NP-трудно, поэтому используется жадное
инкрементальное наращивание: здания подключаются по убыванию нагрузки, и каждое
следующее ищет ближайшую точку не только на существующей сети, но и на уже
построенных участках. Крупнейший потребитель задаёт магистраль, мелкие врезаются
в неё — ровно так это и делают в проектной практике.

После построения дерева нагрузки агрегируются снизу вверх, и каждый участок
получает свой диаметр: общий коридор идёт большим Ду, вводы — меньшими.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field as dc_field

from shapely.geometry import LineString, Point
from shapely.ops import substring, unary_union
from skimage.graph import MCP_Geometric

from ..compliance import report as compliance
from ..config import get_config
from ..costfield.build import (
    CostField,
    SURFACE_ORDER,
    add_blocked_zone,
    aoi_bounds,
    build_cost_field,
    earthwork_rate,
    pipe_rate,
    surface_multipliers,
)
from ..domain.enums import Laying
from ..domain.models import AreaModel, Building, TempSchedule
from ..economics import estimate as economics
from ..economics import fee as fee_module
from ..geo.raster import Grid
from ..hydraulics import diameters
from . import profile as profile_module
from . import solver
from . import straighten as straighten_module
from .solver import (
    AOI_MARGIN_M,
    DEFAULT_RADIUS_M,
    DEFAULT_RESOLUTION_M,
    entry_point_for,
    nearest_edge,
    nearest_free_cell,
    reconstruction_cost,
    solve,
)


@dataclass
class GroupSegment:
    """Участок общего дерева со своим диаметром."""

    id: str
    geometry: LineString
    serves: list[str]           # какие здания питаются через этот участок
    load_gcal_h: float
    du_mm: int
    length_m: float
    laying_rub: float

    @property
    def is_shared(self) -> bool:
        return len(self.serves) > 1


@dataclass
class Connection:
    """Как подключено одно здание."""

    building_id: str
    load_gcal_h: float
    path: LineString
    tap_kind: str               # network | branch
    tap_target: str             # id участка сети или id здания, в чью трассу врезались
    tap_rub: float
    reconstruction_rub: float


@dataclass
class GroupSolution:
    profile: str
    laying: Laying
    schedule: str
    connections: list[Connection] = dc_field(default_factory=list)
    segments: list[GroupSegment] = dc_field(default_factory=list)
    total_length_m: float = 0.0
    total_cost_rub: float = 0.0
    fee_total_with_vat_rub: float = 0.0
    fee_method: str = "tariff"
    independent_cost_rub: float = 0.0
    independent_length_m: float = 0.0
    independent_fee_rub: float = 0.0
    independent_fee_method: str = "tariff"
    compliance_status: str = "pass"
    compliance_failures: int = 0
    compliance_conditionals: int = 0
    notes: dict = dc_field(default_factory=dict)

    @property
    def saving_rub(self) -> float:
        return self.independent_cost_rub - self.total_cost_rub

    @property
    def saving_share(self) -> float:
        if self.independent_cost_rub <= 0:
            return 0.0
        return self.saving_rub / self.independent_cost_rub

    @property
    def length_saving_m(self) -> float:
        return self.independent_length_m - self.total_length_m

    @property
    def fee_comparable(self) -> bool:
        """Сравнима ли плата между вариантами.

        Подключение квартала одним объектом может перевалить порог 1,5 Гкал/ч и
        уйти на индивидуальный проект, тогда как каждое здание по отдельности
        остаётся в тарифном диапазоне. Это реальный регуляторный эффект, но
        сравнивать «0 против 66 млн» нельзя — суммы посчитаны по разным правилам.
        """
        return self.fee_method == self.independent_fee_method == "tariff"

    @property
    def fee_saving_rub(self) -> float:
        if not self.fee_comparable:
            return 0.0
        return self.independent_fee_rub - self.fee_total_with_vat_rub

    def geometry(self) -> LineString | None:
        if not self.segments:
            return None
        merged = unary_union([segment.geometry for segment in self.segments])
        return merged


# --- Вспомогательное ---------------------------------------------------------


def _straight_hint(building: Building, edge) -> float:
    """Грубая оценка длины трассы до участка — для предварительной проверки напора.

    Точная длина известна только после трассировки, но для отсева заведомо
    слабых точек врезки достаточно расстояния по прямой с запасом на обход.
    """
    return max(20.0, edge.geom.distance(building.geom) * 1.35)


def _sample_positions(line: LineString, step: float) -> list[Point]:
    count = max(2, int(line.length / step) + 1)
    return [line.interpolate(line.length * i / (count - 1)) for i in range(count)]


def _mean_surface_multiplier(field: CostField, line: LineString, profile: str) -> float:
    """Средний множитель земляных работ вдоль участка."""
    table = surface_multipliers(profile)
    points = _sample_positions(line, field.grid.resolution)
    total = 0.0
    for point in points:
        row, col = field.grid.rowcol(point.x, point.y)
        surface = SURFACE_ORDER[int(field.surface_code[row, col])]
        total += table.get(surface, 1.0)
    return total / len(points)


def _penalty_integral(field: CostField, line: LineString) -> float:
    """Надбавки зон вдоль участка — веса поиска, в смету не идут (§9.1)."""
    step = field.grid.resolution / 2.0
    points = _sample_positions(line, step)
    if len(points) < 2:
        return 0.0
    actual_step = line.length / (len(points) - 1)
    total = 0.0
    previous: float | None = None
    for point in points:
        row, col = field.grid.rowcol(point.x, point.y)
        value = float(field.penalty[row, col])
        if previous is not None:
            total += (previous + value) / 2.0 * actual_step
        previous = value
    return total


# --- Жадное наращивание дерева ----------------------------------------------


@dataclass
class _Branch:
    building_id: str
    path: LineString
    parent: str | None          # id здания-родителя или None (врезка в сеть)
    tap_distance_m: float       # положение врезки вдоль path родителя
    own_load: float


def _connect_one(
    area: AreaModel,
    field: CostField,
    building: Building,
    branches: dict[str, _Branch],
    q_gcal_h: float,
    *,
    group_load: float,
    schedule: TempSchedule,
) -> tuple[Connection, _Branch] | None:
    """Подключить одно здание к существующей сети либо к уже построенной ветке."""
    grid = field.grid
    entry = entry_point_for(building, area)
    start = nearest_free_cell(field, *grid.rowcol(entry.x, entry.y))

    mcp = MCP_Geometric(field.cost)
    cumulative, _ = mcp.find_costs([start])

    best_total = math.inf
    best: tuple[str, str, Point, float, float] | None = None   # kind, target, point, tap, recon
    node_head = {node.id: node.head_available_m for node in area.heat.nodes}
    head_rejected = 0

    # --- Кандидаты на существующей сети ---
    for edge in area.heat.edges:
        # Проверка напора была только в одиночном расчёте: групповой брал точку
        # врезки исключительно по стоимости и мог посадить весь квартал на
        # заведомо слабый конец сети.
        available = node_head.get(edge.node_a) or node_head.get(edge.node_b)
        if available is not None:
            probe = diameters.check_route(
                group_load, schedule, _straight_hint(building, edge),
                head_available_m=available, is_branch=False,
            )
            if probe is not None and probe.head_known and not probe.head_ok:
                head_rejected += 1
                continue

        for point in _sample_positions(edge.geom, grid.resolution):
            if not grid.contains(point.x, point.y):
                continue
            row, col = grid.rowcol(point.x, point.y)
            route = float(cumulative[row, col])
            if not math.isfinite(route):
                continue
            # Реконструкция проверяется против ВСЕЙ нагрузки, которая пойдёт
            # через этот участок, а не только против нагрузки текущего здания:
            # квартал врезается одной трубой, и участок несёт сумму корпусов.
            recon, _ = reconstruction_cost(edge, group_load)
            tap = economics.tap_rate(edge.du_mm)
            total = route + tap + recon
            if total < best_total:
                best_total = total
                best = ("network", edge.id, point, tap, recon)

    # --- Кандидаты на уже построенных ветках ---
    for branch in branches.values():
        for point in _sample_positions(branch.path, grid.resolution):
            if not grid.contains(point.x, point.y):
                continue
            row, col = grid.rowcol(point.x, point.y)
            route = float(cumulative[row, col])
            if not math.isfinite(route):
                continue
            # Врезка в собственную новую сеть — тройник с камерой, дешевле
            # врезки в действующую магистраль: не нужно отключать потребителей.
            tap = economics.tap_rate(field.du_mm) * 0.6
            total = route + tap
            if total < best_total:
                best_total = total
                best = ("branch", branch.building_id, point, tap, 0.0)

    if best is None:
        return None

    kind, target, tap_point, tap_rub, recon_rub = best
    try:
        raster_path = mcp.traceback(grid.rowcol(tap_point.x, tap_point.y))
    except ValueError:
        return None

    points = straighten_module.straighten(field, [grid.xy(r, c) for r, c in raster_path])
    points = straighten_module.orthogonalize(field, points, [road.geom for road in area.roads])
    if len(points) < 2:
        return None

    path = LineString(points)
    tap_distance = 0.0
    if kind == "branch":
        tap_distance = branches[target].path.project(Point(points[-1]))

    connection = Connection(
        building_id=building.id,
        load_gcal_h=q_gcal_h,
        path=path,
        tap_kind=kind,
        tap_target=target,
        tap_rub=tap_rub,
        reconstruction_rub=recon_rub,
    )
    branch = _Branch(
        building_id=building.id,
        path=path,
        parent=target if kind == "branch" else None,
        tap_distance_m=tap_distance,
        own_load=q_gcal_h,
    )
    return connection, branch


# --- Агрегация нагрузок и подбор диаметров ----------------------------------


def _subtree_ids(branch_id: str, branches: dict[str, _Branch]) -> list[str]:
    """Здание и все, кто питается через его ветку, включая внуков."""
    result = [branch_id]
    for other in branches.values():
        if other.parent == branch_id:
            result.extend(_subtree_ids(other.building_id, branches))
    return result


def _subtree_load(branch_id: str, branches: dict[str, _Branch]) -> float:
    """Суммарная нагрузка ветки со всеми, кто в неё врезался."""
    return sum(branches[i].own_load for i in _subtree_ids(branch_id, branches))


def _split_into_segments(
    branches: dict[str, _Branch],
    field: CostField,
    schedule: TempSchedule,
    profile: str,
    oversized: list[str],
) -> list[GroupSegment]:
    """Разбить ветки в точках врезки и назначить каждому участку свой диаметр.

    Ветка идёт от здания (d = 0) к точке врезки (d = L). Поток движется навстречу,
    поэтому участок [d_k, d_k+1] несёт собственную нагрузку здания плюс нагрузки
    всех поддеревьев, врезавшихся ближе к зданию, чем d_k.
    """
    segments: list[GroupSegment] = []

    for branch in branches.values():
        children = [b for b in branches.values() if b.parent == branch.building_id]
        cuts = sorted({0.0, branch.path.length} | {c.tap_distance_m for c in children})

        for index, (start, end) in enumerate(zip(cuts, cuts[1:])):
            if end - start < 0.5:
                continue

            load = branch.own_load + sum(
                _subtree_load(child.building_id, branches)
                for child in children
                if child.tap_distance_m <= start + 1e-6
            )
            # Питает не только прямых детей, но и всё, что за ними: иначе
            # магистраль подписана неполно, а подпись — это то, по чему
            # проверяют правильность агрегации.
            serves = [branch.building_id]
            for child in children:
                if child.tap_distance_m <= start + 1e-6:
                    serves.extend(_subtree_ids(child.building_id, branches))

            piece = substring(branch.path, start, end)
            if not isinstance(piece, LineString) or piece.length < 0.5:
                continue

            choice = diameters.select_diameter(load, schedule, is_branch=len(serves) == 1)
            if choice.selected is None:
                # Молчаливая подстановка Ду500 скрывала бы отказ подбора:
                # участок получал бы диаметр, который никто не проверял.
                oversized.append(
                    f"{branch.building_id}#{index}: нагрузка {load:.2f} Гкал/ч "
                    "не проходит ни по одному диаметру из ряда"
                )
                continue
            du = choice.selected.du_mm

            multiplier = _mean_surface_multiplier(field, piece, profile)
            laying_rub = piece.length * (
                pipe_rate(du, field.laying) + earthwork_rate(du) * multiplier
            )

            segments.append(
                GroupSegment(
                    id=f"{branch.building_id}#{index}",
                    geometry=piece,
                    serves=sorted(set(serves)),
                    load_gcal_h=round(load, 4),
                    du_mm=du,
                    length_m=round(piece.length, 1),
                    laying_rub=laying_rub,
                )
            )

    return segments


# --- Основной расчёт --------------------------------------------------------


def solve_group(
    area: AreaModel,
    buildings: list[Building],
    *,
    profile: str = "min_cost",
    laying: Laying = Laying.CHANNEL,
    resolution: float = DEFAULT_RESOLUTION_M,
    radius_m: float = DEFAULT_RADIUS_M,
) -> GroupSolution | None:
    """Подключить группу зданий общим коридором и сравнить с раздельным."""
    heated = [b for b in buildings if b.load and b.load.total > 0]
    if not heated:
        return None

    reference = nearest_edge(area, heated[0].geom)
    schedule = reference.schedule if reference else TempSchedule(130, 70)
    total_load = sum(b.load.total for b in heated)

    # Диаметр для построения запретных зон берём по суммарной нагрузке: буферы
    # зависят от Ду только на порогах 500 и 800, поэтому запас безопасен.
    trunk = diameters.select_diameter(total_load, schedule, is_branch=False)
    if not trunk.selected:
        return None

    bounds = [aoi_bounds(area, building, radius_m) for building in heated]
    grid = Grid.covering(
        (
            min(b[0] for b in bounds), min(b[1] for b in bounds),
            max(b[2] for b in bounds), max(b[3] for b in bounds),
        ),
        resolution,
        margin=AOI_MARGIN_M,
    )
    field = build_cost_field(
        area, grid, du_mm=trunk.selected.du_mm, laying=laying,
        target_building=heated[0], profile=profile,
    )

    # --- Жадное наращивание: крупнейший потребитель задаёт магистраль ---
    order = sorted(heated, key=lambda b: -b.load.total)
    branches: dict[str, _Branch] = {}
    connections: list[Connection] = []

    repairs = 0
    for building in order:
        # Тот же ремонт по нормоконтролю, что и в одиночном расчёте: если ветка
        # пошла вдоль чужой сети, место запрещается и она ищется заново. Запрет
        # остаётся в поле и для следующих зданий — ограничение-то настоящее.
        connection = branch = None
        for attempt in range(solver.MAX_REPAIR_PASSES + 1):
            result = _connect_one(
                area, field, building, branches, building.load.total,
                group_load=total_load, schedule=schedule,
            )
            if result is None:
                break
            connection, branch = result
            if attempt == solver.MAX_REPAIR_PASSES:
                break

            protocol = compliance.check_route(area, connection.path, field.context)
            zones = solver.blocking_zones(area, connection.path, protocol, field.context)
            if not zones:
                break
            repairs += 1
            for zone in zones:
                field = add_blocked_zone(field, zone)

        if connection is None or branch is None:
            continue
        connections.append(connection)
        branches[building.id] = branch

    if not connections:
        return None

    oversized: list[str] = []
    segments = _split_into_segments(branches, field, schedule, profile, oversized)
    total_length = sum(segment.length_m for segment in segments)

    # --- Смета группы ---
    laying_rub = sum(segment.laying_rub for segment in segments)
    tap_rub = sum(c.tap_rub for c in connections)
    reconstruction_rub = sum(c.reconstruction_rub for c in connections)

    # Пересечения и нормоконтроль считаются ПО КАЖДОЙ ВЕТКЕ ОТДЕЛЬНО.
    # Склеить дерево в одну ломаную нельзя: ветки расходятся в разные стороны,
    # и склейка порождает фиктивный отрезок через полрайона, который «пересекает»
    # всё подряд. Ветки при этом не перекрываются — каждая идёт от своего здания
    # до точки врезки, — поэтому двойного счёта не возникает.
    crossings = []
    for connection in connections:
        crossings.extend(economics.count_crossings(area, connection.path, field))
    crossings_rub = sum(crossing.cost_rub for crossing in crossings)

    # Интервал берётся из конфига, а не зашивается: раньше в группе стояло 100 м,
    # а в одиночной смете — 200, и сравнение «группой против раздельно» велось
    # по двум разным правилам расстановки камер.
    interval = float(get_config().costs["nodes"]["chamber_interval_m"])
    chambers = max(1, int(total_length // interval))
    chambers_rub = chambers * economics.chamber_rate(trunk.selected.du_mm)
    heat_loss_rub = sum(
        economics.heat_loss_cost(segment.du_mm, segment.length_m) for segment in segments
    )

    # Выемка сверх минимальной — по каждому участку своим диаметром. Участки не
    # перекрываются, поэтому объёмы складываются. Считать по веткам целиком
    # нельзя: на магистральном участке траншея шире, чем на ответвлении.
    excavation_extra_rub = sum(
        economics.excavation_extra(
            profile_module.for_route(
                area, segment.geometry, du_mm=segment.du_mm,
                laying=laying, schedule=schedule,
            ),
            segment.du_mm, laying, segment.geometry.length,
        )
        for segment in segments
    )

    total_cost = (
        laying_rub + crossings_rub + tap_rub + reconstruction_rub
        + chambers_rub + heat_loss_rub + excavation_extra_rub
    )

    # --- Плата за подключение по всей группе ---
    length_by_du: dict[int, float] = {}
    for segment in segments:
        length_by_du[segment.du_mm] = length_by_du.get(segment.du_mm, 0.0) + segment.length_m
    group_fee = fee_module.calculate(total_load, length_by_du, laying)

    # --- Что было бы при раздельном подключении ---
    independent_cost = 0.0
    independent_length = 0.0
    independent_fee = 0.0
    independent_methods: set[str] = set()
    for building in heated:
        alone = solve(
            area, building, profile=profile, laying=laying,
            resolution=resolution, radius_m=radius_m,
        )
        if alone is None:
            continue
        independent_cost += alone.cost.total_rub
        independent_length += alone.length_m
        alone_fee = fee_module.calculate(
            alone.q_gcal_h, {alone.du_mm: alone.length_m}, laying
        )
        independent_fee += alone_fee.total_with_vat_rub
        independent_methods.add(alone_fee.method)

    # --- Нормоконтроль: по каждой ветке, отчёты сливаются ---
    checks: list[compliance.Check] = []
    checks_total = 0
    for connection in connections:
        part = compliance.check_route(area, connection.path, field.context)
        checks.extend(part.checks)
        checks_total += part.checks_total

    status = compliance._overall(checks)
    failures = sum(1 for c in checks if c.status.value == "fail")
    conditionals = sum(1 for c in checks if c.status.value == "conditional")

    return GroupSolution(
        profile=profile,
        laying=laying,
        schedule=str(schedule),
        connections=connections,
        segments=sorted(segments, key=lambda s: -s.du_mm),
        total_length_m=round(total_length, 1),
        total_cost_rub=total_cost,
        fee_total_with_vat_rub=group_fee.total_with_vat_rub,
        fee_method=group_fee.method,
        independent_cost_rub=independent_cost,
        independent_length_m=round(independent_length, 1),
        independent_fee_rub=independent_fee,
        independent_fee_method=(
            independent_methods.pop() if len(independent_methods) == 1 else "mixed"
        ),
        compliance_status=status.value,
        compliance_failures=failures,
        compliance_conditionals=conditionals,
        notes={
            "buildings": len(connections),
            "checks_total": checks_total,
            "total_load_gcal_h": round(total_load, 3),
            "trunk_du_mm": max((s.du_mm for s in segments), default=0),
            "excavation_extra_rub": round(excavation_extra_rub),
            "shared_segments": sum(1 for s in segments if s.is_shared),
            "shared_length_m": round(sum(s.length_m for s in segments if s.is_shared), 1),
            "crossings_rub": round(crossings_rub),
            "chambers": chambers,
            "grid": f"{grid.width}×{grid.height} @ {grid.resolution} м",
            "repairs": repairs,
            "chamber_interval_m": interval,
            "oversized_segments": oversized,
            "search_penalty_rub": round(
                sum(_penalty_integral(field, s.geometry) for s in segments)
            ),
        },
    )
