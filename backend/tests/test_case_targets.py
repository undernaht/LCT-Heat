"""Выход из полигона ОКС: ближайшая граница, запасные кандидаты, точки «на стене»."""

from __future__ import annotations

import sys
from pathlib import Path

from shapely.geometry import Point, Polygon

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_case_solve import OX, OY, basic_area, box, feature, m, solve_area  # noqa: E402

from app.case.field import build_field  # noqa: E402
from app.case.model import parse_case  # noqa: E402
from app.case.targets import approach_candidates  # noqa: E402


def candidates_for(data: dict, oks_id):
    case = parse_case(data, name="t")
    field = build_field(case)
    oks = case.oks_by_id(oks_id)
    return case, approach_candidates(oks, case, field)


def test_nearest_boundary_is_first_when_free():
    case, cands = candidates_for(basic_area(), "p1")
    assert cands[0].is_nearest
    # точка (160, 0) в доме [150..180]: ближайшая стена западная, цель западнее её
    assert cands[0].target.x < OX + 150
    assert abs(cands[0].target.y - OY) < 0.5


def test_point_slightly_outside_polygon_still_belongs_to_it():
    """ТЗ: точка подключения на границе ОКС. 0,3 м снаружи — та же точка, тот же дом."""
    data = basic_area()
    for f in data["features"]:
        if f["properties"].get("id") == "p1":
            f["geometry"] = feature(Point(*m(149.7, 0)), id="p1", object_type="oks_connection_point", flow_tph=20.0)["geometry"]
    case, cands = candidates_for(data, "p1")
    assert cands[0].own_ids == frozenset({"b1"})
    assert cands[0].final_leg is not None
    _, result = solve_area(data)
    assert result.variants[0].cost.unconnected == []


def test_duplicate_polygons_are_both_own():
    data = basic_area([feature(box(150, -40, 180, 40), id="b1-copy", object_type="restriction", restriction_type="oks")])
    case, cands = candidates_for(data, "p1")
    assert cands[0].own_ids == frozenset({"b1", "b1-copy"})
    _, result = solve_area(data)
    assert result.variants[0].cost.unconnected == []


def test_concave_building_falls_back_when_ray_reenters():
    """П-образный дом: ближайшая стена во дворе, луч уходит в тело здания — берём другую."""
    shell = Polygon([m(150, -40), m(200, -40), m(200, 40), m(150, 40)])
    # узкий двор 8 м, открытый на восток: целиком в зоне отступа собственного дома
    notch = Polygon([m(160, -4), m(200, -4), m(200, 4), m(160, 4)])
    building = shell.difference(notch)
    data = basic_area()
    data["features"] = [f for f in data["features"] if f["properties"].get("id") not in ("b1", "p1")]
    data["features"].append(feature(building, id="u", object_type="restriction", restriction_type="oks"))
    # точка в северном крыле у самого двора: ближайшая граница — стена двора
    data["features"].append(feature(Point(*m(180, 6)), id="p1", object_type="oks_connection_point", flow_tph=20.0))
    case, cands = candidates_for(data, "p1")
    assert cands, "кандидаты должны быть"
    # луч из точки через стену двора идёт на юг сквозь двор и входит в южное крыло —
    # такой выход отвергнут; первый кандидат не «ближайший» и смотрит не на юг
    assert not cands[0].is_nearest
    assert cands[0].direction[1] > -0.5
    _, result = solve_area(data)
    v = result.variants[0]
    assert v.cost.unconnected == []
    oks_node = v.network.oks_nodes()[0]
    assert oks_node.approach_rule in ("nearest", "fallback")
