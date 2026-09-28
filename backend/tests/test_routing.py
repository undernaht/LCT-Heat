"""Тесты поля стоимости, нормативных правил и трассировщика.

Район синтетический и маленький: тут проверяется поведение алгоритма, а не
качество датасета. Реальный район прогоняется через solve_cli.py.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from shapely.geometry import LineString, Point, Polygon, box

from app.compliance import rules
from app.compliance.rules import RuleContext
from app.costfield.build import build_cost_field, earthwork_rate, pipe_rate
from app.domain.enums import BuildingUse, HeatNodeKind, Laying, SurfaceClass, UtilityKind
from app.domain.models import (
    AreaModel,
    Building,
    HeatEdge,
    HeatLoad,
    HeatNetwork,
    HeatNode,
    Parcel,
    TempSchedule,
    Utility,
)
from app.geo.raster import Grid, burn, burn_mask
from app.hydraulics import diameters
from app.routing import solver
from app.routing.straighten import collapse_collinear, segment_cost, straighten

SCHEDULE = TempSchedule(130, 70)


# --- Сетка ------------------------------------------------------------------


def test_grid_roundtrip_through_cell_centre():
    grid = Grid.covering((0.0, 0.0, 100.0, 60.0), resolution=2.0)
    row, col = grid.rowcol(51.0, 33.0)
    x, y = grid.xy(row, col)

    assert abs(x - 51.0) <= 1.0
    assert abs(y - 33.0) <= 1.0
    assert grid.rowcol(x, y) == (row, col)


def test_grid_row_zero_is_north():
    grid = Grid.covering((0.0, 0.0, 100.0, 100.0), resolution=1.0)
    top_row, _ = grid.rowcol(50.0, 99.0)
    bottom_row, _ = grid.rowcol(50.0, 1.0)
    assert top_row < bottom_row


def test_burn_writes_values_inside_polygon():
    grid = Grid.covering((0.0, 0.0, 20.0, 20.0), resolution=1.0)
    array = burn(grid, [(box(5, 5, 15, 15), 7.0)], fill=1.0)

    assert array[grid.rowcol(10.0, 10.0)] == pytest.approx(7.0)
    assert array[grid.rowcol(1.0, 1.0)] == pytest.approx(1.0)


def test_burn_mask_is_boolean_coverage():
    grid = Grid.covering((0.0, 0.0, 20.0, 20.0), resolution=1.0)
    mask = burn_mask(grid, [box(0, 0, 10, 10)])

    assert mask.dtype == bool
    assert mask[grid.rowcol(5.0, 5.0)]
    assert not mask[grid.rowcol(15.0, 15.0)]


# --- Нормативные правила ----------------------------------------------------


def test_building_clearance_depends_on_laying():
    """2,0 м при канальной против 5,0 м при бесканальной — СП 124.13330, табл. А.3."""
    channel = rules.horizontal("BUILDING", RuleContext(Laying.CHANNEL, du_mm=80))
    channelless = rules.horizontal("BUILDING", RuleContext(Laying.CHANNELLESS, du_mm=80))

    assert channel.min_m == 2.0
    assert channelless.min_m == 5.0
    assert "А.3" in channel.clause


def test_building_clearance_grows_with_diameter():
    small = rules.horizontal("BUILDING", RuleContext(Laying.CHANNEL, du_mm=400))
    large = rules.horizontal("BUILDING", RuleContext(Laying.CHANNEL, du_mm=600))
    huge = rules.horizontal("BUILDING", RuleContext(Laying.CHANNEL, du_mm=1000))

    assert (small.min_m, large.min_m, huge.min_m) == (2.0, 5.0, 8.0)


def test_subsiding_soil_increases_clearance():
    normal = rules.horizontal("BUILDING", RuleContext(Laying.CHANNEL, du_mm=80, soil="nonsubsiding"))
    subsiding = rules.horizontal("BUILDING", RuleContext(Laying.CHANNEL, du_mm=80, soil="subsiding_1"))

    assert subsiding.min_m > normal.min_m


def test_gas_clearance_depends_on_pressure_class():
    ctx = RuleContext(Laying.CHANNELLESS, du_mm=80)
    low = rules.horizontal("GAS", ctx, pressure_class="<=0.3")
    high = rules.horizontal("GAS", ctx, pressure_class="0.6-1.2")

    assert low.min_m == 1.0
    assert high.min_m == 2.0


def test_vertical_rule_allows_crossing_pipes():
    """Пересекать чужие трубы можно — нужно лишь разойтись на 0,2 м по вертикали."""
    clearance = rules.vertical("WATER")
    assert clearance is not None and clearance.min_m == 0.20


def test_every_rule_carries_a_clause():
    ctx = RuleContext(Laying.CHANNEL, du_mm=80)
    for kind in UtilityKind:
        clearance = rules.horizontal(kind.value, ctx)
        if clearance:
            assert clearance.clause and clearance.rule_id


# --- Синтетический район ----------------------------------------------------


def synthetic_area(*, wall: bool = True, du_mm: int = 100, load: float = 0.4) -> AreaModel:
    """Здание внизу, теплосеть вверху, между ними — стена из зданий.

        y=180  ═══════════ теплосеть ═══════════
        y=100  ▓▓▓▓▓▓▓▓ стена ▓▓▓▓▓▓▓▓
        y= 30            ▢ здание
    """
    area = AreaModel(crs="EPSG:32637")

    network = HeatNetwork()
    network.nodes.append(
        HeatNode(id="hn_a", geom=Point(0.0, 180.0), kind=HeatNodeKind.SOURCE,
                 name="Котельная", head_available_m=30.0)
    )
    network.nodes.append(
        HeatNode(id="hn_b", geom=Point(300.0, 180.0), kind=HeatNodeKind.CHAMBER,
                 name="ТК-1", head_available_m=25.0)
    )
    capacity = diameters.capacity_gcal_h(du_mm, SCHEDULE, is_branch=False)
    network.edges.append(
        HeatEdge(id="he_1", geom=LineString([(0.0, 180.0), (300.0, 180.0)]), du_mm=du_mm,
                 laying=Laying.CHANNEL, schedule=SCHEDULE, node_a="hn_a", node_b="hn_b",
                 load_gcal_h=load, capacity_gcal_h=capacity)
    )
    area.heat = network

    area.buildings.append(
        Building(id="target", geom=box(140.0, 20.0, 160.0, 40.0),
                 use=BuildingUse.RESIDENTIAL, floors=9, is_perspective=True,
                 load=HeatLoad(heating=0.35, dhw_max=0.11))
    )

    if wall:
        area.buildings.append(
            Building(id="wall", geom=box(40.0, 100.0, 260.0, 120.0),
                     use=BuildingUse.RESIDENTIAL, floors=5,
                     load=HeatLoad(heating=1.0))
        )

    return area


def small_grid(area: AreaModel, building: Building, resolution: float = 2.0) -> Grid:
    from app.costfield.build import aoi_bounds

    return Grid.covering(aoi_bounds(area, building, 600.0), resolution, margin=40.0)


# --- Поле стоимости ---------------------------------------------------------


def test_cost_field_blocks_buildings_with_normative_buffer():
    area = synthetic_area()
    target = area.building("target")
    grid = small_grid(area, target)
    field = build_cost_field(area, grid, du_mm=80, laying=Laying.CHANNEL,
                             target_building=target, profile="min_cost")

    assert not math.isfinite(field.value_at(150.0, 110.0))       # внутри стены
    assert not math.isfinite(field.value_at(150.0, 121.0))       # в буфере 2 м
    assert math.isfinite(field.value_at(150.0, 130.0))           # за буфером
    assert field.notes["building_clearance_m"] == 2.0


def test_target_building_is_not_buffered():
    """Своё здание блокирует только пятном — к нему как раз подключаемся."""
    area = synthetic_area(wall=False)
    target = area.building("target")
    grid = small_grid(area, target)
    field = build_cost_field(area, grid, du_mm=80, laying=Laying.CHANNELLESS,
                             target_building=target, profile="min_cost")

    assert not math.isfinite(field.value_at(150.0, 30.0))        # пятно застройки
    assert math.isfinite(field.value_at(150.0, 43.0))            # 3 м от контура


def test_base_cost_equals_pipe_plus_earthwork_on_bare_ground():
    area = synthetic_area(wall=False)
    target = area.building("target")
    grid = small_grid(area, target)
    field = build_cost_field(area, grid, du_mm=80, laying=Laying.CHANNEL,
                             target_building=target, profile="min_cost")

    expected = pipe_rate(80, Laying.CHANNEL) + earthwork_rate(80) * 1.0
    assert field.value_at(20.0, 60.0) == pytest.approx(expected, rel=1e-6)


def test_utility_corridor_is_expensive_but_passable():
    """Ключевой приём §4.3: пересечь можно, идти вдоль — запретительно дорого."""
    area = synthetic_area(wall=False)
    area.utilities.append(
        Utility(id="u1", geom=LineString([(0.0, 90.0), (300.0, 90.0)]), kind=UtilityKind.WATER)
    )
    target = area.building("target")
    grid = small_grid(area, target, resolution=1.0)
    field = build_cost_field(area, grid, du_mm=80, laying=Laying.CHANNEL,
                             target_building=target, profile="min_cost")

    inside = field.value_at(150.0, 90.0)
    outside = field.value_at(150.0, 60.0)

    assert math.isfinite(inside)                 # пересечение возможно
    assert inside > outside                      # но дороже обычной прокладки

    # Свойство, ради которого веса и подбирались (docs/04-algorithm.md §4.3):
    # пересечь коридор поперёк сопоставимо с небольшим обходом, а идти вдоль —
    # заведомо дороже любого разумного обхода.
    crossing = inside * 3.5                      # пересечь коридор шириной 3,5 м
    following = inside * 200.0                   # идти вдоль 200 м

    assert crossing < outside * 30               # дешевле обхода в 30 м
    assert following > outside * 150             # дороже обхода в 150 м


def test_utility_penalty_is_not_wildly_above_the_estimate():
    """Вес поиска не должен на порядок превышать сметную цену пересечения.

    Первая калибровка (250 000 ₽/м) завышала её всемеро, и трассировщик уводил
    трассу на сотню метров в обход ради экономии ста тысяч рублей.
    """
    from app.config import get_config

    penalty = get_config().costs["zone_penalty_per_m"]["utility_offset"]
    setup = get_config().costs["crossing_setup"]["utility_crossing"]

    corridor_width_m = 3.5
    search_cost = penalty * corridor_width_m

    assert 0.5 * setup <= search_cost <= 2.5 * setup


def test_profile_min_approvals_makes_private_parcels_costlier():
    area = synthetic_area(wall=False)
    area.parcels.append(
        Parcel(id="p1", geom=box(100.0, 60.0, 200.0, 80.0), is_public=False)
    )
    target = area.building("target")
    grid = small_grid(area, target)

    cheap = build_cost_field(area, grid, du_mm=80, laying=Laying.CHANNEL,
                             target_building=target, profile="min_cost")
    careful = build_cost_field(area, grid, du_mm=80, laying=Laying.CHANNEL,
                               target_building=target, profile="min_approvals")

    assert careful.value_at(150.0, 70.0) > cheap.value_at(150.0, 70.0) * 3


# --- Спрямление -------------------------------------------------------------


def test_segment_cost_is_infinite_across_blocked_area():
    area = synthetic_area()
    target = area.building("target")
    grid = small_grid(area, target)
    field = build_cost_field(area, grid, du_mm=80, laying=Laying.CHANNEL,
                             target_building=target, profile="min_cost")

    assert not math.isfinite(segment_cost(field, (150.0, 60.0), (150.0, 160.0)))
    assert math.isfinite(segment_cost(field, (20.0, 60.0), (20.0, 160.0)))


def test_collapse_collinear_keeps_corners():
    points = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (2.0, 1.0), (2.0, 2.0)]
    assert collapse_collinear(points) == [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0)]


def test_straighten_removes_staircase_and_keeps_ends():
    area = synthetic_area(wall=False)
    target = area.building("target")
    grid = small_grid(area, target, resolution=1.0)
    field = build_cost_field(area, grid, du_mm=80, laying=Laying.CHANNEL,
                             target_building=target, profile="min_cost")

    staircase = []
    for i in range(40):
        staircase.append((20.0 + i, 60.0 + i))
        staircase.append((20.0 + i + 1, 60.0 + i))

    result = straighten(field, staircase)

    assert result[0] == staircase[0]
    assert result[-1] == staircase[-1]
    assert len(result) < len(staircase) / 4


def test_straighten_never_crosses_blocked_cells():
    area = synthetic_area()
    target = area.building("target")
    grid = small_grid(area, target, resolution=1.0)
    field = build_cost_field(area, grid, du_mm=80, laying=Laying.CHANNEL,
                             target_building=target, profile="min_cost")

    detour = [(150.0, 60.0), (20.0, 60.0), (20.0, 160.0), (150.0, 160.0)]
    result = straighten(field, detour)

    for a, b in zip(result, result[1:]):
        assert math.isfinite(segment_cost(field, a, b))


# --- Выбор точки врезки -----------------------------------------------------


def test_reconstruction_is_free_when_reserve_is_enough():
    area = synthetic_area(load=0.1)
    edge = area.heat.edges[0]
    cost, new_du = solver.reconstruction_cost(edge, 0.2)

    assert cost == 0.0 and new_du is None


def test_reconstruction_costs_money_when_reserve_is_short():
    """Резерва не хватает → перекладка на следующий диаметр, отдельной статьёй."""
    area = synthetic_area(du_mm=65, load=1.4)
    edge = area.heat.edges[0]
    assert edge.reserve_gcal_h < 0.5

    cost, new_du = solver.reconstruction_cost(edge, 0.5)

    assert cost > 0.0
    assert new_du is not None and new_du > edge.du_mm


# --- Сквозной расчёт --------------------------------------------------------


def test_solve_routes_around_the_wall():
    area = synthetic_area(wall=True)
    target = area.building("target")

    solution = solver.solve(area, target, resolution=2.0)

    assert solution is not None
    assert solution.length_m > 200.0            # прямая была бы ~150 м
    assert solution.geometry.is_valid

    wall = area.building("wall").geom.buffer(2.0)
    assert not solution.geometry.intersects(wall)


def test_solve_without_obstacles_is_close_to_straight():
    area = synthetic_area(wall=False)
    target = area.building("target")

    solution = solver.solve(area, target, resolution=2.0)

    assert solution is not None
    assert solution.length_m < 160.0            # почти прямая до сети на y=180


def test_solution_carries_hydraulics_and_cost():
    area = synthetic_area(wall=False)
    target = area.building("target")

    solution = solver.solve(area, target, resolution=2.0)

    assert solution is not None
    assert solution.hydraulics.du_mm == solution.du_mm
    assert solution.hydraulics.length_m == pytest.approx(solution.length_m)
    assert solution.cost.total_rub > 0
    assert solution.cost.laying_rub > 0
    assert solution.connection.total_rub >= solution.connection.route_rub


def test_smeta_excludes_search_penalty():
    """Веса поиска завышены ради поведения алгоритма и в смету входить не должны."""
    area = synthetic_area(wall=False)
    area.utilities.append(
        Utility(id="u1", geom=LineString([(0.0, 90.0), (300.0, 90.0)]), kind=UtilityKind.WATER)
    )
    target = area.building("target")

    solution = solver.solve(area, target, resolution=2.0)

    assert solution is not None
    assert solution.cost.search_penalty_rub > 0          # трасса пересекла коридор
    assert solution.cost.crossings_rub > 0               # и это учтено поштучно
    assert solution.cost.crossings_rub < solution.cost.search_penalty_rub
    assert solution.cost.total_rub > solution.cost.laying_rub


def test_candidates_are_ranked_by_total_cost():
    area = synthetic_area(wall=False)
    target = area.building("target")

    solution = solver.solve(area, target, resolution=2.0)

    assert solution is not None
    totals = [c.total_rub for c in solution.candidates]
    assert totals == sorted(totals)
    assert solution.connection.total_rub == totals[0]


def test_no_solution_without_load():
    area = synthetic_area(wall=False)
    target = area.building("target")
    target.load = None

    assert solver.solve(area, target, resolution=2.0) is None


def test_entry_point_lies_outside_the_building():
    area = synthetic_area(wall=False)
    target = area.building("target")

    entry = solver.entry_point_for(target, area)

    assert not target.geom.contains(entry)
    assert target.geom.distance(entry) < 4.0
