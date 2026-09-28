"""Тесты ручной правки трассы.

Главное требование: ручной трассе никаких поблажек. Если она задевает запретную
зону, отрывается от здания или не доходит до сети — об этом должно быть сказано,
а не «посчитано как есть».
"""

from __future__ import annotations

import pytest
from shapely.geometry import LineString

from app.domain.enums import Laying
from app.routing import manual, solver

from .test_routing import synthetic_area


@pytest.fixture(scope="module")
def scene():
    area = synthetic_area(wall=True)
    return area, area.building("target")


def evaluate(area, building, coords, **kwargs):
    return manual.evaluate(area, building, LineString(coords), resolution=2.0, **kwargs)


# --- Счастливый путь --------------------------------------------------------


def test_good_manual_route_is_admissible(scene):
    """Трасса в обход стены: от здания влево, вверх и к сети."""
    area, building = scene
    result = evaluate(area, building, [
        (150.0, 45.0), (20.0, 45.0), (20.0, 180.0), (150.0, 180.0),
    ])

    assert result is not None
    assert result.admissible, [i.detail for i in result.issues]
    assert result.length_m > 300
    assert result.du_mm >= 32
    assert result.cost.total_rub > 0
    assert result.tap_edge_id == "he_1"


def test_manual_route_gets_the_same_engineering_numbers(scene):
    """Те же модули, что и для расчётной: диаметр, потери, напор, плата."""
    area, building = scene
    result = evaluate(area, building, [
        (150.0, 45.0), (20.0, 45.0), (20.0, 180.0), (150.0, 180.0),
    ])

    assert result.hydraulics.du_mm == result.du_mm
    assert result.hydraulics.length_m == pytest.approx(result.length_m, abs=0.5)
    assert result.hydraulics.head_known is True
    assert result.fee_total_with_vat_rub > 0
    assert result.protocol.checks_total >= 0
    assert result.schedule == "130/70"


def test_longer_manual_route_costs_more_than_the_optimum(scene):
    """Ручное решение можно принять, но его цена должна быть видна."""
    area, building = scene
    optimal = solver.solve(area, building, resolution=2.0)
    detour = evaluate(area, building, [
        (150.0, 45.0), (20.0, 45.0), (20.0, 180.0), (280.0, 180.0), (150.0, 180.0),
    ])

    detour.reference_cost_rub = optimal.cost.total_rub
    detour.reference_length_m = optimal.length_m

    assert detour.length_m > optimal.length_m
    assert detour.cost_delta_rub > 0
    assert detour.cost_delta_share > 0


# --- Проверки геометрии -----------------------------------------------------


def test_route_crossing_a_building_is_flagged(scene):
    """Прямая через стену — самая частая правка «на глаз»."""
    area, building = scene
    result = evaluate(area, building, [(150.0, 45.0), (150.0, 180.0)])

    assert not result.admissible
    kinds = {issue.kind for issue in result.issues}
    assert "crosses_blocked" in kinds
    blocked = next(i for i in result.issues if i.kind == "crosses_blocked")
    assert "запретной зоне" in blocked.detail


def test_route_detached_from_the_building_is_flagged(scene):
    area, building = scene
    result = evaluate(area, building, [
        (20.0, 45.0), (20.0, 180.0), (150.0, 180.0),
    ])

    kinds = {issue.kind for issue in result.issues}
    assert "detached_start" in kinds
    assert not result.admissible


def test_route_not_reaching_the_network_is_flagged(scene):
    area, building = scene
    result = evaluate(area, building, [
        (150.0, 45.0), (20.0, 45.0), (20.0, 120.0),
    ])

    kinds = {issue.kind for issue in result.issues}
    assert "detached_end" in kinds
    assert not result.admissible


def test_self_intersecting_route_is_flagged(scene):
    """Труба не может проходить сквозь себя."""
    area, building = scene
    result = evaluate(area, building, [
        (150.0, 45.0), (20.0, 45.0), (20.0, 180.0),
        (60.0, 180.0), (60.0, 20.0), (10.0, 20.0), (10.0, 180.0), (150.0, 180.0),
    ])

    assert "self_intersect" in {issue.kind for issue in result.issues}


def test_degenerate_route_is_refused(scene):
    area, building = scene
    result = evaluate(area, building, [(150.0, 45.0), (150.0, 47.0)])

    assert "degenerate" in {issue.kind for issue in result.issues}
    assert not result.admissible


def test_building_without_load_gives_nothing(scene):
    area, building = scene
    original = building.load
    building.load = None
    try:
        assert evaluate(area, building, [(150.0, 45.0), (150.0, 180.0)]) is None
    finally:
        building.load = original


# --- Подтягивание концов ----------------------------------------------------


def test_start_is_snapped_to_the_building(scene):
    """Промах курсора на метр — не ошибка проектировщика."""
    area, building = scene
    drawn = LineString([(150.0, 48.0), (20.0, 48.0), (20.0, 180.0), (150.0, 180.0)])

    snapped = manual.snap_to_building(area, building, drawn)
    entry = solver.entry_point_for(building, area)

    assert snapped.coords[0] == pytest.approx((entry.x, entry.y), abs=0.01)
    assert snapped.coords[-1] == drawn.coords[-1]


def test_end_is_snapped_to_the_network(scene):
    area, _ = scene
    drawn = LineString([(20.0, 45.0), (20.0, 175.0), (150.0, 175.0)])

    snapped = manual.snap_to_network(area, drawn)

    assert snapped.coords[-1][1] == pytest.approx(180.0, abs=0.01)
    assert area.heat.edges[0].geom.distance(
        LineString(snapped.coords[-2:]).interpolate(1.0, normalized=True)
    ) == pytest.approx(0.0, abs=0.05)


def test_far_end_is_not_snapped(scene):
    """Подтягивать через полрайона нельзя — это уже не промах курсора."""
    area, _ = scene
    drawn = LineString([(20.0, 45.0), (20.0, 100.0)])

    assert manual.snap_to_network(area, drawn).coords[-1] == drawn.coords[-1]


# --- Нормоконтроль ----------------------------------------------------------


def test_manual_route_along_a_utility_is_caught(scene):
    """Ручная трасса не получает поблажек по нормативам."""
    from app.domain.enums import UtilityKind
    from app.domain.models import Utility

    area, building = scene
    area.utilities.append(
        Utility(id="u_manual", geom=LineString([(0.0, 60.0), (300.0, 60.0)]),
                kind=UtilityKind.WATER, depth_m=2.5)
    )
    try:
        # Идём вдоль водопровода в метре от него — норматив 1,5 м
        result = evaluate(area, building, [
            (150.0, 61.0), (20.0, 61.0), (20.0, 180.0), (150.0, 180.0),
        ])
        violations = [c for c in result.protocol.checks if c.target_id == "u_manual"]
        assert violations
        assert violations[0].kind == "horizontal"
        assert violations[0].actual_m < violations[0].required_m
    finally:
        area.utilities.pop()


def test_channelless_laying_uses_a_wider_buffer(scene):
    """Бесканальная прокладка требует 5,0 м от фундамента вместо 2,0."""
    area, building = scene
    coords = [(150.0, 45.0), (20.0, 45.0), (20.0, 180.0), (150.0, 180.0)]

    channel = evaluate(area, building, coords, laying=Laying.CHANNEL)
    channelless = evaluate(area, building, coords, laying=Laying.CHANNELLESS)

    assert channel.cost.total_rub != channelless.cost.total_rub
    assert channelless.laying is Laying.CHANNELLESS
