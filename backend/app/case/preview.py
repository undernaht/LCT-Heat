"""SVG-картинка района с построенной сетью — проверка глазами без браузера."""

from __future__ import annotations

from pathlib import Path

from shapely.geometry.base import BaseGeometry

from .model import CaseInput
from .network import NODE_CHAMBER_EXISTING, NODE_CHAMBER_NEW, NODE_TECH, Network

RESTRICTION_FILL = {
    "oks": "#d6d3d1", "water": "#93c5fd", "railway": "#a16207", "park": "#86efac",
    "social_area": "#fde68a", "prohibited_site": "#fca5a5", "road": "#e7e5e4",
    "tram_tracks": "#c4b5fd", "gas_pipeline": "#f97316", "power_cable": "#facc15",
}
DU_COLOR = {
    50: "#22c55e", 65: "#16a34a", 80: "#15803d", 100: "#0ea5e9", 125: "#0284c7",
    150: "#1d4ed8", 200: "#7c3aed", 250: "#a21caf", 300: "#be185d", 400: "#b91c1c",
}


def render(case: CaseInput, net: Network | None, path: Path | str, *, width_px: int = 1600,
           title: str = "", extra: list[tuple[BaseGeometry, str]] | None = None) -> Path:
    x0, y0, x1, y1 = case.bounds()
    x0, y0, x1, y1 = x0 - 40, y0 - 40, x1 + 40, y1 + 40
    scale = width_px / (x1 - x0)
    height_px = int((y1 - y0) * scale)

    def X(x: float) -> float:
        return (x - x0) * scale

    def Y(y: float) -> float:
        return height_px - (y - y0) * scale

    def path_d(geom: BaseGeometry) -> str:
        parts = []
        geoms = geom.geoms if hasattr(geom, "geoms") else [geom]
        for g in geoms:
            if g.geom_type == "Polygon":
                rings = [g.exterior, *g.interiors]
            else:
                rings = [g]
            for ring in rings:
                pts = list(ring.coords)
                parts.append("M" + " L".join(f"{X(x):.1f},{Y(y):.1f}" for x, y in pts)
                             + (" Z" if g.geom_type == "Polygon" else ""))
        return " ".join(parts)

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width_px}" height="{height_px}" '
           f'style="background:#fafaf9;font-family:sans-serif">']

    for r in case.restrictions + case.unknown_restrictions:
        fill = RESTRICTION_FILL.get(r.type, "#f5d0fe")
        if r.geom.geom_type.endswith("LineString"):
            out.append(f'<path d="{path_d(r.geom)}" fill="none" stroke="{fill}" stroke-width="3"><title>{r.type} {r.id}</title></path>')
        else:
            out.append(f'<path d="{path_d(r.geom)}" fill="{fill}" fill-opacity="0.75" stroke="#78716c" stroke-width="0.5"><title>{r.type} {r.id}</title></path>')

    for geom, color in extra or []:
        out.append(f'<path d="{path_d(geom)}" fill="{color}" fill-opacity="0.25" stroke="none"/>')

    for e in case.network:
        out.append(f'<path d="{path_d(e.geom)}" fill="none" stroke="#7f1d1d" stroke-width="4" stroke-opacity="0.85"><title>сущ. {e.id} Ду{e.diameter}</title></path>')
    for c in case.chambers:
        out.append(f'<rect x="{X(c.geom.x)-5:.0f}" y="{Y(c.geom.y)-5:.0f}" width="10" height="10" fill="#fff" stroke="#7f1d1d" stroke-width="2"><title>ТК {c.id}, примыканий {c.connections}</title></rect>')
    if case.source is not None:
        out.append(f'<circle cx="{X(case.source.x):.0f}" cy="{Y(case.source.y):.0f}" r="9" fill="#f59e0b" stroke="#78350f" stroke-width="2"/>')

    if net is not None:
        for s in net.segments.values():
            color = DU_COLOR.get(s.du, "#0f172a")
            dash = ' stroke-dasharray="6,4"' if s.laying == "special" else ""
            out.append(f'<path d="{path_d(s.line)}" fill="none" stroke="{color}" stroke-width="3.5"{dash}>'
                       f'<title>{s.id} Ду{s.du} {s.flow_tph:.1f} т/ч {s.length:.0f} м {s.laying}</title></path>')
            mid = s.line.interpolate(0.5, normalized=True)
            out.append(f'<text x="{X(mid.x)+3:.0f}" y="{Y(mid.y)-3:.0f}" font-size="9" fill="{color}" font-weight="bold">Ду{s.du}</text>')
        for n in net.nodes.values():
            x, y = X(n.point[0]), Y(n.point[1])
            if n.kind == NODE_CHAMBER_NEW:
                out.append(f'<rect x="{x-5:.0f}" y="{y-5:.0f}" width="10" height="10" fill="#fef08a" stroke="#0f172a" stroke-width="1.8"><title>новая камера {n.id}</title></rect>')
            elif n.kind == NODE_CHAMBER_EXISTING:
                out.append(f'<rect x="{x-6:.0f}" y="{y-6:.0f}" width="12" height="12" fill="#fde047" stroke="#7f1d1d" stroke-width="2.5"><title>врезка в ТК {n.ref}</title></rect>')
            elif n.kind == NODE_TECH:
                out.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="3.5" fill="#fff" stroke="#0f172a" stroke-width="1.5"><title>техузел {n.id}</title></circle>')

    for o in case.oks:
        out.append(f'<circle cx="{X(o.geom.x):.0f}" cy="{Y(o.geom.y):.0f}" r="5.5" fill="#16a34a" stroke="#fff" stroke-width="1.5"/>'
                   f'<text x="{X(o.geom.x)+7:.0f}" y="{Y(o.geom.y)+4:.0f}" font-size="11" font-weight="bold" fill="#14532d">#{o.id} {o.flow_tph:g}</text>')

    if title:
        out.append(f'<text x="12" y="{height_px-12}" font-size="13" fill="#44403c">{title}</text>')
    out.append("</svg>")
    path = Path(path)
    path.write_text("\n".join(out), encoding="utf-8")
    return path
