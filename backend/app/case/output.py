"""Выходной GeoJSON по §7 приложения.

Один FeatureCollection на режим; варианты различаются `variant_id`. Ссылки
`start_node_id`/`end_node_id` совпадают с геометрическими концами линий и
указывают на точки подключения (их входной id), существующие камеры (входной
id) или новые узлы этого варианта. Координаты — WGS 84 с полной точностью
double. Дополнительные свойства допускаются приложением и игнорируются при
проверке; мы кладём туда то, что нужно интерфейсу и защите.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from shapely.geometry import LineString, Point

from ..geo import crs
from .costing import VariantCost
from .model import CaseInput
from .network import NODE_CHAMBER_EXISTING, NODE_CHAMBER_NEW, NODE_OKS, NODE_TECH, Network, Node


def _wgs(geom, calc_crs: str):
    return crs.to_wgs84(geom, calc_crs)


@dataclass
class Geometry:
    """Координаты варианта, как они уйдут в файл, и длины по ним."""

    node_coords: dict[str, list[float]]         # id узла → [lon, lat]
    segment_coords: dict[str, list[list[float]]]
    lengths: dict[str, float]                   # id участка → длина по round-trip, м


def prepare_geometry(net: Network, case: CaseInput) -> Geometry:
    """§8: концы у точек ОКС и существующих камер — побитово из входа; новые
    узлы проецируются один раз и переиспользуются всеми участками; длина
    считается по координатам, реально записанным в файл."""
    calc = case.calc_crs
    node_coords: dict[str, list[float]] = {}
    for node in net.nodes.values():
        raw = None
        if node.kind == NODE_OKS:
            oks = case.oks_by_id(node.ref)
            raw = oks.raw_coords if oks else None
        elif node.kind == NODE_CHAMBER_EXISTING and node.existing is not None:
            raw = node.existing.raw_coords
        if raw is None:
            p = _wgs(Point(*node.point), calc)
            raw = [p.x, p.y]
        node_coords[node.id] = [float(raw[0]), float(raw[1])]

    segment_coords: dict[str, list[list[float]]] = {}
    lengths: dict[str, float] = {}
    for segment in net.segments.values():
        inner = [list(c) for c in _wgs(LineString(segment.points), calc).coords]
        inner[0] = list(node_coords[segment.start])
        inner[-1] = list(node_coords[segment.end])
        segment_coords[segment.id] = inner
        back = crs.project(LineString(inner), "EPSG:4326", calc)
        lengths[segment.id] = float(back.length)
    return Geometry(node_coords=node_coords, segment_coords=segment_coords, lengths=lengths)


def _node_out_id(node: Node, variant_id: str, new_ids: dict[str, str]) -> Any:
    if node.kind == NODE_OKS:
        return node.ref
    if node.kind == NODE_CHAMBER_EXISTING:
        return node.ref
    return new_ids[node.id]


def variant_features(
    net: Network, cost: VariantCost, case: CaseInput, geometry: Geometry, *, variant_id: str, rank: int,
    mode: str = "2d", extra_summary: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Все объекты одного варианта: участки, новые камеры, техузлы, сводка."""
    features: list[dict[str, Any]] = []
    new_ids: dict[str, str] = {}
    chamber_no = tech_no = 0
    for node in net.nodes.values():
        if node.kind == NODE_CHAMBER_NEW:
            chamber_no += 1
            new_ids[node.id] = f"{variant_id}_chamber_{chamber_no}"
        elif node.kind == NODE_TECH:
            tech_no += 1
            new_ids[node.id] = f"{variant_id}_tn_{tech_no}"

    chamber_cost = {c.node_id: c for c in cost.chambers}

    for index, segment in enumerate(net.segments.values(), start=1):
        props: dict[str, Any] = {
            "id": f"{variant_id}_net_{index}",
            "object_type": "heat_network",
            "variant_id": variant_id,
            "start_node_id": _node_out_id(net.nodes[segment.start], variant_id, new_ids),
            "end_node_id": _node_out_id(net.nodes[segment.end], variant_id, new_ids),
            "flow_tph": round(segment.flow_tph, 3),
            "diameter": int(segment.du),
            "length": round(geometry.lengths[segment.id], 3),
            "laying_method": segment.laying,
            "depth_start": segment.depth_start if mode == "depth" else None,
            "depth_end": segment.depth_end if mode == "depth" else None,
            "cost": cost.segment_cost[segment.id],
            # --- дополнительные, по приложению игнорируются ---
            "k_special": segment.k_special,
            "k_depth": segment.k_depth if mode == "depth" else 1.0,
            "special_types": segment.special_types,
            "special_ids": segment.special_ids,
            "internal_id": segment.id,
        }
        features.append({"type": "Feature", "properties": props,
                         "geometry": {"type": "LineString", "coordinates": geometry.segment_coords[segment.id]}})

    for node in net.nodes.values():
        if node.kind == NODE_CHAMBER_NEW:
            coords = geometry.node_coords[node.id]
            c = chamber_cost[node.id]
            props = {
                "id": new_ids[node.id], "object_type": "heat_chamber", "variant_id": variant_id,
                "diameter": int(c.diameter), "cost": c.cost,
                "on_existing_network": node.on_edge.id if node.on_edge is not None else None,
                "internal_id": node.id,
            }
            features.append({"type": "Feature", "properties": props,
                             "geometry": {"type": "Point", "coordinates": list(coords)}})
        elif node.kind == NODE_TECH:
            coords = geometry.node_coords[node.id]
            props = {"id": new_ids[node.id], "object_type": "technical_node", "variant_id": variant_id,
                     "internal_id": node.id}
            features.append({"type": "Feature", "properties": props,
                             "geometry": {"type": "Point", "coordinates": list(coords)}})

    summary: dict[str, Any] = {
        "id": f"{variant_id}_summary",
        "object_type": "variant_summary",
        "variant_id": variant_id,
        "rank": int(rank),
        **cost.summary(),
        "unconnected_oks_ids": list(cost.unconnected),
    }
    if extra_summary:
        summary.update(extra_summary)
    features.append({"type": "Feature", "properties": summary, "geometry": None})
    return features


def feature_collection(features: list[dict[str, Any]], *, name: str = "") -> dict[str, Any]:
    fc: dict[str, Any] = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": features,
    }
    if name:
        fc["name"] = name
    return fc
