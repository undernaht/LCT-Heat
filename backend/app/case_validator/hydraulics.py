"""Расходы, условные диаметры и предельная длина (§2.3 приложения).

Расход участка пересчитывается по дереву: сумма flow_tph точек за участком.
Ду проверяется по трём правилам сразу — пропускная способность, неубывание к
присоединению, предельная длина по каждому пути — а затем на минимальность:
если участок можно понизить на ступень, ничего не нарушив, завышение
произвольное (§2.3: «произвольное завышение ДУ не допускается»).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.case_validator.findings import Findings
from app.case_validator.model import IdKey, InputData, Segment
from app.case_validator.rules import Rules
from app.case_validator.topology import Component, VariantGraph

FLOW_TOL = 1e-3      # т/ч
LENGTH_TOL = 0.01    # м


@dataclass
class LimitViolation:
    leaf_id: object
    du: int
    length: float
    limit: float
    segment: Segment


def check_hydraulics(graph: VariantGraph, inp: InputData, rules: Rules, f: Findings) -> dict[IdKey, float]:
    """Проверки расходов и Ду; возвращает пересчитанные расходы участков."""
    vid = graph.variant.variant_id
    flows: dict[IdKey, float] = {}
    for comp in graph.components:
        if not comp.is_tree:
            if comp.segments:
                f.info("hydraulics.skipped",
                       f"расходы и Ду компоненты из {len(comp.segments)} участков не проверены из-за ошибок топологии",
                       variant_id=vid, object_id=comp.segments[0].id)
            continue
        for seg in comp.segments:
            flows[seg.key] = sum(inp.oks[k].flow_tph or 0.0 for k in comp.downstream_oks[seg.key])
        _check_flows_and_diameters(comp, flows, rules, f)
        du_of = {s.key: s.diameter for s in comp.segments}
        if any(du is None for du in du_of.values()):
            continue
        for v in _limit_violations(comp, du_of, rules):
            f.error("hydraulics.limit_length",
                    f"предельная длина Ду{v.du} превышена на пути от точки {v.leaf_id!r}: {v.length:.1f} м "
                    f"(отсчёт без смены Ду через камеры и техузлы)",
                    variant_id=vid, object_id=v.segment.id, expected=f"≤ {v.limit:g} м", actual=round(v.length, 2))
        _check_minimality(comp, flows, du_of, rules, f)
    return flows


def _check_flows_and_diameters(comp: Component, flows: dict[IdKey, float], rules: Rules, f: Findings) -> None:
    for seg in comp.segments:
        vid = seg.variant_id
        flow = flows[seg.key]
        if seg.flow_tph is not None and abs(seg.flow_tph - flow) > FLOW_TOL:
            f.error("hydraulics.flow", f"расход участка по сумме точек за ним: {flow:.3f} т/ч",
                    variant_id=vid, object_id=seg.id, expected=round(flow, 3), actual=seg.flow_tph)
        if seg.diameter is None:
            continue
        du_min = rules.min_du_for_flow(flow)
        if du_min is None:
            f.error("hydraulics.capacity", f"расход {flow:.1f} т/ч превышает пропускную способность любого Ду",
                    variant_id=vid, object_id=seg.id, actual=round(flow, 3))
        elif seg.diameter < du_min:
            f.error("hydraulics.capacity",
                    f"Ду{seg.diameter} не пропускает {flow:.2f} т/ч (пропускная способность {rules.row(seg.diameter).capacity_tph} т/ч)",
                    variant_id=vid, object_id=seg.id, expected=f"≥ Ду{du_min}", actual=seg.diameter)
        for child in comp.children[seg.key]:
            if child.diameter is not None and child.diameter > seg.diameter:
                f.error("hydraulics.monotonic",
                        f"Ду уменьшается к присоединению: за участком Ду{seg.diameter} следует участок {child.id!r} Ду{child.diameter}",
                        variant_id=vid, object_id=seg.id, expected=f"≥ {child.diameter}", actual=seg.diameter)
        far = comp.far[seg.key]
        if (far.kind in ("tech", "new_chamber") and far.degree == 2 and not far.is_attachment
                and len(comp.children[seg.key]) == 1):
            child = comp.children[seg.key][0]
            if (child.diameter is not None and child.diameter != seg.diameter
                    and abs(flows[child.key] - flow) <= FLOW_TOL):
                f.error("hydraulics.constant_diameter",
                        f"Ду меняется в узле {far.id!r} без изменения расхода ({flow:.2f} т/ч): Ду{child.diameter} → Ду{seg.diameter}",
                        variant_id=vid, object_id=far.id, expected=child.diameter, actual=seg.diameter)


def _limit_violations(comp: Component, du_of: dict[IdKey, int], rules: Rules) -> list[LimitViolation]:
    """Предельная длина по каждому пути от точки к корню; отсчёт сбрасывается только сменой Ду."""
    violations: list[LimitViolation] = []
    reported: set[tuple[int, IdKey]] = set()
    for leaf_key, path in comp.leaf_paths.items():
        current = du_of[path[0].key]
        acc = 0.0
        for seg in path:
            du = du_of[seg.key]
            if du != current:
                current, acc = du, 0.0
            acc += seg.length or seg.geom.length
            limit = rules.row(current).limit_m
            if acc > limit + LENGTH_TOL and (current, seg.key) not in reported:
                reported.add((current, seg.key))
                violations.append(LimitViolation(_leaf_id(comp, leaf_key), current, acc, limit, seg))
    return violations


def _leaf_id(comp: Component, key: IdKey) -> object:
    for node in comp.nodes:
        if node.key == key:
            return node.id
    return key


def _stretches(comp: Component, flows: dict[IdKey, float]) -> list[list[Segment]]:
    """Группы участков между узлами смены расхода (через техузлы и камеры без разветвления)."""
    parent = {s.key: s.key for s in comp.segments}

    def find(k: IdKey) -> IdKey:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for node in comp.nodes:
        if node.kind not in ("tech", "new_chamber") or node.degree != 2 or node.is_attachment:
            continue
        a, b = node.segments
        if abs(flows[a.key] - flows[b.key]) <= FLOW_TOL:
            parent[find(a.key)] = find(b.key)
    groups: dict[IdKey, list[Segment]] = {}
    for s in comp.segments:
        groups.setdefault(find(s.key), []).append(s)
    return list(groups.values())


def _check_minimality(comp: Component, flows: dict[IdKey, float], du_of: dict[IdKey, int],
                      rules: Rules, f: Findings) -> None:
    if _limit_violations(comp, du_of, rules):
        return  # при нарушении предела минимальность не обсуждается
    for stretch in _stretches(comp, flows):
        du = du_of[stretch[0].key]
        if any(du_of[s.key] != du for s in stretch):
            continue
        lower = rules.step_down(du)
        if lower is None:
            continue
        flow = max(flows[s.key] for s in stretch)
        if rules.row(lower).capacity_tph < flow - 1e-9:
            continue
        keys = {s.key for s in stretch}
        children_ok = all(
            du_of[c.key] <= lower for s in stretch for c in comp.children[s.key] if c.key not in keys
        )
        if not children_ok:
            continue
        trial = dict(du_of)
        for s in stretch:
            trial[s.key] = lower
        if _limit_violations(comp, trial, rules):
            continue
        ids = [s.id for s in stretch]
        f.warning("hydraulics.oversized",
                  f"возможное произвольное завышение Ду: участки {ids} можно понизить с Ду{du} до Ду{lower} "
                  f"без нарушения пропускной способности ({flow:.2f} т/ч), предельной длины и неубывания",
                  variant_id=stretch[0].variant_id, object_id=stretch[0].id, expected=lower, actual=du)
