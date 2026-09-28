"""PNG-картинка района с построенной сетью через matplotlib.

SVG хорош для браузера, но для проверки глазами в терминале нужен растр —
и с возможностью вырезать фрагмент крупно.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Polygon as MplPolygon  # noqa: E402

from .model import CaseInput  # noqa: E402
from .network import NODE_CHAMBER_EXISTING, NODE_CHAMBER_NEW, NODE_TECH, Network  # noqa: E402
from .preview import DU_COLOR, RESTRICTION_FILL  # noqa: E402


def render_png(
    case: CaseInput, net: Network | None, path: Path | str, *,
    bounds: tuple[float, float, float, float] | None = None,
    width_px: int = 1800, title: str = "", labels: bool = True,
    extra: list | None = None,
) -> Path:
    x0, y0, x1, y1 = bounds or case.bounds()
    if bounds is None:
        x0, y0, x1, y1 = x0 - 30, y0 - 30, x1 + 30, y1 + 30
    aspect = (y1 - y0) / (x1 - x0)
    dpi = 100
    fig, ax = plt.subplots(figsize=(width_px / dpi, width_px * aspect / dpi), dpi=dpi)
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect("equal")
    ax.axis("off")
    fig.subplots_adjust(0, 0, 1, 1)

    def draw_geom(geom, *, fill=None, edge="#78716c", lw=0.5, alpha=0.75, ls="-"):
        geoms = geom.geoms if hasattr(geom, "geoms") else [geom]
        for g in geoms:
            if g.geom_type == "Polygon":
                ax.add_patch(MplPolygon(list(g.exterior.coords), closed=True, facecolor=fill or "none",
                                        edgecolor=edge, linewidth=lw, alpha=alpha))
                for hole in g.interiors:
                    ax.add_patch(MplPolygon(list(hole.coords), closed=True, facecolor="#fafaf9",
                                            edgecolor=edge, linewidth=lw))
            elif g.geom_type == "LineString":
                xs, ys = zip(*g.coords)
                ax.plot(xs, ys, color=edge, linewidth=lw, alpha=alpha, linestyle=ls, solid_capstyle="round")

    for r in case.restrictions + case.unknown_restrictions:
        color = RESTRICTION_FILL.get(r.type, "#f5d0fe")
        if r.geom.geom_type.endswith("LineString"):
            draw_geom(r.geom, edge=color, lw=2.5, alpha=0.9)
        else:
            draw_geom(r.geom, fill=color, alpha=0.75)

    for geom, color in extra or []:
        draw_geom(geom, fill=color, edge=color, alpha=0.25)

    for e in case.network:
        draw_geom(e.geom, edge="#7f1d1d", lw=3.0, alpha=0.9)
    for c in case.chambers:
        ax.plot(c.geom.x, c.geom.y, marker="s", ms=7, mfc="white", mec="#7f1d1d", mew=1.6)
        if labels:
            ax.annotate(f"ТК{c.id}", (c.geom.x, c.geom.y), xytext=(5, 4), textcoords="offset points",
                        fontsize=7, color="#7f1d1d", weight="bold")
    if case.source is not None:
        ax.plot(case.source.x, case.source.y, marker="o", ms=11, mfc="#f59e0b", mec="#78350f", mew=1.5)

    if net is not None:
        for s in net.segments.values():
            color = DU_COLOR.get(s.du, "#0f172a")
            draw_geom(s.line, edge=color, lw=2.6, alpha=1.0, ls="--" if s.laying == "special" else "-")
            if labels:
                mid = s.line.interpolate(0.5, normalized=True)
                ax.annotate(f"Ду{s.du}", (mid.x, mid.y), xytext=(3, 3), textcoords="offset points",
                            fontsize=6.5, color=color, weight="bold")
        for n in net.nodes.values():
            x, y = n.point
            if n.kind == NODE_CHAMBER_NEW:
                ax.plot(x, y, marker="s", ms=7, mfc="#fef08a", mec="#0f172a", mew=1.4)
            elif n.kind == NODE_CHAMBER_EXISTING:
                ax.plot(x, y, marker="s", ms=9, mfc="#fde047", mec="#7f1d1d", mew=2.0)
            elif n.kind == NODE_TECH:
                ax.plot(x, y, marker="o", ms=4.5, mfc="white", mec="#0f172a", mew=1.2)

    for o in case.oks:
        ax.plot(o.geom.x, o.geom.y, marker="o", ms=7, mfc="#16a34a", mec="white", mew=1.2)
        if labels:
            ax.annotate(f"#{o.id} {o.flow_tph:g}", (o.geom.x, o.geom.y), xytext=(6, 3),
                        textcoords="offset points", fontsize=8, color="#14532d", weight="bold")

    if title:
        ax.text(0.01, 0.01, title, transform=ax.transAxes, fontsize=10, color="#44403c")
    path = Path(path)
    fig.savefig(path, dpi=dpi, facecolor="#fafaf9")
    plt.close(fig)
    return path
