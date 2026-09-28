"""Экспорт варианта в GeoJSON.

Формат обмена по умолчанию: открывается в QGIS, ArcGIS, любом веб-клиенте.
Всё в EPSG:4326.
"""

from __future__ import annotations

from typing import Any

from shapely.geometry import mapping

from ..geo import crs
from ..routing.alternatives import variant_id_of
from ..routing.solver import RouteSolution


def export(solution: RouteSolution, metric_crs: str) -> dict[str, Any]:
    features: list[dict[str, Any]] = []

    features.append(
        {
            "type": "Feature",
            "geometry": mapping(crs.to_wgs84(solution.geometry, metric_crs)),
            "properties": {
                "role": "route",
                "variant": variant_id_of(solution),
                "profile": solution.profile,
                "length_m": round(solution.length_m, 1),
                "du_mm": solution.du_mm,
                "laying": solution.laying.value,
                "schedule": solution.schedule,
                "q_gcal_h": round(solution.q_gcal_h, 4),
                "v_m_s": round(solution.hydraulics.v_m_s, 2),
                "r_pa_m": round(solution.hydraulics.r_pa_m, 1),
                "dp_bar": round(solution.hydraulics.dp_bar, 3),
                "cost_total_rub": round(solution.cost.total_rub),
                "cost_rub_per_m": round(solution.cost.total_rub / max(solution.length_m, 1)),
                "approval_risk": solution.risk.score,
                "compliance": solution.compliance.status.value,
                "compliance_failures": len(solution.compliance.failures),
                "compliance_conditionals": len(solution.compliance.conditionals),
            },
        }
    )

    features.append(
        {
            "type": "Feature",
            "geometry": mapping(crs.to_wgs84(solution.connection.point, metric_crs)),
            "properties": {
                "role": "tap",
                "name": solution.connection.node_name,
                "edge_id": solution.connection.edge_id,
                "du_mm": solution.connection.du_mm,
                "reserve_gcal_h": round(solution.connection.reserve_gcal_h, 3),
                "head_available_m": solution.connection.head_available_m,
                "tap_rub": round(solution.connection.tap_rub),
                "reconstruction_rub": round(solution.connection.reconstruction_rub),
            },
        }
    )

    for check in solution.compliance.checks:
        if check.status.value == "pass":
            continue
        point = solution.geometry.interpolate((check.start_m + check.end_m) / 2.0)
        features.append(
            {
                "type": "Feature",
                "geometry": mapping(crs.to_wgs84(point, metric_crs)),
                "properties": {
                    "role": "compliance",
                    "status": check.status.value,
                    "rule_id": check.rule_id,
                    "title": check.title,
                    "clause": check.clause,
                    "required_m": check.required_m,
                    "actual_m": check.actual_m,
                    "note": check.note,
                    "where": check.where,
                },
            }
        )

    return {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": features,
    }
