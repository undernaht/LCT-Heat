"""Поле стоимости для конкурсной модели.

Вес ячейки — вклад одного метра трассы через неё в итоговый показатель S,
а не рубли и не метры по отдельности: поиск минимизирует ровно то, по чему
ранжируют. Запреты (таблица 2, `block`) и отступы от них — непроходимы.
Спецпроходные ограничения проходимы с коэффициентом Kспец; следование вдоль
них внутри полосы отступа растр запретить не может, поэтому полоса делается
дороже, а нарушения ловятся постфактум (docs/07-case-model.md §9.9).

Ширина пары для отступов берётся по худшему Ду (§9.6 спецификации).
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any

import numpy as np
from shapely.geometry import LineString, Point
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.prepared import prep

from ..geo import raster
from ..geo.raster import Grid
from .model import CaseInput, Restriction
from .rules import Rules, get_rules


@dataclass
class SpecialZone:
    """Одно спецпроходное ограничение с готовыми геометриями."""

    restriction_id: Any
    type: str
    geom: BaseGeometry              # сам объект
    zone: BaseGeometry              # объект + margin: границы спецучастка
    band: BaseGeometry              # объект + отступ + габариты: где нельзя «идти рядом»
    k_special: float
    min_angle_deg: float | None
    margin_m: float
    object_du: int | None = None    # Ду существующей теплосети — для габарита и глубины


@dataclass
class CaseField:
    grid: Grid
    rules: Rules
    du_plan: int                    # Ду худшего случая, по которому строились отступы
    width_plan_m: float
    blocked: np.ndarray             # bool: непроходимо
    reachable: np.ndarray           # bool: из ячейки можно дойти до существующей сети
    k_zone: np.ndarray              # float32: max Kспец спецзон в ячейке (1.0 вне)
    band: np.ndarray                # bool: полоса отступа спецпроходных объектов
    blocked_geom: BaseGeometry      # векторный запрет (для целей и спрямления)
    blocked_by_id: dict[Any, BaseGeometry]   # запрет каждого объекта отдельно
    specials: list[SpecialZone]
    network_geom: BaseGeometry      # существующая сеть (объединённая)
    extra_blocked: list[BaseGeometry] = dc_field(default_factory=list)
    notes: dict[str, Any] = dc_field(default_factory=dict)

    # Текущий вес хранится отдельно и пересчитывается под Ду ветки; веса по
    # каждому Ду кешируются (их 3–5 на набор, а ветки чередуют Ду)
    weight: np.ndarray | None = None
    weight_du: int | None = None
    weights_by_du: dict[int, np.ndarray] = dc_field(default_factory=dict)

    def weights_for(self, du: int) -> np.ndarray:
        """Вес ячеек в единицах S для ветки заданного Ду."""
        if self.weight is not None and self.weight_du == du:
            return self.weight
        cached = self.weights_by_du.get(du)
        if cached is not None:
            self.weight, self.weight_du = cached, du
            return cached
        base = self.rules.score_per_m(du)                  # обычный участок
        spec = self.rules.diameter(du)
        s = self.rules.raw["score"]
        cost_part = float(s["cost_weight"]) * spec.cost_per_m / float(s["cost_scale"])
        length_part = float(s["length_weight"]) / float(s["length_scale"])
        penalty = float(self.rules.search["special_band_penalty"])

        weight = np.full(self.grid.shape, base, dtype=np.float32)
        in_zone = self.k_zone > 1.0
        # спецзона: стоимость × Kспец, длина без изменений
        weight[in_zone] = cost_part * self.k_zone[in_zone] + length_part
        # полоса отступа: дороже, чтобы не идти вдоль (крест всё равно короткий)
        weight[self.band] *= penalty
        weight[self.blocked] = np.inf
        self.weight, self.weight_du = weight, du
        self.weights_by_du[du] = weight
        return weight

    def value_at(self, x: float, y: float) -> float:
        """Для спрямления: вес ячейки под точкой (inf — запрет)."""
        assert self.weight is not None, "сначала weights_for(du)"
        row, col = self.grid.rowcol(x, y)
        return float(self.weight[row, col])

    def is_blocked_segment(self, a: tuple[float, float], b: tuple[float, float]) -> bool:
        """Прямой отрезок задевает запрет (по вектору, не по растру)."""
        return self._prepared_blocked.intersects(LineString([a, b]))

    def __post_init__(self) -> None:
        self._prepared_blocked = prep(self.blocked_geom)

    def clone(self) -> "CaseField":
        """Копия для одного варианта: точечные запреты ремонта не должны
        просачиваться в другие варианты. Копируются только изменяемые массивы."""
        twin = CaseField(
            grid=self.grid, rules=self.rules, du_plan=self.du_plan, width_plan_m=self.width_plan_m,
            blocked=self.blocked.copy(), reachable=self.reachable.copy(), k_zone=self.k_zone, band=self.band,
            blocked_geom=self.blocked_geom, blocked_by_id=self.blocked_by_id, specials=self.specials,
            network_geom=self.network_geom, extra_blocked=list(self.extra_blocked), notes=dict(self.notes),
        )
        return twin


# --- Построение --------------------------------------------------------------


def choose_grid(case: CaseInput, rules: Rules) -> Grid:
    """Сетка по охвату всех объектов; шаг растёт, если ячеек слишком много."""
    search = rules.search
    resolution = float(search["resolution_m"])
    bounds = case.bounds()
    margin = float(search["aoi_margin_m"])
    grid = Grid.covering(bounds, resolution, margin=margin)
    while grid.cells > int(search["max_cells"]):
        resolution *= 1.5
        grid = Grid.covering(bounds, resolution, margin=margin)
    return grid


def planning_du(case: CaseInput, rules: Rules) -> int:
    spec = rules.du_for_flow(case.total_flow_tph) or rules.diameters[-1]
    return spec.du


def block_geometry(restriction: Restriction, rules: Rules, du: int, width_m: float) -> BaseGeometry:
    """Объект + отступ + половина ширины пары: куда ось трассы заходить не может."""
    rule = rules.restriction(restriction.type)
    offset = rule.clearance_for(du) + width_m / 2.0
    return restriction.geom.buffer(offset, join_style="mitre", mitre_limit=2.0)


def special_zone(
    restriction_id: Any, rtype: str, geom: BaseGeometry, rules: Rules, width_m: float,
    *, object_width_m: float = 0.0, object_du: int | None = None,
) -> SpecialZone:
    rule = rules.restriction(rtype)
    zone = geom.buffer(rule.margin_m + object_width_m / 2.0, cap_style="flat" if geom.geom_type.endswith("LineString") else "round")
    band_offset = rule.clearance_m + width_m / 2.0 + object_width_m / 2.0
    band = geom.buffer(band_offset)
    return SpecialZone(
        restriction_id=restriction_id, type=rtype, geom=geom, zone=zone, band=band,
        k_special=rule.k_special, min_angle_deg=rule.min_angle_deg, margin_m=rule.margin_m,
        object_du=object_du,
    )


def build_field(case: CaseInput, *, rules: Rules | None = None, grid: Grid | None = None) -> CaseField:
    rules = rules or get_rules()
    grid = grid or choose_grid(case, rules)
    du = planning_du(case, rules)
    width = rules.diameter(du).width_m

    # --- запреты ---
    blocked_by_id: dict[Any, BaseGeometry] = {}
    for r in case.restrictions:
        rule = rules.restriction(r.type)
        if rule.is_block:
            zone = block_geometry(r, rules, du, width)
            blocked_by_id[r.id] = unary_union([blocked_by_id[r.id], zone]) if r.id in blocked_by_id else zone
    blocked_parts = list(blocked_by_id.values())
    blocked_geom = unary_union(blocked_parts) if blocked_parts else Point().buffer(0)

    # --- спецпроходы ---
    specials: list[SpecialZone] = []
    for r in case.restrictions:
        rule = rules.restriction(r.type)
        if not rule.is_block:
            specials.append(special_zone(r.id, r.type, r.geom, rules, width, object_width_m=rule.gauge_width_m))
    # существующая сеть — спецпроход без врезки; габарит по её Ду
    for edge in case.network:
        own = rules.diameter(edge.diameter).width_m if rules.has_diameter(edge.diameter) else 0.0
        specials.append(special_zone(edge.id, "heat_network", edge.geom, rules, width, object_width_m=own, object_du=edge.diameter))

    network_geom = unary_union([e.geom for e in case.network]) if case.network else LineString()

    # --- растр ---
    blocked = raster.burn_mask(grid, [blocked_geom]) if not blocked_geom.is_empty else np.zeros(grid.shape, bool)
    k_zone = np.ones(grid.shape, dtype=np.float32)
    band = np.zeros(grid.shape, dtype=bool)
    # Kспец — наибольший из перекрывающихся (§4 приложения)
    for z in sorted(specials, key=lambda z: z.k_special):
        mask = raster.burn_mask(grid, [z.zone])
        k_zone[mask] = np.maximum(k_zone[mask], np.float32(z.k_special))
        band |= raster.burn_mask(grid, [z.band])
    # Полоса отступа дорожает и внутри спецзоны: у дороги полоса (1,5 м + w/2)
    # целиком лежит в зоне (+3 м), и без этого следование вдоль дороги стоило бы
    # столько же, сколько её пересечение.

    reachable = reachable_mask(grid, blocked, network_geom)

    field = CaseField(
        grid=grid, rules=rules, du_plan=du, width_plan_m=width,
        blocked=blocked, reachable=reachable, k_zone=k_zone, band=band, blocked_geom=blocked_geom,
        blocked_by_id=blocked_by_id, specials=specials, network_geom=network_geom,
        notes={
            "grid": f"{grid.width}×{grid.height} @ {grid.resolution:g} м",
            "du_plan": du,
            "blocked_share": round(float(blocked.mean()), 3),
            "special_zones": len(specials),
        },
    )
    return field


def reachable_mask(grid: Grid, blocked: np.ndarray, network_geom: BaseGeometry) -> np.ndarray:
    """Ячейки, из которых существующая сеть достижима по проходимым ячейкам.

    Дворы, замкнутые зонами отступа, выглядят проходимыми, но выйти из них
    нельзя; цель поиска в таком кармане — заведомо неподключённая точка.
    """
    from scipy import ndimage

    labels, _ = ndimage.label(~blocked, structure=np.ones((3, 3), dtype=int))
    network_cells = raster.burn_mask(grid, [network_geom]) if not network_geom.is_empty else np.zeros(grid.shape, bool)
    live = np.unique(labels[network_cells & (labels > 0)])
    if live.size == 0:
        return ~blocked
    return np.isin(labels, live)


def add_blocked(field: CaseField, geom: BaseGeometry) -> None:
    """Точечный запрет (ремонт после нарушения): и в растр, и в вектор."""
    field.extra_blocked.append(geom)
    field.blocked |= raster.burn_mask(field.grid, [geom])
    field.blocked_geom = unary_union([field.blocked_geom, geom])
    field._prepared_blocked = prep(field.blocked_geom)
    field.reachable = reachable_mask(field.grid, field.blocked, field.network_geom)
    field.weight = None
    field.weights_by_du.clear()
