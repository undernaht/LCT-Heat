"""Выгрузка рельефа для района.

Источник — открытые тайлы высот AWS Terrain Tiles (кодировка terrarium):
`elevation = R·256 + G + B/256 − 32768`. Ключ не нужен, лицензия открытая,
под капотом SRTM и Copernicus DEM.

Точность честно ограничена исходником: по горизонтали около 30 м, по вертикали
±5 м. Для продольного профиля трассы длиной в сотни метров этого хватает, для
проектной документации — нет, и в документе это оговаривается.

    python data/fetch_dem.py --name small
"""

from __future__ import annotations

import argparse
import io
import json
import math
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.transform import from_origin

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.ingest.area import resolve_crs  # noqa: E402

CACHE_DIR = Path(__file__).resolve().parent / "cache"
TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
USER_AGENT = "lct2026-heat-network/0.1 (hackathon project)"
TILE_SIZE = 256
WEB_MERCATOR = "EPSG:3857"
EARTH_CIRCUMFERENCE = 2 * math.pi * 6378137.0


def deg2tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    n = 2**zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return x, y


def tile_origin_mercator(x: int, y: int, zoom: int) -> tuple[float, float]:
    """Левый верхний угол тайла в метрах Web Mercator."""
    span = EARTH_CIRCUMFERENCE / 2**zoom
    return -EARTH_CIRCUMFERENCE / 2 + x * span, EARTH_CIRCUMFERENCE / 2 - y * span


def download(url: str, attempts: int = 3) -> bytes:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError) as error:
            last = error
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"тайл не скачался: {url} ({last})")


def decode_terrarium(data: bytes) -> np.ndarray:
    image = Image.open(io.BytesIO(data)).convert("RGB")
    rgb = np.asarray(image, dtype=np.float32)
    return rgb[:, :, 0] * 256.0 + rgb[:, :, 1] + rgb[:, :, 2] / 256.0 - 32768.0


def build_mosaic(bbox: tuple[float, float, float, float], zoom: int):
    """Собрать высоты по bbox [minlon, minlat, maxlon, maxlat]."""
    min_lon, min_lat, max_lon, max_lat = bbox

    x_min, y_max = deg2tile(min_lat, min_lon, zoom)
    x_max, y_min = deg2tile(max_lat, max_lon, zoom)
    x_min, x_max = min(x_min, x_max), max(x_min, x_max)
    y_min, y_max = min(y_min, y_max), max(y_min, y_max)

    columns, rows = x_max - x_min + 1, y_max - y_min + 1
    print(f"  тайлов {columns}×{rows} на зуме {zoom}")

    mosaic = np.zeros((rows * TILE_SIZE, columns * TILE_SIZE), dtype=np.float32)
    for row, y in enumerate(range(y_min, y_max + 1)):
        for column, x in enumerate(range(x_min, x_max + 1)):
            url = TILE_URL.format(z=zoom, x=x, y=y)
            tile = decode_terrarium(download(url))
            mosaic[
                row * TILE_SIZE : (row + 1) * TILE_SIZE,
                column * TILE_SIZE : (column + 1) * TILE_SIZE,
            ] = tile
            print(f"    {x}/{y}: {tile.min():.0f}…{tile.max():.0f} м", flush=True)

    origin_x, origin_y = tile_origin_mercator(x_min, y_min, zoom)
    pixel = EARTH_CIRCUMFERENCE / 2**zoom / TILE_SIZE
    return mosaic, from_origin(origin_x, origin_y, pixel, pixel), pixel


def main() -> int:
    parser = argparse.ArgumentParser(description="Выгрузка рельефа района")
    parser.add_argument("--name", required=True)
    parser.add_argument("--zoom", type=int, default=14)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    area_dir = CACHE_DIR / args.name
    meta_path = area_dir / "meta.json"
    if not meta_path.exists():
        raise SystemExit(f"нет района {area_dir}. Сначала fetch_osm.py")

    target = area_dir / "elevation.tif"
    if target.exists() and not args.force:
        with rasterio.open(target) as source:
            band = source.read(1)
        print(f"кэш найден: {target.name}, {band.min():.0f}…{band.max():.0f} м "
              f"(--force чтобы перекачать)")
        return 0

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    bbox = meta["bbox"]
    if str(meta.get("bbox_order", "latlon")).lower() != "lonlat":
        min_lat, min_lon, max_lat, max_lon = bbox
        bbox = [min_lon, min_lat, max_lon, max_lat]

    print(f"Район «{args.name}», bbox {bbox}")
    elevation, transform, pixel_m = build_mosaic(tuple(bbox), args.zoom)

    with rasterio.open(
        target, "w", driver="GTiff",
        height=elevation.shape[0], width=elevation.shape[1],
        count=1, dtype="float32", crs=WEB_MERCATOR, transform=transform,
        compress="deflate",
    ) as destination:
        destination.write(elevation, 1)

    _, metric_crs = resolve_crs(meta)
    ground_px = pixel_m * math.cos(math.radians((bbox[1] + bbox[3]) / 2))
    print(f"\n→ {target}  ({target.stat().st_size / 1024:.0f} КБ)")
    print(f"   высоты {elevation.min():.0f}…{elevation.max():.0f} м, "
          f"перепад {elevation.max() - elevation.min():.0f} м")
    print(f"   разрешение ≈ {ground_px:.0f} м на пиксель, проекция {WEB_MERCATOR}")
    print(f"   район считается в {metric_crs}, пересчёт при выборке")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
