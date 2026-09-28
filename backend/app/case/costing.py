"""Стоимость и итоговый показатель по §3.2 и §6 приложения.

Считается по построенной сети после назначения Ду и спецпроходов. Все суммы
сводки собираются из тех же округлённых значений, что уходят в выход, — чтобы
программная проверка тождеств сошлась до копейки (§5.3 спецификации).
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any

from .model import CaseInput
from .network import NODE_CHAMBER_EXISTING, NODE_CHAMBER_NEW, Network, Node
from .rules import Rules

LENGTH_DECIMALS = 3      # мм
COST_DECIMALS = 2        # копейки
SCORE_DECIMALS = 6


@dataclass
class ChamberCost:
    node_id: str
    diameter: int
    cost: float


@dataclass
class VariantCost:
    segment_cost: dict[str, float] = dc_field(default_factory=dict)
    chambers: list[ChamberCost] = dc_field(default_factory=list)
    tie_in_count: int = 0
    tie_in_cost: float = 0.0
    unconnected: list[Any] = dc_field(default_factory=list)
    unconnected_penalty: float = 0.0
    new_network_length: float = 0.0
    construction_cost: float = 0.0
    chamber_construction_cost: float = 0.0
    calculated_cost: float = 0.0
    score: float = 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "construction_cost": self.construction_cost,
            "chamber_construction_cost": self.chamber_construction_cost,
            "existing_chamber_tie_in_count": self.tie_in_count,
            "existing_chamber_tie_in_cost": self.tie_in_cost,
            "unconnected_penalty": self.unconnected_penalty,
            "calculated_cost": self.calculated_cost,
            "new_network_length": self.new_network_length,
            "score": self.score,
        }


def chamber_diameter(net: Network, node: Node, rules: Rules) -> int:
    """§3.2: наибольший Ду всех примыкающих участков; §9.3 — включая существующий."""
    dus = [s.du for s in net.incident(node.id)]
    if node.on_edge is not None and rules.chamber_diameter_includes_existing:
        dus.append(node.on_edge.diameter)
    return max(dus) if dus else 0


def price(
    net: Network, case: CaseInput, rules: Rules, unconnected: list[Any],
    *, lengths: dict[str, float] | None = None,
) -> VariantCost:
    """`lengths` — длины по выгружаемым координатам (output.prepare_geometry); без них — внутренние."""
    result = VariantCost()

    for segment in net.segments.values():
        raw_length = lengths[segment.id] if lengths and segment.id in lengths else segment.length
        length = round(raw_length, LENGTH_DECIMALS)
        spec = rules.diameter(segment.du)
        cost = round(length * spec.cost_per_m * segment.k_depth * segment.k_special, COST_DECIMALS)
        segment.cost = cost
        result.segment_cost[segment.id] = cost
        result.new_network_length += length

    for node in net.nodes.values():
        if node.kind == NODE_CHAMBER_NEW:
            du = chamber_diameter(net, node, rules)
            result.chambers.append(ChamberCost(node.id, du, rules.chamber_cost(du)))
        elif node.kind == NODE_CHAMBER_EXISTING:
            # каждый новый участок, заканчивающийся в существующей камере, — врезка
            result.tie_in_count += net.degree(node.id)

    result.tie_in_cost = result.tie_in_count * rules.tie_in_cost
    result.chamber_construction_cost = sum(c.cost for c in result.chambers)
    result.unconnected = list(unconnected)
    for oks_id in unconnected:
        oks = case.oks_by_id(oks_id)
        result.unconnected_penalty += rules.penalty(oks.flow_tph if oks else 0.0)

    result.new_network_length = round(result.new_network_length, LENGTH_DECIMALS)
    result.construction_cost = round(
        sum(result.segment_cost.values()) + result.chamber_construction_cost + result.tie_in_cost,
        COST_DECIMALS,
    )
    result.calculated_cost = round(result.construction_cost + result.unconnected_penalty, COST_DECIMALS)
    result.score = round(rules.score(result.calculated_cost, result.new_network_length), SCORE_DECIMALS)
    return result
