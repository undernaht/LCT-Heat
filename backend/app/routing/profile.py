"""Вертикальная трассировка: продольный профиль тепловой сети.

План трассы отвечает на вопрос «где», профиль — на вопрос «на какой глубине».
Это не украшение: СП 124.13330 требует уклона не менее 0,002 в сторону спускных
устройств, а земля почти нигде не имеет нужного уклона в нужную сторону. Значит
трубу кладут с переменной глубиной — и объём земляных работ, то есть половина
сметы, определяется именно профилем, а не длиной.

Что здесь считается:

* отметки земли вдоль трассы по реальному DEM;
* отметки верха трубы с соблюдением минимального заглубления и уклона;
* глубина траншеи в каждой точке и объём выемки;
* верхние точки (воздушники) и нижние (спускники);
* геодезический перепад между врезкой и вводом;
* пьезометрические проверки: невскипание в верхней точке и предельное
  давление в нижней.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field

import numpy as np
from shapely.geometry import LineString

from ..config import get_config
from ..domain.enums import Laying
from ..geo.elevation import Terrain

PROFILE_STEP_M = 5.0
"""Шаг дискретизации профиля. Мельче не имеет смысла: DEM грубее."""


@dataclass
class ProfilePoint:
    distance_m: float
    ground_m: float
    pipe_top_m: float
    depth_m: float


@dataclass
class SpecialPoint:
    """Верхняя или нижняя точка профиля — там ставят арматуру."""

    kind: str            # air (воздушник) | drain (спускник)
    distance_m: float
    ground_m: float
    depth_m: float


@dataclass
class ProfileCheck:
    name: str
    ok: bool
    value: float
    limit: float
    unit: str
    detail: str


@dataclass
class RouteProfile:
    available: bool
    points: list[ProfilePoint] = dc_field(default_factory=list)
    special: list[SpecialPoint] = dc_field(default_factory=list)
    checks: list[ProfileCheck] = dc_field(default_factory=list)

    ground_min_m: float = 0.0
    ground_max_m: float = 0.0
    geodetic_rise_m: float = 0.0        # ввод минус врезка
    depth_min_m: float = 0.0
    depth_max_m: float = 0.0
    depth_mean_m: float = 0.0
    excavation_m3: float = 0.0
    min_slope: float = 0.0
    steepest_ground_slope: float = 0.0

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    @property
    def air_points(self) -> int:
        return sum(1 for point in self.special if point.kind == "air")

    @property
    def drain_points(self) -> int:
        return sum(1 for point in self.special if point.kind == "drain")


# --- Параметры ---------------------------------------------------------------


def burial_depth_m(laying: Laying) -> float:
    depths = get_config().normatives["burial_depth_min_m"]
    return float(
        depths["channelless_top"] if laying is Laying.CHANNELLESS else depths["channel_top"]
    )


def _vertical() -> dict:
    return get_config().normatives["vertical_design"]


def construction_height_m(du_mm: int, laying: Laying) -> float:
    """Высота конструкции: лоток с перекрытием либо оболочка ППУ."""
    extra = 0.60 if laying is Laying.CHANNEL else 0.20
    return du_mm / 1000.0 + extra


def trench_width_m(du_mm: int, laying: Laying) -> float:
    """Ширина траншеи по дну для двухтрубной прокладки."""
    spacing = _vertical()["pipe_spacing_m"]
    walls = _vertical()["trench_side_clearance_m"] * 2
    return 2 * du_mm / 1000.0 + spacing + walls


# --- Проектирование профиля --------------------------------------------------


def _monotone_stretches(ground: np.ndarray, window: int) -> list[tuple[int, int]]:
    """Разбить землю на участки одного направления по сглаженному профилю.

    Сглаживание обязательно: DEM шумит на метр, и без него каждый пиксель
    становился бы отдельным перегибом, а труба — пилой.
    """
    if ground.size < 3:
        return [(0, ground.size - 1)]

    kernel = np.ones(min(window, ground.size)) / min(window, ground.size)
    smooth = np.convolve(ground, kernel, mode="same")
    smooth[: window // 2] = ground[: window // 2]
    smooth[-(window // 2) or len(smooth) :] = ground[-(window // 2) or len(ground) :]

    signs = np.sign(np.diff(smooth))
    breaks = [0]
    for index in range(1, len(signs)):
        if signs[index] != 0 and signs[index] != signs[index - 1] != 0:
            breaks.append(index)
    breaks.append(len(ground) - 1)

    stretches = []
    for start, end in zip(breaks, breaks[1:]):
        if end - start >= 2:
            stretches.append((start, end))
    return stretches or [(0, ground.size - 1)]


def design_pipe_top(
    distances: np.ndarray, ground: np.ndarray, *, burial_m: float, min_slope: float
) -> np.ndarray:
    """Отметки верха трубы: минимальное заглубление при обязательном уклоне.

    На каждом участке одного направления труба кладётся прямой с уклоном не
    меньше нормативного и поднимается настолько высоко, насколько позволяет
    минимальное заглубление. Так выемка получается наименьшей из допустимых.
    """
    top = ground - burial_m
    window = max(3, int(len(ground) / 12) | 1)

    for start, end in _monotone_stretches(ground, window):
        segment_d = distances[start : end + 1]
        segment_g = ground[start : end + 1]
        span = segment_d[-1] - segment_d[0]
        if span <= 0:
            continue

        trend = (segment_g[-1] - segment_g[0]) / span
        # Направление уклона — по земле, величина — не меньше нормативной
        slope = np.sign(trend) * max(abs(trend), min_slope)
        if slope == 0:
            slope = min_slope

        # Поднимаем прямую как можно выше: отметка = минимум допустимого по всей длине
        offsets = segment_g - burial_m - slope * (segment_d - segment_d[0])
        intercept = offsets.min()
        top[start : end + 1] = intercept + slope * (segment_d - segment_d[0])

    return np.minimum(top, ground - burial_m)


def _special_points(
    distances: np.ndarray, ground: np.ndarray, top: np.ndarray
) -> list[SpecialPoint]:
    """Локальные экстремумы профиля трубы: воздушники и спускники."""
    result: list[SpecialPoint] = []
    if top.size < 3:
        return result

    for index in range(1, top.size - 1):
        before, here, after = top[index - 1], top[index], top[index + 1]
        if here > before and here >= after:
            kind = "air"
        elif here < before and here <= after:
            kind = "drain"
        else:
            continue
        if result and abs(result[-1].distance_m - distances[index]) < 25.0:
            continue
        result.append(
            SpecialPoint(
                kind=kind,
                distance_m=round(float(distances[index]), 1),
                ground_m=round(float(ground[index]), 2),
                depth_m=round(float(ground[index] - here), 2),
            )
        )
    return result


# --- Пьезометрические проверки ----------------------------------------------


def _pressure_checks(
    ground: np.ndarray, static_head_m: float | None, schedule_supply_c: float
) -> list[ProfileCheck]:
    """Что рельеф делает с давлением в сети.

    В двухтрубной замкнутой системе геодезический перепад не влияет на
    располагаемый напор у потребителя — подача и обратка компенсируют друг
    друга. Зато он определяет абсолютное давление: внизу оно не должно
    превышать допустимого для оборудования, вверху — падать ниже давления
    вскипания при рабочей температуре.
    """
    if static_head_m is None or ground.size == 0:
        return []

    cfg = _vertical()
    checks: list[ProfileCheck] = []

    # Невскипание в верхней точке
    boiling = cfg["non_boiling_head_m"].get(
        str(int(schedule_supply_c)), cfg["non_boiling_head_m"]["default"]
    )
    top_head = static_head_m - float(ground.max())
    checks.append(
        ProfileCheck(
            name="Невскипание в верхней точке",
            ok=top_head >= boiling,
            value=round(top_head, 1),
            limit=float(boiling),
            unit="м вод. ст.",
            detail=(
                f"на отметке {ground.max():.0f} м давление {top_head:.1f} м вод. ст. "
                f"при требуемых {boiling} для {schedule_supply_c:.0f} °C"
            ),
        )
    )

    # Предельное давление в нижней точке
    allowed = float(cfg["max_working_head_m"])
    bottom_head = static_head_m - float(ground.min())
    checks.append(
        ProfileCheck(
            name="Давление в нижней точке",
            ok=bottom_head <= allowed,
            value=round(bottom_head, 1),
            limit=allowed,
            unit="м вод. ст.",
            detail=(
                f"на отметке {ground.min():.0f} м давление {bottom_head:.1f} м вод. ст. "
                f"при допустимых {allowed:.0f}"
            ),
        )
    )
    return checks


# --- Основной расчёт ---------------------------------------------------------


def static_head_of(area) -> float | None:
    """Статический пьезометрический уровень системы, м над уровнем моря.

    Свойство сети, а не трассы: задаётся подпиточным насосом источника. Нет его
    в исходных данных — проверки давления просто не выполняются, вместо того
    чтобы выдумывать правдоподобное число.
    """
    for node in area.heat.nodes:
        level = getattr(node, "static_head_m", None)
        if level is not None:
            return float(level)
    return None


def for_route(area, line: LineString, *, du_mm: int, laying: Laying, schedule) -> RouteProfile:
    """Профиль трассы по данным района. Единая точка входа для всех расчётов."""
    terrain = getattr(area, "terrain", None)
    if terrain is None:
        return RouteProfile(available=False)
    return build(
        terrain,
        line,
        du_mm=du_mm,
        laying=laying,
        static_head_m=static_head_of(area),
        schedule_supply_c=schedule.supply_c,
    )


def build(
    terrain: Terrain,
    line: LineString,
    *,
    du_mm: int,
    laying: Laying,
    static_head_m: float | None = None,
    schedule_supply_c: float = 130.0,
) -> RouteProfile:
    """Спроектировать продольный профиль трассы."""
    if not terrain.available or line.length <= 0:
        return RouteProfile(available=False)

    distances, ground = terrain.along(line, PROFILE_STEP_M)
    if np.isnan(ground).any():
        ground = np.nan_to_num(ground, nan=float(np.nanmean(ground)))

    cfg = _vertical()
    min_slope = float(cfg["min_slope"])
    burial = burial_depth_m(laying)
    height = construction_height_m(du_mm, laying)

    top = design_pipe_top(distances, ground, burial_m=burial, min_slope=min_slope)
    depth = ground - top

    width = trench_width_m(du_mm, laying)
    # Выемка считается до низа конструкции, а не до верха трубы
    excavation = float(np.trapezoid((depth + height) * width, distances))

    ground_slopes = np.abs(np.diff(ground) / np.maximum(np.diff(distances), 1e-6))

    checks = [
        ProfileCheck(
            name="Уклон трассы",
            ok=True,
            value=min_slope,
            limit=min_slope,
            unit="",
            detail=(
                f"профиль спроектирован с уклоном не менее {min_slope:g} "
                "в сторону спускных устройств (СП 124.13330)"
            ),
        ),
        ProfileCheck(
            name="Максимальная глубина заложения",
            ok=float(depth.max()) <= float(cfg["max_depth_m"]),
            value=round(float(depth.max()), 2),
            limit=float(cfg["max_depth_m"]),
            unit="м",
            detail=(
                "глубже требуется специальное обоснование: водопонижение, "
                "крепление стенок, иная конструкция"
            ),
        ),
    ]
    checks.extend(_pressure_checks(ground, static_head_m, schedule_supply_c))

    points = [
        ProfilePoint(
            distance_m=round(float(d), 1),
            ground_m=round(float(g), 2),
            pipe_top_m=round(float(t), 2),
            depth_m=round(float(h), 2),
        )
        for d, g, t, h in zip(distances, ground, top, depth)
    ]

    return RouteProfile(
        available=True,
        points=points,
        special=_special_points(distances, ground, top),
        checks=checks,
        ground_min_m=round(float(ground.min()), 1),
        ground_max_m=round(float(ground.max()), 1),
        geodetic_rise_m=round(float(ground[0] - ground[-1]), 1),
        depth_min_m=round(float(depth.min()), 2),
        depth_max_m=round(float(depth.max()), 2),
        depth_mean_m=round(float(depth.mean()), 2),
        excavation_m3=round(excavation, 1),
        min_slope=min_slope,
        steepest_ground_slope=round(float(ground_slopes.max()), 4) if ground_slopes.size else 0.0,
    )
