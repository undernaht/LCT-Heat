"""Сквозные тесты конкурсной модели на маленьких рукотворных районах.

Район строится в метрах (EPSG:32637) и переводится в WGS 84 — ровно так, как
приходит вход. Проверяются свойства решения, а не числа.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from shapely.geometry import LineString, Point, Polygon, mapping

from app.case import solve as solve_module
from app.case.model import parse_case
from app.case.rules import get_rules
from app.geo import crs

CALC = "EPSG:32637"
# Центр района — ЗИЛ, чтобы проекция была та же, что у конкурсного набора
OX, OY = 414_300.0, 6_173_400.0


def feature(geom, **props):
    return {"type": "Feature", "properties": props, "geometry": mapping(crs.to_wgs84(geom, CALC))}


def m(x: float, y: float) -> tuple[float, float]:
    return OX + x, OY + y


def box(x0, y0, x1, y1) -> Polygon:
    return Polygon([m(x0, y0), m(x1, y0), m(x1, y1), m(x0, y1)])


def basic_area(extra: list[dict] | None = None) -> dict:
    """Магистраль Ду400 вдоль оси x = 0; здание 30×80 м в 150 м восточнее.

    Точка стоит у западной стены: ближайшая граница — западная, выход смотрит на сеть.
    """
    features = [
        feature(Point(*m(0, -200)), id="src", object_type="source"),
        feature(LineString([m(0, -300), m(0, 300)]), id="net1", object_type="heat_network", diameter=400),
        feature(Point(*m(0, -300)), id="ch1", object_type="heat_chamber"),
        feature(box(150, -40, 180, 40), id="b1", object_type="restriction", restriction_type="oks"),
        feature(Point(*m(160, 0)), id="p1", object_type="oks_connection_point", flow_tph=20.0),
    ]
    return {"type": "FeatureCollection", "features": features + (extra or [])}


def solve_area(data: dict, **kwargs):
    case = parse_case(data, name="test")
    return case, solve_module.solve(case, max_variants=1, orders=["nearest_first"], **kwargs)


# --- базовое подключение -----------------------------------------------------


def test_single_point_connects_with_new_chamber():
    case, result = solve_area(basic_area())
    v = result.variants[0]
    assert v.cost.unconnected == []
    net = v.network
    assert len(net.oks_nodes()) == 1
    tie = net.tie_in_nodes()
    assert len(tie) == 1 and tie[0].on_edge is not None
    # Ду по расходу 20 т/ч → Ду100; камера на Ду400 → 5 млн
    assert all(s.du == 100 for s in net.segments.values())
    assert v.cost.chambers[0].cost == 5_000_000
    assert v.cost.tie_in_count == 0


def test_summary_identities_hold():
    case, result = solve_area(basic_area())
    v = result.variants[0]
    c = v.cost
    assert c.construction_cost == pytest.approx(sum(c.segment_cost.values()) + c.chamber_construction_cost + c.tie_in_cost, abs=0.01)
    assert c.calculated_cost == pytest.approx(c.construction_cost + c.unconnected_penalty, abs=0.01)
    assert c.score == pytest.approx(get_rules().score(c.calculated_cost, c.new_network_length), abs=1e-5)


def test_output_geojson_structure():
    case, result = solve_area(basic_area())
    fc = result.to_geojson()
    types = {f["properties"]["object_type"] for f in fc["features"]}
    assert {"heat_network", "heat_chamber", "variant_summary"} <= types
    summary = next(f for f in fc["features"] if f["properties"]["object_type"] == "variant_summary")
    assert summary["geometry"] is None and summary["properties"]["rank"] == 1
    assert summary["properties"]["unconnected_oks_ids"] == []
    ids = [f["properties"]["id"] for f in fc["features"]]
    assert len(ids) == len(set(ids))
    # концы участков ссылаются на существующие узлы
    node_ids = {"p1", "ch1"} | {f["properties"]["id"] for f in fc["features"]
                                if f["properties"]["object_type"] in ("heat_chamber", "technical_node")}
    for f in fc["features"]:
        p = f["properties"]
        if p["object_type"] == "heat_network":
            assert p["start_node_id"] in node_ids and p["end_node_id"] in node_ids
            assert p["depth_start"] is None and p["depth_end"] is None
            assert isinstance(p["diameter"], int)


def test_oks_point_coordinates_are_bitwise_input_copies():
    data = basic_area()
    case, result = solve_area(data)
    fc = result.to_geojson()
    raw = next(f for f in data["features"] if f["properties"].get("id") == "p1")["geometry"]["coordinates"]
    ends = []
    for f in fc["features"]:
        if f["properties"]["object_type"] == "heat_network" and "p1" in (f["properties"]["start_node_id"], f["properties"]["end_node_id"]):
            coords = f["geometry"]["coordinates"]
            ends.append(coords[0] if f["properties"]["start_node_id"] == "p1" else coords[-1])
    assert ends and all(e == list(raw) for e in ends)


def test_turns_do_not_exceed_90_degrees():
    from app.case.geometry import turn_angle_deg

    case, result = solve_area(basic_area())
    for s in result.variants[0].network.segments.values():
        for a, b, c in zip(s.points, s.points[1:], s.points[2:]):
            assert turn_angle_deg(a, b, c) <= 90.0 + 1e-6


# --- существующая камера и правило 10 м -------------------------------------


def test_tie_in_uses_existing_chamber_within_10_m():
    """Камера прямо напротив здания: присоединение обязано идти в неё (5 млн врезка)."""
    data = basic_area([feature(Point(*m(0, 0)), id="ch2", object_type="heat_chamber")])
    case, result = solve_area(data)
    v = result.variants[0]
    tie = v.network.tie_in_nodes()[0]
    assert tie.existing is not None and tie.ref == "ch2"
    assert v.cost.tie_in_count == 1 and v.cost.tie_in_cost == 5_000_000
    assert v.cost.chambers == []


# --- спецпроходы ---------------------------------------------------------------


def test_road_crossing_becomes_straight_special_segment():
    """Дорога 12 м поперёк пути: один прямой спецучасток с полями 3 м, Kспец 1,6, два техузла."""
    road = box(60, -100, 72, 100)
    data = basic_area([feature(road, id="r1", object_type="restriction", restriction_type="road")])
    case, result = solve_area(data)
    v = result.variants[0]
    assert v.cost.unconnected == []
    specials = [s for s in v.network.segments.values() if s.laying == "special"]
    assert len(specials) == 1
    special = specials[0]
    assert special.k_special == pytest.approx(1.60)
    assert len(special.points) == 2, "спецпроход — один прямой отрезок"
    assert special.length == pytest.approx(12 + 6, abs=0.6), "полигон плюс 3 м с каждой стороны"
    tech = [n for n in v.network.nodes.values() if n.kind == "technical_node"]
    assert len(tech) == 2
    # угол к границе дороги (вертикальной) ≥ 45°
    (x0, y0), (x1, y1) = special.points
    angle = math.degrees(math.atan2(abs(y1 - y0), abs(x1 - x0)))
    assert 90 - angle >= 45 - 1e-6
    assert special.cost == pytest.approx(round(special.length, 3) * get_rules().diameter(100).cost_per_m * 1.60, abs=1.0)
    assert not v.notes["unresolved_special"]


def test_gas_line_crossing_gets_two_metre_margins():
    gas = LineString([m(80, -100), m(80, 100)])
    data = basic_area([feature(gas, id="g1", object_type="restriction", restriction_type="gas_pipeline")])
    case, result = solve_area(data)
    v = result.variants[0]
    specials = [s for s in v.network.segments.values() if s.laying == "special"]
    assert len(specials) == 1
    assert specials[0].k_special == pytest.approx(1.25)
    assert specials[0].length == pytest.approx(4.0, abs=0.3)
    assert not v.notes["unresolved_special"]


def test_blocking_restriction_is_avoided_with_clearance():
    park = box(60, -60, 90, 60)
    data = basic_area([feature(park, id="pk", object_type="restriction", restriction_type="park")])
    case, result = solve_area(data)
    v = result.variants[0]
    assert v.cost.unconnected == []
    park_m = park
    width = get_rules().diameter(100).width_m
    for s in v.network.segments.values():
        assert s.line.distance(park_m) >= 1.0 + width / 2 - 0.05


# --- неподключаемая точка ----------------------------------------------------------


def test_unreachable_point_is_reported_with_penalty():
    ring = box(120, -60, 220, 60).difference(box(130, -50, 210, 50))
    data = basic_area([feature(ring, id="wall", object_type="restriction", restriction_type="prohibited_site")])
    case, result = solve_area(data)
    v = result.variants[0]
    assert v.cost.unconnected == ["p1"]
    assert v.cost.unconnected_penalty == pytest.approx(100_000_000 + 500_000 * 20.0)
    assert v.cost.construction_cost == 0.0
    assert "p1" in v.build.reasons


# --- конкурсный набор -------------------------------------------------------------

DATASET = Path(__file__).resolve().parents[2] / "ресурсы" / "распаковано" / "ТЗ" / "Датасет скорректированный.geojson"


@pytest.mark.skipif(not DATASET.exists(), reason="конкурсный набор не распакован")
def test_competition_dataset_connects_everything():
    """ТЗ §2.9: набор подготовлен так, что все точки подключаемы. Непустой список — дефект солвера."""
    case = parse_case(json.loads(DATASET.read_text(encoding="utf-8")), name="ЗИЛ")
    result = solve_module.solve(case, max_variants=1, orders=["nearest_first"])
    v = result.variants[0]
    assert v.cost.unconnected == []
    assert len(v.network.oks_nodes()) == 17
    assert v.diameter_report.ok
    assert not v.notes["minimality"]
    assert not v.notes["unresolved_special"]
    assert v.cost.score < 14.0


# --- режим с глубиной ------------------------------------------------------------


def test_depth_mode_goes_above_gas_and_cable_with_valid_ramps():
    gas = LineString([m(80, -100), m(80, 100)])
    cable = LineString([m(95, -100), m(95, 100)])
    data = basic_area([
        feature(gas, id="g1", object_type="restriction", restriction_type="gas_pipeline"),
        feature(cable, id="c1", object_type="restriction", restriction_type="power_cable"),
    ])
    case = parse_case(data, name="t")
    result = solve_module.solve(case, max_variants=1, orders=["nearest_first"], mode="depth")
    v = result.variants[0]
    rules = get_rules()
    assert v.depth_report is not None and v.depth_report.ok
    segments = list(v.network.segments.values())
    # каждый участок несёт глубины, уклон не круче 0,10, глубина в [0,7; 3,0] при проходе сверху
    for s in segments:
        assert s.depth_start is not None and s.depth_end is not None
        assert abs(s.depth_end - s.depth_start) / max(s.length, 1e-6) <= 0.10 + 1e-6
        assert 0.7 - 1e-6 <= min(s.depth_start, s.depth_end) and max(s.depth_start, s.depth_end) <= 3.0 + 1e-6
        assert s.k_depth == 1.0
    # спецпроходы остаются одним прямым участком с постоянной глубиной на полке
    specials = [s for s in segments if s.laying == "special"]
    assert len(specials) == 2
    for s in specials:
        assert len(s.points) == 2 and s.depth_start == s.depth_end
    # над газом: верх нашей трубы ≤ 2,8 − 0,2 − h(Ду100) = 2,42
    gas_piece = next(s for s in specials if s.k_special == pytest.approx(1.25))
    assert gas_piece.depth_start <= 2.8 - 0.2 - rules.diameter(100).height_m + 1e-6
    # глубина в узлах согласована между соседними участками
    for node in v.network.nodes.values():
        depths = set()
        for s in v.network.incident(node.id):
            depths.add(s.depth_start if s.start == node.id else s.depth_end)
        assert len(depths) == 1, f"разные глубины в узле {node.id}: {depths}"
    fc = result.to_geojson()
    nets = [f["properties"] for f in fc["features"] if f["properties"]["object_type"] == "heat_network"]
    assert all(p["depth_start"] is not None for p in nets)


def test_2d_output_keeps_depth_null_even_after_depth_run():
    case, result = solve_area(basic_area())
    fc = result.to_geojson()
    nets = [f["properties"] for f in fc["features"] if f["properties"]["object_type"] == "heat_network"]
    assert all(p["depth_start"] is None and p["depth_end"] is None for p in nets)


# --- косое пересечение перестраивается ------------------------------------------


def test_oblique_road_crossing_is_rebuilt_to_45_degrees():
    """Ломаная пересекает дорогу под 25°: перестройка даёт прямую хорду под ≥ 45°."""
    from shapely.affinity import rotate

    from app.case import special
    from app.case.field import special_zone
    from app.case.geometry import Obstacles
    from shapely.geometry import LineString as LS

    rules = get_rules()
    road = rotate(box(60, -400, 72, 400), 65, origin=(OX + 66, OY))
    zone = special_zone("r1", "road", road, rules, rules.diameter(100).width_m)
    # трасса под 25° к оси дороги: идём вдоль оси x, дорога наклонена на 65° от вертикали
    points = [m(200, 0), m(-100, 0)]
    line = LS(points)
    crossings = special.find_crossings(line, zone, rules)
    assert len(crossings) == 1
    assert min(crossings[0].entry_angle_deg, crossings[0].exit_angle_deg) < 45.0

    obstacles = Obstacles(road.buffer(-100), [])   # запретов рядом нет
    rebuilt = special.rebuild_crossing(points, crossings[0], obstacles, rules)
    assert rebuilt is not None
    new_line = LS(rebuilt)
    new_crossings = special.find_crossings(new_line, zone, rules)
    assert len(new_crossings) == 1
    c = new_crossings[0]
    assert min(c.entry_angle_deg, c.exit_angle_deg) >= rules.min_crossing_angle_deg - 1e-6
    # спецучасток прямой: между его границами нет вершин
    assert special._is_straight(new_line, c.d_start, c.d_end)
    from app.case.geometry import turn_angle_deg
    for a, b, c3 in zip(rebuilt, rebuilt[1:], rebuilt[2:]):
        assert turn_angle_deg(a, b, c3) <= rules.max_turn_deg + 1e-6


# --- разбор входа: многочастные участки сети ------------------------------------------


def test_multilinestring_network_edge_is_split_into_parts():
    """Участок из рабочих систем может прийти MultiLineString: части входят в сеть с общим id."""
    from shapely.geometry import MultiLineString

    data = basic_area()
    data["features"] = [f for f in data["features"] if f["properties"].get("id") != "net1"]
    data["features"].append(feature(
        MultiLineString([[m(0, -300), m(0, 0)], [m(0, 0), m(0, 300)]]),
        id="net1", object_type="heat_network", diameter=400,
    ))
    case = parse_case(data, name="test")
    assert [e.id for e in case.network] == ["net1", "net1"]
    assert not [i for i in case.issues if i.level == "error"]
    _, result = solve_area(data)
    assert result.variants[0].cost.unconnected == []


def test_network_contact_near_free_chamber_redirects_to_it():
    """§2.4: касание сети в 10 м от свободной существующей камеры — врезка в неё, а не новая камера."""
    from app.case.field import build_field
    from app.case.route import Wave
    from app.case.tree import TreeBuilder

    data = basic_area([feature(Point(*m(0, 6)), id="ch_near", object_type="heat_chamber")])
    case = parse_case(data, name="test")
    field = build_field(case, rules=get_rules())
    builder = TreeBuilder(case, field)
    wave: Wave = builder.wave
    # Ячейка сети в 10 м от камеры — не вариант присоединения
    cell = field.grid.rowcol(*m(0, 0))
    assert wave.edge_of[cell] >= 0 and wave.near_chamber[cell]
    assert wave._option_at(cell, 100, 0.0) is None
    assert wave._chamber_near(cell) == field.grid.rowcol(*m(0, 6))
    # Полное решение: точка присоединяется к камере, новых камер в 10 м от неё нет
    _, result = solve_area(data)
    net = result.variants[0].network
    assert result.variants[0].cost.unconnected == []
    chamber = Point(*m(0, 6))
    for node in net.nodes.values():
        if node.kind == "chamber_new":
            assert Point(*node.point).distance(chamber) > get_rules().reuse_chamber_within_m
