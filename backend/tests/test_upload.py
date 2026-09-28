"""Тесты приёма геоданных.

Файлы приходят от постороннего, поэтому проверяется не только счастливый путь:
архив с обходом путей, распаковочная бомба, координаты не в той проекции,
незнакомые имена слоёв.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import routers
from app.ingest import upload
from app.main import app

client = TestClient(app)


# --- Вспомогательное --------------------------------------------------------


def feature(geometry: dict, **props) -> dict:
    return {"type": "Feature", "geometry": geometry, "properties": props}


def square(lon: float, lat: float, size: float = 0.0004) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [[
            [lon, lat], [lon + size, lat], [lon + size, lat + size],
            [lon, lat + size], [lon, lat],
        ]],
    }


def collection(features: list[dict]) -> bytes:
    return json.dumps(
        {"type": "FeatureCollection", "features": features}, ensure_ascii=False
    ).encode("utf-8")


BUILDINGS = collection([
    feature(square(37.575, 55.763), id="b1", use="residential", floors=9,
            q_total_gcal_h=0.5, q_heating_gcal_h=0.4, q_dhw_gcal_h=0.1),
    feature(square(37.577, 55.764), id="b2", use="public", floors=4,
            q_total_gcal_h=0.3, q_heating_gcal_h=0.27, q_dhw_gcal_h=0.03),
])

HEAT = collection([
    feature({"type": "LineString", "coordinates": [[37.571, 55.766], [37.581, 55.766]]},
            id="he_1", du_mm=200, schedule="130/70", head_available_m=60.0),
])

ROADS = collection([
    feature({"type": "LineString", "coordinates": [[37.571, 55.762], [37.581, 55.762]]},
            osm_id=1, highway="residential"),
])


def archive(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def sandbox(tmp_path: Path, monkeypatch):
    """Каждый тест пишет в свой каталог, не трогая data/cache."""
    monkeypatch.setattr(routers, "CACHE_DIR", tmp_path)
    routers.cached_area.cache_clear()
    routers._solve_cached.cache_clear()
    yield tmp_path


# --- Распознавание имён -----------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("buildings.geojson", "buildings_load"),
        ("Здания.geojson", "buildings_load"),
        ("ЗАСТРОЙКА.GeoJSON", "buildings_load"),
        ("teploset.geojson", "gen_heat_edges"),
        ("тепловые_сети.geojson", "gen_heat_edges"),
        ("Дороги.geojson", "roads"),
        ("УДС.geojson", "roads"),
        ("Уличная сеть.geojson", "roads"),
        ("инженерные_сети.geojson", "gen_utilities"),
        ("участки.geojson", "gen_parcels"),
        ("деревья.geojson", "greenery"),
        ("Водные объекты.geojson", "water"),
        ("Теплокамеры.geojson", "gen_heat_nodes"),
        ("Проектируемые.geojson", "gen_perspective"),
    ],
)
def test_layer_names_are_recognised(filename: str, expected: str):
    assert upload.match_layer(filename) == expected


def test_unknown_layer_name_is_not_guessed():
    assert upload.match_layer("что-то_своё.geojson") is None


# --- Безопасность -----------------------------------------------------------


def test_path_traversal_in_archive_is_rejected():
    """Имя вида ../../etc/passwd в архиве — обычное дело, а не экзотика."""
    payload = archive({"../../evil.geojson": BUILDINGS})
    with pytest.raises(upload.UploadError, match="недопустимое имя"):
        upload.read_archive(payload)


def test_absolute_path_in_archive_is_rejected():
    payload = archive({"/etc/passwd.geojson": BUILDINGS})
    with pytest.raises(upload.UploadError):
        upload.read_archive(payload)


def test_too_many_files_are_rejected():
    payload = archive({f"layer_{i}.geojson": b"{}" for i in range(upload.MAX_FILES + 5)})
    with pytest.raises(upload.UploadError, match="больше"):
        upload.read_archive(payload)


def test_non_geojson_entries_are_skipped():
    payload = archive({
        "buildings.geojson": BUILDINGS,
        "readme.txt": b"hello",
        "script.exe": b"MZ",
    })
    files = upload.read_archive(payload)
    assert [f.name for f in files] == ["buildings.geojson"]


def test_broken_archive_is_rejected():
    with pytest.raises(upload.UploadError, match="не читается"):
        upload.read_archive(b"not a zip at all")


def test_area_name_is_validated():
    with pytest.raises(upload.UploadError):
        upload.validate_name("../побег")
    assert upload.validate_name("  Пресня 2  ") == "Пресня 2"


# --- Границы и проекция -----------------------------------------------------


def test_bounds_are_derived_from_the_data():
    bounds = upload.bounds_of({"buildings_load": BUILDINGS, "roads": ROADS})
    assert bounds[0] == pytest.approx(37.571, abs=1e-3)
    assert bounds[3] == pytest.approx(55.7644, abs=1e-3)


def test_metric_crs_is_chosen_from_the_data():
    meta = upload.build_meta("t", {"buildings_load": BUILDINGS}, "EPSG:4326", None)
    assert meta["metric_crs"] == "EPSG:32637"
    assert meta["bbox_order"] == "lonlat"


def test_projected_coordinates_declared_as_degrees_are_refused():
    """Метры под видом градусов раньше давали NaN уже внутри расчёта."""
    projected = collection([feature({
        "type": "Polygon",
        "coordinates": [[[4181000.0, 7501000.0], [4181050.0, 7501000.0],
                         [4181050.0, 7501050.0], [4181000.0, 7501000.0]]],
    }, id="b1")])

    with pytest.raises(upload.UploadError, match="не похожи на градусы"):
        upload.build_meta("t", {"buildings_load": projected}, "EPSG:4326", None)


def test_declared_projection_is_accepted():
    projected = collection([feature({
        "type": "Polygon",
        "coordinates": [[[4181000.0, 7501000.0], [4181050.0, 7501000.0],
                         [4181050.0, 7501050.0], [4181000.0, 7501000.0]]],
    }, id="b1")])

    meta = upload.build_meta("t", {"buildings_load": projected}, "EPSG:3857", None)
    assert meta["crs"] == "EPSG:3857"
    assert meta["metric_crs"].startswith("EPSG:326")


# --- Эндпоинт ---------------------------------------------------------------


def upload_archive(entries: dict[str, bytes], name: str = "test-area", **form):
    return client.post(
        "/api/legacy/v1/projects",
        data={"name": name, **form},
        files={"archive": ("area.zip", archive(entries), "application/zip")},
    )


def test_upload_creates_a_working_area():
    response = upload_archive({
        "Здания.geojson": BUILDINGS,
        "теплосети.geojson": HEAT,
        "Дороги.geojson": ROADS,
    })

    assert response.status_code == 201, response.text
    body = response.json()

    assert body["name"] == "test-area"
    assert set(body["recognised"]) == {"buildings_load", "gen_heat_edges", "roads"}
    assert body["counts"]["buildings"] == 2
    assert body["crs"] == "EPSG:32637"
    assert body["import_report"]["dropped"] == 0


def test_uploaded_area_appears_in_the_list_and_can_be_solved():
    upload_archive({
        "buildings.geojson": BUILDINGS,
        "heat.geojson": HEAT,
        "roads.geojson": ROADS,
    }, name="solvable")

    assert "solvable" in client.get("/api/legacy/v1/projects").json()

    summary = client.get("/api/legacy/v1/projects/solvable").json()
    assert summary["counts"]["heat_edges"] == 1
    assert summary["import_report"]["metric_crs"] == "EPSG:32637"

    solved = client.post(
        "/api/legacy/v1/projects/solvable/solve",
        params={"building_id": "b1", "diversify": 1, "resolution": 2.0},
    )
    assert solved.status_code == 200, solved.text
    assert solved.json()["variants"]


def test_missing_required_layer_is_refused_with_a_hint():
    response = upload_archive({"Дороги.geojson": ROADS}, name="incomplete")

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "buildings_load" in detail and "gen_heat_edges" in detail
    assert "incomplete" not in client.get("/api/legacy/v1/projects").json()


def test_unrecognised_files_are_reported_not_silently_dropped():
    response = upload_archive({
        "buildings.geojson": BUILDINGS,
        "heat.geojson": HEAT,
        "непонятный_слой.geojson": ROADS,
    }, name="with-junk")

    body = response.json()
    assert body["unrecognised"] == ["непонятный_слой.geojson"]
    assert any(
        issue["kind"] == "unrecognised" for issue in body["import_report"]["issues"]
    )


def test_separate_files_without_archive_are_accepted():
    response = client.post(
        "/api/legacy/v1/projects",
        data={"name": "loose-files"},
        files=[
            ("files", ("buildings.geojson", BUILDINGS, "application/geo+json")),
            ("files", ("teploset.geojson", HEAT, "application/geo+json")),
        ],
    )

    assert response.status_code == 201, response.text
    assert response.json()["counts"]["buildings"] == 2


def test_empty_upload_is_refused():
    response = client.post("/api/legacy/v1/projects", data={"name": "nothing"},
                           files={"archive": ("x.zip", archive({}), "application/zip")})
    assert response.status_code == 422


def test_bad_area_name_is_refused():
    response = upload_archive({"buildings.geojson": BUILDINGS, "heat.geojson": HEAT},
                              name="../../побег")
    assert response.status_code == 422


def test_upload_replaces_area_with_the_same_name():
    upload_archive({"buildings.geojson": BUILDINGS, "heat.geojson": HEAT}, name="twice")
    first = client.get("/api/legacy/v1/projects/twice").json()["counts"]["buildings"]

    smaller = collection([feature(square(37.575, 55.763), id="b1", use="residential",
                                 q_total_gcal_h=0.5)])
    upload_archive({"buildings.geojson": smaller, "heat.geojson": HEAT}, name="twice")
    second = client.get("/api/legacy/v1/projects/twice").json()["counts"]["buildings"]

    assert first == 2 and second == 1     # кэш района должен был сброситься


def test_area_can_be_deleted():
    upload_archive({"buildings.geojson": BUILDINGS, "heat.geojson": HEAT},
                   name="temporary")
    assert client.delete("/api/legacy/v1/projects/temporary").status_code == 204
    assert "temporary" not in client.get("/api/legacy/v1/projects").json()


def test_layer_reference_is_published():
    body = client.get("/api/legacy/v1/upload/layers").json()

    assert set(body["required"]) == {"buildings_load", "gen_heat_edges"}
    names = {entry["layer"] for entry in body["layers"]}
    assert "roads" in names and "greenery" in names
    assert body["limits"]["max_files"] == upload.MAX_FILES
