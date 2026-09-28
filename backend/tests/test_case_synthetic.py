"""Тесты генератора синтетических проверочных наборов (data/case_synthetic.py).

Проверяется то, ради чего наборы существуют: файл читается и устроен по §1
приложения, новые ограничения не залезают в здания и действительно стоят на
пути точек к сети, а каждый сценарий содержит именно тот случай, который
объявлен в его названии.
"""

from __future__ import annotations

import json
import math

import pytest
from shapely.geometry import LineString, Point, Polygon, shape
from shapely.ops import unary_union

import case_synthetic as cs
from app.geo import crs

GEOMETRY_BY_TYPE = {
    "source": {"Point"},
    "heat_network": {"LineString"},
    "heat_chamber": {"Point"},
    "oks_connection_point": {"Point"},
    "restriction": {"LineString", "MultiLineString", "Polygon", "MultiPolygon"},
}
REQUIRED_PROPS = {
    "heat_network": {"diameter"},
    "oks_connection_point": {"flow_tph"},
    "restriction": {"restriction_type"},
}


@pytest.fixture(scope="module")
def district() -> cs.District:
    return cs.District.load(cs.DEFAULT_INPUT)


@pytest.fixture(scope="module")
def scenarios(district: cs.District) -> dict[str, cs.Scenario]:
    return {name: cs.build(name, district) for name in cs.SCENARIOS}


@pytest.fixture(scope="module")
def original_ids(district: cs.District) -> set:
    return {f["properties"]["id"] for f in district.raw}


def metric(feature: dict):
    return crs.to_metric(shape(feature["geometry"]), cs.METRIC_CRS)


def by_type(scenario: cs.Scenario, rtype: str) -> list:
    return [g for t, g in scenario.new_geoms if t == rtype]


def _sides(poly: Polygon) -> tuple[float, float]:
    """Стороны минимального описанного прямоугольника: (короткая, длинная)."""
    rect = list(poly.minimum_rotated_rectangle.exterior.coords)
    a = math.dist(rect[0], rect[1])
    b = math.dist(rect[1], rect[2])
    return min(a, b), max(a, b)


def _long_axis(poly: Polygon) -> tuple[float, float]:
    rect = list(poly.minimum_rotated_rectangle.exterior.coords)
    a, b = (rect[0], rect[1]), (rect[1], rect[2])
    p, q = a if math.dist(*a) >= math.dist(*b) else b
    return (q[0] - p[0], q[1] - p[1])


def _angle_deg(u: tuple[float, float], v: tuple[float, float]) -> float:
    dot = abs(u[0] * v[0] + u[1] * v[1])
    return math.degrees(math.acos(min(1.0, dot / (math.hypot(*u) * math.hypot(*v)))))


# --- §1: структура файла ------------------------------------------------------


@pytest.mark.parametrize("name", cs.SCENARIOS)
def test_file_is_valid_geojson_per_section_1(name, scenarios, district, tmp_path):
    path = tmp_path / f"{name}.geojson"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(scenarios[name].to_geojson(), fh, ensure_ascii=False)
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)

    assert data["type"] == "FeatureCollection"
    minx, miny, maxx, maxy = unary_union(
        [shape(f["geometry"]) for f in district.raw]
    ).buffer(0.1).bounds
    ids = []
    for f in data["features"]:
        assert f["type"] == "Feature"
        props, geom = f["properties"], f["geometry"]
        assert "id" in props and props["id"] not in (None, "")
        assert isinstance(props["id"], (str, int, float))
        ids.append(props["id"])
        kind = props["object_type"]
        assert geom["type"] in GEOMETRY_BY_TYPE[kind], (props, geom["type"])
        assert REQUIRED_PROPS.get(kind, set()) <= set(props), props
        g = shape(geom)
        assert g.is_valid and not g.is_empty, props
        gx0, gy0, gx1, gy1 = g.bounds
        assert minx <= gx0 and gx1 <= maxx and miny <= gy0 and gy1 <= maxy, props
    assert len(ids) == len(set(ids)), "id должны быть уникальны"


@pytest.mark.parametrize("name", [n for n in cs.SCENARIOS if n not in ("big", "strings")])
def test_original_features_untouched(name, scenarios, district):
    assert scenarios[name].features[: len(district.raw)] == district.raw


@pytest.mark.parametrize(
    "name", [n for n in cs.SCENARIOS if n not in ("unreachable", "big", "strings")]
)
def test_new_restrictions_keep_clear_of_oks(name, scenarios, district, original_ids):
    scenario = scenarios[name]
    checked = 0
    for f in scenario.features:
        props = f["properties"]
        if props["id"] in original_ids or props.get("restriction_type") == "oks":
            continue
        assert props["object_type"] == "restriction"
        g = metric(f)
        assert g.distance(district.oks_union) >= cs.WALL_GAP - 0.05, props
        checked += 1
    assert checked == sum(t != "oks" for t, _ in scenario.new_geoms)


# --- маршруты: ограничения стоят на пути -----------------------------------------


def test_route_estimate_reaches_every_point(district):
    est = district.base_routes()
    assert not est.unreachable
    assert set(est.routes) == set(district.points)
    for pid, route in est.routes.items():
        start, end = route.coords[0], route.coords[-1]
        own = district.geoms[district.own_polygon_index(district.points[pid])]
        assert own.distance(Point(start)) >= cs.OKS_CLEARANCE + 1.0
        assert district.oks_union.distance(Point(start)) > 0.5
        assert district.network_union.distance(route.interpolate(route.length)) < 0.01
        assert route.length > 10.0, pid
        assert end != start


@pytest.mark.parametrize("name", ("roads", "utilities", "tram", "blocks", "mixed"))
def test_every_new_restriction_lies_on_some_route(name, scenarios, district):
    """Запреты стоят на исходных маршрутах, остальное — на маршрутах в обход запретов."""
    scenario = scenarios[name]
    routes = [*district.base_routes().routes.values(), *scenario.routes.values()]
    for rtype, geom in scenario.new_geoms:
        near = geom.buffer(2.0)
        assert any(r.intersects(near) for r in routes), rtype


# --- сценарии ----------------------------------------------------------------------


def test_roads(scenarios, district):
    scenario = scenarios["roads"]
    roads = by_type(scenario, "road")
    assert 3 <= len(roads) <= 4
    assert all(isinstance(r, Polygon) for r in roads)

    with_hole = [r for r in roads if r.interiors]
    assert len(with_hole) == 1, "ровно одна дорога с разделительной полосой"
    short, long = _sides(Polygon(with_hole[0].interiors[0]))
    assert 6.0 <= short <= 8.0
    assert long > short

    widths = [_sides(r)[0] for r in roads if not r.interiors]
    assert all(7.5 <= w <= 14.5 for w in widths), widths

    routes = district.base_routes().routes
    angles = []
    for road in roads:
        body = Polygon(road.exterior)
        crossing = max(
            (r.intersection(body) for r in routes.values()),
            key=lambda g: g.length,
        )
        parts = [crossing] if isinstance(crossing, LineString) else list(crossing.geoms)
        seg = max(parts, key=lambda g: g.length)
        chord = (seg.coords[-1][0] - seg.coords[0][0], seg.coords[-1][1] - seg.coords[0][1])
        angles.append(_angle_deg(chord, _long_axis(road)))
    assert min(angles) <= 40.0, f"нужна косая дорога: углы {angles}"
    assert sum(a >= 60.0 for a in angles) >= 2, angles


def test_utilities(scenarios, district):
    scenario = scenarios["utilities"]
    lines = [(t, g) for t, g in scenario.new_geoms if t in ("gas_pipeline", "power_cable")]
    assert 4 <= len(lines) <= 6
    assert {t for t, _ in lines} == {"gas_pipeline", "power_cable"}
    assert all(isinstance(g, LineString) for _, g in lines)

    routes = district.base_routes().routes
    along = [g for _, g in lines
             if 30.0 <= g.length <= 60.0
             and any(r.intersection(g.buffer(1.0)).length >= 0.8 * g.length
                     for r in routes.values())]
    assert along, "нужна линия вдоль маршрута на 30–60 м"

    before_tie_in = [g for _, g in lines if 5.0 <= g.distance(district.network_union) <= 9.0]
    assert before_tie_in, "нужна линия параллельно сети перед врезкой"
    assert any(r.intersects(g) for g in before_tie_in for r in routes.values())


def test_tram(scenarios):
    scenario = scenarios["tram"]
    trams = by_type(scenario, "tram_tracks")
    polys = [g for g in trams if isinstance(g, Polygon)]
    lines = [g for g in trams if isinstance(g, LineString)]
    assert len(polys) == 1 and len(lines) == 1
    short, long = _sides(polys[0])
    assert 6.0 <= short <= 7.5 and long >= 50.0
    assert lines[0].length >= 50.0
    assert polys[0].distance(lines[0]) > 10.0, "полигон и линия — на разных улицах"


def test_blocks_detour_but_keep_everyone_reachable(scenarios, district):
    scenario = scenarios["blocks"]
    kinds = {t for t, _ in scenario.new_geoms}
    assert kinds == {"park", "social_area", "prohibited_site"}
    assert all(isinstance(g, Polygon) for _, g in scenario.new_geoms)
    assert set(scenario.routes) == set(district.points), "никто не должен стать недостижимым"

    base = district.base_routes().routes
    longer = [pid for pid in base if scenario.routes[pid].length > base[pid].length + 20.0]
    assert longer, "хотя бы одна точка идёт в обход дальше"

    social = by_type(scenario, "social_area")[0]
    assert social.distance(district.oks_union) <= cs.WALL_GAP + 0.05, "примыкает к одной стене"
    for _, block in scenario.new_geoms:
        assert not any(block.intersects(r) for r in scenario.routes.values()),             "маршруты в обход не проходят сквозь запретные полигоны"


def test_mixed_overlays_road_and_gas(scenarios):
    scenario = scenarios["mixed"]
    kinds = {t for t, _ in scenario.new_geoms}
    assert {"road", "gas_pipeline", "power_cable", "tram_tracks",
            "park", "social_area", "prohibited_site"} <= kinds
    roads = by_type(scenario, "road")
    gas = by_type(scenario, "gas_pipeline")
    cables = by_type(scenario, "power_cable")
    assert any(g.within(r) for r in roads for g in gas), "газопровод внутри дороги"
    assert any(1.5 <= c.distance(r) <= 3.5 and not c.intersects(r) for r in roads for c in cables), \
        "кабель вплотную к краю дороги"


def test_strings(scenarios, district):
    scenario = scenarios["strings"]
    feats = scenario.features
    ids = [f["properties"]["id"] for f in feats]
    assert all(isinstance(v, str) and v.startswith("obj-") for v in ids)
    assert len(ids) == len(set(ids))

    flows = [f["properties"]["flow_tph"] for f in feats
             if f["properties"]["object_type"] == "oks_connection_point"]
    text = [v for v in flows if isinstance(v, str)]
    assert len(text) == 2 and all("," in v for v in text)
    assert all(isinstance(v, (int, float)) for v in flows if not isinstance(v, str))

    oks = [f for f in feats if f["properties"].get("restriction_type") == "oks"]
    assert len(oks) == len(district.oks_indices) + 1
    coords = [json.dumps(f["geometry"]) for f in oks]
    assert len(coords) - len(set(coords)) == 1, "ровно один дубль полигона"

    points = [metric(f) for f in feats if f["properties"]["object_type"] == "oks_connection_point"]
    on_boundary = [p for p in points if district.oks_union.boundary.distance(p) < 1e-3]
    outside = [p for p in points if district.oks_union.distance(p) > 0.1]
    assert len(on_boundary) >= 1
    assert len(outside) == 1 and abs(district.oks_union.distance(outside[0]) - 0.3) < 0.02


def test_big_is_four_connected_copies(scenarios, district):
    scenario = scenarios["big"]
    feats = scenario.features
    assert len(feats) == 4 * len(district.raw) - 3 + 3
    assert sum(f["properties"]["object_type"] == "source" for f in feats) == 1
    assert sum(f["properties"]["object_type"] == "oks_connection_point" for f in feats) == 4 * 17

    parent: dict[int, int] = {}

    def find(a: int) -> int:
        while parent.setdefault(a, a) != a:
            a = parent[a]
        return a

    ends: list[Point] = []
    for f in feats:
        if f["properties"]["object_type"] != "heat_network":
            continue
        assert f["properties"]["diameter"] in (300, 400, 500)
        line = metric(f)
        ends += [Point(line.coords[0]), Point(line.coords[-1])]
        parent[find(len(ends) - 2)] = find(len(ends) - 1)
    for i, p in enumerate(ends):
        for j in range(i + 1, len(ends)):
            if p.distance(ends[j]) < 0.05:
                parent[find(i)] = find(j)
    roots = {find(i) for i in range(len(ends))}
    assert len(roots) == 1, "сети четырёх копий должны быть связаны"


def test_unreachable_ring_seals_one_point(scenarios, district):
    scenario = scenarios["unreachable"]
    rings = by_type(scenario, "prohibited_site")
    assert len(rings) == 1 and isinstance(rings[0], Polygon)
    ring = rings[0]
    assert len(ring.interiors) == 1
    hole = Polygon(ring.interiors[0])
    sealed = [pid for pid, p in district.points.items() if hole.contains(p)]
    assert len(sealed) == 1
    pid = sealed[0]
    own = district.geoms[district.own_polygon_index(district.points[pid])]
    assert hole.contains(own), "кольцо охватывает весь собственный полигон"
    assert pid not in scenario.routes, "оценка маршрутов подтверждает недостижимость"
    assert set(scenario.routes) == set(district.points) - {pid}


# --- CLI ----------------------------------------------------------------------------


def test_cli_writes_geojson_and_png(tmp_path):
    assert cs.main(["--scenario", "strings", "--out", str(tmp_path)]) == 0
    geojson = tmp_path / "strings.geojson"
    png = tmp_path / "strings.png"
    assert geojson.exists() and png.exists() and png.stat().st_size > 10_000
    with open(geojson, encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["type"] == "FeatureCollection" and data["features"]
