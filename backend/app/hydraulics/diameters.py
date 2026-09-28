"""Подбор диаметра и проверка трассы по гидравлике.

Отвергнутые кандидаты сохраняются — из них строится объяснение
«почему Ду80, а не Ду65» (docs/04-algorithm.md §11).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import get_config
from ..domain.models import TempSchedule
from . import flow


@dataclass(frozen=True)
class FlowState:
    """Состояние потока для конкретного диаметра."""

    du_mm: int
    g_t_h: float
    v_m_s: float
    r_pa_m: float


@dataclass
class DiameterChoice:
    selected: FlowState | None
    rejected: list[tuple[FlowState, str]] = field(default_factory=list)
    limits: dict[str, float] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.selected is not None

    def explain(self) -> list[str]:
        """Человекочитаемое объяснение выбора — идёт прямо в UI."""
        out: list[str] = []
        if self.selected:
            out.append(
                f"Ду{self.selected.du_mm}: v = {self.selected.v_m_s:.2f} м/с, "
                f"R = {self.selected.r_pa_m:.0f} Па/м"
            )
        for state, reason in self.rejected:
            out.append(f"Ду{state.du_mm} отклонён: {reason}")
        return out


def _design(key: str) -> float:
    return get_config().normatives["hydraulics"]["design"][key]


def _roughness() -> float:
    return get_config().normatives["hydraulics"]["roughness_m"]["water_heat_network"]


def flow_state(q_gcal_h: float, schedule: TempSchedule, du_mm: int) -> FlowState:
    """Параметры потока при заданных нагрузке, графике и диаметре."""
    rho = _design("water_density_kg_m3")
    d_m = du_mm / 1000.0
    g = flow.mass_flow_t_h(q_gcal_h, schedule.delta_t)
    v = flow.velocity_m_s(g, d_m, rho)
    r = flow.specific_pressure_loss_pa_m(v, d_m, rho, _roughness())
    return FlowState(du_mm=du_mm, g_t_h=g, v_m_s=v, r_pa_m=r)


def select_diameter(
    q_gcal_h: float,
    schedule: TempSchedule,
    *,
    is_branch: bool = True,
    du_min_mm: int | None = None,
) -> DiameterChoice:
    """Наименьший Ду из ряда, удовлетворяющий ограничениям по R и v.

    Практическое наблюдение (docs/04-algorithm.md §3.2): для подключения
    отдельного здания ограничение по скорости почти никогда не активно —
    диаметр определяют удельные потери.
    """
    cfg = get_config().normatives["hydraulics"]
    series: list[int] = list(cfg["design"]["du_series_mm"])
    r_max = _design("r_max_branch_pa_per_m" if is_branch else "r_max_pa_per_m")
    v_max = _design("v_max_m_s")
    min_du = max(cfg["min_inner_diameter_mm"], du_min_mm or 0)

    choice = DiameterChoice(selected=None, limits={"r_max_pa_m": r_max, "v_max_m_s": v_max})

    for du in series:
        if du < min_du:
            continue
        state = flow_state(q_gcal_h, schedule, du)
        if state.r_pa_m > r_max:
            choice.rejected.append(
                (state, f"удельные потери {state.r_pa_m:.0f} Па/м при допустимых {r_max:.0f}")
            )
            continue
        if state.v_m_s > v_max:
            choice.rejected.append(
                (state, f"скорость {state.v_m_s:.2f} м/с при допустимой {v_max:.1f}")
            )
            continue
        choice.selected = state
        return choice

    return choice


@dataclass
class RouteHydraulics:
    """Гидравлика конкретной трассы — то, что уходит в карточку варианта."""

    q_gcal_h: float
    schedule: str
    du_mm: int
    g_t_h: float
    v_m_s: float
    r_pa_m: float
    length_m: float
    dp_pa: float
    dp_bar: float
    head_m: float                        # потери напора по трассе
    head_available_m: float | None       # располагаемый напор в точке врезки
    head_required_at_consumer_m: float   # что обязано остаться на ИТП
    head_reserve_m: float | None         # остаток после трассы и ИТП
    head_known: bool                     # был ли вообще известен располагаемый напор
    head_ok: bool
    explanation: list[str] = field(default_factory=list)

    @property
    def head_verdict(self) -> str:
        """Три состояния вместо двух: «не проверено» — не то же самое, что «прошло».

        Раньше отсутствие данных о напоре давало `head_ok = True`, и проект
        технических условий печатал «располагаемый напор — достаточен» при
        полном отсутствии исходных данных.
        """
        if not self.head_known:
            return "не проверено: располагаемый напор в точке врезки неизвестен"
        return "достаточен" if self.head_ok else "НЕДОСТАТОЧЕН"


def check_route(
    q_gcal_h: float,
    schedule: TempSchedule,
    length_m: float,
    *,
    head_available_m: float | None = None,
    is_branch: bool = True,
) -> RouteHydraulics | None:
    """Полный расчёт по трассе. None, если подходящего диаметра в ряду нет."""
    choice = select_diameter(q_gcal_h, schedule, is_branch=is_branch)
    if not choice.selected:
        return None

    state = choice.selected
    dp = flow.route_pressure_loss_pa(state.r_pa_m, length_m, _design("local_resistance_share"))
    head = flow.pressure_to_head_m(dp, _design("water_density_kg_m3"))

    # На вводе в здание должен остаться напор для ИТП, иначе проверка вырождается
    required_at_consumer = _design("required_head_at_consumer_m")
    reserve = None if head_available_m is None else head_available_m - head - required_at_consumer

    return RouteHydraulics(
        q_gcal_h=q_gcal_h,
        schedule=str(schedule),
        du_mm=state.du_mm,
        g_t_h=state.g_t_h,
        v_m_s=state.v_m_s,
        r_pa_m=state.r_pa_m,
        length_m=length_m,
        dp_pa=dp,
        dp_bar=flow.pressure_to_bar(dp),
        head_m=head,
        head_available_m=head_available_m,
        head_required_at_consumer_m=required_at_consumer,
        head_reserve_m=None if reserve is None else round(reserve, 2),
        head_known=head_available_m is not None,
        head_ok=reserve is not None and reserve >= 0,
        explanation=choice.explain(),
    )


def capacity_gcal_h(du_mm: int, schedule: TempSchedule, *, is_branch: bool = False) -> float:
    """Предельная нагрузка, которую участок данного Ду пропустит по R_доп.

    Нужна, чтобы посчитать резерв пропускной способности существующей сети
    и решить, требуется ли реконструкция при врезке.

    R ∝ v² ∝ Q², поэтому Q_пред = Q_проб · sqrt(R_доп / R_проб).
    """
    r_max = _design("r_max_branch_pa_per_m" if is_branch else "r_max_pa_per_m")
    probe_q = 1.0
    probe = flow_state(probe_q, schedule, du_mm)
    return probe_q * (r_max / probe.r_pa_m) ** 0.5
