"""Тесты оценки тепловой нагрузки по геометрии.

Ключевой тест — сквозной: геометрия здания → нагрузка → диаметр. Он связывает
config/loads.yaml, config/normatives.yaml и docs/04-algorithm.md §3.4 в одну цепь,
поэтому рассинхронизация документа и кода ломает сборку.
"""

from __future__ import annotations

import pytest
from shapely.geometry import Polygon

from app.domain.enums import BuildingUse
from app.domain.models import Building, TempSchedule
from app.hydraulics import diameters
from app.loads import estimate as loads


def square(area_m2: float) -> Polygon:
    side = area_m2**0.5
    return Polygon([(0, 0), (side, 0), (side, side), (0, side)])


def reference_building() -> Building:
    """Перспективное здание из docs/04-algorithm.md §3.4.

    9-этажный жилой дом нового строительства, контур 900 м².
    """
    return Building(
        id="ref",
        geom=square(900.0),
        use=BuildingUse.RESIDENTIAL,
        floors=9,
        built_year=2026,
        is_perspective=True,
    )


# --- Классификация ---------------------------------------------------------


def test_classify_residential():
    assert loads.classify_use({"building": "apartments"}) is BuildingUse.RESIDENTIAL


def test_classify_unheated():
    assert loads.classify_use({"building": "garage"}) is BuildingUse.UNHEATED


def test_classify_public():
    assert loads.classify_use({"building": "school"}) is BuildingUse.PUBLIC


def test_classify_yes_with_address_is_residential():
    """building=yes с адресом — почти всегда жильё; так размечена половина ОSM."""
    assert loads.classify_use({"building": "yes", "addr:housenumber": "14"}) is BuildingUse.RESIDENTIAL


# --- Площадь и этажность ---------------------------------------------------


def test_total_area_uses_floors_and_factor():
    building = reference_building()
    assert loads.total_area_m2(building) == pytest.approx(900 * 9 * 0.85)


def test_floors_derived_from_height():
    building = Building(id="h", geom=square(400.0), use=BuildingUse.RESIDENTIAL, height_m=15.0)
    assert loads.floors_of(building) == 5


def test_unheated_building_has_zero_load():
    building = Building(id="g", geom=square(60.0), use=BuildingUse.UNHEATED, floors=1)
    assert loads.estimate(building).total == 0.0


# --- Старый и новый фонд ---------------------------------------------------


def test_new_construction_is_more_efficient_than_old_stock():
    """Год постройки должен влиять: новое жильё экономичнее старого фонда."""
    new = reference_building()
    old = reference_building()
    old.built_year = 1980

    assert loads.estimate(new).heating < loads.estimate(old).heating


# --- Сквозной пример из docs/04-algorithm.md §3.4 ---------------------------


def test_reference_load_matches_docs():
    load = loads.estimate(reference_building())

    assert load.heating == pytest.approx(0.343, abs=0.005)
    assert load.dhw_max == pytest.approx(0.120, abs=0.005)
    assert load.total == pytest.approx(0.464, abs=0.005)


def test_reference_end_to_end_geometry_to_diameter():
    """Геометрия → нагрузка → расход → диаметр → потери напора."""
    load = loads.estimate(reference_building())
    result = diameters.check_route(load.total, TempSchedule(130, 70), 214.0)

    assert result is not None
    assert result.du_mm == 80
    assert result.g_t_h == pytest.approx(7.73, abs=0.05)
    assert result.v_m_s == pytest.approx(0.45, abs=0.02)
    assert result.r_pa_m == pytest.approx(37, abs=2)
    assert result.dp_bar == pytest.approx(0.20, abs=0.02)
    assert result.head_m == pytest.approx(2.2, abs=0.2)


def test_reference_rejects_du65_by_specific_loss():
    """Ду65 не проходит по удельным потерям, но проходит по скорости."""
    load = loads.estimate(reference_building())
    choice = diameters.select_diameter(load.total, TempSchedule(130, 70), is_branch=True)

    rejected = {state.du_mm: (state, reason) for state, reason in choice.rejected}
    assert 65 in rejected
    state, reason = rejected[65]
    assert state.r_pa_m == pytest.approx(109, abs=3)
    assert state.v_m_s < 3.5
    assert "удельные потери" in reason
