"""Растровая сетка и растеризация геометрии.

Сетка задаётся в метрической проекции. Строка 0 — северный край, как принято
в растровых форматах, поэтому ось Y инвертирована относительно координат.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from rasterio.features import rasterize
from rasterio.transform import Affine, from_origin
from shapely.geometry.base import BaseGeometry

Bounds = tuple[float, float, float, float]


@dataclass(frozen=True)
class Grid:
    min_x: float
    min_y: float
    width: int          # число столбцов
    height: int         # число строк
    resolution: float   # метров на ячейку

    @classmethod
    def covering(cls, bounds: Bounds, resolution: float, margin: float = 0.0) -> Grid:
        min_x, min_y, max_x, max_y = bounds
        min_x -= margin
        min_y -= margin
        max_x += margin
        max_y += margin
        width = max(1, int(np.ceil((max_x - min_x) / resolution)))
        height = max(1, int(np.ceil((max_y - min_y) / resolution)))
        return cls(min_x=min_x, min_y=min_y, width=width, height=height, resolution=resolution)

    @property
    def max_x(self) -> float:
        return self.min_x + self.width * self.resolution

    @property
    def max_y(self) -> float:
        return self.min_y + self.height * self.resolution

    @property
    def shape(self) -> tuple[int, int]:
        return self.height, self.width

    @property
    def cells(self) -> int:
        return self.height * self.width

    @property
    def transform(self) -> Affine:
        return from_origin(self.min_x, self.max_y, self.resolution, self.resolution)

    def rowcol(self, x: float, y: float) -> tuple[int, int]:
        """Координаты → индекс ячейки. Значения зажимаются в границы сетки."""
        col = int((x - self.min_x) / self.resolution)
        row = int((self.max_y - y) / self.resolution)
        return (
            min(max(row, 0), self.height - 1),
            min(max(col, 0), self.width - 1),
        )

    def xy(self, row: int, col: int) -> tuple[float, float]:
        """Индекс ячейки → координаты её центра."""
        return (
            self.min_x + (col + 0.5) * self.resolution,
            self.max_y - (row + 0.5) * self.resolution,
        )

    def contains(self, x: float, y: float) -> bool:
        return self.min_x <= x <= self.max_x and self.min_y <= y <= self.max_y


def burn(
    grid: Grid,
    shapes: list[tuple[BaseGeometry, float]],
    *,
    fill: float = 0.0,
    all_touched: bool = True,
) -> np.ndarray:
    """Растеризовать геометрии со значениями. Последняя перекрывает предыдущие."""
    if not shapes:
        return np.full(grid.shape, fill, dtype=np.float32)

    valid = [(geom, value) for geom, value in shapes if geom is not None and not geom.is_empty]
    if not valid:
        return np.full(grid.shape, fill, dtype=np.float32)

    return rasterize(
        valid,
        out_shape=grid.shape,
        transform=grid.transform,
        fill=fill,
        all_touched=all_touched,
        dtype=np.float32,
    )


def burn_mask(grid: Grid, geoms: list[BaseGeometry], *, all_touched: bool = True) -> np.ndarray:
    """Булева маска покрытия геометриями."""
    return burn(grid, [(g, 1.0) for g in geoms], fill=0.0, all_touched=all_touched) > 0


def burn_mask_window(
    grid: Grid, geoms: list[BaseGeometry], *, all_touched: bool = True, pad: int = 1
) -> tuple[slice, slice, np.ndarray]:
    """Маска покрытия только в окне вокруг геометрий: (строки, столбцы, маска).

    Растеризовать ветку в 30 м на сетку в 4 млн ячеек — значит заполнять 4 млн
    нулей ради сотни единиц; при сотнях веток это уже секунды.
    """
    valid = [g for g in geoms if g is not None and not g.is_empty]
    if not valid:
        return slice(0, 0), slice(0, 0), np.zeros((0, 0), dtype=bool)
    min_x = min(g.bounds[0] for g in valid)
    min_y = min(g.bounds[1] for g in valid)
    max_x = max(g.bounds[2] for g in valid)
    max_y = max(g.bounds[3] for g in valid)
    r_top, c_left = grid.rowcol(min_x, max_y)
    r_bottom, c_right = grid.rowcol(max_x, min_y)
    r0, r1 = max(0, r_top - pad), min(grid.height, r_bottom + pad + 1)
    c0, c1 = max(0, c_left - pad), min(grid.width, c_right + pad + 1)
    if r1 <= r0 or c1 <= c0:
        return slice(0, 0), slice(0, 0), np.zeros((0, 0), dtype=bool)
    window_transform = from_origin(
        grid.min_x + c0 * grid.resolution, grid.max_y - r0 * grid.resolution,
        grid.resolution, grid.resolution,
    )
    mask = rasterize(
        [(g, 1) for g in valid], out_shape=(r1 - r0, c1 - c0), transform=window_transform,
        fill=0, all_touched=all_touched, dtype=np.uint8,
    ) > 0
    return slice(r0, r1), slice(c0, c1), mask
