"""Сборка демонстрационного архива для показа приёма геоданных.

Берёт готовый район и переименовывает слои так, как их называет заказчик, —
чтобы на защите было видно, что сервис принимает не только собственную выгрузку.

    python data/make_demo_zip.py --name small
"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parent / "cache"

# Канонический слой → как его назвал бы заказчик
RENAMES: dict[str, str] = {
    "buildings_load": "Здания.geojson",
    "gen_perspective": "Проектируемые.geojson",
    "gen_heat_edges": "Тепловые сети.geojson",
    "gen_heat_nodes": "Теплокамеры.geojson",
    "gen_utilities": "Инженерные сети.geojson",
    "roads": "УДС.geojson",
    "railways": "Железные дороги.geojson",
    "water": "Водные объекты.geojson",
    "greenery": "Деревья.geojson",
    "landuse": "Землепользование.geojson",
    "gen_parcels": "Кадастр.geojson",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Демо-архив с геоданными")
    parser.add_argument("--name", default="small")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    source = CACHE_DIR / args.name
    if not source.exists():
        raise SystemExit(f"нет района {source}. Сначала fetch_osm.py и generate.py")

    target = Path(args.out) if args.out else source / "demo-area.zip"
    packed = 0

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for layer, filename in RENAMES.items():
            path = source / f"{layer}.geojson"
            if not path.exists():
                print(f"   пропущен (нет файла): {layer}")
                continue
            archive.writestr(filename, path.read_bytes())
            print(f"   {layer:<18} → {filename}")
            packed += 1

    size_mb = target.stat().st_size / 1024 / 1024
    print(f"\n→ {target}  ({packed} слоёв, {size_mb:.1f} МБ)")
    print("   meta.json намеренно НЕ вложен: охват и проекция выводятся из данных")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
