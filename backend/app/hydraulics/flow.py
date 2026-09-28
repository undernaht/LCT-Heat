"""Гидравлика тепловой сети — чистые функции.

Ни геометрии, ни конфигов, ни БД: на входе числа, на выходе числа.
Единственный модуль, который покрыт тестами по-настоящему.

Формулы и обоснование — docs/04-algorithm.md §3.
"""

from __future__ import annotations

import math

G_ACCEL = 9.81          # м/с²
PA_PER_BAR = 100_000.0


def mass_flow_t_h(q_gcal_h: float, delta_t_c: float) -> float:
    """Расчётный расход теплоносителя, т/ч.

    G = 1000 · Q / Δt

    Вывод: 1 Гкал/ч = 1e6 ккал/ч, теплоёмкость воды 1 ккал/(кг·°C),
    значит G [кг/ч] = Q·1e6 / Δt, то есть G [т/ч] = 1000·Q / Δt.
    """
    if delta_t_c <= 0:
        raise ValueError("Δt должно быть положительным")
    return 1000.0 * q_gcal_h / delta_t_c


def velocity_m_s(g_t_h: float, d_m: float, rho_kg_m3: float = 960.0) -> float:
    """Скорость воды в трубопроводе, м/с."""
    if d_m <= 0:
        raise ValueError("диаметр должен быть положительным")
    area_m2 = math.pi * d_m**2 / 4.0
    mass_flow_kg_s = g_t_h * 1000.0 / 3600.0
    return mass_flow_kg_s / (rho_kg_m3 * area_m2)


def friction_factor(d_m: float, roughness_m: float = 0.0005) -> float:
    """Коэффициент гидравлического трения по формуле Шифринсона.

    λ = 0,11 · (kэ/d)^0,25 — квадратичная область сопротивления, в которой
    тепловые сети и работают. kэ = 0,0005 м для водяных сетей (СП 124.13330, п. 8.5).
    """
    if d_m <= 0:
        raise ValueError("диаметр должен быть положительным")
    return 0.11 * (roughness_m / d_m) ** 0.25


def specific_pressure_loss_pa_m(
    v_m_s: float, d_m: float, rho_kg_m3: float = 960.0, roughness_m: float = 0.0005
) -> float:
    """Удельные потери давления на трение, Па/м (Дарси — Вейсбах).

    R = λ · ρ · v² / (2d)
    """
    lam = friction_factor(d_m, roughness_m)
    return lam * rho_kg_m3 * v_m_s**2 / (2.0 * d_m)


def route_pressure_loss_pa(
    r_pa_m: float, length_m: float, local_share: float = 0.3, two_pipe: bool = True
) -> float:
    """Потери давления по трассе, Па.

    ΔP = R · L · (1 + α) · 2

    ×2 — подающий и обратный трубопроводы: теплоноситель проходит трассу дважды.
    α — доля местных сопротивлений (повороты, арматура), учтённая эквивалентной длиной.
    """
    pipes = 2.0 if two_pipe else 1.0
    return r_pa_m * length_m * (1.0 + local_share) * pipes


def pressure_to_head_m(dp_pa: float, rho_kg_m3: float = 960.0) -> float:
    """Перевод потерь давления в метры водяного столба."""
    return dp_pa / (rho_kg_m3 * G_ACCEL)


def pressure_to_bar(dp_pa: float) -> float:
    return dp_pa / PA_PER_BAR


def heat_loss_gcal_year(w_per_m: float, length_m: float, hours_per_year: int = 8400) -> float:
    """Тепловые потери через изоляцию за год, Гкал.

    w_per_m — нормированная линейная плотность теплового потока (подающий+обратный).
    """
    return w_per_m * length_m * hours_per_year * 0.86e-6
