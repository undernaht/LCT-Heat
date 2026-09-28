"""Локальный поиск: снимок/откат сети, снятие ветки, перестройка при остальных как есть."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from shapely.geometry import Point

from app.case import improve
from app.case.field import build_field
from app.case.model import parse_case
from app.case.network import NODE_CHAMBER_NEW, NODE_OKS, Network
from app.case.rules import get_rules
from app.case.solve import _orders, build_variant
from app.case.targets import approach_targets
from app.case.tree import TreeBuilder
from tests.test_case_solve import basic_area, box, feature, m

# --- сеть: примитивы ---------------------------------------------------------------------


def _chain() -> tuple[Network, str, str, str]:
    """ОКС — камера (проходная) — камера врезки: два участка под прямым углом."""
    net = Network()
    oks = net.add_node((0.0, 0.0), NODE_OKS, ref="p")
    mid = net.add_node((10.0, 0.0), NODE_CHAMBER_NEW)
    root = net.add_node((10.0, 20.0), NODE_CHAMBER_NEW, existing_connections=2)
    root.on_edge = object()  # точка присоединения
    net.add_segment([(0.0, 0.0), (10.0, 0.0)], oks.id, mid.id)
    net.add_segment([(10.0, 0.0), (10.0, 20.0)], mid.id, root.id)
    return net, oks.id, mid.id, root.id


def test_snapshot_restore_is_independent_of_later_edits():
    net, oks, mid, root = _chain()
    snap = net.snapshot()
    net.segments[next(iter(net.segments))].points.append((99.0, 99.0))
    net.remove_segment(next(iter(net.segments)))
    net.restore(snap)
    assert len(net.segments) == 2
    assert all(len(s.points) == 2 for s in net.segments.values())
    assert net.nodes[oks].kind == NODE_OKS


def test_merge_through_joins_two_segments_and_drops_node():
    net, oks, mid, root = _chain()
    merged = net.merge_through(mid)
    assert mid not in net.nodes
    assert len(net.segments) == 1
    assert merged.points == [(0.0, 0.0), (10.0, 0.0), (10.0, 20.0)]
    assert {merged.start, merged.end} == {oks, root}


def test_step_is_stamped_on_segments():
    net = Network()
    a = net.add_node((0.0, 0.0), NODE_OKS)
    b = net.add_node((1.0, 0.0), NODE_CHAMBER_NEW)
    net.step = 3
    seg = net.add_segment([(0.0, 0.0), (1.0, 0.0)], a.id, b.id)
    assert seg.step == 3


# --- строитель: снятие ветки и перестройка ---------------------------------------------


def _two_points_area() -> dict:
    """Два здания друг за другом восточнее магистрали: ближнее (p1) и дальнее (p2).

    В порядке «дальняя первой» p2 тянет ветку к магистрали мимо p1, а p1 потом
    строит свою; перестройка ветки p2 при готовой ветке p1 должна найти
    присоединение к ней (общий участок дешевле двух параллельных).
    """
    return basic_area([
        feature(box(300, -40, 330, 40), id="b2", object_type="restriction", restriction_type="oks"),
        feature(Point(*m(310, 0)), id="p2", object_type="oks_connection_point", flow_tph=20.0),
    ])


def _builder(data: dict):
    case = parse_case(data, name="test")
    rules = get_rules()
    field = build_field(case, rules=rules)
    targets = approach_targets(case, field)
    return case, field, targets


def test_detach_removes_own_branch_and_keeps_the_rest():
    case, field, targets = _builder(_two_points_area())
    ordered = _orders(targets, field)["nearest_first"]
    builder = TreeBuilder(case, field)
    builder.build(ordered)
    net = builder.net
    assert not builder.unconnected
    before_segments = len(net.segments)
    p2 = next(n for n in net.oks_nodes() if n.ref == "p2")
    own = builder._own_branch(p2)
    assert own, "у точки есть собственная ветка"

    builder._detach(p2)
    assert p2.id not in net.nodes
    assert all(s.id not in net.segments for s in own)
    assert len(net.segments) < before_segments
    # Ветка p1 и её точка присоединения остались
    assert any(n.ref == "p1" for n in net.oks_nodes())
    assert net.tie_in_nodes()
    # Проходных новых камер после снятия нет — участки слились
    for node in net.nodes.values():
        if node.kind == NODE_CHAMBER_NEW and node.on_edge is None:
            assert net.degree(node.id) != 2


def test_rebuild_leaf_restores_everything_when_no_gain():
    case, field, targets = _builder(basic_area())
    ordered = _orders(targets, field)["nearest_first"]
    builder = TreeBuilder(case, field)
    builder.build(ordered)
    net = builder.net
    p1 = next(n for n in net.oks_nodes() if n.ref == "p1")
    ids_before = (set(net.nodes), set(net.segments))
    points_before = {s.id: list(s.points) for s in net.segments.values()}
    score_before = builder.proxy_score()

    accepted, before, after = builder.rebuild_leaf(p1.id, ordered[0])
    assert not accepted
    assert before == score_before
    assert (set(net.nodes), set(net.segments)) == ids_before
    assert {s.id: list(s.points) for s in net.segments.values()} == points_before
    assert p1.id in builder.joins and p1.id in builder.flow_by_oks_node


def test_rebuild_branches_keeps_tree_valid_and_never_worse():
    case, field, targets = _builder(_two_points_area())
    orders = _orders(targets, field)
    far_first = orders["farthest_first"]
    builder = TreeBuilder(case, field)
    builder.build(far_first)
    assert not builder.unconnected
    score_before = builder.proxy_score()

    improve.rebuild_branches(builder, far_first, rounds=2, network_geom=field.network_geom)
    assert builder.proxy_score() <= score_before + 1e-9
    net = builder.net
    assert not builder.unconnected
    assert {n.ref for n in net.oks_nodes()} == {"p1", "p2"}
    # Дерево связно и каждая точка — лист
    assert len(net.components()) == 1
    assert all(net.degree(n.id) == 1 for n in net.oks_nodes())
    assert set(builder.joins) == {n.id for n in net.oks_nodes()}
    assert set(builder.step_of_oks) == {n.id for n in net.oks_nodes()}


def test_build_variant_with_local_search_not_worse_than_without():
    case, field, targets = _builder(_two_points_area())
    ordered = _orders(targets, field)["farthest_first"]
    plain = build_variant(case, field, ordered, variant_id="v1", order="farthest_first", local_search=False)
    improved = build_variant(case, field, ordered, variant_id="v1", order="farthest_first", local_search=True)
    assert improved.score <= plain.score + 1e-9
    assert not improved.cost.unconnected


DATASET = Path(__file__).resolve().parents[2] / "ресурсы" / "распаковано" / "ТЗ" / "Датасет скорректированный.geojson"


@pytest.mark.skipif(not DATASET.exists(), reason="конкурсный набор не распакован")
def test_branch_rebuild_improves_largest_flow_order_on_competition_set():
    """Порядок «большой расход первым» без перестройки даёт S ≈ 13,51; перестройка веток — ≈ 12,90."""
    case = parse_case(json.loads(DATASET.read_text(encoding="utf-8")), name="ЗИЛ")
    rules = get_rules()
    field = build_field(case, rules=rules)
    ordered = _orders(approach_targets(case, field), field)["largest_flow_first"]
    improved = build_variant(case, field, ordered, variant_id="v1", order="largest_flow_first", local_search=True)
    assert not improved.cost.unconnected
    assert improved.notes["local_search"]
    assert improved.score < 13.2
    assert improved.diameter_report.ok
    assert not improved.notes["unresolved_special"]


def test_worth_rebuilding_skips_branches_with_nothing_new_nearby():
    case, field, targets = _builder(_two_points_area())
    ordered = _orders(targets, field)["nearest_first"]
    builder = TreeBuilder(case, field)
    builder.build(ordered)
    net = builder.net
    # Последняя построенная точка: после неё ничего не появилось — перестраивать нечего
    last = max(net.oks_nodes(), key=lambda n: builder.step_of_oks[n.id])
    assert not improve._worth_rebuilding(net, last.id, builder.step_of_oks[last.id])


# --- волна со старта на ветке и защита от наложения ---------------------------------------


def test_wave_joins_immediately_when_start_lies_on_a_branch():
    """Старт на ветке дерева: вырожденный маршрут в одну точку, а не шаг вдоль ветки."""
    case, field, targets = _builder(basic_area())
    ordered = _orders(targets, field)["nearest_first"]
    builder = TreeBuilder(case, field)
    builder.build(ordered)
    segment = next(iter(builder.net.segments.values()))
    on_branch = segment.line.interpolate(segment.line.length / 2)
    result = builder.wave.run(on_branch, 100, 20.0)
    assert result is not None
    assert result.join.kind in ("branch", "branch_node")
    assert len(result.points) == 1


def test_commit_refuses_branch_lying_on_another_branch():
    case, field, targets = _builder(basic_area())
    ordered = _orders(targets, field)["nearest_first"]
    builder = TreeBuilder(case, field)
    builder.build(ordered)
    segment = next(iter(builder.net.segments.values()))
    a, b = segment.points[0], segment.points[1]          # первое звено ветки
    mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    assert builder._overlaps_existing([a, mid])           # лежит на звене
    assert builder._overlaps_existing([mid, b, (b[0] + 5.0, b[1] + 5.0)])
    assert not builder._overlaps_existing([(mid[0] + 5.0, mid[1] + 5.0), mid])   # касание концом


def test_final_leg_alignment_ignores_own_building_block():
    """Финальный участок идёт из точки внутри здания: запреты для него не проверяются."""
    case, field, targets = _builder(basic_area())
    builder = TreeBuilder(case, field)
    oks = case.oks[0]
    inside = (oks.geom.x, oks.geom.y)
    outside = targets[0][0].target
    assert not builder._obstacles().segment_ok(inside, (outside.x, outside.y))
    assert builder._obstacles(blocked=False).segment_ok(inside, (outside.x, outside.y))


# --- справочно: добавочный расход по существующей сети ------------------------------------


def test_existing_impact_routes_added_flow_to_source():
    """Точка p1 врезается в магистраль; добавка 20 т/ч идёт по участку к источнику (южный конец)."""
    from app.case import existing

    data = basic_area()
    case = parse_case(data, name="test")
    rules = get_rules()
    field = build_field(case, rules=rules)
    ordered = _orders(approach_targets(case, field), field)["nearest_first"]
    variant = build_variant(case, field, ordered, variant_id="v1", order="nearest_first")
    flows = {n.id: case.oks_by_id(n.ref).flow_tph for n in variant.network.oks_nodes()}
    impact = existing.analyse(case, variant.network, rules, flows)
    assert impact.source_found
    assert not impact.unreached
    assert [e.edge_id for e in impact.edges] == ["net1"]
    edge = impact.edges[0]
    assert edge.added_flow_tph == 20.0
    assert edge.capacity_tph == rules.diameter(400).capacity_tph
    assert edge.share is not None and 0 < edge.share < 0.1
    assert edge.tie_ins and edge.tie_ins[0].startswith("участок net1, новая камера ")
    payload = impact.to_dict()
    assert payload["edges"][0]["share_of_capacity"] == round(edge.share, 3)


def test_existing_impact_without_source_near_network_reports_no_path():
    from app.case import existing

    data = basic_area()
    for f in data["features"]:
        if f["properties"].get("object_type") == "source":
            f["geometry"] = feature(Point(*m(2000, 2000)))["geometry"]   # источник далеко от сети
    case = parse_case(data, name="test")
    rules = get_rules()
    field = build_field(case, rules=rules)
    ordered = _orders(approach_targets(case, field), field)["nearest_first"]
    variant = build_variant(case, field, ordered, variant_id="v1", order="nearest_first")
    impact = existing.analyse(case, variant.network, rules, {})
    assert not impact.source_found
    assert impact.edges == []
