"""Тесты приёма геоданных.

Каждый тест здесь соответствует сценарию, который аудит воспроизвёл на живом
коде и который раньше приводил либо к голому 500, либо — что хуже — к молчаливой
потере объектов при успешном ответе.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ingest.area import load_area, resolve_crs

MOSCOW_BBOX_LONLAT = [37.570, 55.760, 37.582, 55.768]
MOSCOW_BBOX_LATLON = [55.760, 37.570, 55.768, 37.582]


def write(area_dir: Path, name: str, features: list[dict]) -> None:
    (area_dir / f"{name}.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False),
        encoding="utf-8",
    )


def square(lon: float, lat: float, size: float = 0.0005) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [[
            [lon, lat], [lon + size, lat], [lon + size, lat + size],
            [lon, lat + size], [lon, lat],
        ]],
    }


def building(identifier: str, lon: float = 37.575, lat: float = 55.763, **props) -> dict:
    return {
        "type": "Feature",
        "geometry": square(lon, lat),
        "properties": {
            "id": identifier, "use": "residential", "floors": 9,
            "q_total_gcal_h": 0.5, "q_heating_gcal_h": 0.4, "q_dhw_gcal_h": 0.1,
            **props,
        },
    }


def heat_edge(identifier: str = "he_1") -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "LineString",
                     "coordinates": [[37.571, 55.766], [37.581, 55.766]]},
        "properties": {"id": identifier, "du_mm": 200, "schedule": "130/70"},
    }


@pytest.fixture
def area_dir(tmp_path: Path) -> Path:
    (tmp_path / "meta.json").write_text(
        json.dumps({
            "name": "t", "bbox": MOSCOW_BBOX_LONLAT, "bbox_order": "lonlat",
            "crs": "EPSG:4326", "metric_crs": "EPSG:32637",
        }),
        encoding="utf-8",
    )
    write(tmp_path, "buildings_load", [building("b1")])
    write(tmp_path, "gen_heat_edges", [heat_edge()])
    write(tmp_path, "roads", [{
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": [[37.571, 55.762], [37.581, 55.762]]},
        "properties": {"osm_id": 1, "highway": "residential"},
    }])
    return tmp_path


# --- Системы координат ------------------------------------------------------


def test_metric_crs_is_taken_from_meta(area_dir: Path):
    source, metric = resolve_crs(json.loads((area_dir / "meta.json").read_text("utf-8")))
    assert source == "EPSG:4326"
    assert metric == "EPSG:32637"


def test_bbox_order_is_declared_not_guessed():
    """Для Москвы и широта, и долгота меньше 90 — угадать порядок нельзя."""
    lonlat = {"bbox": MOSCOW_BBOX_LONLAT, "bbox_order": "lonlat"}
    latlon = {"bbox": MOSCOW_BBOX_LATLON}          # старый формат по умолчанию

    assert resolve_crs(lonlat)[1] == "EPSG:32637"
    assert resolve_crs(latlon)[1] == "EPSG:32637"


def test_wrong_bbox_order_used_to_give_a_different_utm_zone():
    """Регрессия: без явного порядка стандартный bbox давал EPSG:32640."""
    from app.geo.crs import utm_epsg_for_bbox

    correct = utm_epsg_for_bbox(37.570, 55.760, 37.582, 55.768)
    swapped = utm_epsg_for_bbox(55.760, 37.570, 55.768, 37.582)

    assert correct == "EPSG:32637"
    assert swapped != correct          # именно эта подмена и происходила молча


def test_foreign_projection_is_honoured(tmp_path: Path):
    """Датасет в EPSG:3857 раньше давал NaN в площадях без единого сообщения."""
    (tmp_path / "meta.json").write_text(
        json.dumps({
            "bbox": [4180000.0, 7500000.0, 4185000.0, 7505000.0],
            "bbox_order": "lonlat", "crs": "EPSG:3857",
        }),
        encoding="utf-8",
    )
    ring = [[4181000.0, 7501000.0], [4181050.0, 7501000.0],
            [4181050.0, 7501050.0], [4181000.0, 7501050.0], [4181000.0, 7501000.0]]
    write(tmp_path, "buildings_load", [{
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "properties": {"id": "b1", "use": "residential", "q_total_gcal_h": 0.4},
    }])
    write(tmp_path, "gen_heat_edges", [])
    write(tmp_path, "roads", [])

    area = load_area(tmp_path)

    assert area.import_report.source_crs == "EPSG:3857"
    assert len(area.buildings) == 1
    assert area.buildings[0].footprint_m2 == area.buildings[0].footprint_m2   # не NaN
    assert area.buildings[0].footprint_m2 > 0


# --- Геометрия --------------------------------------------------------------


def test_multipolygon_building_is_not_dropped(area_dir: Path):
    """Выгрузки ЕГРН и ИСОГД штатно приходят Multi-* — раньше они терялись молча."""
    write(area_dir, "buildings_load", [{
        "type": "Feature",
        "geometry": {
            "type": "MultiPolygon",
            "coordinates": [
                square(37.575, 55.763)["coordinates"],
                square(37.577, 55.763)["coordinates"],
            ],
        },
        "properties": {"id": "b1", "use": "residential", "q_total_gcal_h": 0.5},
    }])

    area = load_area(area_dir)
    assert len(area.buildings) == 2


def test_multilinestring_road_is_not_dropped(area_dir: Path):
    write(area_dir, "roads", [{
        "type": "Feature",
        "geometry": {
            "type": "MultiLineString",
            "coordinates": [
                [[37.571, 55.762], [37.575, 55.762]],
                [[37.577, 55.762], [37.581, 55.762]],
            ],
        },
        "properties": {"osm_id": 1, "highway": "residential"},
    }])

    area = load_area(area_dir)
    assert len(area.roads) == 2


def test_null_properties_do_not_crash(area_dir: Path):
    """`properties: null` — валидный GeoJSON по RFC 7946."""
    write(area_dir, "landuse", [{
        "type": "Feature", "geometry": square(37.573, 55.764), "properties": None,
    }])

    area = load_area(area_dir)
    assert len(area.surfaces) == 1


def test_single_feature_file_is_read(area_dir: Path):
    """Одиночный Feature вместо FeatureCollection раньше читался как ноль объектов."""
    (area_dir / "landuse.geojson").write_text(
        json.dumps({"type": "Feature", "geometry": square(37.573, 55.764),
                    "properties": {"landuse": "grass"}}),
        encoding="utf-8",
    )

    area = load_area(area_dir)
    assert len(area.surfaces) == 1


# --- Протокол импорта -------------------------------------------------------


def test_missing_required_field_is_reported_not_raised(area_dir: Path):
    """Раньше отсутствие `id` давало KeyError и голый 500."""
    write(area_dir, "buildings_load", [{
        "type": "Feature", "geometry": square(37.575, 55.763),
        "properties": {"use": "residential", "q_total_gcal_h": 0.5},
    }])

    area = load_area(area_dir)

    assert area.buildings == []
    issues = area.import_report.summary()["issues"]
    assert any(i["kind"] == "missing_field" and "id" in i["detail"] for i in issues)


def test_unknown_enum_value_falls_back_and_is_reported(area_dir: Path):
    write(area_dir, "buildings_load", [building("b1", use="apartments")])

    area = load_area(area_dir)

    assert len(area.buildings) == 1
    assert area.buildings[0].use.value == "other"
    assert any(i["kind"] == "bad_value" for i in area.import_report.summary()["issues"])


def test_missing_layer_file_is_reported(area_dir: Path):
    (area_dir / "gen_heat_edges.geojson").unlink()

    area = load_area(area_dir)
    report = area.import_report

    assert not report.ok                       # теплосети обязательны
    assert any(i["kind"] == "missing_file" for i in report.summary()["issues"])


def test_optional_layer_absence_is_not_fatal(area_dir: Path):
    area = load_area(area_dir)                  # нет greenery, landuse, utilities
    assert area.import_report.ok


def test_report_counts_what_was_loaded(area_dir: Path):
    area = load_area(area_dir)
    loaded = area.import_report.summary()["loaded"]

    assert loaded["buildings_load"] == 1
    assert loaded["gen_heat_edges"] == 1
    assert loaded["roads"] == 1


def test_broken_json_is_reported_not_raised(area_dir: Path):
    (area_dir / "landuse.geojson").write_text("{ это не json", encoding="utf-8")

    area = load_area(area_dir)
    assert any(i["kind"] == "bad_value" for i in area.import_report.summary()["issues"])


# --- Атрибуты, которые раньше терялись --------------------------------------


def test_building_height_reaches_the_model(area_dir: Path):
    """Единственный настоящий источник высоты для 3D раньше выбрасывался."""
    write(area_dir, "buildings_load", [building("b1", height_m=31.5)])

    area = load_area(area_dir)
    assert area.buildings[0].height_m == pytest.approx(31.5)


def test_electrified_railway_is_marked(area_dir: Path):
    """Тег читался и не использовался: применялся отступ 4,0 м вместо 10,75 м."""
    write(area_dir, "railways", [{
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": [[37.572, 55.761], [37.580, 55.761]]},
        "properties": {"osm_id": 7, "railway": "rail", "electrified": "contact_line"},
    }])

    area = load_area(area_dir)
    assert area.railways[0].electrified is True


def test_non_electrified_railway_is_not_marked(area_dir: Path):
    write(area_dir, "railways", [{
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": [[37.572, 55.761], [37.580, 55.761]]},
        "properties": {"osm_id": 7, "railway": "rail", "electrified": "no"},
    }])

    area = load_area(area_dir)
    assert area.railways[0].electrified is False
