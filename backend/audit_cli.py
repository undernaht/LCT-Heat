"""Аудит правдоподобия расчётов.

Прогоняет весь конвейер на реальном районе и сверяет ключевые величины с
диапазонами, которые встречаются в проектной практике. Задача — не «проверить,
что код не падает» (для этого есть тесты), а поймать цифры, которые технически
корректны, но в жизни не встречаются.

    python backend/audit_cli.py --area small
"""

from __future__ import annotations

import argparse
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


from app.domain.models import TempSchedule  # noqa: E402
from app.economics import fee as fee_module  # noqa: E402
from app.hydraulics import diameters  # noqa: E402
from app.ingest.area import load_area  # noqa: E402
from app.loads import estimate as loads  # noqa: E402
from app.routing import group as group_module  # noqa: E402
from app.routing import solver  # noqa: E402

CACHE_DIR = Path(__file__).resolve().parents[1] / "data" / "cache"

OK, WARN, BAD = "✅", "⚠️ ", "❌"


@dataclass
class Check:
    name: str
    value: float
    unit: str
    low: float
    high: float
    source: str

    @property
    def status(self) -> str:
        if self.low <= self.value <= self.high:
            return OK
        # за 30 % от границы — предупреждение, дальше — брак
        margin = 0.3
        if self.value < self.low:
            return WARN if self.value >= self.low * (1 - margin) else BAD
        return WARN if self.value <= self.high * (1 + margin) else BAD

    def render(self) -> str:
        return (
            f"  {self.status} {self.name:<44} {self.value:>10,.1f} {self.unit:<9}"
            f" норма {self.low:g}–{self.high:g}   {self.source}"
        ).replace(",", " ")


def section(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Аудит правдоподобия расчётов")
    parser.add_argument("--area", default="small")
    args = parser.parse_args()

    area = load_area(CACHE_DIR / args.area)
    checks: list[Check] = []

    # ------------------------------------------------------------------
    section("1. ТЕПЛОВЫЕ НАГРУЗКИ ЗДАНИЙ")
    # ------------------------------------------------------------------
    heated = [b for b in area.buildings if b.load and b.load.total > 0 and not b.is_perspective]
    specific = []
    for building in heated:
        area_m2 = loads.total_area_m2(building)
        if area_m2 > 100:
            specific.append(building.load.total * 1e6 / 0.86 / area_m2)   # Вт/м²

    print(f"  отапливаемых зданий: {len(heated)}")
    print(f"  удельная нагрузка, Вт/м²: медиана {statistics.median(specific):.0f}, "
          f"диапазон {min(specific):.0f}–{max(specific):.0f}")

    checks.append(Check(
        "Удельная нагрузка, медиана", statistics.median(specific), "Вт/м²",
        50, 110, "укрупнённые показатели для жилья",
    ))

    total_load = sum(b.load.total for b in heated)
    bounds = area.buildings[0].geom.bounds
    for building in area.buildings:
        b = building.geom.bounds
        bounds = (min(bounds[0], b[0]), min(bounds[1], b[1]),
                  max(bounds[2], b[2]), max(bounds[3], b[3]))
    area_km2 = (bounds[2] - bounds[0]) * (bounds[3] - bounds[1]) / 1e6

    checks.append(Check(
        "Плотность тепловой нагрузки", total_load / area_km2, "Гкал/ч·км²",
        30, 150, "плотная городская застройка",
    ))

    # ------------------------------------------------------------------
    section("2. ГИДРАВЛИКА")
    # ------------------------------------------------------------------
    schedule = TempSchedule(130, 70)
    print(f"  {'Q, Гкал/ч':>10} {'Ду':>6} {'G, т/ч':>9} {'v, м/с':>8} {'R, Па/м':>9}")
    for q in (0.1, 0.3, 0.5, 1.0, 3.0, 10.0, 30.0):
        choice = diameters.select_diameter(q, schedule, is_branch=q < 2)
        if not choice.selected:
            print(f"  {q:>10.2f}  диаметр не подобран")
            continue
        s = choice.selected
        print(f"  {q:>10.2f} {s.du_mm:>6} {s.g_t_h:>9.2f} {s.v_m_s:>8.2f} {s.r_pa_m:>9.0f}")

    # Проверка на типовой нагрузке
    typical = diameters.select_diameter(0.5, schedule, is_branch=True).selected
    checks.append(Check(
        "Скорость при 0,5 Гкал/ч (ответвление)", typical.v_m_s, "м/с",
        0.3, 1.5, "распределительные сети",
    ))
    main_line = diameters.select_diameter(20.0, schedule, is_branch=False).selected
    checks.append(Check(
        "Скорость на магистрали 20 Гкал/ч", main_line.v_m_s, "м/с",
        0.8, 3.0, "магистральные сети",
    ))

    # ------------------------------------------------------------------
    section("3. ТРАССИРОВКА")
    # ------------------------------------------------------------------
    perspective = [b for b in area.buildings if b.is_perspective and b.load]
    solutions = []
    for building in perspective:
        solution = solver.solve(area, building, resolution=1.0)
        if solution is None:
            print(f"  {building.id}: трасса не найдена")
            continue
        solutions.append((building, solution))

        straight = min(
            edge.geom.distance(building.geom) for edge in area.heat.edges
        )
        detour = solution.length_m / max(straight, 1.0)
        rub_per_m = solution.cost.total_rub / solution.length_m
        print(f"  {building.id}: {solution.length_m:>5.0f} м (по прямой {straight:>4.0f} м, "
              f"коэф. {detour:.2f}), Ду{solution.du_mm}, "
              f"{rub_per_m / 1000:.0f} тыс ₽/м, вершин {len(solution.geometry.coords)}")

    detours = [
        s.length_m / max(min(e.geom.distance(b.geom) for e in area.heat.edges), 1.0)
        for b, s in solutions
    ]
    checks.append(Check(
        "Коэффициент извилистости трассы", statistics.mean(detours), "×",
        1.05, 1.6, "обход препятствий в застройке",
    ))

    vertex_density = statistics.mean(
        len(s.geometry.coords) / (s.length_m / 100) for _, s in solutions
    )
    checks.append(Check(
        "Вершин на 100 м трассы", vertex_density, "шт",
        1.0, 5.0, "проектная трасса, не ломаная",
    ))

    print("\n  Напор:")
    for building, solution in solutions:
        h = solution.hydraulics
        mark = OK if h.head_ok else BAD
        print(f"   {mark} {building.id}: трасса {h.head_m:>5.1f} м + ИТП "
              f"{h.head_required_at_consumer_m:>4.1f} м из {h.head_available_m:>5.1f} м, "
              f"запас {h.head_reserve_m:>5.1f} м")

    reserves = [s.hydraulics.head_reserve_m for _, s in solutions
                if s.hydraulics.head_reserve_m is not None]
    checks.append(Check(
        "Минимальный запас напора после ИТП", min(reserves), "м вод. ст.",
        0.0, 60.0, "неотрицательный, с запасом на развитие",
    ))

    # ------------------------------------------------------------------
    section("4. СТОИМОСТЬ")
    # ------------------------------------------------------------------
    for building, solution in solutions:
        print(f"\n  {building.id} — {solution.length_m:.0f} м, Ду{solution.du_mm}")
        for title, value in solution.cost.as_rows():
            share = value / solution.cost.total_rub * 100
            per_m = value / solution.length_m / 1000
            print(f"     {title:<50} {value / 1e6:>7.2f} млн  {share:>5.1f} %  "
                  f"{per_m:>6.1f} тыс ₽/м")
        print(f"     {'ИТОГО':<50} {solution.cost.total_rub / 1e6:>7.2f} млн"
              f"          {solution.cost.total_rub / solution.length_m / 1000:>6.1f} тыс ₽/м")
        print(f"     камер: {solution.cost.chambers_count} "
              f"(интервал {solution.length_m / max(solution.cost.chambers_count, 1):.0f} м)")

    unit_costs = [s.cost.total_rub / s.length_m / 1000 for _, s in solutions]
    checks.append(Check(
        "Удельная стоимость трассы Ду80", statistics.mean(unit_costs), "тыс ₽/м",
        25, 60, "строительство распредсетей, Москва",
    ))

    chamber_intervals = [
        s.length_m / max(s.cost.chambers_count, 1) for _, s in solutions
    ]
    checks.append(Check(
        "Интервал между камерами", statistics.mean(chamber_intervals), "м",
        100, 250, "проектная практика",
    ))

    # ------------------------------------------------------------------
    section("4.1. ВЕРТИКАЛЬНАЯ ТРАССИРОВКА")
    # ------------------------------------------------------------------
    profiles = [(b, s.vertical) for b, s in solutions if s.vertical and s.vertical.available]
    if not profiles:
        print("  рельеф не загружен — профиль не строился")
    else:
        for building, vertical in profiles:
            failed = [c.name for c in vertical.checks if not c.ok]
            print(f"  {building.id}: земля {vertical.ground_min_m:.1f}–{vertical.ground_max_m:.1f} м, "
                  f"глубина {vertical.depth_min_m:.2f}–{vertical.depth_max_m:.2f} м, "
                  f"выемка {vertical.excavation_m3:.0f} м³, "
                  f"воздушников {vertical.air_points}, спускников {vertical.drain_points}"
                  + (f"  ⚠️ {', '.join(failed)}" if failed else ""))

        depths = [v.depth_mean_m for _, v in profiles]
        checks.append(Check(
            "Средняя глубина заложения", statistics.mean(depths), "м",
            0.5, 2.5, "канальная прокладка в городе",
        ))

        # Доля рельефа в смете. Ноль означал бы, что профиль не влияет на деньги;
        # больше пятой части — что базовая ставка земляных работ занижена.
        shares = [
            s.cost.excavation_extra_rub / s.cost.total_rub * 100
            for _, s in solutions
            if s.vertical and s.vertical.available and s.cost.total_rub > 0
        ]
        checks.append(Check(
            "Доля выемки из-за рельефа в смете", statistics.mean(shares), "%",
            0.5, 20.0, "рельеф заметен, но не подменяет базовую ставку",
        ))

        failures = sum(1 for _, v in profiles for c in v.checks if not c.ok)
        checks.append(Check(
            "Нарушений пьезометрии и глубины", float(failures), "шт",
            0, 0, "профиль должен проходить свои же проверки",
        ))

    # ------------------------------------------------------------------
    section("5. ПЛАТА ЗА ПОДКЛЮЧЕНИЕ")
    # ------------------------------------------------------------------
    for building, solution in solutions:
        fee = fee_module.calculate(
            solution.q_gcal_h, {solution.du_mm: solution.length_m}, solution.laying
        )
        ratio = fee.total_with_vat_rub / solution.cost.total_rub
        print(f"  {building.id}: плата {fee.total_with_vat_rub / 1e6:>6.2f} млн, "
              f"смета {solution.cost.total_rub / 1e6:>6.2f} млн, "
              f"плата/смета = {ratio:.2f}")

    ratios = []
    for building, solution in solutions:
        fee = fee_module.calculate(
            solution.q_gcal_h, {solution.du_mm: solution.length_m}, solution.laying
        )
        ratios.append(fee.total_with_vat_rub / solution.cost.total_rub)

    checks.append(Check(
        "Плата за подключение / смета СМР", statistics.mean(ratios), "×",
        0.9, 1.6, "плата должна покрывать затраты ТСО",
    ))

    # ------------------------------------------------------------------
    section("6. СУЩЕСТВУЮЩАЯ СЕТЬ (сгенерированная)")
    # ------------------------------------------------------------------
    du_values = [e.du_mm for e in area.heat.edges]
    lengths = [e.geom.length for e in area.heat.edges]
    heads = [n.head_available_m for n in area.heat.nodes if n.head_available_m is not None]
    print(f"  участков {len(du_values)}, длина {sum(lengths) / 1000:.2f} км, "
          f"Ду {min(du_values)}–{max(du_values)}")
    print(f"  напор в узлах: {min(heads):.1f}–{max(heads):.1f} м вод. ст.")

    loaded_edges = [e for e in area.heat.edges if e.capacity_gcal_h]
    utilisation = statistics.mean(
        e.load_gcal_h / e.capacity_gcal_h for e in loaded_edges
    )
    checks.append(Check(
        "Загрузка существующих участков", utilisation * 100, "%",
        40, 90, "сеть не должна быть пустой или запертой",
    ))
    checks.append(Check(
        "Минимальный напор в узлах", min(heads), "м вод. ст.",
        15, 90, "хватает на трассу плюс ИТП",
    ))

    density = sum(lengths) / area_km2 / 1000
    checks.append(Check(
        "Плотность теплосети", density, "км/км²",
        2, 8, "плотная городская застройка",
    ))

    # ------------------------------------------------------------------
    section("7. ГРУППОВОЕ ПОДКЛЮЧЕНИЕ")
    # ------------------------------------------------------------------
    # Проверяем на квартале застройки: разнесённым по району объектам делить
    # коридор почти нечего, и цифра ничего не говорит о качестве алгоритма.
    block = [b for b in perspective if b.id.startswith("kvartal_")]
    group = group_module.solve_group(area, block or perspective, resolution=1.0)
    if group:
        print(f"  квартал: {len(group.connections)} корпусов, "
              f"{sum(c.load_gcal_h for c in group.connections):.2f} Гкал/ч")
        print(f"  дерево {group.total_length_m:.0f} м против "
              f"{group.independent_length_m:.0f} м раздельно")
        print(f"  СМР {group.total_cost_rub / 1e6:.1f} млн против "
              f"{group.independent_cost_rub / 1e6:.1f} млн, "
              f"экономия {group.saving_share * 100:.0f} %")
        print(f"  нормоконтроль: {group.compliance_status}, "
              f"нарушений {group.compliance_failures}")
        checks.append(Check(
            "Экономия от общего коридора (квартал)", group.saving_share * 100, "%",
            20, 65, "общий коридор дешевле отдельных вводов",
        ))
        checks.append(Check(
            "Нарушений нормоконтроля в дереве", group.compliance_failures, "шт",
            0, 0, "недопустимая трасса не предлагается",
        ))

    # ------------------------------------------------------------------
    section("ИТОГ АУДИТА")
    # ------------------------------------------------------------------
    for check in checks:
        print(check.render())

    bad = [c for c in checks if c.status == BAD]
    warn = [c for c in checks if c.status == WARN]
    print(f"\n  Проверок {len(checks)}: в норме {len(checks) - len(bad) - len(warn)}, "
          f"предупреждений {len(warn)}, вне диапазона {len(bad)}")

    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
