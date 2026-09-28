"""HTTP-API конкурсной модели.

Форма API повторяется Java-приложением по ТЗ (docs/02-architecture.md), поэтому
фронтенд работает одинаково с обоими. Здесь же — внутренний вызов для Java:
`POST /solve-file` по пути к файлу на общем томе, без загрузки через HTTP.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from . import jobs as jobs_module
from . import solve as solve_module
from .model import load_case
from .rules import get_rules

router = APIRouter(tags=["кейс ЛЦТ"])
store = jobs_module.Store()

MAX_UPLOAD_BYTES = 3 * 1024 ** 3


# --- схемы ---------------------------------------------------------------------


class SolveRequest(BaseModel):
    mode: str = Field("2d", pattern="^(2d|depth)$")
    max_variants: int = Field(3, ge=1, le=3)
    # standard — три порядка точек; thorough — плюс перестановки у лучшего (дольше вдвое-втрое)
    effort: str = Field("standard", pattern="^(standard|thorough)$")


class SolveFileRequest(BaseModel):
    """Внутренний вызов из Java: файлы на общем томе."""

    input_path: str
    output_path: str
    mode: str = Field("2d", pattern="^(2d|depth)$")
    max_variants: int = Field(3, ge=1, le=3)
    effort: str = Field("standard", pattern="^(standard|thorough)$")
    name: str = ""


# --- проекты --------------------------------------------------------------------


@router.get("/projects")
def list_projects() -> list[dict[str, Any]]:
    return [
        {"id": p.id, "name": p.name, "created_at": p.created_at, "stats": p.stats,
         "issues": len(p.issues), "jobs": p.jobs}
        for p in sorted(store.projects.values(), key=lambda p: -p.created_at)
    ]


@router.post("/projects", status_code=201)
async def create_project(file: UploadFile = File(...), name: str = Form("")) -> dict[str, Any]:
    """Загрузка потоком на диск: файл до 3 ГБ по ТЗ не должен целиком жить в памяти."""
    project_name = name.strip() or Path(file.filename or "проект").stem
    staged = store.stage_upload()
    size = 0
    with staged.open("wb") as out:
        while chunk := await file.read(4 * 1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                out.close()
                staged.unlink(missing_ok=True)
                raise HTTPException(413, "файл больше 3 ГБ")
            out.write(chunk)
    if size == 0:
        staged.unlink(missing_ok=True)
        raise HTTPException(400, "пустой файл")
    try:
        project, _ = store.create_project(project_name, staged)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        staged.unlink(missing_ok=True)
        raise HTTPException(400, f"не GeoJSON: {exc}") from exc
    return {"id": project.id, "name": project.name, "stats": project.stats, "issues": project.issues}


@router.get("/projects/{project_id}")
def get_project(project_id: str) -> dict[str, Any]:
    project = store.projects.get(project_id)
    if project is None:
        raise HTTPException(404, "проект не найден")
    return {
        "id": project.id, "name": project.name, "created_at": project.created_at,
        "stats": project.stats, "issues": project.issues,
        "jobs": [store.jobs[j].public() for j in project.jobs if j in store.jobs],
    }


@router.delete("/projects/{project_id}", status_code=204, response_class=Response)
def delete_project(project_id: str) -> Response:
    if not store.delete_project(project_id):
        raise HTTPException(404, "проект не найден")
    return Response(status_code=204)


@router.get("/projects/{project_id}/input.geojson")
def project_input(project_id: str) -> FileResponse:
    project = store.projects.get(project_id)
    if project is None:
        raise HTTPException(404, "проект не найден")
    return FileResponse(project.input_path, media_type="application/geo+json")


# --- расчёт ---------------------------------------------------------------------


@router.post("/projects/{project_id}/solve", status_code=202)
def solve_project(project_id: str, body: SolveRequest) -> dict[str, Any]:
    if project_id not in store.projects:
        raise HTTPException(404, "проект не найден")
    job = store.submit(project_id, mode=body.mode, max_variants=body.max_variants, effort=body.effort)
    return job.public()


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = store.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "задание не найдено")
    return job.public()


@router.get("/jobs/{job_id}/output.geojson")
def job_output(job_id: str) -> FileResponse:
    job = store.jobs.get(job_id)
    if job is None or job.status != "done" or not job.output_path:
        raise HTTPException(404, "результат ещё не готов")
    return FileResponse(job.output_path, media_type="application/geo+json",
                        filename=f"result_{job_id}.geojson")


@router.get("/jobs/{job_id}/summary")
def job_summary(job_id: str) -> dict[str, Any]:
    job = store.jobs.get(job_id)
    if job is None or job.status != "done" or job.summary is None:
        raise HTTPException(404, "результат ещё не готов")
    return job.summary


@router.get("/jobs/{job_id}/validation")
def job_validation(job_id: str) -> dict[str, Any]:
    """Протокол независимого валидатора по выходу задания (app/case_validator).

    Считается при первом обращении и кэшируется рядом с результатом: на защите
    это аргумент «мы не просим верить на слово».
    """
    job = store.jobs.get(job_id)
    if job is None or job.status != "done" or not job.output_path:
        raise HTTPException(404, "результат ещё не готов")
    project = store.projects[job.project_id]
    cache = Path(job.output_path).parent / "validation.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    from ..case_validator import validate_files

    report = validate_files(project.input_path, job.output_path, allow_unconnected=True).to_dict()
    cache.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


# --- внутренний вызов для Java-приложения ------------------------------------------


@router.post("/solve-file")
def solve_file(body: SolveFileRequest) -> dict[str, Any]:
    """Синхронный расчёт по путям на диске. Java держит задание и таймаут у себя."""
    input_path = Path(body.input_path)
    if not input_path.exists():
        raise HTTPException(404, f"нет файла {input_path}")
    case = load_case(input_path, name=body.name or input_path.stem)
    result = solve_module.solve(case, max_variants=body.max_variants, mode=body.mode, effort=body.effort)
    output_path = Path(body.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result.to_geojson(mode=body.mode), ensure_ascii=False), encoding="utf-8")
    summary = jobs_module.summarize(result)
    (output_path.parent / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"status": "done", "output_path": str(output_path), "summary": summary}


class InspectFileRequest(BaseModel):
    input_path: str
    name: str = ""


@router.post("/inspect-file")
def inspect_file(body: InspectFileRequest) -> dict[str, Any]:
    """Паспорт входа для Java-приложения: статистика и протокол разбора без расчёта."""
    input_path = Path(body.input_path)
    if not input_path.exists():
        raise HTTPException(404, f"нет файла {input_path}")
    try:
        case = load_case(input_path, name=body.name or input_path.stem)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, f"не GeoJSON: {exc}") from exc
    return {
        "stats": case.stats(),
        "issues": [{"level": i.level, "where": i.where, "detail": i.detail, "count": i.count} for i in case.issues],
    }


class ValidateFileRequest(BaseModel):
    input_path: str
    output_path: str


@router.post("/validate-file")
def validate_file(body: ValidateFileRequest) -> dict[str, Any]:
    """Протокол независимого валидатора по файлам на диске — для Java-приложения
    (то же, что /jobs/{id}/validation, но без своего реестра заданий)."""
    input_path, output_path = Path(body.input_path), Path(body.output_path)
    if not input_path.exists() or not output_path.exists():
        raise HTTPException(404, "нет входного или выходного файла")
    from ..case_validator import validate_files

    return validate_files(input_path, output_path, allow_unconnected=True).to_dict()


@router.get("/rules")
def rules() -> dict[str, Any]:
    """Таблицы приложения — чтобы интерфейс показывал, откуда числа."""
    return get_rules().raw
