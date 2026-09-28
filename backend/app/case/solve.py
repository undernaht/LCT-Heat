"""Оркестрация расчёта по конкурсной модели: вход → варианты → выход.

Порядок: разбор → поле стоимости → цели → дерево → Ду → спецпроходы →
стоимость по выгружаемой геометрии → GeoJSON. Каждый вариант — отдельное
дерево из другого порядка обработки точек; ранжирование по S.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any

from shapely.geometry import Point

from . import costing, depth, diameters, existing, improve, output, special
from .field import CaseField, add_blocked, build_field
from .model import CaseInput, load_case
from .network import Network
from .rules import Rules, get_rules
from .targets import ApproachTarget, approach_targets
from .tree import BuildResult, TreeBuilder, path_turn_issues


@dataclass
class Variant:
    id: str
    network: Network
    cost: costing.VariantCost
    geometry: output.Geometry
    build: BuildResult
    diameter_report: diameters.DiameterReport
    special_report: special.SpecialReport
    order: str
    mode: str = "2d"
    depth_report: depth.DepthReport | None = None
    rank: int = 0
    repairs: list[str] = dc_field(default_factory=list)
    notes: dict[str, Any] = dc_field(default_factory=dict)

    @property
    def score(self) -> float:
        return self.cost.score


@dataclass
class SolveResult:
    case: CaseInput
    field: CaseField
    variants: list[Variant]
    elapsed_s: float
    notes: dict[str, Any] = dc_field(default_factory=dict)

    def to_geojson(self, *, mode: str | None = None) -> dict[str, Any]:
        mode = mode or str(self.notes.get("mode", "2d"))
        features: list[dict[str, Any]] = []
        for variant in self.variants:
            features.extend(output.variant_features(
                variant.network, variant.cost, self.case, variant.geometry,
                variant_id=variant.id, rank=variant.rank, mode=mode,
                extra_summary={
                    "build_order": variant.order,
                    "repairs": variant.repairs,
                    "special_crossings": len(variant.special_report.crossings),
                    "unresolved_special": variant.notes.get("unresolved_special", []),
                    "depth": depth.summary(variant.depth_report) if variant.depth_report else None,
                    "connected_count": len(variant.network.oks_nodes()),
                    "diameter_bumps": variant.diameter_report.bumps,
                    "fallback_approach_oks_ids": [
                        n.ref for n in variant.network.oks_nodes() if n.approach_rule == "fallback"
                    ],
                },
            ))
        return output.feature_collection(features, name=f"{self.case.name} — варианты подключения")


# --- Порядки обработки точек ---------------------------------------------------


def _orders(targets: list[list[ApproachTarget]], field: CaseField) -> dict[str, list[list[ApproachTarget]]]:
    """Разные порядки дают разные деревья; это и есть источник вариантов."""
    by_distance = sorted(targets, key=lambda t: -t[0].target.distance(field.network_geom))
    by_flow = sorted(targets, key=lambda t: -t[0].oks.flow_tph)
    by_near = sorted(targets, key=lambda t: t[0].target.distance(field.network_geom))
    return {"farthest_first": by_distance, "largest_flow_first": by_flow, "nearest_first": by_near}


# Радиус точечного запрета при ремонте нарушения, м
REPAIR_BLOCK_RADIUS_M = 4.0


def build_variant(
    case: CaseInput, shared_field: CaseField, ordered: list[list[ApproachTarget]], *, variant_id: str, order: str,
    mode: str = "2d", local_search: bool = False,
) -> Variant:
    """Дерево → (перестройка веток) → Ду → спецпроходы → (ремонт нарушений → заново) → стоимость.

    Растровый поиск не умеет запрещать следование вдоль спецпроходного объекта
    и не гарантирует прямого пересечения. Поэтому после построения нарушения
    ищутся по вектору, их места запрещаются точечно, и дерево строится заново —
    не больше `max_repair_passes` раз (docs/07-case-model.md §9.9).

    `local_search` — после жадного дерева каждая ветка перестраивается при
    остальных как есть (improve.rebuild_branches); делается в каждом проходе,
    чтобы ремонт видел итоговое дерево.
    """
    rules = shared_field.rules
    field = shared_field.clone()
    repairs: list[str] = []
    local_log: list[str] = []
    passes = int(rules.search["max_repair_passes"])

    for attempt in range(passes + 1):
        builder = TreeBuilder(case, field)
        builder.build(ordered)
        if local_search:
            local_log = improve.rebuild_branches(
                builder, ordered, rounds=int(rules.search.get("branch_rebuild_rounds", 2)),
                network_geom=field.network_geom,
            )
        build = builder.result()
        net = build.network
        flows = {n.id: case.oks_by_id(n.ref).flow_tph for n in net.oks_nodes()}
        diameters.assign(net, rules, flows)
        special_report = special.apply(net, field)
        following = special.following_violations(net, field)
        problems = special_report.issues + following
        # Углы в камерах вдоль путей — как их считает проверяющий; что не
        # исправилось подходом под 45°, тоже уходит в ремонт
        turn_issues = path_turn_issues(net, rules, field.blocked_geom, field.network_geom)
        for node_id, angle, at in turn_issues:
            problems.append(special.SpecialIssue(node_id, node_id, "turn", f"поворот {angle:.1f}° в камере", at))
        if not problems or attempt == passes:
            break
        for issue in problems:
            add_blocked(field, Point(*issue.at).buffer(REPAIR_BLOCK_RADIUS_M))
            repairs.append(f"проход {attempt + 1}: {issue.type} {issue.restriction_id} — {issue.detail}")

    # Ду заново: техузлы разрезали участки, а перестройка пересечений сдвинула вершины
    report = diameters.assign(net, rules, flows)
    # Режим с глубиной — отдельный набор вариантов (§5 приложения): тот же план,
    # плюс профиль глубины, техузлы в изломах и Kгл в стоимости
    depth_report = depth.apply(net, field) if mode == "depth" else None
    if depth_report is not None:
        report = diameters.assign(net, rules, flows)
    geometry = output.prepare_geometry(net, case)
    cost = costing.price(net, case, rules, build.unconnected, lengths=geometry.lengths)
    return Variant(
        id=variant_id, network=net, cost=cost, geometry=geometry, build=build,
        diameter_report=report, special_report=special_report, order=order, repairs=repairs,
        mode=mode, depth_report=depth_report,
        notes={
            "unconnected_reasons": build.reasons,
            "local_search": local_log,
            "minimality": diameters.minimality_issues(net, rules),
            "unresolved_special": [f"{i.type} {i.restriction_id}: {i.detail}" for i in problems],
            "existing_impact": existing.analyse(case, net, rules, flows).to_dict(),
        },
    )


def solve(case: CaseInput, *, rules: Rules | None = None, max_variants: int = 3,
          orders: list[str] | None = None, mode: str = "2d", effort: str = "standard") -> SolveResult:
    """effort: standard — три порядка, в каждом перестройка веток при остальных
    как есть; thorough — плюс повторная вставка точек у лучшего."""
    rules = rules or get_rules()
    started = time.perf_counter()
    field = build_field(case, rules=rules)
    targets = approach_targets(case, field)
    local_search = int(rules.search.get("branch_rebuild_rounds", 2)) > 0

    candidates: list[Variant] = []
    orderings = _orders(targets, field)
    for index, (order, ordered) in enumerate(orderings.items(), start=1):
        if orders and order not in orders:
            continue
        candidates.append(build_variant(
            case, field, ordered, variant_id=f"v{index}", order=order, mode=mode, local_search=local_search,
        ))

    improvement_log: list[str] = []
    if effort == "thorough" and candidates:
        best = min(candidates, key=lambda v: v.score)
        ordered = orderings[best.order]
        improved, improvement_log = improve.reinsertion(
            ordered, best,
            lambda trial: build_variant(
                case, field, trial, variant_id=best.id, order=best.order + "+reinsertion", mode=mode,
                local_search=local_search,
            ),
            trials=int(rules.search.get("reinsertion_trials", 5)), network_geom=field.network_geom,
        )
        if improved is not best:
            candidates.append(improved)

    # Ранжирование по S; одинаковые по сути варианты схлопываются (§9.13 спецификации)
    candidates.sort(key=lambda v: v.score)
    distinct: list[Variant] = []
    for variant in candidates:
        if any(_same_variant(variant, other) for other in distinct):
            continue
        distinct.append(variant)
        if len(distinct) >= max_variants:
            break
    for rank, variant in enumerate(distinct, start=1):
        variant.rank = rank
        variant.id = f"v{rank}"

    # Журнал улучшений лучшего варианта. В хронологии: сначала перестановки точек
    # (если были), потом перестройки веток внутри итогового дерева — они шли при
    # его построении, поэтому их S не продолжает ряд перестановок.
    if distinct:
        local = list(distinct[0].notes.get("local_search", []))
        if improvement_log and local:
            local = [f"в итоговом дереве — {line}" for line in local]
        improvement_log = improvement_log + local

    return SolveResult(
        case=case, field=field, variants=distinct, elapsed_s=time.perf_counter() - started,
        notes={"field": field.notes, "orders_tried": len(candidates), "mode": mode,
               "effort": effort, "improvement": improvement_log},
    )


def _same_variant(a: Variant, b: Variant) -> bool:
    """§9.13: разные точки присоединения или состав групп — разные варианты."""
    def signature(v: Variant):
        ties = sorted(
            (round(n.point[0]), round(n.point[1])) for n in v.network.tie_in_nodes()
        )
        return ties, len(v.network.segments)
    if signature(a) != signature(b):
        return False
    return abs(a.score - b.score) < 1e-3


def solve_file(path: Path | str, out: Path | str | None = None, **kwargs: Any) -> SolveResult:
    case = load_case(path)
    result = solve(case, **kwargs)
    if out is not None:
        Path(out).write_text(json.dumps(result.to_geojson(), ensure_ascii=False, indent=1), encoding="utf-8")
    return result
