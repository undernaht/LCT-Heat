"""Вертикальная трассировка: продольный профиль и его нормативные свойства.

Проверяется не совпадение с эталонными числами (они поедут при любой
перекалибровке), а сами требования: минимальный уклон соблюдён, заглубление не
меньше нормативного, объём выемки растёт вместе с рельефом, а пьезометрические
проверки срабатывают на неподходящем статическом уровне.
"""

from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import LineString

from app.config import get_config
from app.domain.enums import Laying
from app.economics import estimate
from app.geo.elevation import Terrain
from app.routing import profile as profile_module

STEP = profile_module.PROFILE_STEP_M


def flat_terrain(height: float = 150.0, size: int = 60, pixel: float = 5.0) -> Terrain:
    """Ровная площадка. Начало растра — в (0, size*pixel), как у настоящего GeoTIFF."""
    from affine import Affine

    return Terrain(
        values=np.full((size, size), height, dtype=np.float32),
        transform=Affine(pixel, 0.0, 0.0, 0.0, -pixel, size * pixel),
        raster_crs="EPSG:32637",
        area_crs="EPSG:32637",
        pixel_m=pixel,
    )


def sloped_terrain(drop_per_m: float, size: int = 60, pixel: float = 5.0) -> Terrain:
    """Склон, падающий на восток."""
    terrain = flat_terrain(size=size, pixel=pixel)
    cols = np.arange(size) * pixel
    terrain.values = (150.0 + cols * drop_per_m).astype(np.float32)[None, :].repeat(size, axis=0)
    return terrain


def bumpy_terrain(amplitude: float, size: int = 60, pixel: float = 5.0) -> Terrain:
    """Волнистый рельеф заданной амплитуды — источник перегибов профиля."""
    terrain = flat_terrain(size=size, pixel=pixel)
    cols = np.arange(size) * pixel
    wave = 150.0 + amplitude * np.sin(cols / 40.0)
    terrain.values = wave.astype(np.float32)[None, :].repeat(size, axis=0)
    return terrain


def line_across(length: float = 200.0, y: float = 150.0) -> LineString:
    return LineString([(10.0, y), (10.0 + length, y)])


# --- Геометрия профиля -------------------------------------------------------


def test_no_terrain_gives_unavailable_profile():
    from app.geo import elevation

    result = profile_module.build(
        elevation.EMPTY, line_across(), du_mm=100, laying=Laying.CHANNEL
    )
    assert result.available is False
    assert result.points == []


def test_minimum_burial_is_never_violated():
    result = profile_module.build(
        bumpy_terrain(4.0), line_across(), du_mm=100, laying=Laying.CHANNEL
    )
    required = profile_module.burial_depth_m(Laying.CHANNEL)
    assert result.available
    # Округление до сантиметра в выдаче допускаем, но не отход от нормы
    assert result.depth_min_m >= required - 0.01
    assert all(point.depth_m >= required - 0.01 for point in result.points)


def test_minimum_slope_is_held_on_every_segment():
    result = profile_module.build(
        bumpy_terrain(4.0), line_across(), du_mm=100, laying=Laying.CHANNEL
    )
    tops = np.array([p.pipe_top_m for p in result.points])
    distances = np.array([p.distance_m for p in result.points])
    slopes = np.abs(np.diff(tops) / np.diff(distances))

    # Горизонтальных участков быть не должно: вода не уйдёт к спускнику.
    # Допуск — на округление отметок до сантиметра при шаге в 5 м.
    tolerance = 0.01 / STEP
    assert (slopes >= result.min_slope - tolerance).all()


def test_flat_ground_still_gets_a_slope():
    """Самый жёсткий случай: земля ровная, а уклон обязан быть."""
    result = profile_module.build(
        flat_terrain(), line_across(), du_mm=100, laying=Laying.CHANNEL
    )
    tops = np.array([p.pipe_top_m for p in result.points])
    assert result.available
    assert not np.allclose(tops, tops[0]), "труба уложена горизонтально"


def test_channelless_laying_is_buried_deeper_than_channel():
    line = line_across()
    channel = profile_module.build(
        bumpy_terrain(3.0), line, du_mm=100, laying=Laying.CHANNEL
    )
    channelless = profile_module.build(
        bumpy_terrain(3.0), line, du_mm=100, laying=Laying.CHANNELLESS
    )
    assert channelless.depth_min_m >= channel.depth_min_m


def test_geodetic_rise_follows_the_slope():
    """Трасса вниз по склону: ввод ниже врезки, перепад положительный по модулю."""
    result = profile_module.build(
        sloped_terrain(-0.02), line_across(), du_mm=100, laying=Laying.CHANNEL
    )
    assert result.geodetic_rise_m == pytest.approx(0.02 * 200.0, abs=1.5)


# --- Особые точки ------------------------------------------------------------


def test_bumpy_relief_produces_air_and_drain_points():
    result = profile_module.build(
        bumpy_terrain(6.0, size=120), LineString([(10.0, 300.0), (560.0, 300.0)]),
        du_mm=100, laying=Laying.CHANNEL,
    )
    assert result.air_points >= 1, "на холме нет воздушника"
    assert result.drain_points >= 1, "в низине нет спускника"


def test_flat_ground_needs_no_arrangement():
    result = profile_module.build(
        flat_terrain(), line_across(), du_mm=100, laying=Laying.CHANNEL
    )
    # Один непрерывный уклон — экстремумов внутри трассы быть не должно
    assert result.air_points + result.drain_points == 0


# --- Земляные работы ---------------------------------------------------------


def test_excavation_grows_with_relief():
    line = line_across()
    calm = profile_module.build(flat_terrain(), line, du_mm=100, laying=Laying.CHANNEL)
    rough = profile_module.build(bumpy_terrain(8.0), line, du_mm=100, laying=Laying.CHANNEL)
    assert rough.excavation_m3 > calm.excavation_m3


def test_excavation_extra_is_zero_without_profile():
    assert estimate.excavation_extra(None, 100, Laying.CHANNEL, 200.0) == 0.0


def test_excavation_extra_charged_only_above_the_minimum_trench():
    """Доплата берётся за грунт сверх минимальной траншеи — и только за него.

    На ровной земле доплата не нулевая: обязательный уклон 0,002 сам по себе
    углубляет трубу к концу трассы. Но она должна быть заметно меньше, чем на
    пересечённом рельефе, и сопоставима с объёмом клина от уклона.
    """
    line = line_across()
    calm = profile_module.build(flat_terrain(), line, du_mm=100, laying=Laying.CHANNEL)
    rough = profile_module.build(bumpy_terrain(8.0), line, du_mm=100, laying=Laying.CHANNEL)

    calm_extra = estimate.excavation_extra(calm, 100, Laying.CHANNEL, line.length)
    rough_extra = estimate.excavation_extra(rough, 100, Laying.CHANNEL, line.length)

    # Клин от уклона: средняя добавка глубины — половина полного перепада
    wedge_m3 = (
        0.5 * calm.min_slope * line.length
        * profile_module.trench_width_m(100, Laying.CHANNEL)
        * line.length
    )
    rate = get_config().costs["terrain"]["excavation_extra_rub_per_m3"]
    assert calm_extra == pytest.approx(wedge_m3 * rate, rel=0.15)
    assert rough_extra > 2 * calm_extra


def test_excavation_extra_is_a_visible_line_in_the_estimate():
    """Доплата не должна раствориться в прокладке: у неё своя строка."""
    breakdown = estimate.CostBreakdown(
        total_rub=1_134_400.0, laying_rub=1_000_000.0, crossings_rub=0.0, tap_rub=0.0,
        chambers_rub=0.0, reconstruction_rub=0.0, heat_loss_rub=0.0,
        excavation_extra_rub=134_400.0,
    )
    rows = dict(breakdown.as_rows())
    assert rows["Дополнительная выемка из-за рельефа"] == 134_400.0
    assert sum(rows.values()) == pytest.approx(breakdown.total_rub)


# --- Пьезометрия -------------------------------------------------------------


def test_pressure_checks_absent_without_static_level():
    result = profile_module.build(
        bumpy_terrain(4.0), line_across(), du_mm=100, laying=Laying.CHANNEL
    )
    names = {check.name for check in result.checks}
    assert "Невскипание в верхней точке" not in names


def test_low_static_level_fails_the_boiling_check():
    """Статический уровень чуть выше земли — вверху сеть вскипит."""
    result = profile_module.build(
        bumpy_terrain(4.0), line_across(), du_mm=100, laying=Laying.CHANNEL,
        static_head_m=152.0, schedule_supply_c=130.0,
    )
    check = next(c for c in result.checks if c.name == "Невскипание в верхней точке")
    assert not check.ok
    assert result.ok is False


def test_high_static_level_fails_the_pressure_check():
    result = profile_module.build(
        bumpy_terrain(4.0), line_across(), du_mm=100, laying=Laying.CHANNEL,
        static_head_m=400.0, schedule_supply_c=130.0,
    )
    check = next(c for c in result.checks if c.name == "Давление в нижней точке")
    assert not check.ok


def test_reasonable_static_level_passes_both_checks():
    result = profile_module.build(
        bumpy_terrain(4.0), line_across(), du_mm=100, laying=Laying.CHANNEL,
        static_head_m=175.0, schedule_supply_c=130.0,
    )
    assert result.ok, [c.detail for c in result.checks if not c.ok]


def test_boiling_limit_depends_on_the_schedule():
    """При 150 °C требуется больший запас, чем при 95 °C."""
    kwargs = dict(du_mm=100, laying=Laying.CHANNEL, static_head_m=162.0)
    hot = profile_module.build(
        bumpy_terrain(4.0), line_across(), schedule_supply_c=150.0, **kwargs
    )
    mild = profile_module.build(
        bumpy_terrain(4.0), line_across(), schedule_supply_c=95.0, **kwargs
    )
    hot_check = next(c for c in hot.checks if c.name == "Невскипание в верхней точке")
    mild_check = next(c for c in mild.checks if c.name == "Невскипание в верхней точке")
    assert hot_check.limit > mild_check.limit
    assert mild_check.ok and not hot_check.ok
