"""Правила приложения: таблицы и формулы сходятся с первоисточником."""

from __future__ import annotations

import pytest

from app.case.rules import get_rules


@pytest.fixture(scope="module")
def rules():
    return get_rules()


def test_score_matches_example_from_appendix(rules):
    """§7.3 приложения: 100 м Ду100 + врезка = 13 974 800 ₽, score 0,6913."""
    cost = 100 * rules.diameter(100).cost_per_m + rules.tie_in_cost
    assert cost == pytest.approx(13_974_800)
    assert round(rules.score(cost, 100.0), 4) == 0.6913


def test_diameter_table_is_monotone(rules):
    dus = [d.du for d in rules.diameters]
    assert dus == sorted(dus)
    caps = [d.capacity_tph for d in rules.diameters]
    limits = [d.limit_length_m for d in rules.diameters]
    widths = [d.width_m for d in rules.diameters]
    assert caps == sorted(caps) and limits == sorted(limits) and widths == sorted(widths)


def test_du_for_flow_picks_minimum(rules):
    assert rules.du_for_flow(3.5).du == 50
    assert rules.du_for_flow(3.51).du == 65
    assert rules.du_for_flow(22.3).du == 100
    assert rules.du_for_flow(488.72).du == 400
    assert rules.du_for_flow(30_000) is None


def test_du_for_flow_and_length_respects_limit(rules):
    # 16 т/ч → Ду100 по расходу, но 480 м > 419 → Ду125
    assert rules.du_for_flow_and_length(16.0, 480.0).du == 125
    assert rules.du_for_flow_and_length(16.0, 400.0).du == 100


def test_chamber_cost_bands(rules):
    assert rules.chamber_cost(50) == 3_000_000
    assert rules.chamber_cost(200) == 3_000_000
    assert rules.chamber_cost(250) == 5_000_000
    assert rules.chamber_cost(500) == 5_000_000
    assert rules.chamber_cost(1000) == 8_000_000
    assert rules.chamber_cost(1400) == 12_000_000


def test_penalty_formula(rules):
    assert rules.penalty(0.0) == 100_000_000
    assert rules.penalty(76.27) == pytest.approx(100_000_000 + 500_000 * 76.27)


def test_oks_clearance_depends_on_du(rules):
    rule = rules.restriction("oks")
    assert rule.clearance_for(100) == 5.0
    assert rule.clearance_for(500) == 7.0
    assert rule.clearance_for(900) == 9.0


def test_special_rules_from_table_2(rules):
    road = rules.restriction("road")
    assert (road.k_special, road.margin_m, road.min_angle_deg, road.clearance_m) == (1.60, 3.0, 45.0, 1.5)
    gas = rules.restriction("gas_pipeline")
    assert (gas.k_special, gas.margin_m, gas.gauge_width_m, gas.gauge_top_depth_m) == (1.25, 2.0, 0.40, 2.8)
    heat = rules.restriction("heat_network")
    assert heat.k_special == 1.05 and heat.gauge_by_diameter


def test_score_per_metre_orders_diameters(rules):
    """Метр ствола дороже метра ветки, но не в разы: длина весит столько же, сколько стоимость."""
    assert rules.score_per_m(100) < rules.score_per_m(200) < rules.score_per_m(400)
    assert rules.score_per_m(100) == pytest.approx(0.7 * 89_748 / 25e6 + 0.3 / 100)


def test_k_depth(rules):
    assert rules.k_depth(3.0) == 1.0
    assert rules.k_depth(2.0) == 1.0
    assert rules.k_depth(4.0) == pytest.approx(1.10)
