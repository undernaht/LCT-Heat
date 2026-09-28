"""Тесты протокола нормоконтроля.

Проверяется главное различение: идти вдоль чужой сети ближе норматива нельзя,
а пересекать — можно, разойдясь по вертикали. Ошибка здесь означала бы либо
пропущенное нарушение, либо ложный отказ на каждом пересечении.
"""

from __future__ import annotations

import pytest
from shapely.geometry import LineString, box

from app.compliance import report
from app.compliance.rules import RuleContext
from app.domain.enums import BuildingUse, ComplianceStatus, Laying, UtilityKind
from app.domain.models import AreaModel, Building, HeatLoad, Road, Utility
from app.domain.enums import SurfaceClass

CTX = RuleContext(Laying.CHANNEL, du_mm=80)


def area_with(*utilities: Utility, roads: list[Road] | None = None) -> AreaModel:
    area = AreaModel(crs="EPSG:32637")
    area.utilities.extend(utilities)
    area.roads.extend(roads or [])
    return area


def water(depth_m: float = 2.5) -> Utility:
    return Utility(
        id="u_water", geom=LineString([(0.0, 100.0), (300.0, 100.0)]),
        kind=UtilityKind.WATER, depth_m=depth_m,
    )


def power(depth_m: float = 0.8) -> Utility:
    return Utility(
        id="u_power", geom=LineString([(0.0, 100.0), (300.0, 100.0)]),
        kind=UtilityKind.POWER, depth_m=depth_m, voltage_kv=10.0,
    )


# --- Глубины ----------------------------------------------------------------


def test_channel_sits_higher_and_thicker_than_channelless():
    channel_top, channel_bottom = report.pipe_depth_range(RuleContext(Laying.CHANNEL, 80))
    less_top, less_bottom = report.pipe_depth_range(RuleContext(Laying.CHANNELLESS, 80))

    assert channel_top == 0.5 and less_top == 0.7          # табл. А.1, прим. 1
    assert channel_bottom - channel_top > less_bottom - less_top


def test_vertical_clearance_below_above_and_collision():
    ctx = RuleContext(Laying.CHANNEL, 80)                  # верх 0,50, низ 1,18

    assert report.vertical_clearance(ctx, 2.5) == pytest.approx(1.32, abs=0.01)
    assert report.vertical_clearance(ctx, 0.2) == pytest.approx(0.30, abs=0.01)
    assert report.vertical_clearance(ctx, 0.8) == 0.0      # внутри конструкции


# --- Разрешение пересечений -------------------------------------------------


def test_crossing_passes_when_naturally_clear():
    actual, status, note = report.resolve_crossing(CTX, other_depth_m=2.5, required_m=0.2)

    assert status is ComplianceStatus.PASS
    assert note is None
    assert actual > 0.2


def test_crossing_conflict_is_conditional_with_required_deepening():
    """Конфликт по вертикали решается заглублением, а не переносом трассы."""
    actual, status, note = report.resolve_crossing(CTX, other_depth_m=0.8, required_m=0.5)

    assert actual == 0.0
    assert status is ComplianceStatus.CONDITIONAL
    assert "заглубление" in note and "0.80" in note


def test_crossing_fails_when_deepening_is_unreasonable():
    """Крупный диаметр: конструкция толще, требуемое заглубление выходит за предел.

    Окно, в котором пересечение неразрешимо, узкое, и это не случайность:
    просвет меньше нормы возможен, только пока чужая сеть близко к нашей
    конструкции, а это само по себе ограничивает требуемое заглубление сверху
    величиной `высота конструкции + 2 × норматив`. Для Ду80 это 1,68 м — всегда
    меньше предела 2,5 м, поэтому пересечения мелких сетей разрешимы всегда.
    """
    huge = RuleContext(Laying.CHANNEL, du_mm=1000)          # верх 0,50, низ 2,10
    _, status, note = report.resolve_crossing(huge, other_depth_m=2.55, required_m=0.5)

    assert status is ComplianceStatus.FAIL
    assert "глубоко" in note


def test_crossing_shallow_utility_is_always_resolvable():
    """Для обычных диаметров пересечение чужой сети решается всегда."""
    for depth in (0.6, 0.9, 1.2, 1.6, 2.0):
        _, status, _ = report.resolve_crossing(CTX, other_depth_m=depth, required_m=0.5)
        assert status is not ComplianceStatus.FAIL


# --- Следование против пересечения ------------------------------------------


def test_parallel_run_close_to_water_is_a_violation():
    area = area_with(water())
    route = LineString([(10.0, 101.0), (200.0, 101.0)])     # 1,0 м при норме 1,5

    result = report.check_route(area, route, CTX)

    assert result.status is not ComplianceStatus.PASS
    violation = next(c for c in result.checks if c.target_id == "u_water")
    assert violation.kind == "horizontal"
    assert violation.required_m == 1.5
    assert violation.actual_m == pytest.approx(1.0, abs=0.05)


def test_parallel_run_far_below_norm_is_a_hard_failure():
    area = area_with(water())
    route = LineString([(10.0, 100.4), (200.0, 100.4)])     # 0,4 м при норме 1,5

    result = report.check_route(area, route, CTX)
    assert result.status is ComplianceStatus.FAIL


def test_perpendicular_crossing_is_not_a_horizontal_violation():
    """Пересекать чужую сеть можно — по горизонтали это не проверяется."""
    area = area_with(water())
    route = LineString([(150.0, 40.0), (150.0, 160.0)])

    result = report.check_route(area, route, CTX)

    water_checks = [c for c in result.checks if c.target_id == "u_water"]
    assert all(c.kind != "horizontal" for c in water_checks)
    assert result.status is ComplianceStatus.PASS


def test_crossing_a_shallow_cable_is_reported_as_conditional():
    area = area_with(power())
    route = LineString([(150.0, 40.0), (150.0, 160.0)])

    result = report.check_route(area, route, CTX)

    check = next(c for c in result.checks if c.target_id == "u_power")
    assert check.kind == "vertical"
    assert check.status is ComplianceStatus.CONDITIONAL
    assert "заглубление" in check.note


def test_short_graze_near_a_crossing_is_not_treated_as_following():
    """Подход к пересечению неизбежно проходит вплотную — это не следование вдоль."""
    area = area_with(water())
    route = LineString([(150.0, 40.0), (152.0, 100.0), (154.0, 160.0)])

    result = report.check_route(area, route, CTX)

    assert all(c.kind != "horizontal" for c in result.checks if c.target_id == "u_water")


def test_long_run_inside_corridor_is_following_even_without_intersection():
    area = area_with(water())
    route = LineString([(10.0, 101.2), (120.0, 101.2)])     # 110 м вдоль, не пересекая

    result = report.check_route(area, route, CTX)

    check = next(c for c in result.checks if c.target_id == "u_water")
    assert check.kind == "horizontal"
    assert check.end_m - check.start_m > 100.0


# --- Бортовой камень --------------------------------------------------------


def road(name: str = "Улица") -> Road:
    return Road(
        id="r1", geom=LineString([(0.0, 100.0), (300.0, 100.0)]),
        surface_class=SurfaceClass.STREET_LOCAL, width_m=7.0, name=name,
    )


def test_crossing_a_street_does_not_violate_kerb_clearance():
    """Ложное срабатывание, из-за которого проверка и была переписана."""
    area = area_with(roads=[road()])
    route = LineString([(150.0, 60.0), (150.0, 140.0)])

    result = report.check_route(area, route, CTX)

    kerb_checks = [c for c in result.checks if c.rule_id == "transport.kerb"]
    assert all(c.kind != "horizontal" for c in kerb_checks)


def test_running_along_the_kerb_too_close_is_a_violation():
    area = area_with(roads=[road()])
    route = LineString([(20.0, 104.0), (200.0, 104.0)])      # 0,5 м от кромки при норме 1,5

    result = report.check_route(area, route, CTX)

    check = next(c for c in result.checks if c.rule_id == "transport.kerb")
    assert check.kind == "horizontal"
    assert check.actual_m == pytest.approx(0.5, abs=0.05)


# --- Здания -----------------------------------------------------------------


def test_building_clearance_violation_is_caught():
    area = AreaModel(crs="EPSG:32637")
    area.buildings.append(
        Building(id="b1", geom=box(100.0, 100.0, 140.0, 140.0),
                 use=BuildingUse.RESIDENTIAL, floors=5, load=HeatLoad(heating=0.5))
    )
    route = LineString([(50.0, 99.0), (200.0, 99.0)])        # 1,0 м при норме 2,0

    result = report.check_route(area, route, CTX)

    check = next(c for c in result.checks if c.target_id == "b1")
    assert check.actual_m == pytest.approx(1.0, abs=0.05)
    assert check.status is ComplianceStatus.CONDITIONAL      # 1,0 ≥ 2,0 × 0,5


def test_target_building_is_excluded_from_checks():
    area = AreaModel(crs="EPSG:32637")
    target = Building(id="target", geom=box(100.0, 100.0, 140.0, 140.0),
                      use=BuildingUse.RESIDENTIAL, floors=9, is_perspective=True,
                      load=HeatLoad(heating=0.4))
    area.buildings.append(target)
    route = LineString([(50.0, 99.0), (200.0, 99.0)])

    result = report.check_route(area, route, CTX, target_building=target)
    assert not [c for c in result.checks if c.target_id == "target"]


# --- Формирование протокола -------------------------------------------------


def test_protocol_keeps_only_the_tightest_passing_check_per_rule():
    """Из проходящих проверок в протокол идёт одна — с минимальным запасом.

    Иначе протокол на реальном районе — это сотни строк «до дома 40 м при норме
    2 м», в которых теряется единственное, что важно согласующему инженеру.
    """
    area = AreaModel(crs="EPSG:32637")
    for index, offset in enumerate((2.5, 3.0, 40.0)):
        area.buildings.append(
            Building(id=f"b{index}", geom=box(100.0, 100.0 + offset, 140.0, 140.0 + offset),
                     use=BuildingUse.RESIDENTIAL, floors=5, load=HeatLoad(heating=0.5))
        )
    route = LineString([(50.0, 100.0), (200.0, 100.0)])

    result = report.check_route(area, route, CTX)

    assert result.checks_total == 2     # дальнее здание даже не проверяется
    reported = {c.target_id for c in result.checks}
    assert reported == {"b0"}           # 2,5 м — самый тугой запас при норме 2,0


def test_status_is_worst_of_all_checks():
    area = area_with(water())
    clean = report.check_route(area, LineString([(10.0, 60.0), (200.0, 60.0)]), CTX)
    conditional = report.check_route(area, LineString([(10.0, 101.0), (200.0, 101.0)]), CTX)
    failing = report.check_route(area, LineString([(10.0, 100.4), (200.0, 100.4)]), CTX)

    assert clean.status is ComplianceStatus.PASS
    assert conditional.status is ComplianceStatus.CONDITIONAL
    assert failing.status is ComplianceStatus.FAIL


def test_check_reports_position_along_the_route():
    area = area_with(water())
    route = LineString([(10.0, 101.0), (200.0, 101.0)])

    check = next(c for c in report.check_route(area, route, CTX).checks
                 if c.target_id == "u_water")

    assert 0.0 <= check.start_m < check.end_m <= route.length + 1.0
    assert "м" in check.where


def test_every_reported_check_cites_a_clause():
    area = area_with(water(), power())
    route = LineString([(150.0, 40.0), (150.0, 160.0)])

    for check in report.check_route(area, route, CTX).checks:
        assert check.clause
        assert check.rule_id
