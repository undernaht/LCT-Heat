"""Быстрый SVG-предпросмотр района: реальная подложка + сгенерированные сети.

Нужен, чтобы глазами проверить генерацию до того, как появится фронтенд.
Самодостаточный SVG без внешних зависимостей и тайлов.

    python data/preview.py --name small
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from shapely.geometry import LineString, Point, Polygon, shape  # noqa: E402

from app.geo import crs  # noqa: E402
from app.ingest.area import resolve_crs  # noqa: E402

CACHE_DIR = Path(__file__).resolve().parent / "cache"

WIDTH = 1400
MARGIN = 24

UTILITY_STYLE = {
    "WATER": ("#3b82f6", 0.9),
    "SEWER": ("#78716c", 0.9),
    "STORM": ("#0ea5e9", 0.7),
    "GAS": ("#eab308", 0.9),
    "POWER": ("#ef4444", 0.7),
    "COMM": ("#a855f7", 0.7),
}
UTILITY_TITLE = {
    "WATER": "водопровод", "SEWER": "канализация", "STORM": "ливневая",
    "GAS": "газопровод", "POWER": "кабели", "COMM": "связь",
}


def load(area_dir: Path, name: str) -> list[dict]:
    path = area_dir / f"{name}.geojson"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))["features"]


class Canvas:
    """Перевод метрических координат в координаты SVG (ось Y инвертирована)."""

    def __init__(self, bounds: tuple[float, float, float, float]) -> None:
        self.min_x, self.min_y, self.max_x, self.max_y = bounds
        span_x = max(1.0, self.max_x - self.min_x)
        span_y = max(1.0, self.max_y - self.min_y)
        self.scale = (WIDTH - 2 * MARGIN) / span_x
        self.height = int(span_y * self.scale + 2 * MARGIN)

    def xy(self, x: float, y: float) -> tuple[float, float]:
        return (
            MARGIN + (x - self.min_x) * self.scale,
            self.height - MARGIN - (y - self.min_y) * self.scale,
        )

    def path_of(self, coords) -> str:
        pts = [self.xy(x, y) for x, y in coords]
        return "M " + " L ".join(f"{px:.1f},{py:.1f}" for px, py in pts)


def load_intensity_color(q: float) -> str:
    """Цвет здания по удельной нагрузке — видно, что оценка работает."""
    if q <= 0:
        return "#d4d4d8"
    steps = [
        (0.05, "#e0f2fe"), (0.15, "#bae6fd"), (0.35, "#fdba74"),
        (0.70, "#fb923c"), (1.50, "#f97316"),
    ]
    for threshold, color in steps:
        if q < threshold:
            return color
    return "#ea580c"


ROUTE_STYLE = {
    "min_cost": ("#15803d", "минимальная стоимость"),
    "min_approvals": ("#1d4ed8", "минимум согласований"),
    "reliable": ("#7e22ce", "надёжность и резерв"),
}


def solve_routes(area_name: str, diversify: int) -> list[dict]:
    """Посчитать варианты трасс для всех перспективных зданий."""
    from app.domain.enums import Laying
    from app.ingest.area import load_area
    from app.routing import alternatives

    area = load_area(CACHE_DIR / area_name)
    routes: list[dict] = []

    for building in area.buildings:
        if not building.is_perspective:
            continue
        result = alternatives.solve_variants(
            area, building, laying=Laying.CHANNEL, resolution=1.0, diversify=diversify
        )
        for solution in result.variants:
            variant_id = alternatives.variant_id_of(solution)
            routes.append(
                {
                    "geom": solution.geometry,
                    "building": building.id,
                    "profile": solution.profile,
                    "variant_id": variant_id,
                    "is_pareto": variant_id in result.pareto_ids,
                    "is_primary": variant_id == alternatives.variant_id_of(result.variants[0]),
                    "length_m": solution.length_m,
                    "cost_rub": solution.cost.total_rub,
                    "du_mm": solution.du_mm,
                    "tap": solution.connection.point,
                    "tap_name": solution.connection.node_name,
                }
            )
        print(f"  {building.id}: вариантов {len(result.variants)}, "
              f"Парето {result.pareto_ids}")

    return routes


def solve_group(area_name: str) -> dict | None:
    """Подключить все перспективные здания общим коридором."""
    from app.domain.enums import Laying
    from app.ingest.area import load_area
    from app.routing import group

    area = load_area(CACHE_DIR / area_name)
    # Квартал застройки, если он есть: разнесённым объектам делить коридор нечего
    block = [b for b in area.buildings if b.id.startswith("kvartal_") and b.load]
    targets = block or [b for b in area.buildings if b.is_perspective and b.load]
    if len(targets) < 2:
        return None

    result = group.solve_group(area, targets, laying=Laying.CHANNEL, resolution=1.0)
    if result is None:
        return None

    print(f"  группа: {result.total_length_m:.0f} м против "
          f"{result.independent_length_m:.0f} м раздельно, "
          f"экономия {result.saving_share * 100:.0f} %")
    return {
        "segments": [
            {"geom": s.geometry, "du_mm": s.du_mm, "shared": s.is_shared,
             "load": s.load_gcal_h, "serves": s.serves}
            for s in result.segments
        ],
        "summary": result,
    }


def build_svg(area: str, routes: list[dict] | None = None,
              group_result: dict | None = None) -> str:
    area_dir = CACHE_DIR / area
    meta = json.loads((area_dir / "meta.json").read_text(encoding="utf-8"))
    _, metric = resolve_crs(meta)

    layers = {
        name: [(crs.to_metric(shape(f["geometry"]), metric), f["properties"])
               for f in load(area_dir, name)]
        for name in ("buildings_load", "roads", "railways", "water", "gen_utilities",
                     "gen_heat_edges", "gen_heat_nodes", "gen_parcels", "gen_perspective")
    }

    all_geoms = [g for items in layers.values() for g, _ in items]
    if not all_geoms:
        raise SystemExit("Нет данных для предпросмотра")

    xs_min = min(g.bounds[0] for g in all_geoms)
    ys_min = min(g.bounds[1] for g in all_geoms)
    xs_max = max(g.bounds[2] for g in all_geoms)
    ys_max = max(g.bounds[3] for g in all_geoms)
    canvas = Canvas((xs_min, ys_min, xs_max, ys_max))

    out: list[str] = []
    add = out.append

    add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{canvas.height}" '
        f'viewBox="0 0 {WIDTH} {canvas.height}" font-family="system-ui, sans-serif">')
    add(f'<rect width="{WIDTH}" height="{canvas.height}" fill="#fafaf9"/>')

    # --- Участки ---
    add('<g stroke="#a3a3a3" stroke-width="0.8" stroke-dasharray="4 3" fill="#f5f5f4">')
    for geom, props in layers["gen_parcels"]:
        if isinstance(geom, Polygon):
            fill = "#f5f5f4" if props.get("is_public") else "#fef3c7"
            add(f'<path d="{canvas.path_of(geom.exterior.coords)} Z" fill="{fill}"/>')
    add("</g>")

    # --- Вода ---
    add('<g fill="#bfdbfe" stroke="none">')
    for geom, _ in layers["water"]:
        if isinstance(geom, Polygon):
            add(f'<path d="{canvas.path_of(geom.exterior.coords)} Z"/>')
    add("</g>")

    # --- Дороги ---
    add('<g stroke="#d6d3d1" fill="none" stroke-linecap="round">')
    for geom, props in layers["roads"]:
        if isinstance(geom, LineString):
            major = props.get("highway") in ("motorway", "trunk", "primary", "secondary")
            add(f'<path d="{canvas.path_of(geom.coords)}" stroke-width="{3.2 if major else 1.6}"/>')
    add("</g>")

    # --- Ж/д ---
    add('<g stroke="#57534e" fill="none" stroke-width="2" stroke-dasharray="8 4">')
    for geom, _ in layers["railways"]:
        if isinstance(geom, LineString):
            add(f'<path d="{canvas.path_of(geom.coords)}"/>')
    add("</g>")

    # --- Здания, окрашенные по нагрузке ---
    add('<g stroke="#a8a29e" stroke-width="0.4">')
    for geom, props in layers["buildings_load"]:
        if isinstance(geom, Polygon):
            color = load_intensity_color(float(props.get("q_total_gcal_h") or 0.0))
            add(f'<path d="{canvas.path_of(geom.exterior.coords)} Z" fill="{color}"/>')
    add("</g>")

    # --- Попутные сети ---
    for kind, (color, width) in UTILITY_STYLE.items():
        add(f'<g stroke="{color}" stroke-width="{width}" fill="none" opacity="0.75">')
        for geom, props in layers["gen_utilities"]:
            if props.get("kind") == kind and isinstance(geom, LineString):
                add(f'<path d="{canvas.path_of(geom.coords)}"/>')
        add("</g>")

    # --- Тепловая сеть: толщина по диаметру ---
    add('<g stroke="#dc2626" fill="none" stroke-linecap="round" opacity="0.95">')
    for geom, props in layers["gen_heat_edges"]:
        if isinstance(geom, LineString):
            du = int(props.get("du_mm") or 100)
            add(f'<path d="{canvas.path_of(geom.coords)}" stroke-width="{1.2 + du / 110:.2f}"/>')
    add("</g>")

    # --- Узлы ---
    for geom, props in layers["gen_heat_nodes"]:
        if not isinstance(geom, Point):
            continue
        px, py = canvas.xy(geom.x, geom.y)
        kind = props.get("kind")
        if kind == "source":
            add(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="9" fill="#7f1d1d" '
                f'stroke="#fff" stroke-width="2"/>')
            add(f'<text x="{px + 13:.1f}" y="{py + 4:.1f}" font-size="13" font-weight="600" '
                f'fill="#7f1d1d">{props.get("name", "источник")}</text>')
        elif kind == "ctp":
            add(f'<rect x="{px - 4:.1f}" y="{py - 4:.1f}" width="8" height="8" '
                f'fill="#b91c1c" stroke="#fff" stroke-width="1.2"/>')

    # --- Дерево группового подключения ---
    for segment in (group_result or {}).get("segments", []):
        path = canvas.path_of(segment["geom"].coords)
        width = 1.5 + segment["du_mm"] / 45
        colour = "#7c2d12" if segment["shared"] else "#ea580c"
        add(f'<path d="{path}" stroke="#ffffff" stroke-width="{width + 3:.1f}" fill="none" '
            f'stroke-linecap="round" stroke-linejoin="round" opacity="0.9"/>')
        dash = "" if segment["shared"] else ' stroke-dasharray="10 5"'
        add(f'<path d="{path}" stroke="{colour}" stroke-width="{width:.1f}" fill="none" '
            f'stroke-linecap="round" stroke-linejoin="round"{dash}/>')

        if segment["shared"] or segment["geom"].length > 200:
            mid = segment["geom"].interpolate(segment["geom"].length / 2)
            mx, my = canvas.xy(mid.x, mid.y)
            add(f'<text x="{mx:.1f}" y="{my - 8:.1f}" font-size="12" font-weight="700" '
                f'fill="{colour}" text-anchor="middle" stroke="#fff" stroke-width="3" '
                f'paint-order="stroke">Ду{segment["du_mm"]} · {segment["load"]:.2f} Гкал/ч</text>')

    # --- Найденные трассы ---
    for route in routes or []:
        colour = ROUTE_STYLE.get(route["profile"], ("#0f766e", ""))[0]
        path = canvas.path_of(route["geom"].coords)
        width = 4.0 if route["is_primary"] else 2.4
        dash = "" if route["is_primary"] else ' stroke-dasharray="9 6"'
        # белая обводка, чтобы трасса читалась поверх насыщенной подложки
        add(f'<path d="{path}" stroke="#ffffff" stroke-width="{width + 2.6:.1f}" fill="none" '
            f'stroke-linecap="round" stroke-linejoin="round" opacity="0.9"/>')
        add(f'<path d="{path}" stroke="{colour}" stroke-width="{width}" fill="none" '
            f'stroke-linecap="round" stroke-linejoin="round"{dash}/>')

        tap = route["tap"]
        tx, ty = canvas.xy(tap.x, tap.y)
        add(f'<circle cx="{tx:.1f}" cy="{ty:.1f}" r="5.5" fill="#ffffff" '
            f'stroke="{colour}" stroke-width="2.5"/>')
        if route["is_primary"]:
            add(f'<text x="{tx + 9:.1f}" y="{ty + 4:.1f}" font-size="11.5" font-weight="600" '
                f'fill="{colour}">{route["tap_name"] or "врезка"}</text>')

    # --- Перспективные здания и «наивная прямая» до ближайшей сети ---
    heat_lines = [g for g, _ in layers["gen_heat_edges"] if isinstance(g, LineString)]
    for geom, props in layers["gen_perspective"]:
        if not isinstance(geom, Polygon):
            continue
        centre = geom.centroid
        if heat_lines and not routes:
            nearest = min(heat_lines, key=lambda line: line.distance(centre))
            target = nearest.interpolate(nearest.project(centre))
            add(f'<path d="{canvas.path_of([(centre.x, centre.y), (target.x, target.y)])}" '
                f'stroke="#16a34a" stroke-width="1.6" stroke-dasharray="7 5" fill="none"/>')

        add(f'<path d="{canvas.path_of(geom.exterior.coords)} Z" fill="#22c55e" '
            f'fill-opacity="0.55" stroke="#15803d" stroke-width="2"/>')
        px, py = canvas.xy(centre.x, centre.y)
        add(f'<text x="{px:.1f}" y="{py - 20:.1f}" font-size="12" font-weight="700" '
            f'fill="#14532d" text-anchor="middle">{props["id"]} · '
            f'{props["q_total_gcal_h"]:.2f} Гкал/ч</text>')
        add(f'<text x="{px:.1f}" y="{py - 7:.1f}" font-size="11" fill="#166534" '
            f'text-anchor="middle">до сети {props["distance_to_network_m"]:.0f} м</text>')

    add(_legend(canvas, area, meta, area_dir, routes, group_result))
    add("</svg>")
    return "\n".join(out)


def _legend(canvas: Canvas, area: str, meta: dict, area_dir: Path,
            routes: list[dict] | None = None, group_result: dict | None = None) -> str:
    gen_meta_path = area_dir / "generated_meta.json"
    gen: dict[str, Any] = (
        json.loads(gen_meta_path.read_text(encoding="utf-8")) if gen_meta_path.exists() else {}
    )

    rows: list[tuple[str, str]] = []
    if group_result:
        rows += [
            ("#7c2d12", "общий коридор группы (толщина ∝ Ду)"),
            ("#ea580c", "вводы к отдельным зданиям"),
        ]
    if routes:
        used = {r["profile"] for r in routes}
        rows += [
            (ROUTE_STYLE[p][0], f"трасса — {ROUTE_STYLE[p][1]}")
            for p in ROUTE_STYLE if p in used
        ]
    rows += [
        ("#22c55e", "перспективные здания"),
        ("#ea580c", "здания — заливка по тепловой нагрузке"),
        ("#dc2626", "тепловая сеть (толщина ∝ Ду)"),
        ("#3b82f6", UTILITY_TITLE["WATER"]),
        ("#78716c", UTILITY_TITLE["SEWER"]),
        ("#eab308", UTILITY_TITLE["GAS"]),
        ("#ef4444", UTILITY_TITLE["POWER"]),
        ("#a855f7", UTILITY_TITLE["COMM"]),
        ("#fef3c7", "частные участки"),
    ]

    x, y = 16, 22
    parts = [
        f'<g><rect x="{x - 8}" y="{y - 16}" width="330" height="{28 + len(rows) * 17 + 34}" '
        f'rx="6" fill="#ffffff" fill-opacity="0.92" stroke="#e7e5e4"/>',
        f'<text x="{x}" y="{y}" font-size="14" font-weight="700" fill="#1c1917">'
        f'Район «{area}» · {meta.get("crs", "")} → EPSG:32637</text>',
    ]
    for i, (color, label) in enumerate(rows):
        row_y = y + 20 + i * 17
        parts.append(f'<rect x="{x}" y="{row_y - 8}" width="12" height="10" fill="{color}"/>')
        parts.append(f'<text x="{x + 19}" y="{row_y}" font-size="12" fill="#44403c">{label}</text>')

    footer_y = y + 20 + len(rows) * 17 + 8
    if group_result:
        summary = group_result["summary"]
        parts.append(
            f'<text x="{x}" y="{footer_y}" font-size="12" font-weight="700" fill="#14532d">'
            f'Группой {summary.total_length_m:.0f} м вместо '
            f'{summary.independent_length_m:.0f} м раздельно · '
            f'экономия {summary.saving_share * 100:.0f} % СМР</text>'
        )
        footer_y += 16
    if gen:
        parts.append(
            f'<text x="{x}" y="{footer_y}" font-size="11.5" fill="#78716c">'
            f'{gen.get("buildings_heated")} отапливаемых зданий · '
            f'{gen.get("total_load_gcal_h")} Гкал/ч · ЦТП {gen.get("ctp")} · '
            f'напор {gen.get("head_min_m")}–{gen.get("head_max_m")} м</text>'
        )
        parts.append(
            f'<text x="{x}" y="{footer_y + 15}" font-size="11.5" fill="#78716c">'
            f'подложка OSM (ODbL) · сети сгенерированы, seed {gen.get("seed")}</text>'
        )
    parts.append("</g>")
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="SVG-предпросмотр района")
    parser.add_argument("--name", required=True)
    parser.add_argument("--solve", action="store_true",
                        help="посчитать и нарисовать трассы для перспективных зданий")
    parser.add_argument("--diversify", type=int, default=1)
    parser.add_argument("--group", action="store_true",
                        help="подключить перспективные здания общим коридором")
    args = parser.parse_args()

    routes = solve_routes(args.name, args.diversify) if args.solve else None
    group_result = solve_group(args.name) if args.group else None

    svg = build_svg(args.name, routes, group_result)
    suffix = "-group" if args.group else ("-routes" if args.solve else "")
    out_path = CACHE_DIR / args.name / f"preview{suffix}.svg"
    out_path.write_text(svg, encoding="utf-8")
    print(f"→ {out_path}  ({out_path.stat().st_size / 1024:.0f} КБ)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
