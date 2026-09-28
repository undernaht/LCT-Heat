"""Проекты и задания расчёта: файловое хранилище и фоновые потоки.

Здесь нет базы данных намеренно: это вычислительный сервис, а учёт проектов и
заданий по ТЗ ведёт Java-приложение с PostgreSQL. Файловая реализация нужна
для разработки, автономного демо и как источник истины о том, какие данные
должно хранить приложение (docs/02-architecture.md).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field as dc_field
from pathlib import Path
from typing import Any

from . import depth
from . import solve as solve_module
from .model import CaseInput, load_case

log = logging.getLogger("heat-network.case")

# В контейнере — общий том с приложением (HEAT_DATA_DIR), локально — data/projects
DATA_DIR = Path(os.environ.get("HEAT_DATA_DIR") or (Path(__file__).resolve().parents[3] / "data" / "projects"))
MAX_WORKERS = 2


@dataclass
class Project:
    id: str
    name: str
    created_at: float
    input_path: str
    stats: dict[str, Any] = dc_field(default_factory=dict)
    issues: list[dict[str, Any]] = dc_field(default_factory=list)
    jobs: list[str] = dc_field(default_factory=list)

    @property
    def dir(self) -> Path:
        return Path(self.input_path).parent


@dataclass
class Job:
    id: str
    project_id: str
    mode: str
    max_variants: int
    effort: str = "standard"
    status: str = "queued"           # queued | running | done | failed
    created_at: float = dc_field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    progress: str = ""
    error: str | None = None
    output_path: str | None = None
    summary: dict[str, Any] | None = None

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data["elapsed_s"] = round(
            ((self.finished_at or time.time()) - self.started_at), 1
        ) if self.started_at else None
        return data


class Store:
    """Проекты на диске, задания в памяти; на старте задания восстанавливаются из meta.json."""

    def __init__(self, root: Path = DATA_DIR):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.projects: dict[str, Project] = {}
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="solve")
        self._load()

    # --- проекты ---

    def _load(self) -> None:
        for meta in self.root.glob("*/project.json"):
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
                project = Project(**{k: v for k, v in data.items() if k in Project.__dataclass_fields__})
                self.projects[project.id] = project
                for job_file in meta.parent.glob("jobs/*.json"):
                    job_data = json.loads(job_file.read_text(encoding="utf-8"))
                    job = Job(**{k: v for k, v in job_data.items() if k in Job.__dataclass_fields__})
                    if job.status in ("queued", "running"):
                        job.status, job.error = "failed", "сервис перезапущен во время расчёта"
                        job.finished_at = job.finished_at or time.time()
                    self.jobs[job.id] = job
            except Exception as exc:  # noqa: BLE001
                log.warning("проект %s не прочитан: %s", meta.parent.name, exc)

    def _save_project(self, project: Project) -> None:
        (project.dir / "project.json").write_text(
            json.dumps(asdict(project), ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def _save_job(self, job: Job) -> None:
        project = self.projects[job.project_id]
        jobs_dir = project.dir / "jobs"
        jobs_dir.mkdir(exist_ok=True)
        (jobs_dir / f"{job.id}.json").write_text(
            json.dumps(asdict(job), ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def stage_upload(self) -> Path:
        """Временный файл для потоковой загрузки; станет input.geojson проекта."""
        staging = self.root / "_staging"
        staging.mkdir(parents=True, exist_ok=True)
        return staging / f"{uuid.uuid4().hex}.geojson"

    def create_project(self, name: str, staged: Path) -> tuple[Project, CaseInput]:
        project_id = uuid.uuid4().hex[:12]
        project_dir = self.root / project_id
        project_dir.mkdir(parents=True)
        input_path = project_dir / "input.geojson"
        staged.replace(input_path)
        case = load_case(input_path, name=name)
        project = Project(
            id=project_id, name=name, created_at=time.time(), input_path=str(input_path),
            stats=case.stats(),
            issues=[{"level": i.level, "where": i.where, "detail": i.detail, "count": i.count} for i in case.issues],
        )
        with self._lock:
            self.projects[project_id] = project
        self._save_project(project)
        return project, case

    def delete_project(self, project_id: str) -> bool:
        import shutil

        project = self.projects.pop(project_id, None)
        if project is None:
            return False
        for job_id in project.jobs:
            self.jobs.pop(job_id, None)
        shutil.rmtree(project.dir, ignore_errors=True)
        return True

    # --- задания ---

    def submit(self, project_id: str, *, mode: str = "2d", max_variants: int = 3, effort: str = "standard") -> Job:
        project = self.projects[project_id]
        job = Job(id=uuid.uuid4().hex[:12], project_id=project_id, mode=mode, max_variants=max_variants, effort=effort)
        with self._lock:
            self.jobs[job.id] = job
            project.jobs.append(job.id)
        self._save_project(project)
        self._save_job(job)
        self._pool.submit(self._run, job)
        return job

    def _run(self, job: Job) -> None:
        project = self.projects[job.project_id]
        job.status, job.started_at, job.progress = "running", time.time(), "поле стоимости"
        self._save_job(job)
        try:
            case = load_case(project.input_path, name=project.name)
            result = solve_module.solve(case, max_variants=job.max_variants, mode=job.mode, effort=job.effort)
            out_dir = project.dir / "results" / job.id
            out_dir.mkdir(parents=True, exist_ok=True)
            output_path = out_dir / "output.geojson"
            output_path.write_text(
                json.dumps(result.to_geojson(mode=job.mode), ensure_ascii=False), encoding="utf-8"
            )
            summary = summarize(result)
            (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
            job.output_path = str(output_path)
            job.summary = summary
            job.status, job.progress = "done", "готово"
        except Exception as exc:  # noqa: BLE001 — любая ошибка расчёта должна попасть в задание
            log.exception("расчёт %s упал", job.id)
            job.status, job.error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            job.finished_at = time.time()
            self._save_job(job)


# --- Сводка для интерфейса ------------------------------------------------------


def summarize(result: solve_module.SolveResult) -> dict[str, Any]:
    """Всё, что нужно интерфейсу помимо GeoJSON: таблицы участков, проверки, узлы."""
    variants = []
    for v in result.variants:
        net = v.network
        segments = []
        for s in net.segments.values():
            segments.append({
                "id": s.id, "start": net.nodes[s.start].id, "end": net.nodes[s.end].id,
                "flow_tph": round(s.flow_tph, 3), "du": s.du, "length_m": round(v.geometry.lengths[s.id], 3),
                "laying": s.laying, "k_special": s.k_special, "special_types": s.special_types,
                "depth_start": s.depth_start, "depth_end": s.depth_end, "k_depth": s.k_depth,
                "cost": v.cost.segment_cost[s.id],
            })
        checks = [
            {"oks_id": c.oks_id, "du": c.du, "run_length_m": c.run_length_m, "limit_m": c.limit_m, "ok": c.ok}
            for c in v.diameter_report.checks
        ]
        nodes = [
            {"id": n.id, "kind": n.kind, "ref": n.ref, "approach_rule": n.approach_rule,
             "on_existing_network": n.on_edge.id if n.on_edge is not None else None,
             "connections_used": net.connections_used(n) if n.kind != "oks" else None}
            for n in net.nodes.values()
        ]
        variants.append({
            "id": v.id, "rank": v.rank, "order": v.order, "score": v.cost.score,
            **v.cost.summary(),
            "unconnected_oks_ids": list(v.cost.unconnected),
            "unconnected_reasons": v.build.reasons,
            "chambers": [{"node_id": c.node_id, "diameter": c.diameter, "cost": c.cost} for c in v.cost.chambers],
            "segments": segments,
            "limit_length_checks": checks,
            "diameter_bumps": v.diameter_report.bumps,
            "special_crossings": len(v.special_report.crossings),
            "special_rebuilt": v.special_report.rebuilt,
            "repairs": v.repairs,
            "unresolved_special": v.notes.get("unresolved_special", []),
            "minimality": v.notes.get("minimality", []),
            "depth": depth.summary(v.depth_report) if v.depth_report else None,
            "existing_impact": v.notes.get("existing_impact"),
            "nodes": nodes,
        })
    return {
        "elapsed_s": round(result.elapsed_s, 1),
        "mode": result.notes.get("mode", "2d"),
        "effort": result.notes.get("effort", "standard"),
        "improvement": result.notes.get("improvement", []),
        "field": result.notes.get("field", {}),
        "stats": result.case.stats(),
        "variants": variants,
    }
