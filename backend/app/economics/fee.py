"""Плата за подключение по ФЗ-190 и ПП РФ № 787.

Это связка, ради которой всё считается: плата двухставочная и зависит от
**протяжённости сети по диаметрам**, то есть оптимизатор минимизирует ровно ту
сумму, которую потом платит застройщик. Смета подрядчика (economics/estimate.py)
и плата за подключение — разные величины: первая про стоимость работ, вторая
про тариф, утверждённый регулятором.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import get_config
from ..costfield.build import _interp_by_du
from ..domain.enums import Laying


@dataclass
class FeeLine:
    title: str
    quantity: float
    unit: str
    rate_rub: float
    amount_rub: float


@dataclass
class ConnectionFee:
    """Расчёт платы за подключение."""

    method: str                     # flat | tariff | individual
    method_title: str
    load_gcal_h: float
    lines: list[FeeLine] = field(default_factory=list)
    total_rub: float = 0.0          # без НДС
    vat_rub: float = 0.0
    total_with_vat_rub: float = 0.0
    basis: str = ""
    note: str = ""
    calibrated: bool = False

    @property
    def is_estimated(self) -> bool:
        return self.method == "tariff" and not self.calibrated


def _rate_per_m(du_mm: int, laying: Laying) -> float:
    table = get_config().costs["connection_fee"]["length_rate_rub_per_m"]
    key = "channel" if laying is Laying.CHANNEL else "channelless"
    return _interp_by_du(table[key], du_mm)


def calculate(
    load_gcal_h: float,
    length_by_du: dict[int, float],
    laying: Laying,
) -> ConnectionFee:
    """Плата за подключение по подключаемой нагрузке и протяжённости сетей.

    `length_by_du` — длина трассы в метрах по каждому диаметру. Сейчас на трассу
    подбирается один диаметр, но структура сразу многодиаметровая: при подключении
    группы зданий общий коридор идёт большим Ду, а вводы — меньшими.
    """
    cfg = get_config().costs["connection_fee"]
    thresholds = cfg["thresholds_gcal_h"]
    basis = cfg["basis"]
    calibrated = bool(cfg.get("calibrated"))

    # --- До 0,1 Гкал/ч — фиксированная плата (ФЗ-190 ст. 14) ---
    if load_gcal_h <= thresholds["flat_fee_max"]:
        flat = float(cfg["flat_fee_rub"])
        return ConnectionFee(
            method="flat",
            method_title="Фиксированная плата",
            load_gcal_h=load_gcal_h,
            lines=[FeeLine("Плата за подключение (с НДС)", 1, "подключение", flat, flat)],
            total_rub=flat / (1 + cfg["vat_rate"]),
            vat_rub=flat - flat / (1 + cfg["vat_rate"]),
            total_with_vat_rub=flat,
            basis=basis,
            note=(
                f"Подключаемая нагрузка {load_gcal_h:.3f} Гкал/ч не превышает "
                f"{thresholds['flat_fee_max']} Гкал/ч — плата установлена законом."
            ),
            calibrated=True,
        )

    # --- Свыше 1,5 Гкал/ч — по индивидуальному проекту ---
    if load_gcal_h > thresholds["tariff_max"]:
        return ConnectionFee(
            method="individual",
            method_title="По индивидуальному проекту",
            load_gcal_h=load_gcal_h,
            basis=basis,
            note=(
                f"Подключаемая нагрузка {load_gcal_h:.3f} Гкал/ч превышает "
                f"{thresholds['tariff_max']} Гкал/ч — плата определяется по "
                "индивидуальному проекту и утверждается органом регулирования. "
                "Расчёт по ставкам неприменим; ниже приведена смета работ."
            ),
        )

    # --- Двухставочная плата по утверждённым ставкам ---
    lines: list[FeeLine] = []

    load_rate = float(cfg["load_rate_rub_per_gcal_h"])
    lines.append(
        FeeLine(
            title="Ставка за подключаемую тепловую нагрузку",
            quantity=round(load_gcal_h, 4),
            unit="Гкал/ч",
            rate_rub=load_rate,
            amount_rub=load_rate * load_gcal_h,
        )
    )

    laying_title = "канальная" if laying is Laying.CHANNEL else "бесканальная"
    for du_mm, length_m in sorted(length_by_du.items()):
        rate = _rate_per_m(du_mm, laying)
        lines.append(
            FeeLine(
                title=f"Ставка за протяжённость сетей, Ду{du_mm}, {laying_title} прокладка",
                quantity=round(length_m, 1),
                unit="м",
                rate_rub=rate,
                amount_rub=rate * length_m,
            )
        )

    total = sum(line.amount_rub for line in lines)
    vat = total * float(cfg["vat_rate"])

    return ConnectionFee(
        method="tariff",
        method_title="По утверждённым ставкам",
        load_gcal_h=load_gcal_h,
        lines=lines,
        total_rub=total,
        vat_rub=vat,
        total_with_vat_rub=total + vat,
        basis=basis,
        note=(
            f"Нагрузка {load_gcal_h:.3f} Гкал/ч в диапазоне "
            f"{thresholds['flat_fee_max']}–{thresholds['tariff_max']} Гкал/ч: плата "
            "двухставочная — за нагрузку и за протяжённость сетей по диаметрам "
            "и типу прокладки."
        ),
        calibrated=calibrated,
    )
