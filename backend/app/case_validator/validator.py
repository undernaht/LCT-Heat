"""Оркестровка проверок: структура → топология → ограничения → геометрия →
расходы → стоимость. Порядок важен: спецпроходы дают Kспец для стоимости и
набор пересекаемых объектов для обоснования техузлов.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.case_validator.costs import VariantTotals, check_costs
from app.case_validator.findings import Finding, Findings
from app.case_validator.geometry import check_geometry
from app.case_validator.hydraulics import check_hydraulics
from app.case_validator.restrictions import (
    build_runs,
    check_block_restrictions,
    check_runs,
    check_special_clearances,
    compute_zones,
    own_polygons,
    segment_infos,
    special_objects,
)
from app.case_validator.rules import Rules
from app.case_validator.structure import parse_input, parse_output
from app.case_validator.topology import build_graph, check_technical_node_reasons, check_topology


@dataclass
class Report:
    findings: list[Finding]
    variants: list[VariantTotals]
    input_path: str | None = None
    output_path: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)

    def count(self, severity: str, variant_id: Any = ...) -> int:
        return sum(
            1 for f in self.findings
            if f.severity == severity and (variant_id is ... or f.variant_id == variant_id)
        )

    def codes(self) -> set[str]:
        return {f.code for f in self.findings}

    def by_code(self, code: str) -> list[Finding]:
        return [f for f in self.findings if f.code == code]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "input": self.input_path,
            "output": self.output_path,
            "counts": {s: self.count(s) for s in ("error", "warning", "info")},
            "variants": [
                {
                    **asdict(v),
                    "errors": self.count("error", v.variant_id),
                    "warnings": self.count("warning", v.variant_id),
                }
                for v in self.variants
            ],
            "findings": [f.to_dict() for f in self.findings],
            "meta": self.meta,
        }


def validate(input_geojson: Any, output_geojson: Any, rules: Rules | None = None,
             allow_unconnected: bool = False) -> Report:
    rules = rules or Rules.load()
    f = Findings()
    inp = parse_input(input_geojson, rules, f)
    out = parse_output(output_geojson, inp, rules, f)
    own = own_polygons(inp)
    objects = special_objects(inp, rules)

    totals: list[VariantTotals] = []
    for variant in out.variants.values():
        graph = build_graph(variant, inp, rules)
        check_topology(graph, inp, rules, f)
        runs = build_runs(graph)
        compute_zones(runs, objects, rules, f)
        infos = segment_infos(graph, runs)
        check_runs(runs, rules, f)
        check_technical_node_reasons(graph, {k: v.crossed for k, v in infos.items()}, f)
        check_block_restrictions(graph, inp, rules, own, f)
        check_special_clearances(graph, objects, infos, rules, f)
        check_geometry(graph, inp, rules, own, f)
        check_hydraulics(graph, inp, rules, f)
        totals.append(check_costs(graph, inp, rules, infos, f, allow_unconnected))

    order = {"error": 0, "warning": 1, "info": 2}
    findings = sorted(f.items, key=lambda x: (order[x.severity], repr(x.variant_id), x.code))
    return Report(findings, totals, meta={"chamber_diameter_includes_existing": rules.chamber_diameter_includes_existing})


def validate_files(input_path: str | Path, output_path: str | Path, rules_path: str | Path | None = None,
                   allow_unconnected: bool = False) -> Report:
    with open(input_path, encoding="utf-8") as fh:
        input_geojson = json.load(fh)
    with open(output_path, encoding="utf-8") as fh:
        output_geojson = json.load(fh)
    rules = Rules.load(rules_path) if rules_path else Rules.load()
    report = validate(input_geojson, output_geojson, rules, allow_unconnected)
    report.input_path = str(input_path)
    report.output_path = str(output_path)
    return report
