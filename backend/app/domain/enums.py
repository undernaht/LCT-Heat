"""Перечисления доменной модели."""

from __future__ import annotations

from enum import StrEnum


class Laying(StrEnum):
    """Способ прокладки. Определяет нормативный буфер и стоимость."""

    CHANNEL = "channel"            # канальная, в лотках
    CHANNELLESS = "channelless"    # бесканальная, ППУ
    OVERHEAD = "overhead"          # надземная, на опорах


class BuildingUse(StrEnum):
    RESIDENTIAL = "residential"
    PUBLIC = "public"
    INDUSTRIAL = "industrial"
    OTHER = "other"
    UNHEATED = "unheated"          # гаражи, навесы — нагрузка не считается


class UtilityKind(StrEnum):
    """Классы чужих сетей. Совпадают с `target` в normatives.yaml."""

    WATER = "WATER"
    SEWER = "SEWER"
    STORM = "STORM"
    GAS = "GAS"
    POWER = "POWER"
    COMM = "COMM"


class HeatNodeKind(StrEnum):
    SOURCE = "source"              # ТЭЦ, котельная
    CHAMBER = "chamber"            # тепловая камера
    CTP = "ctp"                    # центральный тепловой пункт
    JUNCTION = "junction"          # узел ветвления
    CONSUMER = "consumer"          # ввод в здание (ИТП)


class SurfaceClass(StrEnum):
    """Классы покрытия. Совпадают с ключами `surface_multiplier` в costs.yaml."""

    GROUND = "ground"
    LAWN = "lawn"
    SIDEWALK = "sidewalk"
    YARD = "yard"
    STREET_LOCAL = "street_local"
    STREET_MAJOR = "street_major"
    UTILITY_CORRIDOR = "utility_corridor"


class ComplianceStatus(StrEnum):
    PASS = "pass"
    CONDITIONAL = "conditional"    # можно, но нужны защитные мероприятия
    FAIL = "fail"
