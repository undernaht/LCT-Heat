"""Нормализация геометрии на входе.

Три вещи, на которых спотыкались реальные выгрузки:

1. **Multi-геометрии.** Данные из ЕГРН, ИСОГД и любой PostGIS штатно приходят
   как `MultiPolygon` и `MultiLineString`. Раньше проверка `isinstance(geom,
   Polygon)` молча выбрасывала такое здание целиком: слой загружался, объектов
   в нём оказывалось ноль, ошибки не было.
2. **`properties: null`.** Валидный GeoJSON по RFC 7946; падал с AttributeError.
3. **Чужая система координат.** `meta['crs']` не читался вовсе, и датасет в
   EPSG:3857 давал площади `nan` без единого предупреждения.
"""

from __future__ import annotations

from typing import Any, Iterator

from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from ..geo import crs


def properties_of(feature: dict) -> dict[str, Any]:
    """Свойства объекта. `null` — валидный GeoJSON, но работать с ним нельзя."""
    props = feature.get("properties")
    return props if isinstance(props, dict) else {}


def geometries_of(feature: dict) -> Iterator[BaseGeometry]:
    """Геометрии объекта: Multi-* разбирается на части, пустые пропускаются."""
    raw = feature.get("geometry")
    if not raw:
        return

    try:
        geom = shape(raw)
    except (ValueError, KeyError, TypeError):
        return

    if geom.is_empty:
        return

    if geom.geom_type.startswith("Multi") or geom.geom_type == "GeometryCollection":
        for part in geom.geoms:
            if not part.is_empty:
                yield part
    else:
        yield geom


def reproject(geom: BaseGeometry, source_crs: str, metric_crs: str) -> BaseGeometry:
    """Перевести в метрическую проекцию из ЛЮБОЙ исходной, а не только из WGS84."""
    return crs.project(geom, source_crs, metric_crs)


def is_valid_metric(geom: BaseGeometry) -> bool:
    """Отсечь то, что после перепроецирования выродилось в NaN.

    Проекция из неверно объявленной системы координат не бросает исключение —
    она молча отдаёт `nan`. Ловим это здесь, а не в расчёте.
    """
    if geom.is_empty:
        return False
    bounds = geom.bounds
    return all(value == value and abs(value) < 1e12 for value in bounds)
