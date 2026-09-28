"""Оценка тепловой нагрузки здания по геометрии.

Стартовое приближение, чтобы «оживить» весь загруженный район без ручного ввода.
Проектная нагрузка, если она есть, вводится в UI и перекрывает расчёт.

Методика — docs/04-algorithm.md §2, коэффициенты — config/loads.yaml.
"""

from __future__ import annotations

from ..config import get_config
from ..domain.enums import BuildingUse
from ..domain.models import Building, HeatLoad


def classify_use(tags: dict[str, str]) -> BuildingUse:
    """Тип застройки по тегам OSM."""
    cfg = get_config().loads
    value = (tags.get("building") or "yes").lower()

    if value in cfg["osm_building_skip"]:
        return BuildingUse.UNHEATED

    mapping = cfg["osm_building_use"]
    for use, values in mapping.items():
        if value in values:
            return BuildingUse(use)

    # building=yes и прочее неопознанное: жилое, если есть жилые признаки
    if tags.get("addr:housenumber") and not tags.get("shop") and not tags.get("office"):
        return BuildingUse.RESIDENTIAL
    return BuildingUse.OTHER


def floors_of(building: Building) -> int:
    """Этажность: из тега, из высоты, иначе значение по умолчанию."""
    cfg = get_config().loads["geometry"]
    if building.floors:
        return max(1, building.floors)
    if building.height_m:
        return max(1, round(building.height_m / cfg["default_floor_height_m"]))
    return cfg["default_floors"]


def total_area_m2(building: Building) -> float:
    """Общая площадь = площадь контура × этажность × коэффициент полезной площади."""
    cfg = get_config().loads["geometry"]
    return building.footprint_m2 * floors_of(building) * cfg["usable_area_factor"]


def _specific_heating_w_m2(use: BuildingUse, floors: int, built_year: int | None) -> float:
    """Удельный расход на отопление, Вт/м², из таблицы config/loads.yaml."""
    table = get_config().loads["heating_w_per_m2"]
    rows = table.get(use.value) or table["other"]

    for row in rows:
        floors_max = row.get("floors_max")
        before = row.get("built_before")
        if floors_max is not None and floors > floors_max:
            continue
        if before is not None:
            # строка для старого фонда: год постройки известен и раньше порога
            if built_year is None or built_year >= before:
                continue
        else:
            # строка для нового фонда: год неизвестен или не раньше порога
            if built_year is not None and built_year < 2000:
                continue
        return float(row["value"])

    return float(rows[-1]["value"])


def estimate(building: Building) -> HeatLoad:
    """Расчётная тепловая нагрузка здания, Гкал/ч."""
    cfg = get_config().loads
    use = building.use

    if use is BuildingUse.UNHEATED:
        return HeatLoad(source="estimated")

    floors = floors_of(building)
    area = total_area_m2(building)
    climate = cfg["climate"]
    w_to_gcal = cfg["units"]["w_to_gcal_h"]

    # --- Отопление и вентиляция ---
    q_specific = _specific_heating_w_m2(use, floors, building.built_year)
    design_delta = climate["t_inside_c"] - climate["t_outside_design_c"]
    base_delta = climate["t_inside_c"] - climate["t_outside_base_c"]
    heating_w = q_specific * area * (design_delta / base_delta)
    heating = heating_w * w_to_gcal

    # --- ГВС ---
    dhw_cfg = cfg["dhw"]
    if use is BuildingUse.RESIDENTIAL:
        residents = area / dhw_cfg["area_per_person_m2"]
        delta_dhw = climate["t_dhw_c"] - climate["t_cold_water_winter_c"]
        # Q_ср [Гкал/ч] = a · N · Δt · 1e-6 / 24
        dhw_avg = dhw_cfg["consumption_l_per_person_day"] * residents * delta_dhw * 1e-6 / 24.0
        dhw_max = dhw_avg * dhw_cfg["hourly_peak_factor"]
    else:
        dhw_max = heating * dhw_cfg["nonresidential_share_of_heating"]

    return HeatLoad(heating=heating, ventilation=0.0, dhw_max=dhw_max, source="estimated")


def estimate_all(buildings: list[Building]) -> None:
    """Проставить нагрузку всем зданиям, у которых её ещё нет."""
    for building in buildings:
        if building.load is None or building.load.source == "estimated":
            building.load = estimate(building)
