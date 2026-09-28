"""HTTP-эндпоинты.

Расчёт синхронный: замер на районе `small` — 2–3 с на все варианты, из них
основное время занимает волна Дейкстры. Городить очередь задач ради этого
незачем; район держится в памяти между запросами, поэтому повторный расчёт
не перечитывает GeoJSON. Если районы вырастут настолько, что расчёт перевалит
за десяток секунд, здесь появится фоновая задача с опросом статуса.
"""

from __future__ import annotations

import json
import shutil
import time
from functools import lru_cache
from pathlib import Path

from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import JSONResponse
from shapely.geometry import LineString

from ..config import get_config
from ..domain.enums import Laying
from ..domain.models import AreaModel
from ..export import dxf as dxf_export
from ..export import geojson as geojson_export
from ..export import pdf as pdf_export
from ..geo import crs
from ..ingest import upload
from ..ingest.area import load_area
from ..loads import estimate as loads
from ..routing import alternatives, group, manual
from . import schemas

CACHE_DIR = Path(__file__).resolve().parents[3] / "data" / "cache"

LAYERS = {
    "buildings": "buildings_load",
    "roads": "roads",
    "railways": "railways",
    "water": "water",
    "landuse": "landuse",
    "heat_edges": "gen_heat_edges",
    "heat_nodes": "gen_heat_nodes",
    "utilities": "gen_utilities",
    "parcels": "gen_parcels",
    "perspective": "gen_perspective",
}

router = APIRouter()


# --- Районы -----------------------------------------------------------------


def area_dir(name: str) -> Path:
    path = CACHE_DIR / name
    if not (path / "meta.json").exists():
        raise HTTPException(404, f"Район «{name}» не найден. Запусти data/fetch_osm.py")
    return path


@lru_cache(maxsize=4)
def cached_area(name: str) -> AreaModel:
    """Район держится в памяти: повторный расчёт не перечитывает GeoJSON."""
    return load_area(area_dir(name))


@router.get("/projects", response_model=list[str])
def list_projects() -> list[str]:
    if not CACHE_DIR.exists():
        return []
    return sorted(p.name for p in CACHE_DIR.iterdir() if (p / "meta.json").exists())


@router.get("/projects/{name}", response_model=schemas.AreaSummary)
def get_project(name: str) -> schemas.AreaSummary:
    path = area_dir(name)
    meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))

    generated: dict = {}
    generated_path = path / "generated_meta.json"
    if generated_path.exists():
        generated = json.loads(generated_path.read_text(encoding="utf-8"))

    area = cached_area(name)
    report = area.import_report
    return schemas.AreaSummary(
        name=name,
        crs=area.crs,
        bbox=_bbox_lonlat(meta),
        counts=area.stats(),
        perspective=generated.get("perspective", []),
        normatives_verified=get_config().normatives_verified,
        import_report=report.summary() if report else None,
    )


def _bbox_lonlat(meta: dict) -> list[float]:
    """bbox в порядке RFC 7946 независимо от того, как он записан в meta.json."""
    bbox = list(meta["bbox"])
    if str(meta.get("bbox_order", "latlon")).lower() == "lonlat":
        return bbox
    min_lat, min_lon, max_lat, max_lon = bbox
    return [min_lon, min_lat, max_lon, max_lat]


@router.post("/projects", response_model=schemas.UploadResult, status_code=201)
async def upload_project(
    name: str = Form(..., description="имя района"),
    source_crs: str = Form("EPSG:4326", alias="crs"),
    archive: UploadFile | None = File(None, description="ZIP со слоями GeoJSON"),
    files: list[UploadFile] = File(default_factory=list, description="отдельные GeoJSON"),
) -> schemas.UploadResult:
    """Принять массив геоданных: ZIP-архив либо набор GeoJSON-файлов.

    Имена слоёв распознаются по словарю синонимов — выгрузка почти никогда не
    называется так, как ждёт наш код. Охват и метрическая проекция выводятся из
    самих данных. После записи район сразу читается разбором, и пользователь
    получает протокол импорта: что загружено, что отброшено и почему.
    """
    collected: list[upload.UploadedFile] = []

    try:
        if archive is not None:
            collected.extend(upload.read_archive(await archive.read()))
        for item in files:
            if item.filename:
                collected.append(
                    upload.UploadedFile(name=item.filename, data=await item.read())
                )
        if not collected:
            raise upload.UploadError("не приложено ни одного файла")

        found = upload.recognise(collected)
        target, notes = upload.write_area(CACHE_DIR, name, found, source_crs)
    except upload.UploadError as error:
        raise HTTPException(422, str(error)) from error

    # Кэш района мог хранить прежнюю версию с тем же именем
    cached_area.cache_clear()
    _solve_cached.cache_clear()

    try:
        area = cached_area(target.name)
    except Exception as error:                       # разбор упал на чужих данных
        shutil.rmtree(target, ignore_errors=True)
        raise HTTPException(422, f"данные не удалось разобрать: {error}") from error

    report = area.import_report
    summary = report.summary() if report else {}
    summary.setdefault("issues", [])
    summary["issues"] = notes.summary()["issues"] + summary["issues"]

    if report and not report.ok:
        shutil.rmtree(target, ignore_errors=True)
        cached_area.cache_clear()
        raise HTTPException(
            422,
            {
                "message": "в данных нет слоёв, без которых расчёт невозможен",
                "import_report": summary,
            },
        )

    return schemas.UploadResult(
        name=target.name,
        recognised={layer: len(raw) for layer, raw in found.layers.items()},
        unrecognised=found.unknown,
        counts=area.stats(),
        crs=area.crs,
        import_report=summary,
    )


@router.delete("/projects/{name}", status_code=204)
def delete_project(name: str) -> Response:
    """Удалить загруженный район."""
    path = area_dir(name)
    shutil.rmtree(path, ignore_errors=True)
    cached_area.cache_clear()
    _solve_cached.cache_clear()
    return Response(status_code=204)


@router.get("/upload/layers")
def upload_layers() -> dict:
    """Какие слои ждёт сервис и как их можно называть."""
    return {
        "layers": upload.expected_layers(),
        "required": list(upload.REQUIRED_LAYERS),
        "limits": {
            "max_archive_mb": upload.MAX_ARCHIVE_BYTES // 1024 // 1024,
            "max_files": upload.MAX_FILES,
            "suffixes": sorted(upload.ALLOWED_SUFFIXES),
        },
    }


@router.get("/projects/{name}/layers/{layer}")
def get_layer(name: str, layer: str) -> JSONResponse:
    """Слой в GeoJSON (EPSG:4326) прямо из кэша — без перепроецирования."""
    if layer not in LAYERS:
        raise HTTPException(404, f"Неизвестный слой «{layer}». Доступны: {sorted(LAYERS)}")

    path = area_dir(name) / f"{LAYERS[layer]}.geojson"
    if not path.exists():
        return JSONResponse({"type": "FeatureCollection", "features": []})
    return JSONResponse(json.loads(path.read_text(encoding="utf-8")))


@router.get("/projects/{name}/buildings", response_model=list[schemas.BuildingBrief])
def list_buildings(
    name: str,
    perspective_only: bool = Query(False, description="только перспективные здания"),
    limit: int = Query(500, ge=1, le=5000),
) -> list[schemas.BuildingBrief]:
    area = cached_area(name)
    result: list[schemas.BuildingBrief] = []

    for building in area.buildings:
        if perspective_only and not building.is_perspective:
            continue
        centroid = crs.to_wgs84(building.geom.centroid, area.crs)
        result.append(
            schemas.BuildingBrief(
                id=building.id,
                name=building.tags.get("name"),
                use=building.use.value,
                floors=loads.floors_of(building),
                is_perspective=building.is_perspective,
                q_total_gcal_h=round(building.load.total, 4) if building.load else 0.0,
                footprint_m2=round(building.footprint_m2, 1),
                centroid=[centroid.x, centroid.y],
            )
        )
        if len(result) >= limit:
            break

    result.sort(key=lambda b: (not b.is_perspective, -b.q_total_gcal_h))
    return result


# --- Расчёт -----------------------------------------------------------------


@lru_cache(maxsize=16)
def _solve_cached(
    name: str, building_id: str, laying: Laying, resolution: float, diversify: int
) -> tuple[alternatives.VariantSet, float]:
    """Результат расчёта кэшируется: экспорт не пересчитывает то же самое заново."""
    area = cached_area(name)
    building = area.building(building_id)
    if building is None:
        raise HTTPException(404, f"Здание «{building_id}» не найдено")
    if building.load is None or building.load.total <= 0:
        raise HTTPException(422, f"У здания «{building_id}» нулевая тепловая нагрузка")

    started = time.perf_counter()
    result = alternatives.solve_variants(
        area, building, laying=laying, resolution=resolution, diversify=diversify
    )
    elapsed = time.perf_counter() - started

    if not result.variants:
        raise HTTPException(
            422,
            "Трасса не найдена: здание отрезано запретными зонами "
            "либо поблизости нет допустимой точки врезки",
        )
    return result, elapsed


@router.post("/projects/{name}/solve", response_model=schemas.SolveOut)
def solve(
    name: str,
    building_id: str = Query(..., description="id здания для подключения"),
    laying: Laying = Query(Laying.CHANNEL),
    resolution: float = Query(1.0, ge=0.5, le=5.0),
    diversify: int = Query(2, ge=1, le=4),
) -> schemas.SolveOut:
    result, elapsed = _solve_cached(name, building_id, laying, resolution, diversify)
    return schemas.solve_out(result, cached_area(name).crs, elapsed)


@router.post("/projects/{name}/validate", response_model=schemas.ManualEvalOut)
def validate_route(name: str, payload: schemas.ManualRouteIn) -> schemas.ManualEvalOut:
    """Проверить трассу, нарисованную или поправленную руками.

    Автоматический оптимум не приговор: инженер знает то, чего нет в данных.
    Поправленная трасса проходит те же проверки теми же модулями — диаметр,
    потери, смету, плату, полный протокол нормоконтроля — и сравнивается с
    расчётной, чтобы было видно, сколько стоит ручное решение.
    """
    area = cached_area(name)
    building = area.building(payload.building_id)
    if building is None:
        raise HTTPException(404, f"Здание «{payload.building_id}» не найдено")

    try:
        laying = Laying(payload.laying)
    except ValueError:
        raise HTTPException(422, f"Неизвестный способ прокладки: {payload.laying}") from None

    try:
        line = crs.to_metric(LineString(payload.coordinates), area.crs)
    except (ValueError, TypeError) as error:
        raise HTTPException(422, f"Ломаная не читается: {error}") from error

    if payload.snap:
        line = manual.snap_to_building(area, building, line)
        line = manual.snap_to_network(area, line)

    started = time.perf_counter()
    result = manual.evaluate(
        area, building, line, laying=laying, resolution=payload.resolution
    )
    if result is None:
        raise HTTPException(
            422, "У здания нулевая нагрузка либо диаметр не подбирается"
        )

    if payload.compare:
        try:
            variants, _ = _solve_cached(
                name, payload.building_id, laying, payload.resolution, 1
            )
            reference = variants.variants[0] if variants.variants else None
        except HTTPException:
            reference = None
        if reference is not None:
            result.reference_length_m = round(reference.length_m, 1)
            result.reference_cost_rub = reference.cost.total_rub
            result.reference_risk = reference.risk.score

    return schemas.manual_out(result, line, area.crs, time.perf_counter() - started)


@router.post("/projects/{name}/solve-group", response_model=schemas.GroupSolveOut)
def solve_group(
    name: str,
    building_ids: str | None = Query(
        None, description="id зданий через запятую; по умолчанию все перспективные"
    ),
    laying: Laying = Query(Laying.CHANNEL),
    resolution: float = Query(1.0, ge=0.5, le=5.0),
) -> schemas.GroupSolveOut:
    """Подключить группу зданий общим коридором и сравнить с раздельным."""
    area = cached_area(name)

    if building_ids:
        wanted = [part.strip() for part in building_ids.split(",") if part.strip()]
        targets = [area.building(bid) for bid in wanted]
        missing = [bid for bid, b in zip(wanted, targets) if b is None]
        if missing:
            raise HTTPException(404, f"Здания не найдены: {missing}")
        buildings = [b for b in targets if b is not None]
    else:
        buildings = [b for b in area.buildings if b.is_perspective]

    if len(buildings) < 2:
        raise HTTPException(422, "Для группового подключения нужно минимум два здания")

    started = time.perf_counter()
    result = group.solve_group(area, buildings, laying=laying, resolution=resolution)
    elapsed = time.perf_counter() - started

    if result is None:
        raise HTTPException(422, "Группу подключить не удалось")

    return schemas.group_out(result, area.crs, elapsed)


@router.get("/projects/{name}/export")
def export_variant(
    name: str,
    building_id: str = Query(...),
    variant_id: str = Query(..., description="id варианта из ответа solve"),
    fmt: str = Query("geojson", pattern="^(geojson|dxf|pdf)$", alias="format"),
    laying: Laying = Query(Laying.CHANNEL),
    resolution: float = Query(1.0, ge=0.5, le=5.0),
    diversify: int = Query(2, ge=1, le=4),
) -> Response:
    """Выгрузка варианта: GeoJSON для ГИС, DXF для CAD, PDF с проектом техусловий."""
    area = cached_area(name)
    building = area.building(building_id)
    if building is None:
        raise HTTPException(404, f"Здание «{building_id}» не найдено")

    result, _ = _solve_cached(name, building_id, laying, resolution, diversify)
    solution = result.variant(variant_id)
    if solution is None:
        available = [alternatives.variant_id_of(v) for v in result.variants]
        raise HTTPException(404, f"Вариант «{variant_id}» не найден. Есть: {available}")

    stem = f"{name}_{building_id}_{variant_id}".replace("+", "-")

    if fmt == "geojson":
        body = json.dumps(
            geojson_export.export(solution, area.crs), ensure_ascii=False
        ).encode("utf-8")
        media_type = "application/geo+json"
        filename = f"{stem}.geojson"
    elif fmt == "dxf":
        body = dxf_export.export(solution, area, building)
        media_type = "application/dxf"
        filename = f"{stem}.dxf"
    else:
        body = pdf_export.export(solution, building, area_name=name)
        media_type = "application/pdf"
        filename = f"{stem}_ТУ.pdf"

    return Response(
        content=body,
        media_type=media_type,
        headers={
            # filename* — иначе кириллица в имени файла ломает заголовок
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
        },
    )


@router.get("/normatives")
def get_normatives() -> dict:
    """Нормативный профиль — чтобы UI мог показать, откуда взялась каждая цифра."""
    normatives = get_config().normatives
    return {
        "meta": normatives["meta"],
        "site": normatives["site"],
        "horizontal": normatives["horizontal"],
        "vertical": normatives["vertical"],
        "hydraulics": normatives["hydraulics"],
    }
