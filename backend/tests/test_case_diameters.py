"""Ду, предельная длина и минимальность на рукотворных деревьях."""

from __future__ import annotations

from app.case import diameters
from app.case.model import ExistingEdge
from app.case.network import NODE_CHAMBER_NEW, NODE_OKS, Network
from app.case.rules import get_rules
from shapely.geometry import LineString


def chain(lengths: list[float], flows: list[float]):
    """Цепочка: точка → камера → камера → … → присоединение; в каждой камере
    дополнительная точка с заданным расходом. Возвращает сеть и расходы."""
    net = Network()
    edge = ExistingEdge(id="e1", geom=LineString([(0, -10), (0, 10)]), diameter=500)
    x = 0.0
    root = net.add_node((x, 0.0), NODE_CHAMBER_NEW, on_edge=edge, existing_connections=2)
    prev = root
    flow_by_oks: dict[str, float] = {}
    for index, (length, flow) in enumerate(zip(lengths, flows)):
        x += length
        if index == len(lengths) - 1:
            node = net.add_node((x, 0.0), NODE_OKS, ref=f"o{index}")
            flow_by_oks[node.id] = flow
        else:
            node = net.add_node((x, 0.0), NODE_CHAMBER_NEW)
            leaf = net.add_node((x, 20.0), NODE_OKS, ref=f"o{index}")
            flow_by_oks[leaf.id] = flow
            net.add_segment([(x, 20.0), (x, 0.0)], leaf.id, node.id)
        net.add_segment([(x, 0.0), (prev.point[0], 0.0)], node.id, prev.id)
        prev = node
    return net, flow_by_oks


def test_flows_aggregate_toward_tie_in():
    net, flows = chain([100, 100], [10.0, 5.0])
    diameters.assign(net, get_rules(), flows)
    by_start = {(net.nodes[s.start].point, net.nodes[s.end].point): s.flow_tph for s in net.segments.values()}
    trunk = next(f for (a, b), f in by_start.items() if a == (100.0, 0.0) and b == (0.0, 0.0))
    assert trunk == 15.0


def test_du_never_decreases_toward_tie_in():
    net, flows = chain([50, 50, 50], [30.0, 1.0, 1.0])   # первая точка большая, дальше мелочь
    diameters.assign(net, get_rules(), flows)
    root = net.tie_in_nodes()[0]
    for _, path in net.paths_to_root(root.id).items():
        dus = [s.du for s in path]
        assert dus == sorted(dus), dus


def test_limit_length_bumps_only_the_tie_in_side():
    """Пример из ревью: лист 300 м (20 т/ч) → камера → 200 м (22 т/ч).

    Отрезок Ду100 = 500 м > 419: поднимается только участок у присоединения.
    """
    net, flows = chain([200, 300], [2.0, 20.0])
    report = diameters.assign(net, get_rules(), flows)
    segs = {round(s.length): s for s in net.segments.values() if s.length > 100}
    assert segs[300].du == 100, "участок со стороны точки не трогаем"
    assert segs[200].du == 125, "участок у присоединения поднят"
    assert report.ok
    assert not diameters.minimality_issues(net, get_rules())


def test_long_single_branch_is_bumped_to_fit():
    net, flows = chain([480], [16.0])                     # Ду100, предел 419
    report = diameters.assign(net, get_rules(), flows)
    (segment,) = net.segments.values()
    assert segment.du == 125
    assert report.ok and report.bumps


def test_minimality_flags_arbitrary_oversize():
    net, flows = chain([100], [5.0])
    diameters.assign(net, get_rules(), flows)
    (segment,) = net.segments.values()
    segment.du = 150                                     # завысили вручную
    issues = diameters.minimality_issues(net, get_rules())
    assert issues and "Ду150" in issues[0]
