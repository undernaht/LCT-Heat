"""Локальное улучшение дерева: перестройка веток и повторная вставка точек.

Жадное дерево зависит от порядка: точка, обработанная рано, тянет свою ветку
к магистрали, хотя позже рядом появится чужая ветка, к которой было бы дешевле
присоединиться. Два лекарства:

* перестройка ветки — собственная ветка точки снимается и прокладывается
  заново при остальных ветках как есть (одна волна на точку, дёшево; можно
  пройти все точки и повторить, пока есть улучшение);
* повторная вставка — точка переставляется в конец порядка и дерево строится
  заново (дорого, зато соседние ветки тоже перестраиваются без неё).

Улучшение по S принимается, ухудшение отбрасывается. Кандидаты — точки с
самой длинной собственной веткой относительно расстояния до сети: у них
больше всего «лишних» метров.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from shapely.ops import unary_union

from .network import Network, Segment
from .targets import ApproachTarget

if TYPE_CHECKING:
    from .tree import TreeBuilder


def _own_branch(net: Network, oks_node_id: str) -> list[Segment]:
    """Участки от точки до первого узла, где ветка встречается с другими."""
    own: list[Segment] = []
    current = oks_node_id
    seen = {current}
    while True:
        incident = net.incident(current)
        if current != oks_node_id and len(incident) != 2:
            break
        nxt = [s for s in incident if s.other(current) not in seen]
        if not nxt:
            break
        segment = nxt[0]
        own.append(segment)
        current = segment.other(current)
        seen.add(current)
        node = net.nodes[current]
        if node.is_tie_in_point or (node.kind != "technical_node" and len(net.incident(current)) != 2):
            break
    return own


def _own_branch_length(net: Network, oks_node_id: str) -> float:
    return sum(s.length for s in _own_branch(net, oks_node_id))


def _worth_rebuilding(net: Network, oks_node_id: str, built_at: int) -> bool:
    """Есть ли смысл перестраивать ветку: после неё построено что-то ближе её длины.

    Ветка, рядом с которой ничего нового не появилось, ляжет так же — волна
    детерминирована; тратить на неё поиск незачем.
    """
    own = _own_branch(net, oks_node_id)
    if not own:
        return False
    own_ids = {s.id for s in own}
    own_length = sum(s.length for s in own)
    own_geom = unary_union([s.line for s in own])
    return any(
        s.step > built_at and s.line.distance(own_geom) < own_length
        for s in net.segments.values() if s.id not in own_ids
    )


def _ranked(net: Network, order: list[list[ApproachTarget]], network_geom) -> list[tuple[float, str, list[ApproachTarget]]]:
    """Точки по убыванию «лишних» метров: (отношение, id узла точки, кандидаты)."""
    ranked = []
    for node in net.oks_nodes():
        cands = next((c for c in order if c[0].oks.id == node.ref), None)
        if cands is None:
            continue
        straight = cands[0].target.distance(network_geom) if network_geom is not None else 1.0
        ranked.append((_own_branch_length(net, node.id) / max(straight, 1.0), node.id, cands))
    ranked.sort(key=lambda item: -item[0])
    return ranked


def rebuild_branches(
    builder: "TreeBuilder", ordered: list[list[ApproachTarget]], *, rounds: int = 2, network_geom=None,
) -> list[str]:
    """Перестроить ветки всех точек по очереди; повторять, пока есть улучшение."""
    log: list[str] = []
    for round_index in range(rounds):
        improved = False
        for _ratio, node_id, cands in _ranked(builder.net, ordered, network_geom):
            if node_id not in builder.net.nodes:
                continue                     # узел исчез при перестройке соседа (слияние камер)
            if not _worth_rebuilding(builder.net, node_id, builder.step_of_oks.get(node_id, 0)):
                continue
            accepted, before, after = builder.rebuild_leaf(node_id, cands)
            if accepted:
                improved = True
                log.append(f"ветка {cands[0].oks.id} заново: S {_ru(before)} → {_ru(after)}")
        if not improved:
            break
    return log


def _ru(value: float) -> str:
    """Число для журнала в русской записи — как остальные подписи интерфейса."""
    return f"{value:.4f}".replace(".", ",")


def reinsertion(
    ordered: list[list[ApproachTarget]],
    base: Any,
    build: Callable[[list[list[ApproachTarget]]], Any],
    *,
    trials: int = 5,
    network_geom=None,
) -> tuple[Any, list[str]]:
    """Перебрать до `trials` точек; вернуть лучший вариант и журнал попыток."""
    log: list[str] = []
    best, order = base, list(ordered)
    net = base.network
    detours = []
    for node in net.oks_nodes():
        cands = next((c for c in order if c[0].oks.id == node.ref), None)
        if cands is None:
            continue
        straight = cands[0].target.distance(network_geom) if network_geom is not None else 1.0
        detours.append((_own_branch_length(net, node.id) / max(straight, 1.0), cands))
    detours.sort(key=lambda item: -item[0])

    for ratio, cands in detours[:trials]:
        trial_order = [c for c in order if c is not cands] + [cands]
        candidate = build(trial_order)
        if candidate.score < best.score - 1e-6:
            log.append(f"{cands[0].oks.id} в конец: S {_ru(best.score)} → {_ru(candidate.score)}")
            best, order = candidate, trial_order
        else:
            log.append(f"{cands[0].oks.id} в конец: без улучшения ({_ru(candidate.score)} ≥ {_ru(best.score)})")
    return best, log


__all__ = ["rebuild_branches", "reinsertion"]
