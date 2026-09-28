"""Таблицы Технического приложения, прочитанные из case_rules.yaml.

Чисел в коде валидатора нет: единственный источник — YAML, который сверяется
с первоисточником. Здесь только доступ к таблицам и формулы §3, §5, §6
приложения.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "case_rules.yaml"


@dataclass(frozen=True)
class DuRow:
    du: int
    capacity_tph: float
    limit_m: float
    unit_cost: float
    width_m: float
    height_m: float


@dataclass(frozen=True)
class RestrictionRule:
    rtype: str
    rule: str                       # block — запрет, special — спецпроход
    clearance_m: float
    margin_m: float = 0.0           # поле спецучастка за объектом, вдоль трассы
    min_angle_deg: float | None = None
    k_special: float = 1.0
    gauge_width_m: float = 0.0      # собственная ширина объекта; 0 — габарита нет
    gauge_by_diameter: bool = False  # ширина по таблице 1 (существующая сеть)

    @property
    def is_block(self) -> bool:
        return self.rule == "block"

    @property
    def is_special(self) -> bool:
        return self.rule == "special"


class Rules:
    def __init__(self, data: dict[str, Any]) -> None:
        self.raw = data
        rows = [
            DuRow(int(du), float(v[0]), float(v[1]), float(v[2]), float(v[3]), float(v[4]))
            for du, v in data["diameters"].items()
        ]
        self.rows = sorted(rows, key=lambda r: r.du)
        self.by_du = {r.du: r for r in self.rows}

        chambers = data["chambers"]
        self.chamber_cost_table = [
            (int(a), int(b), float(c)) for a, b, c in chambers["cost_by_max_du"]
        ]
        self.max_connections = int(chambers["max_connections"])
        self.reuse_within_m = float(chambers["reuse_existing_within_m"])
        self.tie_in_cost = float(chambers["tie_in_cost"])
        self.attach_tol_m = float(chambers.get("attach_tolerance_m", 0.5))
        self.chamber_diameter_includes_existing = bool(
            chambers.get("chamber_diameter_includes_existing", True)
        )

        penalty = data["penalty"]
        self.penalty_fixed = float(penalty["fixed"])
        self.penalty_per_tph = float(penalty["per_tph"])

        score = data["score"]
        self.cost_weight = float(score["cost_weight"])
        self.cost_scale = float(score["cost_scale"])
        self.length_weight = float(score["length_weight"])
        self.length_scale = float(score["length_scale"])

        self.restrictions: dict[str, RestrictionRule] = {}
        for rtype, cfg in data["restrictions"].items():
            gauge = cfg.get("gauge") or {}
            self.restrictions[rtype] = RestrictionRule(
                rtype=rtype,
                rule=str(cfg["rule"]),
                clearance_m=float(cfg.get("clearance_m", 0.0)),
                margin_m=float(cfg.get("margin_m", 0.0)),
                min_angle_deg=(
                    float(cfg["min_angle_deg"]) if cfg.get("min_angle_deg") is not None else None
                ),
                k_special=float(cfg.get("k_special", 1.0)),
                gauge_width_m=float(gauge.get("width_m", 0.0)),
                gauge_by_diameter=bool(gauge.get("by_diameter", False)),
            )
        self.oks_clearance_table = [
            (int(a), int(b), float(c))
            for a, b, c in data["restrictions"]["oks"]["clearance_by_du"]
        ]
        self.aliases: dict[str, str] = dict(data.get("aliases") or {})

        depth = data.get("depth") or {}
        self.normal_depth_m = float(depth.get("normal_top_depth_m", 3.0))
        self.k_per_m_below_normal = float(depth.get("k_per_m_below_normal", 0.10))

    @classmethod
    def load(cls, path: str | Path = DEFAULT_RULES_PATH) -> Rules:
        with open(path, encoding="utf-8") as fh:
            return cls(yaml.safe_load(fh))

    # --- таблица 1 -------------------------------------------------------

    def row(self, du: Any) -> DuRow | None:
        if isinstance(du, bool) or not isinstance(du, int):
            return None
        return self.by_du.get(du)

    def min_du_for_flow(self, flow_tph: float) -> int | None:
        """Наименьший Ду с пропускной способностью не меньше расхода."""
        for r in self.rows:
            if r.capacity_tph >= flow_tph - 1e-9:
                return r.du
        return None

    def step_down(self, du: int) -> int | None:
        """Ду на ступень меньше (для проверки минимальности)."""
        prev = None
        for r in self.rows:
            if r.du == du:
                return prev
            prev = r.du
        return None

    # --- §3.2 -------------------------------------------------------------

    def chamber_cost(self, du: int) -> float | None:
        for lo, hi, cost in self.chamber_cost_table:
            if lo <= du <= hi:
                return cost
        return None

    # --- таблица 2 --------------------------------------------------------

    def canonical_type(self, rtype: Any) -> str:
        return self.aliases.get(str(rtype), str(rtype))

    def restriction(self, rtype: Any) -> RestrictionRule | None:
        return self.restrictions.get(self.canonical_type(rtype))

    def oks_clearance(self, du: int) -> float:
        for lo, hi, clearance in self.oks_clearance_table:
            if lo <= du <= hi:
                return clearance
        return self.restrictions["oks"].clearance_m

    def half_gauge(self, rule: RestrictionRule, diameter: Any = None) -> float:
        """Полуширина собственного габарита объекта (0, если его нет)."""
        if rule.gauge_by_diameter:
            row = self.row(diameter)
            return row.width_m / 2.0 if row else 0.0
        return rule.gauge_width_m / 2.0

    # --- §5, §6 ------------------------------------------------------------

    def k_depth(self, top_depth_m: float) -> float:
        if top_depth_m <= self.normal_depth_m:
            return 1.0
        return 1.0 + self.k_per_m_below_normal * (top_depth_m - self.normal_depth_m)

    def penalty(self, flow_tph: float) -> float:
        return self.penalty_fixed + self.penalty_per_tph * flow_tph

    def score(self, calculated_cost: float, new_network_length: float) -> float:
        return (
            self.cost_weight * calculated_cost / self.cost_scale
            + self.length_weight * new_network_length / self.length_scale
        )
