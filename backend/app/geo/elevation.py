"""Рельеф: выборка высот и уклонов.

Источник — GeoTIFF в любой проекции; точки приходят в метрической проекции
района и пересчитываются под растр. Модуль намеренно терпим к отсутствию
данных: рельеф необязателен, и без него всё остальное должно работать.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio

from . import crs


@dataclass
class Terrain:
    """Модель рельефа района."""

    values: np.ndarray          # высоты, м
    transform: object           # affine растра
    raster_crs: str
    area_crs: str
    pixel_m: float

    @property
    def available(self) -> bool:
        return self.values.size > 0

    # --- Выборка ---

    def _pixels(self, xs: np.ndarray, ys: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if self.raster_crs != self.area_crs:
            xs, ys = crs.transform_arrays(xs, ys, self.area_crs, self.raster_crs)
        inverse = ~self.transform
        cols, rows = inverse * (xs, ys)
        return np.asarray(cols, dtype=float), np.asarray(rows, dtype=float)

    def sample(self, xs, ys) -> np.ndarray:
        """Высоты в точках. Билинейная интерполяция, за границей — ближайшая."""
        xs = np.atleast_1d(np.asarray(xs, dtype=float))
        ys = np.atleast_1d(np.asarray(ys, dtype=float))
        if not self.available:
            return np.full(xs.shape, np.nan)

        cols, rows = self._pixels(xs, ys)
        height, width = self.values.shape

        col0 = np.clip(np.floor(cols - 0.5).astype(int), 0, width - 1)
        row0 = np.clip(np.floor(rows - 0.5).astype(int), 0, height - 1)
        col1 = np.clip(col0 + 1, 0, width - 1)
        row1 = np.clip(row0 + 1, 0, height - 1)

        fx = np.clip(cols - 0.5 - col0, 0.0, 1.0)
        fy = np.clip(rows - 0.5 - row0, 0.0, 1.0)

        top = self.values[row0, col0] * (1 - fx) + self.values[row0, col1] * fx
        bottom = self.values[row1, col0] * (1 - fx) + self.values[row1, col1] * fx
        return top * (1 - fy) + bottom * fy

    def at(self, x: float, y: float) -> float:
        return float(self.sample([x], [y])[0])

    def along(self, line, step_m: float = 5.0) -> tuple[np.ndarray, np.ndarray]:
        """Продольный профиль: расстояния от начала и отметки земли."""
        count = max(2, int(line.length / step_m) + 1)
        distances = np.linspace(0.0, line.length, count)
        points = [line.interpolate(float(d)) for d in distances]
        xs = np.array([p.x for p in points])
        ys = np.array([p.y for p in points])
        return distances, self.sample(xs, ys)

    def slope_grid(self, grid) -> np.ndarray:
        """Крутизна склона в каждой ячейке сетки, доли (tg угла).

        Нужна полю стоимости: копать на склоне дороже, а очень крутые участки
        для прокладки непригодны.
        """
        if not self.available:
            return np.zeros(grid.shape, dtype=np.float32)

        rows, cols = np.mgrid[0 : grid.height, 0 : grid.width]
        xs = grid.min_x + (cols + 0.5) * grid.resolution
        ys = grid.max_y - (rows + 0.5) * grid.resolution
        heights = self.sample(xs.ravel(), ys.ravel()).reshape(grid.shape)

        dy, dx = np.gradient(heights, grid.resolution)
        return np.hypot(dx, dy).astype(np.float32)


EMPTY = Terrain(
    values=np.zeros((0, 0), dtype=np.float32),
    transform=None, raster_crs="", area_crs="", pixel_m=0.0,
)


def load(area_dir: Path, area_crs: str) -> Terrain:
    """Прочитать рельеф района. Нет файла — пустая модель, а не отказ."""
    path = area_dir / "elevation.tif"
    if not path.exists():
        return EMPTY

    with rasterio.open(path) as source:
        values = source.read(1).astype(np.float32)
        # Пропуски заполняем средним: дыра в DEM не должна ронять расчёт
        nodata = source.nodata
        if nodata is not None:
            mask = values == nodata
            if mask.any():
                values[mask] = float(np.nanmean(values[~mask])) if (~mask).any() else 0.0
        return Terrain(
            values=values,
            transform=source.transform,
            raster_crs=str(source.crs),
            area_crs=area_crs,
            pixel_m=abs(source.transform.a),
        )
