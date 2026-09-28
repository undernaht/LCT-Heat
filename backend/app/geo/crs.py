"""Проекции.

Вход и выход сервиса — EPSG:4326 (так говорят все клиенты и OSM).
Весь расчёт — в метрической проекции: в градусах нельзя ни буфер построить,
ни длину посчитать.

Перепроецирование происходит ровно в двух местах — ingest и export.
"""

from __future__ import annotations

from functools import lru_cache

from pyproj import Transformer
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform

WGS84 = "EPSG:4326"


def utm_epsg_for(lon: float, lat: float) -> str:
    """Код UTM-зоны для точки.

    Для Москвы даёт EPSG:32637 (UTM 37N). Искажение длин в пределах города —
    доли процента, для трассировки несущественно.
    """
    zone = int((lon + 180.0) / 6.0) + 1
    base = 32600 if lat >= 0 else 32700
    return f"EPSG:{base + zone}"


def utm_epsg_for_bbox(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> str:
    """Зона по центру области — чтобы весь район считался в одной проекции."""
    return utm_epsg_for((min_lon + max_lon) / 2.0, (min_lat + max_lat) / 2.0)


@lru_cache(maxsize=32)
def _transformer(src: str, dst: str) -> Transformer:
    return Transformer.from_crs(src, dst, always_xy=True)


def project(geom: BaseGeometry, src: str, dst: str) -> BaseGeometry:
    """Перепроецировать геометрию."""
    if src == dst:
        return geom
    return shapely_transform(_transformer(src, dst).transform, geom)


def to_metric(geom: BaseGeometry, metric_crs: str) -> BaseGeometry:
    return project(geom, WGS84, metric_crs)


def to_wgs84(geom: BaseGeometry, metric_crs: str) -> BaseGeometry:
    return project(geom, metric_crs, WGS84)


def point_to_metric(lon: float, lat: float, metric_crs: str) -> tuple[float, float]:
    return _transformer(WGS84, metric_crs).transform(lon, lat)


def point_to_wgs84(x: float, y: float, metric_crs: str) -> tuple[float, float]:
    return _transformer(metric_crs, WGS84).transform(x, y)


def transform_arrays(xs, ys, source_crs: str, target_crs: str):
    """Пересчитать массивы координат — для выборки растра по многим точкам."""
    if source_crs == target_crs:
        return xs, ys
    return _transformer(source_crs, target_crs).transform(xs, ys)


def point_to_wgs84_from(x: float, y: float, source_crs: str) -> tuple[float, float]:
    """Точка из произвольной проекции в градусы — нужно, чтобы выбрать зону UTM
    по bbox, заданному не в WGS84."""
    if source_crs == WGS84:
        return x, y
    return _transformer(source_crs, WGS84).transform(x, y)
