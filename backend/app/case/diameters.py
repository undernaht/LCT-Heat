"""Расходы и условные диаметры по §2.3 приложения.

1. Расход участка — сумма расходов точек дальше по дереву.
2. Ду — минимальный по расходу, но не меньше Ду участков выше по дереву
   (по направлению к присоединению Ду не уменьшается).
3. Предельная длина — по каждому пути от точки до присоединения, по каждому
   максимальному отрезку одного Ду. Превышение — поднять Ду всего отрезка на
   следующий, проходящий и по расходу, и по длине. Поднятый отрезок может
   слиться с соседним — поэтому до неподвижной точки (§9.5 спецификации).
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any

from .network import NODE_OKS, Network, Segment
from .rules import Rules


@dataclass
class LimitCheck:
    """Один непрерывный путь одного Ду: что проверялось и что вышло."""

    oks_id: Any
    du: int
    run_length_m: float
    limit_m: float
    segment_ids: list[str]

    @property
    def ok(self) -> bool:
        return self.run_length_m <= self.limit_m + 1e-6


@dataclass
class DiameterReport:
    checks: list[LimitCheck] = dc_field(default_factory=list)
    bumps: list[str] = dc_field(default_factory=list)      # что и почему поднято
    unresolved: list[str] = dc_field(default_factory=list) # где предельную длину закрыть нечем

    @property
    def ok(self) -> bool:
        return not self.unresolved and all(c.ok for c in self.checks)


def assign(net: Network, rules: Rules, flow_by_oks_node: dict[str, float]) -> DiameterReport:
    report = DiameterReport()
    roots = net.tie_in_nodes()
    if not roots:
        return report

    # --- 1. расходы ---
    for segment in net.segments.values():
        segment.flow_tph = 0.0
    orientation: dict[str, tuple[str, str]] = {}     # segment id → (ближний к корню, дальний)
    root_of_segment: dict[str, str] = {}
    for root in roots:
        flows = net.subtree_flow(root.id, flow_by_oks_node)
        for s, near, far in net.oriented_from_root(root.id):
            if s.id in orientation:
                continue           # компонента уже обойдена от другого корня
            s.flow_tph = flows[s.id]
            orientation[s.id] = (near, far)
            root_of_segment[s.id] = root.id

    # --- 2. Ду по расходу, монотонно к корню ---
    for segment in net.segments.values():
        spec = rules.du_for_flow(segment.flow_tph)
        if spec is None:
            report.unresolved.append(f"{segment.id}: расход {segment.flow_tph:.1f} т/ч выше Ду1400")
            segment.du = rules.diameters[-1].du
        else:
            segment.du = spec.du
    _enforce_monotone(net, roots, report)

    # --- 3. предельная длина: до неподвижной точки ---
    # Путь идёт от точки подключения к присоединению. Внутри отрезка одного Ду
    # длина накапливается; на первом участке, где она превысила предел, и на
    # всех участках того же отрезка ближе к присоединению Ду поднимается.
    # Участки со стороны точки не трогаются: они и так укладываются в предел,
    # а поднять их было бы «произвольным завышением» (§9.5 спецификации).
    for _ in range(24):
        report.checks = []
        bumped = False
        for root in roots:
            paths = net.paths_to_root(root.id)
            for node_id, path in paths.items():
                node = net.nodes[node_id]
                if node.kind != NODE_OKS:
                    continue
                for run in _runs(path):
                    length = sum(s.length for s in run)
                    limit = rules.diameter(run[0].du).limit_length_m
                    report.checks.append(LimitCheck(
                        oks_id=node.ref, du=run[0].du, run_length_m=round(length, 1),
                        limit_m=limit, segment_ids=[s.id for s in run],
                    ))
                    if length <= limit + 1e-6:
                        continue
                    # первый участок, на котором накопленная длина превысила предел
                    accumulated = 0.0
                    first = 0
                    for index, s in enumerate(run):
                        accumulated += s.length
                        if accumulated > limit + 1e-6:
                            first = index
                            break
                    tail = run[first:]
                    tail_length = sum(s.length for s in tail)
                    flow = max(s.flow_tph for s in tail)
                    spec = rules.du_for_flow_and_length(flow, tail_length, at_least=run[0].du + 1)
                    if spec is None:
                        report.unresolved.append(
                            f"путь от {node.ref}: отрезок Ду{run[0].du} длиной {length:.0f} м "
                            f"не закрывается ни одним Ду таблицы"
                        )
                        continue
                    for s in tail:
                        s.du = spec.du
                    report.bumps.append(
                        f"путь от {node.ref}: Ду{run[0].du} → Ду{spec.du} на {len(tail)} уч. "
                        f"со стороны присоединения ({length:.0f} м > {limit:.0f} м)"
                    )
                    bumped = True
                    break        # отрезки изменились — пути перестроить
                if bumped:
                    break
            if bumped:
                break
        if not bumped:
            break
        _enforce_monotone(net, roots, report)
    return report


def minimality_issues(net: Network, rules: Rules) -> list[str]:
    """§3: ни один участок нельзя понизить на ступень без нарушения условий.

    Проверка «произвольного завышения»: для каждого участка пробуем Ду на
    ступень меньше и смотрим, проходят ли способность, предельная длина по
    всем путям через него и неубывание к присоединению.
    """
    issues: list[str] = []
    roots = net.tie_in_nodes()
    for segment in list(net.segments.values()):
        current = segment.du
        lower = [d for d in rules.diameters if d.du < current]
        if not lower:
            continue
        candidate = lower[-1]
        if candidate.capacity_tph < segment.flow_tph - 1e-9:
            continue
        # неубывание: все участки дальше от корня должны быть ≤ candidate
        ok = True
        segment.du = candidate.du
        try:
            for root in roots:
                paths = net.paths_to_root(root.id)
                for node_id, path in paths.items():
                    if net.nodes[node_id].kind != NODE_OKS or segment not in path:
                        continue
                    dus = [s.du for s in path]
                    if any(a > b for a, b in zip(dus, dus[1:])):
                        ok = False
                        break
                    for run in _runs(path):
                        if sum(s.length for s in run) > rules.diameter(run[0].du).limit_length_m + 1e-6:
                            ok = False
                            break
                    if not ok:
                        break
                if not ok:
                    break
        finally:
            segment.du = current
        if ok:
            issues.append(f"{segment.id}: Ду{current} можно понизить до Ду{candidate.du} без нарушений")
    return issues


def _enforce_monotone(net: Network, roots, report: DiameterReport) -> None:
    """Ду не уменьшается к присоединению: участок ≥ всех участков дальше от корня."""
    for root in roots:
        order = list(net.oriented_from_root(root.id))
        # от листьев к корню: обратный порядок обхода в глубину
        children: dict[str, list[Segment]] = {}
        for s, near, far in order:
            children.setdefault(near, []).append(s)
        far_of = {s.id: far for s, near, far in order}

        def max_below(segment: Segment) -> int:
            far = far_of[segment.id]
            best = segment.du
            for child in children.get(far, []):
                best = max(best, max_below(child))
            segment.du = best
            return best

        for s in children.get(root.id, []):
            max_below(s)


def _runs(path: list[Segment]) -> list[list[Segment]]:
    """Разбить путь (от точки к корню) на максимальные отрезки одного Ду."""
    runs: list[list[Segment]] = []
    for s in path:
        if runs and runs[-1][-1].du == s.du:
            runs[-1].append(s)
        else:
            runs.append([s])
    return runs
