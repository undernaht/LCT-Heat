"""Стоимость, сводка варианта и неподключённые точки (§3.2, §6, §7 приложения).

Тождества сводки проверяются по выгруженным атрибутам — так, как это сделал
бы проверяющий, у которого нет внутреннего состояния солвера. Параллельно
считается полностью независимая оценка (длины по геометрии, стоимости по
таблицам, камеры по правилу), чтобы показать «score пересчитанный» рядом с
заявленным.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.case_validator.findings import Findings
from app.case_validator.model import IdKey, InputData, Segment, Variant, id_key, id_text
from app.case_validator.restrictions import SegmentInfo
from app.case_validator.rules import Rules
from app.case_validator.topology import VariantGraph

COST_TOL = 1.0       # руб.
LENGTH_TOL = 0.01    # м
SCORE_TOL = 1e-4


@dataclass
class VariantTotals:
    variant_id: Any
    segments: int = 0
    chambers: int = 0
    tech_nodes: int = 0
    length_declared: float = 0.0
    length_recomputed: float = 0.0
    construction_cost_recomputed: float = 0.0
    chamber_cost_recomputed: float = 0.0
    tie_in_count: int = 0
    unconnected_computed: list[Any] = field(default_factory=list)
    unconnected_declared: list[Any] | None = None
    penalty_recomputed: float = 0.0
    calculated_cost_recomputed: float = 0.0
    score_recomputed: float = 0.0
    score_declared: float | None = None
    calculated_cost_declared: float | None = None
    rank: int | None = None


def _k_depth(seg: Segment, rules: Rules) -> float:
    if seg.depth_start is None or seg.depth_end is None:
        return 1.0
    return (rules.k_depth(seg.depth_start) + rules.k_depth(seg.depth_end)) / 2.0


def check_costs(graph: VariantGraph, inp: InputData, rules: Rules, infos: dict[IdKey, SegmentInfo],
                f: Findings, allow_unconnected: bool = False) -> VariantTotals:
    variant = graph.variant
    vid = variant.variant_id
    totals = VariantTotals(vid, len(variant.segments), len(variant.chambers), len(variant.tech_nodes))
    if any(s.depth_start is not None for s in variant.segments.values()):
        f.info("cost.depth_mode", "в файле заданы глубины: Kгл считается по §5 как среднее концов, вертикальные требования не проверяются",
               variant_id=vid)

    declared_segment_cost = 0.0
    for seg in variant.segments.values():
        row = rules.row(seg.diameter)
        if seg.cost is not None:
            declared_segment_cost += seg.cost
        if seg.geom is None:
            totals.length_declared += seg.length or 0.0
            continue
        geom_length = seg.geom.length
        totals.length_recomputed += geom_length
        # без length (пример §7.3) сводка считается по геометрии с допуском длины
        length = seg.length if seg.length is not None else geom_length
        totals.length_declared += length
        if seg.length is not None and abs(seg.length - geom_length) > LENGTH_TOL:
            f.error("cost.length", f"length не совпадает с длиной геометрии в EPSG:32637 ({geom_length:.3f} м)",
                    variant_id=vid, object_id=seg.id, expected=round(geom_length, 3), actual=seg.length)
        if row is None:
            continue
        k = _k_depth(seg, rules) * (infos[seg.key].k_special if seg.key in infos else 1.0)
        totals.construction_cost_recomputed += geom_length * row.unit_cost * k
        if seg.cost is not None:
            expected = length * row.unit_cost * k
            tol = COST_TOL if seg.length is not None else COST_TOL + LENGTH_TOL * row.unit_cost * k
            if abs(expected - seg.cost) > tol:
                f.error("cost.segment",
                        f"cost ≠ length · c(Ду{seg.diameter}) · Kгл · Kспец = {length:.3f} · {row.unit_cost:g} · {k:.4g} = {expected:.2f}",
                        variant_id=vid, object_id=seg.id, expected=round(expected, 2), actual=seg.cost)

    declared_chamber_cost = _check_chambers(graph, inp, rules, f, totals)
    totals.tie_in_count = sum(
        1 for s in variant.usable_segments for ref in (s.start_ref, s.end_ref) if ref.kind == "existing_chamber"
    )
    _check_unconnected(variant, graph, inp, rules, f, totals, allow_unconnected)
    totals.construction_cost_recomputed += totals.chamber_cost_recomputed + totals.tie_in_count * rules.tie_in_cost
    totals.calculated_cost_recomputed = totals.construction_cost_recomputed + totals.penalty_recomputed
    totals.score_recomputed = rules.score(totals.calculated_cost_recomputed, totals.length_recomputed)

    summary = variant.summary
    if summary is None:
        return totals
    totals.rank = summary.rank
    totals.score_declared = summary.score
    totals.calculated_cost_declared = summary.calculated_cost

    def identity(name: str, expected: float | None, actual: float | None, tol: float, formula: str) -> None:
        if expected is None or actual is None:
            return
        if abs(expected - actual) > tol:
            f.error(f"summary.{name}", f"{name} ≠ {formula} = {expected:.4f}",
                    variant_id=vid, object_id=summary.id, expected=round(expected, 4), actual=actual)

    identity("chamber_construction_cost", declared_chamber_cost, summary.chamber_construction_cost, COST_TOL,
             "Σ heat_chamber.cost")
    if summary.existing_chamber_tie_in_count is not None and summary.existing_chamber_tie_in_count != totals.tie_in_count:
        f.error("summary.existing_chamber_tie_in_count",
                f"число врезок ≠ числу участков, заканчивающихся в существующих камерах ({totals.tie_in_count})",
                variant_id=vid, object_id=summary.id, expected=totals.tie_in_count, actual=summary.existing_chamber_tie_in_count)
    if summary.existing_chamber_tie_in_count is not None:
        identity("existing_chamber_tie_in_cost", summary.existing_chamber_tie_in_count * rules.tie_in_cost,
                 summary.existing_chamber_tie_in_cost, COST_TOL, f"{rules.tie_in_cost:g} · existing_chamber_tie_in_count")
    if summary.chamber_construction_cost is not None and summary.existing_chamber_tie_in_cost is not None:
        identity("construction_cost",
                 declared_segment_cost + summary.chamber_construction_cost + summary.existing_chamber_tie_in_cost,
                 summary.construction_cost, COST_TOL,
                 "Σ heat_network.cost + chamber_construction_cost + existing_chamber_tie_in_cost")
    if summary.unconnected_oks_ids is not None:
        penalty = 0.0
        for oid in summary.unconnected_oks_ids:
            point = inp.oks.get(id_key(oid))
            if point is None:
                continue
            penalty += rules.penalty(point.flow_tph or 0.0)
        identity("unconnected_penalty", penalty, summary.unconnected_penalty, COST_TOL,
                 f"Σ ({rules.penalty_fixed:g} + {rules.penalty_per_tph:g} · G) по unconnected_oks_ids")
    if summary.construction_cost is not None and summary.unconnected_penalty is not None:
        identity("calculated_cost", summary.construction_cost + summary.unconnected_penalty, summary.calculated_cost,
                 COST_TOL, "construction_cost + unconnected_penalty")
    identity("new_network_length", totals.length_declared, summary.new_network_length, LENGTH_TOL, "Σ heat_network.length")
    if summary.calculated_cost is not None and summary.new_network_length is not None:
        identity("score", rules.score(summary.calculated_cost, summary.new_network_length), summary.score, SCORE_TOL,
                 f"{rules.cost_weight} · calculated_cost / {rules.cost_scale:g} + {rules.length_weight} · new_network_length / {rules.length_scale:g}")
    return totals


def _check_chambers(graph: VariantGraph, inp: InputData, rules: Rules, f: Findings, totals: VariantTotals) -> float:
    """Ду и стоимость новых камер по наибольшему Ду примыкающих участков; возвращает Σ заявленных cost."""
    variant = graph.variant
    vid = variant.variant_id
    declared_total = 0.0
    for chamber in variant.chambers.values():
        if chamber.cost is not None:
            declared_total += chamber.cost
        node = graph.nodes.get(chamber.key)
        if node is None or node.degree == 0:
            continue
        new_dus = [s.diameter for s in node.segments if s.diameter is not None]
        if not new_dus:
            continue
        du_excl = max(new_dus)
        existing = [ln.diameter for ln in node.on_lines if ln.diameter is not None]
        du_incl = max([du_excl] + existing)
        du_expected, du_other = (du_incl, du_excl) if rules.chamber_diameter_includes_existing else (du_excl, du_incl)
        cost_expected = rules.chamber_cost(du_expected)
        cost_other = rules.chamber_cost(du_other)
        totals.chamber_cost_recomputed += cost_expected or 0.0
        if chamber.diameter is not None and chamber.diameter != du_expected:
            if chamber.diameter == du_other:
                f.warning("cost.chamber_diameter",
                          f"Ду камеры {chamber.diameter} соответствует другой трактовке §9.3 (без учёта существующей линии: Ду{du_excl}, с учётом: Ду{du_incl})",
                          variant_id=vid, object_id=chamber.id, expected=du_expected, actual=chamber.diameter)
            else:
                f.error("cost.chamber_diameter",
                        f"Ду камеры должен равняться наибольшему Ду примыкающих участков (без существующей линии Ду{du_excl}, с ней Ду{du_incl})",
                        variant_id=vid, object_id=chamber.id, expected=du_expected, actual=chamber.diameter)
        if chamber.cost is not None and cost_expected is not None and abs(chamber.cost - cost_expected) > COST_TOL:
            if cost_other is not None and abs(chamber.cost - cost_other) <= COST_TOL:
                f.warning("cost.chamber",
                          f"стоимость камеры соответствует другой трактовке §9.3: по Ду{du_other} = {cost_other:g}, по Ду{du_expected} = {cost_expected:g}",
                          variant_id=vid, object_id=chamber.id, expected=cost_expected, actual=chamber.cost)
            else:
                f.error("cost.chamber", f"стоимость новой камеры по таблице §3.2 для Ду{du_expected}: {cost_expected:g}",
                        variant_id=vid, object_id=chamber.id, expected=cost_expected, actual=chamber.cost)
    return declared_total


def _check_unconnected(variant: Variant, graph: VariantGraph, inp: InputData, rules: Rules, f: Findings,
                       totals: VariantTotals, allow_unconnected: bool) -> None:
    vid = variant.variant_id
    used = {node.key for node in graph.nodes.values() if node.kind == "oks"}
    computed = [p.id for key, p in inp.oks.items() if key not in used]
    totals.unconnected_computed = computed
    totals.penalty_recomputed = sum(inp.oks[id_key(oid)].flow_tph or 0.0 for oid in computed) * rules.penalty_per_tph
    totals.penalty_recomputed += len(computed) * rules.penalty_fixed
    if computed:
        add = f.warning if allow_unconnected else f.error
        add("unconnected.points",
            f"не подключены точки {computed}: приложение допускает это только при отсутствии допустимого маршрута, "
            "набор подготовлен так, что все точки подключаемы",
            variant_id=vid, actual=computed)
    summary = variant.summary
    if summary is None or summary.unconnected_oks_ids is None:
        return
    totals.unconnected_declared = list(summary.unconnected_oks_ids)
    declared_keys = {id_key(v) for v in summary.unconnected_oks_ids}
    computed_keys = {id_key(v) for v in computed}
    for oid in summary.unconnected_oks_ids:
        key = id_key(oid)
        if key in computed_keys:
            continue
        if key in inp.oks:
            f.error("unconnected.declared_but_connected",
                    f"точка {oid!r} указана как неподключённая, но входит в узлы участков варианта",
                    variant_id=vid, object_id=summary.id, actual=oid)
        elif any(id_text(oid) == id_text(c) for c in computed):
            f.warning("unconnected.id_type", f"id {oid!r} в unconnected_oks_ids отличается типом от входного id",
                      variant_id=vid, object_id=summary.id, actual=oid)
        else:
            f.error("unconnected.unknown_id", f"id {oid!r} в unconnected_oks_ids не является точкой подключения входа",
                    variant_id=vid, object_id=summary.id, actual=oid)
    for oid in computed:
        if id_key(oid) not in declared_keys and not any(id_text(oid) == id_text(d) for d in summary.unconnected_oks_ids):
            f.error("unconnected.missing",
                    f"точка {oid!r} не встречается ни в одном участке варианта, но отсутствует в unconnected_oks_ids",
                    variant_id=vid, object_id=summary.id, actual=oid)
    if len(declared_keys) != len(summary.unconnected_oks_ids):
        f.warning("unconnected.duplicates", "в unconnected_oks_ids есть повторы", variant_id=vid, object_id=summary.id)
