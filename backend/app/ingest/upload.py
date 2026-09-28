"""Приём массива геоданных от пользователя.

До этого модуля район можно было подготовить только скриптом: разложить файлы с
точными именами в `data/cache/<район>/` и запустить генератор. Формулировка кейса
начинается со слов «на основе обработки массива геоданных» — то есть массив дают,
а сервис его переваривает. Здесь и появляется вход.

Принимается ZIP-архив или набор GeoJSON-файлов. Имена слоёв распознаются по
словарю синонимов: выгрузка редко называется так, как её ждёт наш код.
"""

from __future__ import annotations

import json
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..geo import crs
from .report import ImportReport

# --- Ограничения. Загрузка идёт от постороннего, поэтому границы жёсткие ---

MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
MAX_UNPACKED_BYTES = 1024 * 1024 * 1024
MAX_FILES = 60
ALLOWED_SUFFIXES = {".geojson", ".json"}


class UploadError(Exception):
    """Данные принять нельзя. Сообщение показывается пользователю как есть."""


# --- Распознавание слоёв ----------------------------------------------------

# Канонические имена, которые ждёт ingest, и то, как их называют в выгрузках.
# Кириллица и латиница вперемешку — так и приходит от заказчиков.
LAYER_ALIASES: dict[str, tuple[str, ...]] = {
    "buildings_load": (
        "buildings_load", "buildings", "building", "здания", "здание",
        "zdaniya", "houses", "zastroyka", "застройка",
    ),
    "gen_perspective": (
        "gen_perspective", "perspective", "перспективные", "перспектива",
        "new_buildings", "planned", "planned_buildings", "проектируемые",
    ),
    "gen_heat_edges": (
        "gen_heat_edges", "heat_edges", "heat", "heating", "teploset",
        "теплосеть", "теплосети", "heat_network", "тепловые_сети",
    ),
    "gen_heat_nodes": (
        "gen_heat_nodes", "heat_nodes", "nodes", "камеры", "теплокамеры",
        "chambers", "teplokamery", "узлы",
    ),
    "gen_utilities": (
        "gen_utilities", "utilities", "сети", "инженерные_сети", "communications",
        "inzhenernye_seti", "коммуникации",
    ),
    "roads": (
        "roads", "дороги", "highways", "ulicy", "улицы", "road", "удс",
        "уличная_сеть", "автодороги", "streets", "проезды",
    ),
    "railways": (
        "railways", "railway", "жд", "железные_дороги", "rail", "железная_дорога",
    ),
    "water": (
        "water", "вода", "hydro", "водные_объекты", "gidro", "гидрография",
    ),
    "landuse": (
        "landuse", "землепользование", "zemlepolzovanie", "zones", "зоны",
        "функциональные_зоны", "озеленение",
    ),
    "greenery": (
        "greenery", "деревья", "trees", "зелень", "зелёные_насаждения",
        "zelenye_nasazhdeniya", "green",
    ),
    "gen_parcels": (
        "gen_parcels", "parcels", "участки", "кадастр", "kadastr",
        "cadastre", "земельные_участки",
    ),
}

REQUIRED_LAYERS = ("buildings_load", "gen_heat_edges")


def normalise(name: str) -> str:
    """Имя файла → ключ для сопоставления: без расширения, регистра и разделителей."""
    stem = Path(name).stem.lower()
    stem = re.sub(r"[\s\-.]+", "_", stem)
    return re.sub(r"[^0-9a-zа-яё_]+", "", stem)


def match_layer(filename: str) -> str | None:
    """Канонический слой для файла. None — файл не опознан."""
    key = normalise(filename)
    for canonical, aliases in LAYER_ALIASES.items():
        if key in aliases:
            return canonical
    # запасной вариант: имя содержит синоним как отдельное слово
    for canonical, aliases in LAYER_ALIASES.items():
        for alias in aliases:
            if alias in key.split("_") or key.startswith(alias) or key.endswith(alias):
                return canonical
    return None


# --- Разбор архива ----------------------------------------------------------


@dataclass
class UploadedFile:
    name: str
    data: bytes


@dataclass
class Recognised:
    layers: dict[str, bytes] = field(default_factory=dict)
    meta: dict[str, Any] | None = None
    unknown: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)


def read_archive(data: bytes) -> list[UploadedFile]:
    """Распаковать ZIP в память с защитой от обхода путей и распаковочных бомб."""
    if len(data) > MAX_ARCHIVE_BYTES:
        raise UploadError(
            f"архив больше {MAX_ARCHIVE_BYTES // 1024 // 1024} МБ"
        )

    import io

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise UploadError(f"архив не читается: {error}") from error

    entries = [item for item in archive.infolist() if not item.is_dir()]
    if len(entries) > MAX_FILES:
        raise UploadError(f"в архиве больше {MAX_FILES} файлов")

    total = sum(item.file_size for item in entries)
    if total > MAX_UNPACKED_BYTES:
        raise UploadError("распакованный размер архива слишком велик")

    result: list[UploadedFile] = []
    for item in entries:
        name = item.filename.replace("\\", "/")
        # Обход путей: имя вида ../../etc/passwd в архиве — обычное дело
        if name.startswith("/") or ".." in Path(name).parts:
            raise UploadError(f"недопустимое имя файла в архиве: {item.filename}")
        if Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
            continue
        result.append(UploadedFile(name=Path(name).name, data=archive.read(item)))

    return result


def recognise(files: Iterable[UploadedFile]) -> Recognised:
    """Разложить принесённые файлы по каноническим слоям."""
    found = Recognised()
    for item in files:
        if normalise(item.name) == "meta":
            try:
                found.meta = json.loads(item.data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                found.unknown.append(item.name)
            continue

        layer = match_layer(item.name)
        if layer is None:
            found.unknown.append(item.name)
            continue
        if layer in found.layers:
            found.duplicates.append(item.name)
            continue
        found.layers[layer] = item.data

    return found


# --- Границы и проекция -----------------------------------------------------


def _positions(node: Any) -> Iterable[tuple[float, float]]:
    """Пары чисел из произвольно вложенного массива координат."""
    if isinstance(node, (list, tuple)):
        if (
            len(node) >= 2
            and isinstance(node[0], (int, float))
            and not isinstance(node[0], bool)
            and isinstance(node[1], (int, float))
            and not isinstance(node[1], bool)
        ):
            yield float(node[0]), float(node[1])
            return
        for item in node:
            yield from _positions(item)


def walk_coordinates(node: Any) -> Iterable[tuple[float, float]]:
    """Координаты из GeoJSON любой вложенности.

    Обход прицельный — только по `geometry`/`coordinates`/`geometries`, а не по
    всем значениям подряд: иначе числовые свойства объектов (этажность,
    нагрузка, кадастровый номер) попадали бы в охват района как координаты.
    """
    if isinstance(node, dict):
        if "coordinates" in node:
            yield from _positions(node["coordinates"])
        for key in ("geometry", "geometries", "features"):
            if key in node:
                yield from walk_coordinates(node[key])
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from walk_coordinates(item)


def bounds_of(layers: dict[str, bytes]) -> tuple[float, float, float, float]:
    """Общий охват всех слоёв в исходных координатах."""
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")

    for name, raw in layers.items():
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UploadError(f"слой «{name}» не читается как JSON: {error}") from error

        for x, y in walk_coordinates(payload.get("features") or payload):
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)

    if min_x > max_x:
        raise UploadError("в присланных слоях нет ни одной координаты")
    return min_x, min_y, max_x, max_y


def build_meta(
    name: str, layers: dict[str, bytes], source_crs: str, supplied: dict | None
) -> dict[str, Any]:
    """Собрать meta.json: охват из самих данных, проекция — явно."""
    if supplied and supplied.get("metric_crs") and supplied.get("bbox"):
        meta = dict(supplied)
        meta.setdefault("bbox_order", "lonlat")
        meta["name"] = name
        return meta

    min_x, min_y, max_x, max_y = bounds_of(layers)

    if source_crs != crs.WGS84:
        lon_min, lat_min = crs.point_to_wgs84_from(min_x, min_y, source_crs)
        lon_max, lat_max = crs.point_to_wgs84_from(max_x, max_y, source_crs)
    else:
        lon_min, lat_min, lon_max, lat_max = min_x, min_y, max_x, max_y
        if not (-180 <= lon_min <= 180 and -90 <= lat_min <= 90):
            raise UploadError(
                "координаты не похожи на градусы, а система координат объявлена "
                "как EPSG:4326 — укажите фактическую в поле «crs»"
            )

    return {
        "name": name,
        "bbox": [min_x, min_y, max_x, max_y],
        "bbox_order": "lonlat",
        "crs": source_crs,
        "metric_crs": crs.utm_epsg_for_bbox(lon_min, lat_min, lon_max, lat_max),
        "source": "загружено пользователем",
    }


# --- Запись района ----------------------------------------------------------


AREA_NAME_PATTERN = re.compile(r"^[0-9a-zA-Zа-яА-ЯёЁ_\- ]{1,48}$")


def validate_name(name: str) -> str:
    cleaned = name.strip()
    if not AREA_NAME_PATTERN.match(cleaned):
        raise UploadError(
            "имя района: до 48 символов, только буквы, цифры, пробел, дефис и подчёркивание"
        )
    return cleaned


def write_area(
    cache_dir: Path, name: str, found: Recognised, source_crs: str
) -> tuple[Path, ImportReport]:
    """Записать район на диск. Возвращает каталог и предварительные замечания."""
    name = validate_name(name)
    missing = [layer for layer in REQUIRED_LAYERS if layer not in found.layers]
    if missing:
        expected = ", ".join(sorted(LAYER_ALIASES[layer][1] for layer in missing))
        raise UploadError(
            f"не хватает обязательных слоёв: {', '.join(missing)}. "
            f"Ожидаются файлы с именами вроде: {expected}"
        )

    meta = build_meta(name, found.layers, source_crs, found.meta)

    target = cache_dir / name
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    try:
        for layer, raw in found.layers.items():
            (target / f"{layer}.geojson").write_bytes(raw)
        (target / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        shutil.rmtree(target, ignore_errors=True)
        raise

    report = ImportReport(source_crs=meta["crs"], metric_crs=meta["metric_crs"])
    for filename in found.unknown:
        report.add("(файл)", "unrecognised", f"слой не опознан: {filename}")
    for filename in found.duplicates:
        report.add("(файл)", "duplicate", f"повтор слоя, файл пропущен: {filename}")

    return target, report


def expected_layers() -> list[dict[str, Any]]:
    """Справка для интерфейса: какие слои нужны и как их можно называть."""
    return [
        {
            "layer": canonical,
            "required": canonical in REQUIRED_LAYERS,
            "aliases": list(aliases),
        }
        for canonical, aliases in LAYER_ALIASES.items()
    ]
