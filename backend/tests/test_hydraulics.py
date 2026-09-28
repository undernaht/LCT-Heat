"""Тесты гидравлики.

Эталон — сквозной пример из docs/04-algorithm.md §3.4. Он же используется в демо,
поэтому расхождение между документом и кодом должно ломать сборку.
"""

from __future__ import annotations

import math

import pytest

from app.domain.models import TempSchedule
from app.hydraulics import diameters, flow


# --- Формулы ---------------------------------------------------------------


def test_mass_flow_reference():
    """1 Гкал/ч при графике 150/70 даёт 12,5 т/ч — проверяемый вручную случай."""
    assert flow.mass_flow_t_h(1.0, 80.0) == pytest.approx(12.5)


def test_mass_flow_rejects_nonpositive_delta():
    with pytest.raises(ValueError):
        flow.mass_flow_t_h(1.0, 0.0)


def test_friction_factor_shifrinson():
    """λ = 0,11·(kэ/d)^0.25 при kэ = 0,0005 и d = 0,08 м."""
    assert flow.friction_factor(0.08, 0.0005) == pytest.approx(0.11 * (0.0005 / 0.08) ** 0.25)


def test_velocity_matches_continuity():
    """Скорость обязана удовлетворять уравнению неразрывности."""
    g_t_h, d_m, rho = 9.5, 0.08, 960.0
    v = flow.velocity_m_s(g_t_h, d_m, rho)
    area = math.pi * d_m**2 / 4
    assert v * area * rho == pytest.approx(g_t_h * 1000 / 3600)


def test_specific_loss_scales_with_square_of_flow():
    """R ∝ v² — на этом построен расчёт пропускной способности."""
    r1 = flow.specific_pressure_loss_pa_m(1.0, 0.08)
    r2 = flow.specific_pressure_loss_pa_m(2.0, 0.08)
    assert r2 / r1 == pytest.approx(4.0)


def test_route_loss_counts_both_pipes():
    """Подающий и обратный: ×2. Забыть эту двойку — классическая ошибка."""
    single = flow.route_pressure_loss_pa(50.0, 100.0, 0.0, two_pipe=False)
    double = flow.route_pressure_loss_pa(50.0, 100.0, 0.0, two_pipe=True)
    assert double == pytest.approx(2 * single)
    assert single == pytest.approx(5000.0)


# --- Сквозной пример из docs/04-algorithm.md §3.4 ---------------------------

REFERENCE_Q = 0.464         # Гкал/ч, 9-этажный жилой дом нового строительства
REFERENCE_SCHEDULE = TempSchedule(130, 70)
REFERENCE_LENGTH = 214.0    # м


def test_reference_example_matches_docs():
    result = diameters.check_route(REFERENCE_Q, REFERENCE_SCHEDULE, REFERENCE_LENGTH)

    assert result is not None
    assert result.du_mm == 80
    assert result.g_t_h == pytest.approx(7.73, abs=0.05)
    assert result.v_m_s == pytest.approx(0.45, abs=0.02)
    assert result.r_pa_m == pytest.approx(37, abs=2)
    assert result.dp_bar == pytest.approx(0.20, abs=0.02)


def test_reference_rejects_smaller_diameter_by_pressure_loss():
    """Ду65 отклоняется по удельным потерям, а не по скорости."""
    choice = diameters.select_diameter(REFERENCE_Q, REFERENCE_SCHEDULE)

    assert choice.selected is not None and choice.selected.du_mm == 80
    rejected = {state.du_mm: reason for state, reason in choice.rejected}
    assert 65 in rejected
    assert "удельные потери" in rejected[65]
    # Скорость на отклонённых диаметрах остаётся в норме — ограничение неактивно
    assert all(state.v_m_s < 3.5 for state, _ in choice.rejected)


def test_head_check_fails_on_long_route():
    """При недостаточном располагаемом напоре трасса не проходит."""
    ok = diameters.check_route(REFERENCE_Q, REFERENCE_SCHEDULE, 214.0, head_available_m=30.0)
    too_long = diameters.check_route(REFERENCE_Q, REFERENCE_SCHEDULE, 3000.0, head_available_m=30.0)

    assert ok is not None and ok.head_ok
    assert too_long is not None and not too_long.head_ok


def test_head_check_reserves_pressure_for_the_consumer():
    """На вводе обязан остаться напор для ИТП.

    Без этого проверка вырождается: «напора хватило» означало бы, что до здания
    теплоноситель доходит с нулевым располагаемым напором и внутренняя система
    не работает.
    """
    result = diameters.check_route(
        REFERENCE_Q, REFERENCE_SCHEDULE, 214.0, head_available_m=30.0
    )

    assert result is not None
    assert result.head_required_at_consumer_m > 0
    assert result.head_reserve_m == pytest.approx(
        30.0 - result.head_m - result.head_required_at_consumer_m, abs=0.01
    )

    # Напора хватает на трассу, но не на трассу плюс ИТП — это отказ
    tight = diameters.check_route(
        REFERENCE_Q, REFERENCE_SCHEDULE, 214.0,
        head_available_m=result.head_m + 1.0,
    )
    assert tight is not None and not tight.head_ok


# --- Пропускная способность ------------------------------------------------


def test_capacity_is_consistent_with_selection():
    """Нагрузка на пределе пропускной способности даёт ровно этот же диаметр."""
    schedule = TempSchedule(130, 70)
    cap = diameters.capacity_gcal_h(100, schedule, is_branch=False)
    state = diameters.flow_state(cap, schedule, 100)

    assert state.r_pa_m == pytest.approx(80.0, rel=1e-6)


def test_capacity_grows_with_diameter():
    schedule = TempSchedule(130, 70)
    caps = [diameters.capacity_gcal_h(du, schedule) for du in (50, 80, 100, 150, 200)]
    assert caps == sorted(caps)
