"""Построение поля стоимости.

Значение ячейки — стоимость прокладки одного метра трассы в этой точке, в рублях:

    cost = pipe(Ду, способ) + earthwork(Ду) × surface_mult + penalty

Модель аддитивная: покрытие удорожает земляные работы, но не саму трубу.
Обоснование и приём с пересечением чужих сетей — docs/04-algorithm.md §4.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any

import numpy as np
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from ..compliance import rules
from ..compliance.rules import RuleContext
from ..config import get_config
from ..domain.enums import Laying, SurfaceClass
from ..domain.models import AreaModel, Building
from ..geo import raster
from ..geo.raster import Grid

SURFACE_ORDER: list[SurfaceClass] = [
    SurfaceClass.GROUND,
    SurfaceClass.LAWN,
    SurfaceClass.SIDEWALK,
    SurfaceClass.YARD,
    SurfaceClass.STREET_LOCAL,
    SurfaceClass.STREET_MAJOR,
    SurfaceClass.UTILITY_CORRIDOR,
]


@dataclass
class CostField:
    grid: Grid
    cost: np.ndarray            # ₽/м, np.inf — непроходимо
    blocked: np.ndarray         # bool
    surface_code: np.ndarray    # индекс в SURFACE_ORDER
    penalty: np.ndarray         # ₽/м, только надбавки зон
    du_mm: int
    laying: Laying
    context: RuleContext
    base_rub_per_m: float
    profile: str = "min_cost"
    notes: dict[str, Any] = dc_field(default_factory=dict)

    def value_at(self, x: float, y: float) -> float:
        row, col = self.grid.rowcol(x, y)
        return float(self.cost[row, col])

    def surface_at(self, x: float, y: float) -> SurfaceClass:
        row, col = self.grid.rowcol(x, y)
        return SURFACE_ORDER[int(self.surface_code[row, col])]


# --- Ставки -----------------------------------------------------------------


def _interp_by_du(table: dict[int, float] | dict[str, float], du_mm: int) -> float:
    """Ставка для произвольного Ду по таблице опорных значений."""
    points = sorted((int(k), float(v)) for k, v in table.items())
    if du_mm <= points[0][0]:
        return points[0][1]
    if du_mm >= points[-1][0]:
        return points[-1][1]
    for (du_a, value_a), (du_b, value_b) in zip(points, points[1:]):
        if du_a <= du_mm <= du_b:
            share = (du_mm - du_a) / (du_b - du_a)
            return value_a + share * (value_b - value_a)
    return points[-1][1]


def pipe_rate(du_mm: int, laying: Laying) -> float:
    costs = get_config().costs
    base = _interp_by_du(costs["pipe_per_m"]["channelless"], du_mm)
    if laying is Laying.CHANNEL:
        return base * costs["pipe_per_m"]["channel_factor"]
    if laying is Laying.OVERHEAD:
        return base * costs["pipe_per_m"]["overhead_factor"]
    return base


def earthwork_rate(du_mm: int) -> float:
    return _interp_by_du(get_config().costs["earthwork_per_m"]["base_by_du"], du_mm)


def _profile_settings(profile: str) -> dict[str, Any]:
    return get_config().costs["profiles"].get(profile, {})


def surface_multipliers(profile: str) -> dict[SurfaceClass, float]:
    costs = get_config().costs
    table = {SurfaceClass(k): float(v) for k, v in costs["surface_multiplier"].items()}
    override = _profile_settings(profile).get("surface_multiplier_override") or {}
    for key, value in override.items():
        table[SurfaceClass(key)] = float(value)
    return table


def zone_penalties(profile: str) -> dict[str, float]:
    costs = get_config().costs
    table = {k: float(v) for k, v in costs["zone_penalty_per_m"].items()}
    scale = _profile_settings(profile).get("zone_penalty_scale") or {}
    for key, factor in scale.items():
        if key in table:
            table[key] *= float(factor)
    return table


# --- Полигоны покрытий ------------------------------------------------------


CORRIDOR_HALF_WIDTH_M = 5.0
"""Ширина полосы вдоль существующей теплосети, считающейся техкоридором."""


def surface_shapes(area: AreaModel) -> list[tuple[BaseGeometry, SurfaceClass]]:
    """Полигоны покрытий.

    Порядок важен — последний нарисованный побеждает:
    землепользование → участки → тротуары → улицы → магистрали → техкоридор.
    """
    sidewalk_width = float(get_config().costs["sidewalk_width_m"])
    sidewalks: list[tuple[BaseGeometry, SurfaceClass]] = []
    minor: list[tuple[BaseGeometry, SurfaceClass]] = []
    major: list[tuple[BaseGeometry, SurfaceClass]] = []
    carriageways: list[BaseGeometry] = []

    for road in area.roads:
        half = road.width_m / 2.0
        sidewalks.append((road.geom.buffer(half + sidewalk_width), SurfaceClass.SIDEWALK))
        carriageway = road.geom.buffer(half)
        carriageways.append(carriageway)
        if road.surface_class is SurfaceClass.STREET_MAJOR:
            major.append((carriageway, SurfaceClass.STREET_MAJOR))
        else:
            minor.append((carriageway, road.surface_class))

    # Землепользование из OSM: парки, газоны, промзоны. Самый нижний слой —
    # всё остальное рисуется поверх.
    landuse = [(patch.geom, patch.surface_class) for patch in area.surfaces]

    lawns = [
        (parcel.geom, SurfaceClass.LAWN)
        for parcel in area.parcels
        if parcel.is_public
    ]

    # Существующий технический коридор: вдоль действующей теплосети уже есть
    # разрытие, коммуникационная полоса и согласованный отвод, поэтому прокладка
    # рядом дешевле. Рисуется последним, чтобы скидка не терялась под тротуаром,
    # но вычитается из проезжей части: под асфальтом коридора нет.
    corridor: list[tuple[BaseGeometry, SurfaceClass]] = []
    if area.heat.edges:
        band = unary_union(
            [edge.geom.buffer(CORRIDOR_HALF_WIDTH_M) for edge in area.heat.edges]
        )
        if carriageways:
            band = band.difference(unary_union(carriageways))
        if not band.is_empty:
            corridor.append((band, SurfaceClass.UTILITY_CORRIDOR))

    return landuse + lawns + sidewalks + minor + major + corridor


# --- Сборка -----------------------------------------------------------------


def aoi_bounds(area: AreaModel, building: Building, radius_m: float) -> tuple[float, ...]:
    """Рабочая область: здание плюс участки сети в радиусе."""
    centre = building.geom.centroid
    parts: list[BaseGeometry] = [building.geom]
    parts += [e.geom for e in area.heat.edges if e.geom.distance(centre) <= radius_m]

    xs_min = min(p.bounds[0] for p in parts)
    ys_min = min(p.bounds[1] for p in parts)
    xs_max = max(p.bounds[2] for p in parts)
    ys_max = max(p.bounds[3] for p in parts)
    return xs_min, ys_min, xs_max, ys_max


def build_cost_field(
    area: AreaModel,
    grid: Grid,
    *,
    du_mm: int,
    laying: Laying,
    target_building: Building,
    profile: str = "min_cost",
) -> CostField:
    ctx = RuleContext.from_config(laying, du_mm)
    multipliers = surface_multipliers(profile)
    penalties = zone_penalties(profile)

    pipe = pipe_rate(du_mm, laying)
    earth = earthwork_rate(du_mm)

    # --- Покрытия ---
    code_of = {surface: i for i, surface in enumerate(SURFACE_ORDER)}
    surface_code = raster.burn(
        grid,
        [(geom, float(code_of[surface])) for geom, surface in surface_shapes(area)],
        fill=float(code_of[SurfaceClass.GROUND]),
    ).astype(np.uint8)

    multiplier_lut = np.array(
        [multipliers.get(surface, 1.0) for surface in SURFACE_ORDER], dtype=np.float32
    )
    surface_multiplier = multiplier_lut[surface_code]

    # --- Рельеф ---
    #
    # Копать на склоне дороже: террасирование, крепление стенок, вывоз грунта.
    # Множитель земляных работ растёт с крутизной, а слишком крутые участки
    # для прокладки непригодны вовсе.
    terrain_cfg = get_config().costs["terrain"]
    slope = np.zeros(grid.shape, dtype=np.float32)
    steep = np.zeros(grid.shape, dtype=bool)
    terrain = getattr(area, "terrain", None)
    if terrain is not None and terrain.available:
        slope = terrain.slope_grid(grid)
        surface_multiplier = surface_multiplier * (
            1.0 + float(terrain_cfg["slope_cost_factor"]) * slope
        )
        steep = slope > float(terrain_cfg["max_slope"])

    cost = pipe + earth * surface_multiplier

    # --- Надбавки зон ---
    penalty = np.zeros(grid.shape, dtype=np.float32)

    # Полосы нормативного отступа от чужих сетей: высокая, но КОНЕЧНАЯ надбавка.
    # Пересечь поперёк можно, идти вдоль — запретительно дорого (§4.3).
    utility_shapes: list[BaseGeometry] = []
    for utility in area.utilities:
        clearance = rules.for_utility(utility, ctx)
        if clearance:
            utility_shapes.append(utility.geom.buffer(clearance.min_m))
    if utility_shapes:
        penalty += raster.burn_mask(grid, utility_shapes) * penalties["utility_offset"]

    # Проезжая часть магистралей: только закрытый способ
    closed_road_shapes = [
        road.geom.buffer(road.width_m / 2.0)
        for road in area.roads
        if road.requires_closed_crossing
    ]
    if closed_road_shapes:
        penalty += raster.burn_mask(grid, closed_road_shapes) * penalties["road_closed_method"]

    # Полоса 1,5 м от бортового камня. Тот же приём, что с чужими сетями:
    # пересечь улицу поперёк можно, идти вдоль внутри полосы — нельзя.
    kerb_clearance = rules.horizontal("KERB", ctx)
    if kerb_clearance and area.roads:
        kerb_shapes = [
            road.geom.buffer(road.width_m / 2.0 + kerb_clearance.min_m)
            for road in area.roads
        ]
        penalty += raster.burn_mask(grid, kerb_shapes) * penalties["kerb_offset"]

    # Полоса отвода железной дороги
    rail_shapes = [rail.geom.buffer(rail.corridor_width_m / 2.0) for rail in area.railways]
    if rail_shapes:
        penalty += raster.burn_mask(grid, rail_shapes) * penalties["rail_corridor"]

    # Чужие участки и ЗОУИТ
    private_shapes = [p.geom for p in area.parcels if not p.is_public and not p.is_protected_zone]
    if private_shapes:
        penalty += raster.burn_mask(grid, private_shapes) * penalties["private_parcel"]

    protected_shapes = [p.geom for p in area.parcels if p.is_protected_zone]
    if protected_shapes:
        penalty += raster.burn_mask(grid, protected_shapes) * penalties["protected_zone"]

    # Зелёные насаждения. Дерево — не абсолютное препятствие: его можно снести,
    # заплатив компенсационную стоимость. Поэтому надбавка, а не запрет; норматив
    # (2,0 м до ствола, 1,0 м до кустарника) задаёт радиус зоны.
    tree_rule = rules.horizontal("TREE", ctx)
    shrub_rule = rules.horizontal("SHRUB", ctx)
    greenery_shapes: list[BaseGeometry] = []
    for green in area.greenery:
        rule = tree_rule if green.is_tree else shrub_rule
        if rule:
            greenery_shapes.append(green.geom.buffer(rule.min_m))
    if greenery_shapes:
        penalty += raster.burn_mask(grid, greenery_shapes) * penalties["greenery_offset"]

    cost = cost + penalty

    # --- Непроходимое ---
    building_clearance = rules.horizontal("BUILDING", ctx)
    buffer_m = building_clearance.min_m if building_clearance else 2.0

    blocked_shapes: list[BaseGeometry] = []
    for other in area.buildings:
        if other.id == target_building.id:
            # Своё здание блокирует только пятном: к нему как раз и подключаемся
            blocked_shapes.append(other.geom)
        else:
            blocked_shapes.append(other.geom.buffer(buffer_m))
    blocked_shapes += [w.geom for w in area.water]
    blocked_shapes += [p.geom for p in area.parcels if p.prohibits_laying]

    blocked = raster.burn_mask(grid, blocked_shapes) | steep
    cost = np.where(blocked, np.inf, cost).astype(np.float64)

    return CostField(
        grid=grid,
        cost=cost,
        blocked=blocked,
        surface_code=surface_code,
        penalty=penalty,
        du_mm=du_mm,
        laying=laying,
        context=ctx,
        base_rub_per_m=pipe + earth,
        profile=profile,
        notes={
            "pipe_rub_per_m": round(pipe),
            "earthwork_rub_per_m": round(earth),
            "building_clearance_m": buffer_m,
            "building_clearance_rule": building_clearance.rule_id if building_clearance else None,
            "blocked_share": round(float(blocked.mean()), 4),
            "terrain": terrain is not None and terrain.available,
            "steep_share": round(float(steep.mean()), 4),
            "slope_max": round(float(slope.max()), 3) if slope.size else 0.0,
        },
    )


def add_blocked_zone(field: CostField, geometry: BaseGeometry) -> CostField:
    """Сделать зону непроходимой.

    Используется для ремонта трассы по результатам нормоконтроля: поле стоимости
    скалярно и не отличает «идти вдоль» от «пересечь», поэтому следование вдоль
    чужой сети запрещается точечно — на том участке, где оно фактически случилось.
    Блокировать коридор целиком нельзя: тогда сеть станет непересекаемой.
    """
    mask = raster.burn_mask(field.grid, [geometry])
    blocked = field.blocked | mask
    return CostField(
        grid=field.grid,
        cost=np.where(mask, np.inf, field.cost),
        blocked=blocked,
        surface_code=field.surface_code,
        penalty=field.penalty,
        du_mm=field.du_mm,
        laying=field.laying,
        context=field.context,
        base_rub_per_m=field.base_rub_per_m,
        profile=field.profile,
        notes=field.notes,
    )


def add_corridor_penalty(
    field: CostField, geometry: BaseGeometry, *, width_m: float, factor: float
) -> CostField:
    """Удорожить коридор вокруг найденной трассы — для поиска непохожих вариантов.

    Надбавка добавляется, а не умножает всю ячейку. Умножение на 3 задирало и
    стоимость самой трубы, из-за чего цель поиска для диверсифицированного
    варианта переставала выражаться в рублях вовсе, а его смета всё равно
    сравнивалась со сметой исходного варианта на Парето-фронте.
    """
    corridor: Polygon = geometry.buffer(width_m / 2.0)
    mask = raster.burn_mask(field.grid, [corridor])
    extra = (mask & ~field.blocked) * (field.base_rub_per_m * (factor - 1.0))
    cost = field.cost + extra
    return CostField(
        grid=field.grid,
        cost=cost,
        blocked=field.blocked,
        surface_code=field.surface_code,
        # Надбавка диверсификации — вес поиска, в смету она входить не должна
        penalty=field.penalty + extra.astype(np.float32),
        du_mm=field.du_mm,
        laying=field.laying,
        context=field.context,
        base_rub_per_m=field.base_rub_per_m,
        profile=field.profile,
        notes=field.notes,
    )
