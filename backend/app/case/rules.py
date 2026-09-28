"""Правила приложения: таблицы, стоимости, показатель.

Всё читается из `config/case_rules.yaml`. В коде — только формулы приложения
и доступ к таблицам; ни одного числа модели здесь быть не должно.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "case_rules.yaml"


@dataclass(frozen=True)
class DiameterSpec:
    du: int
    capacity_tph: float
    limit_length_m: float
    cost_per_m: float
    width_m: float
    height_m: float


@dataclass(frozen=True)
class RestrictionRule:
    kind: str                       # block | special
    clearance_m: float
    clearance_by_du: tuple[tuple[int, int, float], ...] = ()
    margin_m: float = 0.0
    min_angle_deg: float | None = None
    k_special: float = 1.0
    gauge_width_m: float = 0.0      # 0 — габарита у объекта нет
    gauge_height_m: float = 0.0
    gauge_by_diameter: bool = False
    gauge_top_depth_m: float | None = None
    depth: dict[str, Any] | None = None

    @property
    def is_block(self) -> bool:
        return self.kind == "block"

    def clearance_for(self, du: int) -> float:
        for low, high, value in self.clearance_by_du:
            if low <= du <= high:
                return float(value)
        return float(self.clearance_m)


class Rules:
    """Обёртка над конфигом с формулами приложения."""

    def __init__(self, raw: dict[str, Any]):
        self.raw = raw
        self.diameters: list[DiameterSpec] = sorted(
            (
                DiameterSpec(int(du), *[float(v) for v in row])
                for du, row in raw["diameters"].items()
            ),
            key=lambda d: d.du,
        )
        self._by_du = {d.du: d for d in self.diameters}
        self.restrictions: dict[str, RestrictionRule] = {
            name: _restriction_rule(body) for name, body in raw["restrictions"].items()
        }
        self.aliases: dict[str, str] = {
            str(k).lower(): str(v) for k, v in (raw.get("aliases") or {}).items()
        }

    # --- Таблица 1 ---

    def diameter(self, du: int) -> DiameterSpec:
        try:
            return self._by_du[int(du)]
        except KeyError:
            raise ValueError(f"Ду{du} нет в таблице 1") from None

    def has_diameter(self, du: int) -> bool:
        return int(du) in self._by_du

    def du_for_flow(self, flow_tph: float, *, at_least: int = 0) -> DiameterSpec | None:
        """Минимальный Ду с пропускной способностью ≥ расхода (и ≥ at_least)."""
        for spec in self.diameters:
            if spec.du >= at_least and spec.capacity_tph >= flow_tph - 1e-9:
                return spec
        return None

    def du_for_flow_and_length(
        self, flow_tph: float, run_length_m: float, *, at_least: int = 0
    ) -> DiameterSpec | None:
        """§2.3 приложения: минимальный Ду, проходящий и по расходу, и по предельной длине."""
        for spec in self.diameters:
            if spec.du < at_least:
                continue
            if spec.capacity_tph >= flow_tph - 1e-9 and spec.limit_length_m >= run_length_m - 1e-6:
                return spec
        return None

    def next_du(self, du: int) -> DiameterSpec | None:
        for spec in self.diameters:
            if spec.du > du:
                return spec
        return None

    def width_for_flow_upper_bound(self, total_flow_tph: float) -> float:
        """Ширина пары при худшем Ду (все расходы в одном участке) — для поиска."""
        spec = self.du_for_flow(total_flow_tph) or self.diameters[-1]
        return spec.width_m

    # --- §3.2 камеры и врезки ---

    def chamber_cost(self, max_du: int) -> float:
        for low, high, cost in self.raw["chambers"]["cost_by_max_du"]:
            if int(low) <= max_du <= int(high):
                return float(cost)
        # Ду между полосами (например 350) в таблице нет — берём ближайшую сверху
        for low, high, cost in self.raw["chambers"]["cost_by_max_du"]:
            if max_du <= int(high):
                return float(cost)
        return float(self.raw["chambers"]["cost_by_max_du"][-1][2])

    @property
    def tie_in_cost(self) -> float:
        return float(self.raw["chambers"]["tie_in_cost"])

    @property
    def max_connections(self) -> int:
        return int(self.raw["chambers"]["max_connections"])

    @property
    def reuse_chamber_within_m(self) -> float:
        return float(self.raw["chambers"]["reuse_existing_within_m"])

    @property
    def attach_tolerance_m(self) -> float:
        return float(self.raw["chambers"]["attach_tolerance_m"])

    @property
    def chamber_diameter_includes_existing(self) -> bool:
        return bool(self.raw["chambers"].get("chamber_diameter_includes_existing", True))

    @property
    def max_turn_deg(self) -> float:
        return float(self.search.get("max_turn_deg", 90.0))

    @property
    def min_crossing_angle_deg(self) -> float:
        return float(self.search.get("min_crossing_angle_deg", 45.0))

    @property
    def approach_fallback_lenient(self) -> bool:
        return str(self.search.get("approach_fallback", "lenient")).lower() != "strict"

    # --- §6 штраф и показатель ---

    def penalty(self, flow_tph: float) -> float:
        p = self.raw["penalty"]
        return float(p["fixed"]) + float(p["per_tph"]) * float(flow_tph)

    def score(self, calculated_cost: float, new_length_m: float) -> float:
        s = self.raw["score"]
        return (
            float(s["cost_weight"]) * calculated_cost / float(s["cost_scale"])
            + float(s["length_weight"]) * new_length_m / float(s["length_scale"])
        )

    def score_per_m(self, du: int, k_special: float = 1.0, k_depth: float = 1.0) -> float:
        """Вклад одного метра участка в S — вес поиска в «единицах показателя».

        Поиск минимизирует ровно S, а не длину и не деньги по отдельности:
        0,7·c(Ду)·K/25 млн + 0,3/100. Для Ду100 это 0,0025 + 0,003 — длина и
        стоимость весят примерно поровну, и это свойство модели, а не наш выбор.
        """
        s = self.raw["score"]
        cost = self.diameter(du).cost_per_m * k_special * k_depth
        return (
            float(s["cost_weight"]) * cost / float(s["cost_scale"])
            + float(s["length_weight"]) / float(s["length_scale"])
        )

    def score_of_cost(self, rub: float) -> float:
        """Вклад фиксированной стоимости (камера, врезка) в S."""
        s = self.raw["score"]
        return float(s["cost_weight"]) * rub / float(s["cost_scale"])

    # --- Таблица 2 ---

    def canonical_type(self, restriction_type: str | None) -> str | None:
        if restriction_type is None:
            return None
        key = str(restriction_type).strip().lower()
        key = self.aliases.get(key, key)
        return key if key in self.restrictions else None

    def restriction(self, canonical: str) -> RestrictionRule:
        return self.restrictions[canonical]

    # --- §5 глубина ---

    @property
    def depth(self) -> dict[str, Any]:
        return self.raw["depth"]

    def k_depth(self, top_depth_m: float) -> float:
        normal = float(self.depth["normal_top_depth_m"])
        if top_depth_m <= normal + 1e-9:
            return 1.0
        return 1.0 + float(self.depth["k_per_m_below_normal"]) * (top_depth_m - normal)

    # --- наши параметры поиска ---

    @property
    def search(self) -> dict[str, Any]:
        return self.raw["search"]


def _restriction_rule(body: dict[str, Any]) -> RestrictionRule:
    gauge = body.get("gauge") or {}
    return RestrictionRule(
        kind=str(body["rule"]),
        clearance_m=float(body["clearance_m"]),
        clearance_by_du=tuple(
            (int(a), int(b), float(c)) for a, b, c in (body.get("clearance_by_du") or [])
        ),
        margin_m=float(body.get("margin_m", 0.0)),
        min_angle_deg=(
            float(body["min_angle_deg"]) if body.get("min_angle_deg") is not None else None
        ),
        k_special=float(body.get("k_special", 1.0)),
        gauge_width_m=float(gauge.get("width_m", 0.0)),
        gauge_height_m=float(gauge.get("height_m", 0.0)),
        gauge_by_diameter=bool(gauge.get("by_diameter", False)),
        gauge_top_depth_m=(
            float(gauge["top_depth_m"]) if gauge.get("top_depth_m") is not None else None
        ),
        depth=body.get("depth"),
    )


@lru_cache(maxsize=1)
def get_rules() -> Rules:
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        return Rules(yaml.safe_load(fh))
