"""Несколько содержательно разных вариантов трассы.

Три ответа получаются не тремя запусками одного алгоритма, а тремя РАЗНЫМИ
профилями стоимости плюс диверсификацией: после найденной трассы коридор вокруг
неё дорожает, и следующий поиск вынужден идти иначе (docs/04-algorithm.md §10).
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from ..costfield.build import aoi_bounds, add_corridor_penalty, build_cost_field
from ..domain.enums import ComplianceStatus, Laying
from ..domain.models import AreaModel, Building
from ..geo.raster import Grid
from ..hydraulics import diameters
from .solver import (
    AOI_MARGIN_M,
    DEFAULT_RADIUS_M,
    DEFAULT_RESOLUTION_M,
    RouteSolution,
    nearest_edge,
    solve,
)

DEFAULT_PROFILES = ("min_cost", "min_approvals", "reliable")
DIVERSIFY_WIDTH_M = 30.0
DIVERSIFY_FACTOR = 3.0
SIMILARITY_M = 25.0        # трассы ближе этого считаем одной и той же


@dataclass
class VariantSet:
    building_id: str
    variants: list[RouteSolution] = dc_field(default_factory=list)
    pareto_ids: list[str] = dc_field(default_factory=list)

    def best_by(self, key: str) -> RouteSolution | None:
        if not self.variants:
            return None
        if key == "cost":
            return min(self.variants, key=lambda v: v.cost.total_rub)
        if key == "risk":
            return min(self.variants, key=lambda v: v.risk.score)
        if key == "length":
            return min(self.variants, key=lambda v: v.length_m)
        return None

    def variant(self, variant_id: str) -> RouteSolution | None:
        return next((v for v in self.variants if variant_id_of(v) == variant_id), None)


def variant_id_of(solution: RouteSolution) -> str:
    return solution.field_notes.get("variant_id", solution.profile)


def _is_similar(a: RouteSolution, b: RouteSolution) -> bool:
    """Похожи ли трассы — чтобы не показывать три параллельные линии."""
    return a.geometry.hausdorff_distance(b.geometry) < SIMILARITY_M


MEANINGFUL_COST_DIFFERENCE = 0.03
"""Разница в стоимости меньше 3 % считается шумом расчёта, а не преимуществом."""


def pareto_front(variants: list[RouteSolution]) -> list[str]:
    """Недоминируемые по {стоимость, риск согласований}.

    Варианты, не прошедшие нормоконтроль, на фронт не попадают: недопустимая
    трасса — не компромисс, а брак, и предлагать её как «зато дешевле» нельзя.

    Тепловые потери убраны из осей сравнения. Они почти линейны по длине и уже
    входят в стоимость отдельной статьёй, поэтому третьей осью работали как
    лазейка: вариант дороже на 18 млн ₽ при равном риске попадал на фронт за
    счёт 0,7 % разницы в потерях. Осей осталось две, и обе — то, между чем
    заказчик действительно выбирает.

    Разница в стоимости меньше 3 % преимуществом не считается: точность самой
    сметы заведомо ниже.
    """
    admissible = [v for v in variants if v.compliance.status is not ComplianceStatus.FAIL]
    front: list[str] = []

    for candidate in admissible:
        dominated = False
        for other in admissible:
            if other is candidate:
                continue
            cheaper_or_equal = (
                other.cost.total_rub
                <= candidate.cost.total_rub * (1.0 + MEANINGFUL_COST_DIFFERENCE)
            )
            safer_or_equal = other.risk.score <= candidate.risk.score
            strictly_better = (
                other.cost.total_rub
                < candidate.cost.total_rub * (1.0 - MEANINGFUL_COST_DIFFERENCE)
                or other.risk.score < candidate.risk.score
            )
            if cheaper_or_equal and safer_or_equal and strictly_better:
                dominated = True
                break
        if not dominated:
            front.append(variant_id_of(candidate))

    return front


def solve_variants(
    area: AreaModel,
    building: Building,
    *,
    profiles: tuple[str, ...] = DEFAULT_PROFILES,
    laying: Laying = Laying.CHANNEL,
    resolution: float = DEFAULT_RESOLUTION_M,
    radius_m: float = DEFAULT_RADIUS_M,
    diversify: int = 1,
) -> VariantSet:
    """Посчитать варианты по всем профилям с диверсификацией внутри каждого."""
    result = VariantSet(building_id=building.id)
    if building.load is None or building.load.total <= 0:
        return result

    q = building.load.total
    edge = nearest_edge(area, building.geom)
    schedule = edge.schedule if edge else None
    choice = diameters.select_diameter(
        q, schedule or building_default_schedule(), is_branch=True
    )
    if not choice.selected:
        return result

    # Сетка одна на все профили — растеризация не повторяется (docs/02 §9)
    grid = Grid.covering(
        aoi_bounds(area, building, radius_m), resolution, margin=AOI_MARGIN_M
    )

    for profile in profiles:
        field = build_cost_field(
            area,
            grid,
            du_mm=choice.selected.du_mm,
            laying=laying,
            target_building=building,
            profile=profile,
        )

        for attempt in range(max(1, diversify)):
            solution = solve(
                area, building, profile=profile, laying=laying,
                resolution=resolution, radius_m=radius_m, prepared_field=field,
            )
            if solution is None:
                break

            variant_id = profile if attempt == 0 else f"{profile}+{attempt}"
            solution.field_notes["variant_id"] = variant_id

            if any(_is_similar(solution, existing) for existing in result.variants):
                break
            result.variants.append(solution)

            if attempt + 1 < diversify:
                field = add_corridor_penalty(
                    field, solution.geometry,
                    width_m=DIVERSIFY_WIDTH_M, factor=DIVERSIFY_FACTOR,
                )

    result.variants.sort(key=lambda v: v.cost.total_rub)
    result.pareto_ids = pareto_front(result.variants)
    return result


def building_default_schedule():
    from ..domain.models import TempSchedule

    return TempSchedule(130, 70)
