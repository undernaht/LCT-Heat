"""Тесты API.

Расчётные эндпоинты прогоняются на сгенерированном районе, если он есть:
на чистой машине датасета нет, и тесты не должны от него падать.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.routers import CACHE_DIR
from app.main import app

client = TestClient(app)

AREA = "small"
has_area = (CACHE_DIR / AREA / "meta.json").exists()
needs_area = pytest.mark.skipif(not has_area, reason="нет сгенерированного района small")


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_unknown_project_returns_404():
    assert client.get("/api/legacy/v1/projects/no-such-area").status_code == 404


def test_normatives_expose_clauses():
    """UI должен уметь показать, откуда взялась каждая цифра."""
    data = client.get("/api/legacy/v1/normatives").json()

    assert data["meta"]["source"].startswith("СП 124.13330")
    assert all(rule["clause"] for rule in data["horizontal"])
    assert data["hydraulics"]["roughness_m"]["water_heat_network"] == 0.0005


@needs_area
def test_projects_list_contains_area():
    assert AREA in client.get("/api/legacy/v1/projects").json()


@needs_area
def test_project_summary():
    data = client.get(f"/api/legacy/v1/projects/{AREA}").json()

    assert data["crs"].startswith("EPSG:")
    assert data["counts"]["heat_edges"] > 0
    assert len(data["bbox"]) == 4


@needs_area
def test_layer_returns_geojson_in_wgs84():
    data = client.get(f"/api/legacy/v1/projects/{AREA}/layers/heat_edges").json()

    assert data["type"] == "FeatureCollection"
    lon, lat = data["features"][0]["geometry"]["coordinates"][0]
    assert 36.0 < lon < 39.0 and 55.0 < lat < 56.5     # Москва


@needs_area
def test_unknown_layer_returns_404():
    assert client.get(f"/api/legacy/v1/projects/{AREA}/layers/nope").status_code == 404


@needs_area
def test_perspective_buildings_come_first():
    data = client.get(
        f"/api/legacy/v1/projects/{AREA}/buildings", params={"perspective_only": True}
    ).json()

    assert data and all(b["is_perspective"] for b in data)
    assert all(b["q_total_gcal_h"] > 0 for b in data)


@needs_area
def test_solve_returns_variants_with_full_payload():
    response = client.post(
        f"/api/legacy/v1/projects/{AREA}/solve",
        params={"building_id": "persp_1", "diversify": 1, "resolution": 2.0},
    )
    assert response.status_code == 200
    data = response.json()

    assert data["variants"]
    variant = data["variants"][0]

    assert variant["geometry"]["type"] == "LineString"
    assert variant["length_m"] > 0
    assert variant["hydraulics"]["du_mm"] >= 32
    assert variant["cost"]["total_rub"] > 0
    assert variant["cost"]["rub_per_m"] > 0
    assert variant["compliance"]["status"] in {"pass", "conditional", "fail"}
    assert variant["candidates"]

    # Смета не должна включать веса поиска
    assert variant["cost"]["search_penalty_rub"] >= 0
    rows_total = sum(row["value_rub"] for row in variant["cost"]["rows"])
    assert rows_total == pytest.approx(variant["cost"]["total_rub"], rel=0.01)


@needs_area
def test_solve_rejects_unknown_building():
    response = client.post(
        f"/api/legacy/v1/projects/{AREA}/solve", params={"building_id": "nope"}
    )
    assert response.status_code == 404


@needs_area
def test_pareto_ids_reference_existing_variants():
    data = client.post(
        f"/api/legacy/v1/projects/{AREA}/solve",
        params={"building_id": "persp_1", "diversify": 2, "resolution": 2.0},
    ).json()

    ids = {v["id"] for v in data["variants"]}
    assert set(data["pareto_ids"]) <= ids
    assert all(v["is_pareto"] == (v["id"] in data["pareto_ids"]) for v in data["variants"])
