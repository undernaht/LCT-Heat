"""Тесты подключения группы зданий общим коридором.

Проверяется то, ради чего задача и решается: общий коридор дешевле суммы
отдельных вводов, нагрузка агрегируется вверх по дереву, а диаметр общего
участка не меньше диаметров веток.
"""

from __future__ import annotations

import pytest
from shapely.geometry import LineString, box

from app.domain.enums import BuildingUse, HeatNodeKind, Laying
from app.domain.models import (
    AreaModel,
    Building,
    HeatEdge,
    HeatLoad,
    HeatNetwork,
    HeatNode,
    TempSchedule,
)
from app.hydraulics import diameters
from app.routing import group

SCHEDULE = TempSchedule(130, 70)


def area_with_buildings(*positions: tuple[float, float, float]) -> AreaModel:
    """Сеть вдоль y = 400, здания задаются как (x, y, нагрузка)."""
    area = AreaModel(crs="EPSG:32637")

    network = HeatNetwork()
    network.nodes.append(
        HeatNode(id="hn_a", geom=__import__("shapely.geometry", fromlist=["Point"]).Point(0.0, 400.0),
                 kind=HeatNodeKind.SOURCE, name="Котельная", head_available_m=40.0)
    )
    capacity = diameters.capacity_gcal_h(300, SCHEDULE, is_branch=False)
    network.edges.append(
        HeatEdge(id="he_1", geom=LineString([(0.0, 400.0), (600.0, 400.0)]), du_mm=300,
                 laying=Laying.CHANNEL, schedule=SCHEDULE, node_a="hn_a",
                 load_gcal_h=1.0, capacity_gcal_h=capacity)
    )
    area.heat = network

    for index, (x, y, load) in enumerate(positions):
        area.buildings.append(
            Building(
                id=f"p{index + 1}",
                geom=box(x - 15, y - 15, x + 15, y + 15),
                use=BuildingUse.RESIDENTIAL,
                floors=9,
                is_perspective=True,
                load=HeatLoad(heating=load * 0.75, dhw_max=load * 0.25),
            )
        )
    return area


def solve(area: AreaModel, **kwargs):
    return group.solve_group(
        area, [b for b in area.buildings if b.is_perspective], resolution=4.0, **kwargs
    )


# --- Совместный коридор -----------------------------------------------------


def test_close_buildings_share_a_corridor():
    """Два здания рядом далеко от сети — второе врезается в ветку первого."""
    area = area_with_buildings((300.0, 60.0, 0.8), (360.0, 60.0, 0.5))
    result = solve(area)

    assert result is not None
    assert any(c.tap_kind == "branch" for c in result.connections)
    assert any(segment.is_shared for segment in result.segments)


def test_distant_buildings_connect_separately():
    """Здания в разных концах района общего коридора не дают — и это верно."""
    area = area_with_buildings((60.0, 60.0, 0.8), (560.0, 60.0, 0.5))
    result = solve(area)

    assert result is not None
    assert all(c.tap_kind == "network" for c in result.connections)


def test_sharing_saves_money_and_length():
    area = area_with_buildings((300.0, 60.0, 0.8), (360.0, 60.0, 0.5))
    result = solve(area)

    assert result is not None
    assert result.total_length_m < result.independent_length_m
    assert result.saving_rub > 0
    assert 0 < result.saving_share < 1


def test_group_reduces_connection_fee():
    """Плата за подключение зависит от протяжённости — экономия видна и в ней."""
    area = area_with_buildings((300.0, 60.0, 0.8), (360.0, 60.0, 0.5))
    result = solve(area)

    assert result is not None
    assert result.fee_total_with_vat_rub < result.independent_fee_rub


# --- Агрегация нагрузок -----------------------------------------------------


def test_shared_segment_carries_the_sum_of_loads():
    area = area_with_buildings((300.0, 60.0, 0.8), (360.0, 60.0, 0.5))
    result = solve(area)

    assert result is not None
    shared = [s for s in result.segments if s.is_shared]
    assert shared

    total = sum(b.load.total for b in area.buildings if b.is_perspective)
    assert max(s.load_gcal_h for s in shared) == pytest.approx(total, rel=0.02)


def test_trunk_diameter_is_not_smaller_than_branches():
    area = area_with_buildings((300.0, 60.0, 0.9), (360.0, 60.0, 0.7))
    result = solve(area)

    assert result is not None
    shared = [s for s in result.segments if s.is_shared]
    branches = [s for s in result.segments if not s.is_shared]
    assert shared and branches
    assert max(s.du_mm for s in shared) >= max(s.du_mm for s in branches)


def test_every_building_is_connected():
    area = area_with_buildings((280.0, 60.0, 0.6), (330.0, 70.0, 0.5), (380.0, 60.0, 0.4))
    result = solve(area)

    assert result is not None
    connected = {c.building_id for c in result.connections}
    assert connected == {"p1", "p2", "p3"}


def test_segments_cover_the_whole_tree():
    area = area_with_buildings((300.0, 60.0, 0.8), (360.0, 60.0, 0.5))
    result = solve(area)

    assert result is not None
    paths_length = sum(c.path.length for c in result.connections)
    assert result.total_length_m == pytest.approx(paths_length, rel=0.02)


def test_largest_consumer_defines_the_trunk():
    """Порядок подключения — по убыванию нагрузки: магистраль задаёт крупнейший."""
    area = area_with_buildings((300.0, 60.0, 0.4), (360.0, 60.0, 0.9))
    result = solve(area)

    assert result is not None
    assert result.connections[0].building_id == "p2"      # 0,9 Гкал/ч подключается первым


# --- Нормоконтроль ----------------------------------------------------------


def test_compliance_is_computed_per_branch():
    """Склейка дерева в одну ломаную давала фиктивный отрезок через полрайона
    и десятки ложных нарушений. Ветки проверяются по отдельности."""
    area = area_with_buildings((100.0, 60.0, 0.6), (500.0, 60.0, 0.6))
    result = solve(area)

    assert result is not None
    assert result.compliance_status in {"pass", "conditional"}
    assert result.compliance_failures == 0


def test_empty_group_returns_none():
    area = area_with_buildings()
    assert group.solve_group(area, [], resolution=4.0) is None


def test_group_without_load_returns_none():
    area = area_with_buildings((300.0, 60.0, 0.8))
    for building in area.buildings:
        building.load = None

    assert solve(area) is None
