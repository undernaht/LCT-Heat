"""Тесты независимого валидатора выходного GeoJSON (app.case_validator).

Сцены собираются в метрах (EPSG:32637) и выгружаются в WGS 84 тем же путём,
что и настоящий результат, — так что узлы совпадают с концами линий побитово,
а длины и стоимости считаются из тех же чисел, что видит валидатор.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from pyproj import Transformer
from shapely.geometry import LineString

from app.case_validator import Rules, validate
from app.case_validator.validator import Report

ORIGIN = (412000.0, 6179000.0)
_TO_WGS = Transformer.from_crs("EPSG:32637", "EPSG:4326", always_xy=True)
RULES = Rules.load()


def wgs(x: float, y: float) -> list[float]:
    lon, lat = _TO_WGS.transform(ORIGIN[0] + x, ORIGIN[1] + y)
    return [lon, lat]


def box(x0: float, y0: float, x1: float, y1: float) -> dict:
    ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return {"type": "Polygon", "coordinates": [[wgs(x, y) for x, y in ring]]}


def line_geom(coords: list[tuple[float, float]]) -> dict:
    return {"type": "LineString", "coordinates": [wgs(x, y) for x, y in coords]}


def point_geom(xy: tuple[float, float]) -> dict:
    return {"type": "Point", "coordinates": wgs(*xy)}


class Scene:
    """Вход и выход в метрах; сводка считается по тем же правилам, что у валидатора."""

    def __init__(self) -> None:
        self.input_features: list[dict] = []
        self.output_features: list[dict] = []
        self.input_chambers: set[Any] = set()
        self.oks_flows: dict[Any, float] = {}

    # --- вход ---
    def source(self, oid: Any, xy: tuple[float, float]) -> None:
        self.input_features.append({"type": "Feature", "properties": {"id": oid, "object_type": "source"},
                                    "geometry": point_geom(xy)})

    def line(self, oid: Any, coords: list[tuple[float, float]], diameter: int) -> None:
        self.input_features.append({"type": "Feature",
                                    "properties": {"id": oid, "object_type": "heat_network", "diameter": diameter},
                                    "geometry": line_geom(coords)})

    def chamber(self, oid: Any, xy: tuple[float, float]) -> None:
        self.input_chambers.add(oid)
        self.input_features.append({"type": "Feature", "properties": {"id": oid, "object_type": "heat_chamber"},
                                    "geometry": point_geom(xy)})

    def oks(self, oid: Any, xy: tuple[float, float], flow: float, building: tuple | None = None) -> None:
        self.oks_flows[oid] = flow
        self.input_features.append({"type": "Feature",
                                    "properties": {"id": oid, "object_type": "oks_connection_point", "flow_tph": flow},
                                    "geometry": point_geom(xy)})
        if building is not None:
            self.restriction(f"bld_{oid}", "oks", box(*building))

    def restriction(self, oid: Any, rtype: str, geometry: dict) -> None:
        self.input_features.append({"type": "Feature",
                                    "properties": {"id": oid, "object_type": "restriction", "restriction_type": rtype},
                                    "geometry": geometry})

    # --- выход ---
    def new_chamber(self, oid: Any, xy: tuple[float, float], diameter: int, variant: Any = "v1") -> None:
        self.output_features.append({"type": "Feature",
                                     "properties": {"id": oid, "object_type": "heat_chamber", "variant_id": variant,
                                                    "diameter": diameter, "cost": RULES.chamber_cost(diameter)},
                                     "geometry": point_geom(xy)})

    def tech(self, oid: Any, xy: tuple[float, float], variant: Any = "v1") -> None:
        self.output_features.append({"type": "Feature",
                                     "properties": {"id": oid, "object_type": "technical_node", "variant_id": variant},
                                     "geometry": point_geom(xy)})

    def segment(self, oid: Any, coords: list[tuple[float, float]], start: Any, end: Any, flow: float,
                diameter: int, method: str = "base", k_special: float = 1.0, variant: Any = "v1",
                **overrides: Any) -> dict:
        length = LineString(coords).length
        props = {
            "id": oid, "object_type": "heat_network", "variant_id": variant,
            "start_node_id": start, "end_node_id": end, "flow_tph": flow, "diameter": diameter,
            "length": length, "laying_method": method, "depth_start": None, "depth_end": None,
            "cost": length * RULES.row(diameter).unit_cost * k_special,
        }
        props.update(overrides)
        feature = {"type": "Feature", "properties": props, "geometry": line_geom(coords)}
        self.output_features.append(feature)
        return props

    def summary(self, variant: Any = "v1", rank: int = 1, **overrides: Any) -> dict:
        segs = [ft["properties"] for ft in self.output_features
                if ft["properties"]["object_type"] == "heat_network" and ft["properties"]["variant_id"] == variant]
        chambers = [ft["properties"] for ft in self.output_features
                    if ft["properties"]["object_type"] == "heat_chamber" and ft["properties"]["variant_id"] == variant]
        tie_ins = sum(1 for s in segs for k in ("start_node_id", "end_node_id") if s[k] in self.input_chambers)
        used = {s[k] for s in segs for k in ("start_node_id", "end_node_id")}
        unconnected = [oid for oid in self.oks_flows if oid not in used]
        penalty = sum(RULES.penalty(self.oks_flows[oid]) for oid in unconnected)
        chamber_cost = sum(c["cost"] for c in chambers)
        construction = sum(s["cost"] for s in segs) + chamber_cost + tie_ins * RULES.tie_in_cost
        length = sum(s["length"] for s in segs)
        props = {
            "id": f"{variant}_summary", "object_type": "variant_summary", "variant_id": variant, "rank": rank,
            "construction_cost": construction, "chamber_construction_cost": chamber_cost,
            "existing_chamber_tie_in_count": tie_ins, "existing_chamber_tie_in_cost": tie_ins * RULES.tie_in_cost,
            "unconnected_penalty": penalty, "calculated_cost": construction + penalty,
            "new_network_length": length, "score": RULES.score(construction + penalty, length),
            "unconnected_oks_ids": unconnected,
        }
        props.update(overrides)
        self.output_features.append({"type": "Feature", "properties": props, "geometry": None})
        return props

    def input(self) -> dict:
        return {"type": "FeatureCollection", "features": copy.deepcopy(self.input_features)}

    def output(self) -> dict:
        return {"type": "FeatureCollection", "features": copy.deepcopy(self.output_features)}

    def validate(self, **kw: Any) -> Report:
        report = validate(self.input(), self.output(), RULES, **kw)
        json.dumps(report.to_dict(), ensure_ascii=False)  # отчёт должен сериализоваться
        return report


def errors(report: Report) -> list[str]:
    return [f"{f.code}: {f.message}" for f in report.findings if f.severity == "error"]


def error_codes(report: Report) -> set[str]:
    return {f.code for f in report.findings if f.severity == "error"}


def warning_codes(report: Report) -> set[str]:
    return {f.code for f in report.findings if f.severity == "warning"}


# --- базовая сцена: существующая линия по оси x, газопровод, две точки -----


def base_scene() -> Scene:
    """Существующая магистраль Ду300 по y=0, камеры на её концах, газопровод по y=30."""
    s = Scene()
    s.source("src", (-50, 0))
    s.line("L1", [(0, 0), (200, 0)], 300)
    s.chamber("E1", (0, 0))
    s.chamber("E2", (200, 0))
    s.restriction("gas", "gas_pipeline", line_geom([(40, 30), (160, 30)]))
    return s


def good_scene() -> Scene:
    """Две точки, камера разветвления, спецпроход газопровода, камера на магистрали."""
    s = base_scene()
    s.oks("P1", (100, 100), 3.0, building=(90, 90, 110, 110))
    s.oks("P2", (140, 100), 3.0, building=(130, 90, 150, 110))
    s.new_chamber("C", (100, 60), 65)
    s.tech("T1", (100, 32))
    s.tech("T2", (100, 28))
    s.new_chamber("A", (100, 0), 300)
    s.segment("s1", [(100, 100), (100, 60)], "P1", "C", 3.0, 50)
    s.segment("s2", [(140, 100), (140, 60), (100, 60)], "P2", "C", 3.0, 50)
    s.segment("s3", [(100, 60), (100, 32)], "C", "T1", 6.0, 65)
    s.segment("s4", [(100, 32), (100, 28)], "T1", "T2", 6.0, 65, method="special", k_special=1.25)
    s.segment("s5", [(100, 28), (100, 0)], "T2", "A", 6.0, 65)
    return s


# --- тесты ------------------------------------------------------------------


def test_appendix_example_passes_with_score_0_6913() -> None:
    inp = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"id": "input_chamber_1", "object_type": "heat_chamber"},
         "geometry": {"type": "Point", "coordinates": [37.6, 55.75]}},
        {"type": "Feature", "properties": {"id": "input_oks_1", "object_type": "oks_connection_point", "flow_tph": 20.0},
         "geometry": {"type": "Point", "coordinates": [37.599967825, 55.750898263]}},
    ]}
    out = {"type": "FeatureCollection", "features": [
        {"type": "Feature",
         "properties": {"id": "v1_net_1", "object_type": "heat_network", "variant_id": "v1",
                        "start_node_id": "input_chamber_1", "end_node_id": "input_oks_1", "flow_tph": 20.0,
                        "diameter": 100, "laying_method": "base", "depth_start": None, "depth_end": None,
                        "cost": 8974800},
         "geometry": {"type": "LineString", "coordinates": [[37.600000000, 55.750000000], [37.599967825, 55.750898263]]}},
        {"type": "Feature",
         "properties": {"id": "v1_summary", "object_type": "variant_summary", "variant_id": "v1", "rank": 1,
                        "construction_cost": 13974800, "chamber_construction_cost": 0,
                        "existing_chamber_tie_in_count": 1, "existing_chamber_tie_in_cost": 5000000,
                        "unconnected_penalty": 0, "calculated_cost": 13974800, "new_network_length": 100.0,
                        "score": 0.6913, "unconnected_oks_ids": []},
         "geometry": None},
    ]}
    report = validate(inp, out, RULES)
    assert errors(report) == []
    (totals,) = report.variants
    assert totals.score_declared == pytest.approx(0.6913, abs=1e-4)
    assert totals.score_recomputed == pytest.approx(0.6913, abs=1e-4)
    assert totals.tie_in_count == 1
    assert totals.calculated_cost_recomputed == pytest.approx(13974800, abs=5)


def test_good_scene_has_no_errors_and_recomputes_score() -> None:
    s = good_scene()
    summary = s.summary()
    report = s.validate()
    assert errors(report) == []
    assert "hydraulics.oversized" not in warning_codes(report)
    (totals,) = report.variants
    assert totals.score_recomputed == pytest.approx(summary["score"], abs=1e-6)
    assert totals.unconnected_computed == []
    # Kспец газопровода учтён в пересчёте стоимости
    assert totals.calculated_cost_recomputed == pytest.approx(summary["calculated_cost"], abs=1.0)


def test_turn_over_90_degrees_is_error() -> None:
    s = base_scene()
    s.oks("P1", (100, 100), 3.0, building=(90, 90, 110, 110))
    s.new_chamber("A", (100, 0), 300)
    # вершина в (100, 60), затем назад-вбок: поворот 100°
    s.segment("s1", [(100, 100), (100, 60), (100 + 40 * 0.9848, 60 + 40 * 0.1736), (100, 0)], "P1", "A", 3.0, 50)
    s.summary()
    report = s.validate()
    turns = report.by_code("geometry.turn")
    assert turns and turns[0].severity == "error"
    assert turns[0].actual == pytest.approx(100.0, abs=0.1)


def test_turn_at_node_along_path_is_checked() -> None:
    s = base_scene()
    s.oks("P1", (100, 100), 3.0, building=(90, 90, 110, 110))
    s.new_chamber("A", (100, 0), 300)
    s.tech("T", (100, 60))
    s.segment("s1", [(100, 100), (100, 60)], "P1", "T", 3.0, 50)
    # из техузла назад под 120° к направлению прихода
    s.segment("s2", [(100, 60), (100 + 30 * 0.866, 60 + 30 * 0.5), (100, 0)], "T", "A", 3.0, 50)
    s.summary()
    report = s.validate()
    node_turns = [f for f in report.by_code("geometry.turn") if f.object_id == "T"]
    assert node_turns and node_turns[0].actual == pytest.approx(120.0, abs=0.1)


def test_following_along_gas_pipeline_is_error() -> None:
    s = Scene()
    s.line("L1", [(0, 0), (200, 0)], 300)
    s.chamber("E1", (0, 0))
    s.oks("P1", (100, 100), 3.0, building=(90, 90, 110, 110))
    s.restriction("gas", "gas_pipeline", line_geom([(101.5, 10), (101.5, 50)]))  # параллельно трассе в 1,5 м
    s.new_chamber("A", (100, 0), 300)
    s.segment("s1", [(100, 100), (100, 0)], "P1", "A", 3.0, 50)
    s.summary()
    report = s.validate()
    along = report.by_code("restriction.along")
    assert along and along[0].severity == "error"
    assert along[0].actual == pytest.approx(1.5 - 0.2 - 0.2, abs=0.01)


def test_base_segment_crossing_gas_without_special_is_error() -> None:
    s = base_scene()
    s.oks("P1", (100, 100), 3.0, building=(90, 90, 110, 110))
    s.new_chamber("A", (100, 0), 300)
    s.segment("s1", [(100, 100), (100, 0)], "P1", "A", 3.0, 50)
    s.summary()
    report = s.validate()
    assert "restriction.crossing_without_special" in error_codes(report)


def test_special_pass_without_2m_margin_is_error() -> None:
    s = base_scene()
    s.oks("P1", (100, 100), 3.0, building=(90, 90, 110, 110))
    s.tech("T1", (100, 31))
    s.tech("T2", (100, 29))
    s.new_chamber("A", (100, 0), 300)
    s.segment("s1", [(100, 100), (100, 31)], "P1", "T1", 3.0, 50)
    s.segment("s2", [(100, 31), (100, 29)], "T1", "T2", 3.0, 50, method="special", k_special=1.25)
    s.segment("s3", [(100, 29), (100, 0)], "T2", "A", 3.0, 50)
    s.summary()
    report = s.validate()
    coverage = report.by_code("special.coverage")
    assert len(coverage) == 2
    assert all(f.expected == 2.0 and f.actual == pytest.approx(1.0, abs=0.01) for f in coverage)


def test_special_pass_road_angle_and_straightness() -> None:
    s = Scene()
    s.line("L1", [(0, 0), (200, 0)], 300)
    s.chamber("E1", (0, 0))
    s.oks("P1", (100, 100), 3.0, building=(90, 90, 110, 110))
    s.restriction("road", "road", box(0, 40, 200, 50))
    s.new_chamber("A", (100, 0), 300)
    s.tech("T1", (100, 53))
    s.tech("T2", (100, 37))
    s.segment("s1", [(100, 100), (100, 53)], "P1", "T1", 3.0, 50)
    s.segment("s2", [(100, 53), (100, 37)], "T1", "T2", 3.0, 50, method="special", k_special=1.60)
    s.segment("s3", [(100, 37), (100, 0)], "T2", "A", 3.0, 50)
    s.summary()
    assert errors(s.validate()) == []

    # косое пересечение под 30° — ошибка угла
    s2 = Scene()
    s2.line("L1", [(0, 0), (200, 0)], 300)
    s2.chamber("E1", (0, 0))
    s2.oks("P1", (100, 100), 3.0, building=(90, 90, 110, 110))
    s2.restriction("road", "road", box(0, 40, 200, 50))
    s2.new_chamber("A", (100, 0), 300)
    dx = 10 / 0.5773  # tan 30°: смещение по x на 10 м высоты
    s2.tech("T1", (100, 53))
    s2.tech("T2", (100 + dx * 1.6, 37))
    s2.segment("s1", [(100, 100), (100, 53)], "P1", "T1", 3.0, 50)
    s2.segment("s2", [(100, 53), (100 + dx * 1.6, 37)], "T1", "T2", 3.0, 50, method="special", k_special=1.60)
    s2.segment("s3", [(100 + dx * 1.6, 37), (100, 0)], "T2", "A", 3.0, 50)
    s2.summary()
    report = s2.validate()
    angles = report.by_code("special.angle")
    assert angles and angles[0].actual == pytest.approx(30.0, abs=0.5)


def test_diameter_must_not_decrease_towards_attachment() -> None:
    s = good_scene()
    for ft in s.output_features:
        p = ft["properties"]
        if p["object_type"] == "heat_network" and p["id"] in ("s1", "s2"):
            p["diameter"] = 80
            p["cost"] = p["length"] * RULES.row(80).unit_cost
    s.summary()
    report = s.validate()
    assert "hydraulics.monotonic" in error_codes(report)
    assert "hydraulics.oversized" in warning_codes(report)


def test_diameter_below_capacity_and_flow_mismatch() -> None:
    s = good_scene()
    for ft in s.output_features:
        p = ft["properties"]
        if p["object_type"] == "heat_network" and p["id"] == "s5":
            p["diameter"] = 50
            p["flow_tph"] = 3.0
            p["cost"] = p["length"] * RULES.row(50).unit_cost
    s.summary()
    report = s.validate()
    codes = error_codes(report)
    assert {"hydraulics.capacity", "hydraulics.flow"} <= codes


def test_limit_length_per_path() -> None:
    s = Scene()
    s.line("L1", [(0, 0), (400, 0)], 300)
    s.chamber("E1", (0, 0))
    s.oks("P1", (100, 250), 3.0, building=(90, 240, 110, 260))
    s.new_chamber("A", (100, 0), 300)
    s.tech("T", (100, 120))
    s.segment("s1", [(100, 250), (100, 120)], "P1", "T", 3.0, 50)
    s.segment("s2", [(100, 120), (100, 0)], "T", "A", 3.0, 50)  # 250 м Ду50 при пределе 181
    s.summary()
    report = s.validate()
    limits = report.by_code("hydraulics.limit_length")
    assert limits and limits[0].severity == "error"
    assert limits[0].actual == pytest.approx(250.0, abs=0.01)
    assert "topology.unjustified_tech_node" in warning_codes(report)

    # Ду65 на всём пути: предел 245 м тоже мал — а Ду80 проходит и не считается завышением
    s2 = Scene()
    s2.line("L1", [(0, 0), (400, 0)], 300)
    s2.chamber("E1", (0, 0))
    s2.oks("P1", (100, 250), 3.0, building=(90, 240, 110, 260))
    s2.new_chamber("A", (100, 0), 300)
    s2.segment("s1", [(100, 250), (100, 0)], "P1", "A", 3.0, 80)
    s2.summary()
    report2 = s2.validate()
    assert errors(report2) == []
    assert "hydraulics.oversized" not in warning_codes(report2)


def test_oversized_diameter_is_warning() -> None:
    s = good_scene()
    for ft in s.output_features:
        p = ft["properties"]
        if p["object_type"] == "heat_network" and p["id"] in ("s3", "s4", "s5"):
            p["diameter"] = 80
            p["cost"] = p["length"] * RULES.row(80).unit_cost * (1.25 if p["id"] == "s4" else 1.0)
        if p["object_type"] == "heat_chamber" and p["id"] == "C":
            p["diameter"] = 80
    s.summary()
    report = s.validate()
    assert errors(report) == []
    oversized = report.by_code("hydraulics.oversized")
    assert oversized and oversized[0].expected == 65 and oversized[0].actual == 80


def test_summary_identities() -> None:
    s = good_scene()
    s.summary(construction_cost=1.0, existing_chamber_tie_in_count=2, score=0.1)
    report = s.validate()
    codes = error_codes(report)
    assert "summary.construction_cost" in codes
    assert "summary.existing_chamber_tie_in_count" in codes
    assert "summary.existing_chamber_tie_in_cost" in codes
    assert "summary.calculated_cost" in codes
    assert "summary.score" in codes


def test_segment_cost_and_length_identities() -> None:
    s = good_scene()
    for ft in s.output_features:
        p = ft["properties"]
        if p["object_type"] == "heat_network" and p["id"] == "s4":
            p["cost"] = p["length"] * RULES.row(65).unit_cost  # без Kспец 1,25
        if p["object_type"] == "heat_network" and p["id"] == "s1":
            p["length"] = p["length"] + 0.05          # cost согласован с length, расходится геометрия
            p["cost"] = p["length"] * RULES.row(50).unit_cost
    s.summary()
    report = s.validate()
    assert {f.object_id for f in report.by_code("cost.segment")} == {"s4"}
    assert {f.object_id for f in report.by_code("cost.length")} == {"s1"}


def test_chamber_cost_by_max_diameter_and_interpretation() -> None:
    s = good_scene()
    for ft in s.output_features:
        p = ft["properties"]
        if p["object_type"] == "heat_chamber" and p["id"] == "A":
            p["diameter"] = 65  # без учёта существующей линии Ду300
            p["cost"] = RULES.chamber_cost(65)
        if p["object_type"] == "heat_chamber" and p["id"] == "C":
            p["diameter"] = 200
            p["cost"] = RULES.chamber_cost(300)
    s.summary()
    report = s.validate()
    by_obj = {(f.object_id, f.severity) for f in report.findings if f.code.startswith("cost.chamber")}
    assert ("A", "warning") in by_obj      # другая трактовка §9.3
    assert ("C", "error") in by_obj


def test_ten_metre_rule_requires_existing_chamber() -> None:
    s = Scene()
    s.line("L1", [(0, 0), (200, 0)], 300)
    s.chamber("E1", (100, 0))          # внутри линии: занято 2 примыкания
    s.oks("P1", (106, 60), 3.0, building=(96, 50, 116, 70))
    s.new_chamber("A", (106, 0), 300)  # 6 м от E1, у которой хватает мест
    s.segment("s1", [(106, 60), (106, 0)], "P1", "A", 3.0, 50)
    s.summary()
    report = s.validate()
    rule = report.by_code("topology.reuse_existing_chamber")
    assert rule and rule[0].severity == "error"
    assert rule[0].actual == pytest.approx(6.0, abs=0.01)


def test_ten_metre_rule_not_applied_when_chamber_full() -> None:
    s = Scene()
    s.line("L1", [(0, 0), (200, 0)], 300)
    s.line("L2", [(100, -100), (100, 100)], 300)
    s.chamber("E1", (100, 0))          # два сквозных участка: 4 примыкания
    s.oks("P1", (120, 60), 3.0, building=(110, 50, 130, 70))
    s.new_chamber("A", (108, 0), 300)
    s.segment("s1", [(120, 60), (120, 12), (108, 0)], "P1", "A", 3.0, 50)
    s.summary()
    report = s.validate()
    assert "topology.reuse_existing_chamber" not in report.codes()


def test_more_than_four_connections_is_error() -> None:
    s = Scene()
    s.line("L1", [(0, 0), (200, 0)], 300)
    s.line("L2", [(100, -100), (100, 100)], 300)
    s.chamber("E1", (100, 0))          # 4 существующих примыкания
    s.oks("P1", (120, 60), 3.0, building=(110, 50, 130, 70))
    s.segment("s1", [(120, 60), (120, 20), (100, 0)], "P1", "E1", 3.0, 50)
    s.summary()
    report = s.validate()
    conn = report.by_code("topology.max_connections")
    assert conn and conn[0].severity == "error" and conn[0].actual == 5
    (totals,) = report.variants
    assert totals.tie_in_count == 1

    # новая камера на линии (2 занято) с тремя новыми участками — тоже 5
    s2 = Scene()
    s2.line("L1", [(0, 0), (200, 0)], 300)
    s2.chamber("E1", (0, 0))
    for i, x in enumerate((60, 100, 140)):
        s2.oks(f"P{i}", (x, 60), 3.0, building=(x - 10, 50, x + 10, 70))
    s2.new_chamber("A", (100, 0), 300)
    s2.segment("s0", [(60, 60), (60, 30), (100, 0)], "P0", "A", 3.0, 50)
    s2.segment("s1", [(100, 60), (100, 0)], "P1", "A", 3.0, 50)
    s2.segment("s2", [(140, 60), (140, 30), (100, 0)], "P2", "A", 3.0, 50)
    s2.summary()
    report2 = s2.validate()
    assert any(f.object_id == "A" and f.actual == 5 for f in report2.by_code("topology.max_connections"))


def test_topology_errors_cycle_leaf_and_attachment() -> None:
    s = base_scene()
    s.oks("P1", (100, 100), 3.0, building=(90, 90, 110, 110))
    s.new_chamber("C", (100, 60), 50)
    s.new_chamber("D", (60, 60), 50)   # висячая камера вне сети
    s.segment("s1", [(100, 100), (100, 60)], "P1", "C", 3.0, 50)
    s.segment("s2", [(100, 60), (60, 60)], "C", "D", 3.0, 50)
    s.summary()
    report = s.validate()
    codes = error_codes(report)
    assert "topology.no_attachment" in codes
    assert "topology.dangling" in codes
    assert "unconnected.points" not in codes  # точка есть в участках — она «подключена» по списку

    s2 = base_scene()
    s2.oks("P1", (100, 100), 3.0, building=(90, 90, 110, 110))
    s2.new_chamber("C", (100, 60), 50)
    s2.new_chamber("A", (100, 0), 300)
    s2.tech("T", (60, 30))
    s2.segment("s1", [(100, 100), (100, 60)], "P1", "C", 3.0, 50)
    s2.segment("s2", [(100, 60), (100, 0)], "C", "A", 3.0, 50)
    s2.segment("s3", [(100, 60), (60, 30)], "C", "T", 3.0, 50)
    s2.segment("s4", [(60, 30), (100, 0)], "T", "A", 3.0, 50)
    s2.summary()
    assert "topology.cycle" in error_codes(s2.validate())


def test_oks_clearance_and_own_polygon_exemption() -> None:
    s = base_scene()
    s.oks("P1", (100, 100), 3.0, building=(90, 90, 110, 110))
    s.restriction("bld_x", "oks", box(70, 60, 97, 80))   # чужое здание в 3 м от трассы
    s.new_chamber("A", (100, 0), 300)
    s.tech("T1", (100, 32))
    s.tech("T2", (100, 28))
    s.segment("s1", [(100, 100), (100, 32)], "P1", "T1", 3.0, 50)
    s.segment("s2", [(100, 32), (100, 28)], "T1", "T2", 3.0, 50, method="special", k_special=1.25)
    s.segment("s3", [(100, 28), (100, 0)], "T2", "A", 3.0, 50)
    s.summary()
    report = s.validate()
    clearance = [f for f in report.by_code("restriction.clearance") if f.severity == "error"]
    assert clearance and clearance[0].actual == pytest.approx(3.0 - 0.2, abs=0.01)
    # собственный полигон P1 не порождает ошибок
    assert not any("bld_P1" in f.message for f in report.findings if f.severity == "error")


def test_final_segment_must_leave_own_polygon_in_one_straight_edge() -> None:
    s = base_scene()
    s.oks("P1", (100, 100), 3.0, building=(90, 90, 110, 110))
    s.new_chamber("A", (100, 0), 300)
    s.tech("T1", (100, 32))
    s.tech("T2", (100, 28))
    # поворот внутри здания: вершина (100, 95) внутри полигона
    s.segment("s1", [(100, 100), (105, 95), (100, 32)], "P1", "T1", 3.0, 50)
    s.segment("s2", [(100, 32), (100, 28)], "T1", "T2", 3.0, 50, method="special", k_special=1.25)
    s.segment("s3", [(100, 28), (100, 0)], "T2", "A", 3.0, 50)
    s.summary()
    report = s.validate()
    assert any(f.severity == "error" for f in report.by_code("geometry.final_segment"))


def _approach_scene(own_ring: list[tuple[float, float]], oks_xy: tuple[float, float],
                    foreign: tuple | None = None) -> Scene:
    """Магистраль по y=0; точка P1 в своём здании, финальный участок уходит вниз к камере A."""
    s = Scene()
    s.source("src", (-50, 0))
    s.line("L1", [(0, 0), (200, 0)], 300)
    s.chamber("E1", (0, 0))
    s.chamber("E2", (200, 0))
    s.oks("P1", oks_xy, 3.0)
    s.restriction("bld_P1", "oks", {"type": "Polygon", "coordinates": [[wgs(x, y) for x, y in own_ring]]})
    if foreign is not None:
        s.restriction("bld_x", "oks", box(*foreign))
    s.new_chamber("A", (100, 0), 300)
    s.segment("s1", [oks_xy, (100, 0)], "P1", "A", 3.0, 50)
    s.summary()
    return s


def test_final_segment_not_from_nearest_boundary_reports_why() -> None:
    square = [(90, 90), (110, 90), (110, 110), (90, 110), (90, 90)]
    # ближайшая стена y=110 (2 м), выход через y=90 (18 м); над зданием свободно — отступление не обосновано
    report = _approach_scene(square, (100, 108)).validate()
    found = report.by_code("geometry.final_segment")
    assert [f.severity for f in found] == ["warning"] and "не обосновано" in found[0].message
    # над ближайшей стеной чужое здание: его полоса отступа перекрывает прямой выход
    report = _approach_scene(square, (100, 108), foreign=(90, 116, 110, 130)).validate()
    found = report.by_code("geometry.final_segment")
    assert [f.severity for f in found] == ["warning"] and "заблокирована" in found[0].message
    # здание буквой «С» с двором 8 м: ближайшая стена — внутренняя, луч от неё снова входит в здание — только info
    c_shape = [(80, 100), (120, 100), (120, 108), (88, 108), (88, 116), (120, 116), (120, 124), (80, 124), (80, 100)]
    report = _approach_scene(c_shape, (100, 106)).validate()
    found = report.by_code("geometry.final_segment")
    assert [f.severity for f in found] == ["info"] and "снова входит в здание" in found[0].message
    assert not error_codes(report)


def test_final_segment_reentering_own_building_is_error() -> None:
    # то же «С», но трасса уходит вверх через двор и верхнее крыло: после первой границы — обычное пересечение
    c_shape = [(80, 100), (120, 100), (120, 108), (88, 108), (88, 116), (120, 116), (120, 124), (80, 124), (80, 100)]
    s = Scene()
    s.source("src", (-50, 150))
    s.line("L1", [(0, 150), (200, 150)], 300)
    s.chamber("E1", (0, 150))
    s.chamber("E2", (200, 150))
    s.oks("P1", (100, 106), 3.0)
    s.restriction("bld_P1", "oks", {"type": "Polygon", "coordinates": [[wgs(x, y) for x, y in c_shape]]})
    s.new_chamber("A", (100, 150), 300)
    s.segment("s1", [(100, 106), (100, 150)], "P1", "A", 3.0, 50)
    s.summary()
    report = s.validate()
    found = [f for f in report.by_code("geometry.final_segment") if f.severity == "error"]
    assert found and "снова входит в собственное здание" in found[0].message


def test_new_segments_must_not_cross() -> None:
    s = base_scene()
    s.oks("P1", (60, 100), 3.0, building=(50, 90, 70, 110))
    s.oks("P2", (140, 100), 3.0, building=(130, 90, 150, 110))
    s.new_chamber("A1", (140, 0), 300)
    s.new_chamber("A2", (60, 0), 300)
    s.tech("T1", (110, 32))
    s.tech("T2", (108.7, 28))
    s.tech("T3", (90, 32))
    s.tech("T4", (91.3, 28))
    s.segment("s1", [(60, 100), (110, 32)], "P1", "T1", 3.0, 50)
    s.segment("s2", [(110, 32), (108.7, 28)], "T1", "T2", 3.0, 50, method="special", k_special=1.25)
    s.segment("s3", [(108.7, 28), (140, 0)], "T2", "A1", 3.0, 50)
    s.segment("s4", [(140, 100), (90, 32)], "P2", "T3", 3.0, 50)
    s.segment("s5", [(90, 32), (91.3, 28)], "T3", "T4", 3.0, 50, method="special", k_special=1.25)
    s.segment("s6", [(91.3, 28), (60, 0)], "T4", "A2", 3.0, 50)
    s.summary()
    report = s.validate()
    assert "geometry.crossing" in error_codes(report)


def test_structure_checks() -> None:
    s = good_scene()
    s.summary(rank=2, unconnected_oks_ids=None)
    out = s.output()
    out["features"][0]["properties"]["id"] = "L1"           # совпадает с входным id
    out["features"][1]["properties"]["id"] = out["features"][2]["properties"]["id"]  # дубль
    out["features"][3]["properties"]["diameter"] = 65.0     # не integer
    report = validate(s.input(), out, RULES)
    codes = error_codes(report)
    assert {"structure.id_unique", "structure.attribute", "structure.rank"} <= codes
    assert any("unconnected_oks_ids" in f.message for f in report.findings if f.severity == "error")


def test_reference_must_match_geometry_and_variant() -> None:
    s = good_scene()
    s.summary()
    out = s.output()
    seg = next(ft for ft in out["features"] if ft["properties"].get("id") == "s1")
    seg["properties"]["end_node_id"] = "A"      # узел есть, но геометрически это не конец линии
    report = validate(s.input(), out, RULES)
    assert "structure.node_coincidence" in error_codes(report)


def test_unconnected_list_and_penalty() -> None:
    s = good_scene()
    s.oks("P3", (30, 100), 4.0, building=(20, 90, 40, 110))
    summary = s.summary()
    assert summary["unconnected_oks_ids"] == ["P3"]
    assert summary["unconnected_penalty"] == pytest.approx(100_000_000 + 500_000 * 4.0)
    report = s.validate()
    assert "unconnected.points" in error_codes(report)
    assert "unconnected.missing" not in error_codes(report)
    report2 = s.validate(allow_unconnected=True)
    assert "unconnected.points" not in error_codes(report2)
    assert "unconnected.points" in warning_codes(report2)
    (totals,) = report2.variants
    assert totals.penalty_recomputed == pytest.approx(summary["unconnected_penalty"])

    s3 = good_scene()
    s3.summary(unconnected_oks_ids=["P1"])
    assert "unconnected.declared_but_connected" in error_codes(s3.validate())


def test_variants_ranked_by_score_and_ids_unique_across_variants() -> None:
    s = good_scene()
    s.summary("v1", rank=1)
    # второй вариант — та же трасса, дороже (Ду100 вместо Ду65 на стволе), rank должен быть 2
    for ft in list(s.output_features):
        p = copy.deepcopy(ft["properties"])
        if p["object_type"] == "variant_summary":
            continue
        p["variant_id"] = "v2"
        p["id"] = f"{p['id']}_v2"
        for k in ("start_node_id", "end_node_id"):
            if k in p and p[k] in ("C", "T1", "T2", "A"):
                p[k] = f"{p[k]}_v2"
        s.output_features.append({"type": "Feature", "properties": p, "geometry": copy.deepcopy(ft["geometry"])})
    s.summary("v2", rank=2, score=0.05)
    report = s.validate()
    assert "structure.rank" in error_codes(report)
    assert "structure.id_unique" not in error_codes(report)
    assert len(report.variants) == 2


def test_yaml_matches_appendix_tables() -> None:
    """Числа таблиц 1, 2 и §3.2 приложения — контроль против случайной правки YAML."""
    assert RULES.row(100).unit_cost == 89748 and RULES.row(100).capacity_tph == 22.3
    assert RULES.row(1400).limit_m == 11276 and RULES.row(1400).width_m == 3.45
    assert RULES.chamber_cost(200) == 3_000_000 and RULES.chamber_cost(250) == 5_000_000
    assert RULES.chamber_cost(1000) == 8_000_000 and RULES.chamber_cost(1200) == 12_000_000
    assert RULES.oks_clearance(400) == 5.0 and RULES.oks_clearance(500) == 7.0 and RULES.oks_clearance(900) == 9.0
    road = RULES.restriction("road")
    assert (road.clearance_m, road.min_angle_deg, road.margin_m, road.k_special) == (1.5, 45, 3.0, 1.60)
    tram = RULES.restriction("tram_tracks")
    assert (tram.clearance_m, tram.margin_m, tram.k_special) == (1.5, 3.0, 1.75)
    gas = RULES.restriction("gas_pipeline")
    assert (gas.clearance_m, gas.margin_m, gas.k_special, gas.gauge_width_m) == (2.0, 2.0, 1.25, 0.40)
    cable = RULES.restriction("power_cable")
    assert (cable.clearance_m, cable.margin_m, cable.k_special, cable.gauge_width_m) == (2.0, 2.0, 1.15, 0.20)
    hn = RULES.restriction("heat_network")
    assert (hn.clearance_m, hn.margin_m, hn.k_special, hn.gauge_by_diameter) == (1.0, 2.0, 1.05, True)
    assert RULES.penalty(2.0) == 101_000_000
    assert RULES.score(13_974_800, 100.0) == pytest.approx(0.6913, abs=1e-4)


def test_overlapping_special_zones_road_and_gas() -> None:
    """Дорога y∈[40,50] и газопровод y=52: зоны [37,53] и [50,54] накладываются, три коллинеарных спецучастка."""
    s = Scene()
    s.line("L1", [(0, 0), (200, 0)], 300)
    s.chamber("E1", (0, 0))
    s.oks("P1", (100, 100), 3.0, building=(90, 90, 110, 110))
    s.restriction("road", "road", box(0, 40, 200, 50))
    s.restriction("gas", "gas_pipeline", line_geom([(0, 52), (200, 52)]))
    s.new_chamber("A", (100, 0), 300)
    for name, y in (("T1", 54), ("T2", 53), ("T3", 50), ("T4", 37)):
        s.tech(name, (100, y))
    s.segment("s1", [(100, 100), (100, 54)], "P1", "T1", 3.0, 50)
    s.segment("s2", [(100, 54), (100, 53)], "T1", "T2", 3.0, 50, method="special", k_special=1.25)
    s.segment("s3", [(100, 53), (100, 50)], "T2", "T3", 3.0, 50, method="special", k_special=1.60)  # наложение
    s.segment("s4", [(100, 50), (100, 37)], "T3", "T4", 3.0, 50, method="special", k_special=1.60)
    s.segment("s5", [(100, 37), (100, 0)], "T4", "A", 3.0, 50)
    s.summary()
    report = s.validate()
    assert errors(report) == []
    assert "geometry.proximity" not in warning_codes(report)
    assert "topology.unjustified_tech_node" not in warning_codes(report)


def test_crossing_existing_network_without_tie_in() -> None:
    s = Scene()
    s.line("L1", [(0, 0), (200, 0)], 300)
    s.chamber("E1", (0, 0))
    s.line("L2", [(0, 30), (200, 30)], 400)
    s.oks("P1", (100, 100), 3.0, building=(90, 90, 110, 110))
    s.new_chamber("A", (100, 0), 300)
    s.tech("T1", (100, 32))
    s.tech("T2", (100, 28))
    s.segment("s1", [(100, 100), (100, 32)], "P1", "T1", 3.0, 50)
    s.segment("s2", [(100, 32), (100, 28)], "T1", "T2", 3.0, 50, method="special", k_special=1.05)
    s.segment("s3", [(100, 28), (100, 0)], "T2", "A", 3.0, 50)
    s.summary()
    assert errors(s.validate()) == []

    s2 = Scene()
    s2.line("L1", [(0, 0), (200, 0)], 300)
    s2.chamber("E1", (0, 0))
    s2.line("L2", [(0, 30), (200, 30)], 400)
    s2.oks("P1", (100, 100), 3.0, building=(90, 90, 110, 110))
    s2.new_chamber("A", (100, 0), 300)
    s2.segment("s1", [(100, 100), (100, 0)], "P1", "A", 3.0, 50)
    s2.summary()
    assert "restriction.crossing_without_special" in error_codes(s2.validate())


def test_special_margin_is_measured_along_trajectory() -> None:
    """Пересечение дороги под 45°: техузел в 3 м по вертикали даёт лишние 1,24 м вдоль трассы."""
    s = Scene()
    s.line("L1", [(0, 0), (200, 0)], 300)
    s.chamber("E1", (0, 0))
    s.oks("P1", (100, 100), 3.0, building=(90, 90, 110, 110))
    s.restriction("road", "road", box(0, 40, 200, 50))
    s.new_chamber("A", (100, 0), 300)
    s.tech("T1", (100, 53))
    s.tech("T2", (116, 37))
    s.segment("s1", [(100, 100), (100, 53)], "P1", "T1", 3.0, 50)
    s.segment("s2", [(100, 53), (116, 37)], "T1", "T2", 3.0, 50, method="special", k_special=1.60)
    s.segment("s3", [(116, 37), (100, 0)], "T2", "A", 3.0, 50)
    s.summary()
    report = s.validate()
    assert errors(report) == []
    extra = report.by_code("special.extra_length")
    assert extra and extra[0].actual == pytest.approx(2 * (3 * 2 ** 0.5 - 3), abs=0.02)


def test_new_chamber_at_existing_chamber_location() -> None:
    s = Scene()
    s.line("L1", [(0, 0), (200, 0)], 300)
    s.chamber("E1", (100, 0))
    s.oks("P1", (100, 100), 3.0, building=(90, 90, 110, 110))
    s.new_chamber("A", (100.2, 0), 300)
    s.segment("s1", [(100, 100), (100.2, 0)], "P1", "A", 3.0, 50)
    s.summary()
    assert "topology.chamber_duplicate" in error_codes(s.validate())


def test_numeric_ids_and_type_preservation() -> None:
    s = Scene()
    s.line(1, [(0, 0), (200, 0)], 300)
    s.chamber(2, (0, 0))
    s.oks(3, (100, 100), 3.0, building=(90, 90, 110, 110))
    s.oks(4, (30, 100), 3.0, building=(20, 90, 40, 110))
    s.new_chamber(101, (100, 0), 300, variant=1)
    s.segment(102, [(100, 100), (100, 0)], 3, 101, 3.0, 50, variant=1)
    s.summary(variant=1)
    report = s.validate(allow_unconnected=True)
    assert errors(report) == []
    (totals,) = report.variants
    assert totals.unconnected_declared == [4] and totals.unconnected_computed == [4]

    # тот же файл, но ссылка строкой "3" вместо числа 3 — предупреждение о типе id
    out = s.output()
    seg = next(ft for ft in out["features"] if ft["properties"].get("id") == 102)
    seg["properties"]["start_node_id"] = "3"
    report2 = validate(s.input(), out, RULES, allow_unconnected=True)
    assert errors(report2) == []
    assert any(f.code == "structure.reference" and f.severity == "warning" for f in report2.findings)
