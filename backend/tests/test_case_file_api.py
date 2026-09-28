"""Внутренний контракт Python-сервиса с Java-приложением: файлы по путям на диске."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import app
from tests.test_case_solve import basic_area

client = TestClient(app)


def test_inspect_solve_validate_by_paths(tmp_path):
    source = tmp_path / "input.geojson"
    source.write_text(json.dumps(basic_area()), encoding="utf-8")
    output = tmp_path / "results" / "job" / "output.geojson"

    inspected = client.post("/api/v1/inspect-file", json={"input_path": str(source), "name": "тест"})
    assert inspected.status_code == 200
    assert inspected.json()["stats"]["oks_points"] == 1

    solved = client.post("/api/v1/solve-file", json={
        "input_path": str(source), "output_path": str(output), "mode": "2d", "max_variants": 1,
        "effort": "standard", "name": "тест",
    })
    assert solved.status_code == 200, solved.text
    body = solved.json()
    assert body["status"] == "done" and output.exists()
    assert (output.parent / "summary.json").exists()
    assert body["summary"]["variants"][0]["unconnected_oks_ids"] == []

    validated = client.post("/api/v1/validate-file", json={"input_path": str(source), "output_path": str(output)})
    assert validated.status_code == 200, validated.text
    report = validated.json()
    assert report["variants"][0]["errors"] == 0


def test_validate_file_missing_paths_is_404(tmp_path):
    response = client.post("/api/v1/validate-file", json={
        "input_path": str(tmp_path / "no.geojson"), "output_path": str(tmp_path / "no_out.geojson"),
    })
    assert response.status_code == 404
