"""Подбор нормативного расстояния под конкретные условия.

Единственное место, которое интерпретирует `config/normatives.yaml`.
Используется дважды: при построении запретных зон в поле стоимости и при
нормоконтроле готовой трассы.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import get_config
from ..domain.enums import Laying, UtilityKind
from ..domain.models import Utility


@dataclass(frozen=True)
class Clearance:
    """Нормативное расстояние со ссылкой на пункт — то, что уйдёт в протокол."""

    rule_id: str
    title: str
    min_m: float
    clause: str
    note: str | None = None

    @property
    def source(self) -> str:
        return f"{get_config().normatives['meta']['source']}, {self.clause}"


@dataclass(frozen=True)
class RuleContext:
    """Условия, в которых применяются правила."""

    laying: Laying
    du_mm: int
    soil: str = "nonsubsiding"
    system: str = "closed"

    @classmethod
    def from_config(cls, laying: Laying, du_mm: int) -> RuleContext:
        site = get_config().normatives["site"]
        return cls(laying=laying, du_mm=du_mm, soil=site["soil"], system=site["system"])


def _matches(
    when: dict,
    ctx: RuleContext,
    pressure_class: str | None,
    voltage_kv: float | None,
) -> bool:
    if "laying" in when and when["laying"] != ctx.laying.value:
        return False
    if "soil" in when and when["soil"] != ctx.soil:
        return False
    if "system" in when and when["system"] != ctx.system:
        return False
    if "du_lt" in when and not ctx.du_mm < when["du_lt"]:
        return False
    if "du_ge" in when and not ctx.du_mm >= when["du_ge"]:
        return False
    if "pressure_class" in when and pressure_class != when["pressure_class"]:
        return False
    if "pressure_class_in" in when and pressure_class not in when["pressure_class_in"]:
        return False
    if "voltage_kv_max" in when and (voltage_kv is None or voltage_kv > when["voltage_kv_max"]):
        return False
    if "voltage_kv_min" in when and (voltage_kv is None or voltage_kv <= when["voltage_kv_min"]):
        return False
    return True


def horizontal(
    target: str,
    ctx: RuleContext,
    *,
    pressure_class: str | None = None,
    voltage_kv: float | None = None,
) -> Clearance | None:
    """Расстояние по горизонтали — когда трасса идёт ВДОЛЬ объекта."""
    for rule in get_config().normatives["horizontal"]:
        if rule["target"] != target:
            continue
        if not _matches(rule.get("when") or {}, ctx, pressure_class, voltage_kv):
            continue
        return Clearance(
            rule_id=rule["id"],
            title=rule["title"],
            min_m=float(rule["min_m"]),
            clause=rule["clause"],
            note=rule.get("note"),
        )
    return None


def vertical(target: str) -> Clearance | None:
    """Расстояние по вертикали — когда трасса ПЕРЕСЕКАЕТ объект."""
    for rule in get_config().normatives["vertical"]:
        if target in rule["targets"]:
            return Clearance(
                rule_id=rule["id"],
                title=rule["title"],
                min_m=float(rule["min_m"]),
                clause=rule["clause"],
                note=rule.get("note"),
            )
    return None


def for_utility(utility: Utility, ctx: RuleContext) -> Clearance | None:
    """Отступ до конкретной чужой сети с учётом её класса давления или напряжения."""
    return horizontal(
        utility.kind.value,
        ctx,
        pressure_class=utility.pressure_class,
        voltage_kv=utility.voltage_kv,
    )


def utility_clearances(ctx: RuleContext) -> dict[UtilityKind, float]:
    """Максимальный отступ по каждому классу сетей — для быстрой оценки буферов."""
    result: dict[UtilityKind, float] = {}
    for kind in UtilityKind:
        clearance = horizontal(kind.value, ctx)
        if clearance:
            result[kind] = clearance.min_m
    return result


def relaxation_ratio() -> float:
    """Доля норматива, ниже которой нарушение перестаёт быть `conditional`."""
    block = get_config().normatives.get("relaxation", {})
    return float(block.get("min_ratio", 0.5)) if block.get("enabled") else 1.0
