"""Расчёт трассы из командной строки — до появления API и фронтенда.

    python backend/solve_cli.py --area small
    python backend/solve_cli.py --area small --building persp_2 --profile min_approvals
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.domain.enums import Laying  # noqa: E402
from app.ingest.area import load_area  # noqa: E402
from app.routing import alternatives, group, solver  # noqa: E402

CACHE_DIR = Path(__file__).resolve().parents[1] / "data" / "cache"


def money(value: float) -> str:
    return f"{value / 1e6:.2f} млн ₽" if value >= 1e6 else f"{value / 1e3:.0f} тыс ₽"


def report(solution: solver.RouteSolution) -> None:
    print(f"\n=== Профиль «{solution.profile}» ===")
    print(f"  Трасса:      {solution.length_m:.0f} м, {len(solution.geometry.coords)} вершин")
    print(f"  Подключение: {solution.connection.node_name or solution.connection.edge_id} "
          f"(Ду{solution.connection.du_mm}, резерв {solution.connection.reserve_gcal_h:.2f} Гкал/ч)")

    h = solution.hydraulics
    print(f"  Гидравлика:  Q = {h.q_gcal_h:.3f} Гкал/ч, график {h.schedule}, G = {h.g_t_h:.2f} т/ч")
    print(f"               Ду{h.du_mm}, v = {h.v_m_s:.2f} м/с, R = {h.r_pa_m:.0f} Па/м, "
          f"ΔP = {h.dp_bar:.2f} бар ({h.head_m:.1f} м вод. ст.)")
    print(f"               располагаемый напор {h.head_available_m} м → "
          f"{'проходит' if h.head_ok else 'НЕ ПРОХОДИТ'}")

    print(f"  Стоимость:   {money(solution.cost.total_rub)} "
          f"({solution.cost.total_rub / solution.length_m / 1000:.0f} тыс ₽/м)")
    for title, value in solution.cost.as_rows():
        print(f"     {title:<52} {money(value):>12}")
    for crossing in solution.cost.crossings:
        print(f"        · {crossing.kind:<18} {crossing.target:<26} {money(crossing.cost_rub):>12}")
    print(f"     [вес поиска, в смету не входит: {money(solution.cost.search_penalty_rub)}]")

    print("  Длина по покрытиям:")
    for surface, length in sorted(
        solution.cost.length_by_surface_m.items(), key=lambda kv: -kv[1]
    ):
        print(f"     {surface:<20} {length:>7.0f} м")

    print(f"  Риск согласований: {solution.risk.score}")
    for title, value in solution.risk.as_rows():
        print(f"     {title:<40} {value}")

    protocol = solution.compliance
    icon = {"pass": "✅", "conditional": "⚠️", "fail": "❌"}
    print(f"  Нормоконтроль: {icon[protocol.status.value]} {protocol.status.value.upper()} "
          f"— проверок {protocol.checks_total}, "
          f"нарушений {len(protocol.failures)}, условно {len(protocol.conditionals)}")
    for check in protocol.checks[:12]:
        print(f"     {icon[check.status.value]} {check.where:>12}  {check.title[:44]:<44} "
              f"норма {check.required_m:>5.2f} факт {check.actual_m:>6.2f}  {check.clause}")
        if check.note:
            print(f"                     └ {check.note}")

    print("  Почему такой диаметр:")
    for line in solution.diameter_explanation[:4]:
        print(f"     {line}")

    print(f"  Кандидатов врезки рассмотрено: {len(solution.candidates)}")
    for candidate in solution.candidates[1:4]:
        delta = candidate.total_rub - solution.connection.total_rub
        recon = ", нужна реконструкция" if candidate.needs_reconstruction else ""
        print(f"     {candidate.node_name or candidate.edge_id}: +{money(delta)}{recon}")


def report_group(result: group.GroupSolution) -> None:
    print("\n=== Подключение группы общим коридором ===")
    print(f"  Дерево:      {result.total_length_m:.0f} м, участков {len(result.segments)}, "
          f"общих {result.notes['shared_segments']} "
          f"({result.notes['shared_length_m']:.0f} м)")
    print(f"  Магистраль:  Ду{result.notes['trunk_du_mm']}, "
          f"суммарная нагрузка {result.notes['total_load_gcal_h']} Гкал/ч")

    print("\n  Участки по диаметрам:")
    print(f"     {'участок':<14}{'Ду':>6}{'длина':>9}{'нагрузка':>11}  питает")
    for segment in result.segments:
        serves = ", ".join(segment.serves)
        print(f"     {segment.id:<14}{segment.du_mm:>6}{segment.length_m:>8.0f}м"
              f"{segment.load_gcal_h:>10.3f}  {serves}")

    print("\n  Подключения:")
    for connection in result.connections:
        target = ("сеть " if connection.tap_kind == "network" else "ветка ") + connection.tap_target
        print(f"     {connection.building_id:<10} {connection.load_gcal_h:>7.3f} Гкал/ч  "
              f"→ {target}")

    print(f"\n  Нормоконтроль: {result.compliance_status.upper()} "
          f"(нарушений {result.compliance_failures}, условно {result.compliance_conditionals})")

    print("\n  Сравнение с раздельным подключением:")
    print(f"     {'':<22}{'группой':>16}{'раздельно':>16}{'экономия':>16}")
    print(f"     {'длина трассы':<22}{result.total_length_m:>15.0f}м"
          f"{result.independent_length_m:>15.0f}м{result.length_saving_m:>15.0f}м")
    print(f"     {'стоимость СМР':<22}{money(result.total_cost_rub):>16}"
          f"{money(result.independent_cost_rub):>16}{money(result.saving_rub):>16}")
    print(f"     {'плата за подключение':<22}{money(result.fee_total_with_vat_rub):>16}"
          f"{money(result.independent_fee_rub):>16}{money(result.fee_saving_rub):>16}")
    print(f"\n  Экономия по СМР: {result.saving_share * 100:.0f} %")


def main() -> int:
    parser = argparse.ArgumentParser(description="Расчёт трассы подключения")
    parser.add_argument("--area", required=True)
    parser.add_argument("--building", help="id здания; по умолчанию первое перспективное")
    parser.add_argument("--profile", default="min_cost",
                        choices=["min_cost", "min_approvals", "reliable"])
    parser.add_argument("--variants", action="store_true",
                        help="посчитать все профили с диверсификацией и Парето-фронт")
    parser.add_argument("--diversify", type=int, default=2)
    parser.add_argument("--group", action="store_true",
                        help="подключить все перспективные здания общим коридором")
    parser.add_argument("--laying", default="channel", choices=["channel", "channelless"])
    parser.add_argument("--resolution", type=float, default=1.0)
    args = parser.parse_args()

    started = time.perf_counter()
    area = load_area(CACHE_DIR / args.area)
    print(f"Район «{args.area}»: {area.stats()}")
    print(f"  загрузка {time.perf_counter() - started:.1f} с")

    if args.building:
        building = area.building(args.building)
    else:
        building = next((b for b in area.buildings if b.is_perspective), None)
    if building is None:
        print("Здание не найдено", file=sys.stderr)
        return 2

    print(f"  здание {building.id}: {building.floors} эт., "
          f"Q = {building.load.total:.3f} Гкал/ч" if building.load else "")

    started = time.perf_counter()

    if args.group:
        targets = [b for b in area.buildings if b.is_perspective and b.load]
        print(f"\nГруппа: {len(targets)} перспективных зданий, "
              f"суммарная нагрузка {sum(b.load.total for b in targets):.3f} Гкал/ч")

        result = group.solve_group(
            area, targets, laying=Laying(args.laying), resolution=args.resolution
        )
        elapsed = time.perf_counter() - started
        if result is None:
            print("Группу подключить не удалось", file=sys.stderr)
            return 1

        report_group(result)
        print(f"\n  расчёт {elapsed:.1f} с, поле {result.notes['grid']}")
        return 0

    if args.variants:
        result = alternatives.solve_variants(
            area, building, laying=Laying(args.laying),
            resolution=args.resolution, diversify=args.diversify,
        )
        elapsed = time.perf_counter() - started
        if not result.variants:
            print("Ни одного варианта не найдено", file=sys.stderr)
            return 1

        for solution in result.variants:
            report(solution)

        print("\n=== Сравнение ===")
        print(f"  {'вариант':<16}{'длина':>8}{'стоимость':>14}{'риск':>8}"
              f"{'нормоконтроль':>16}{'Парето':>8}")
        for solution in result.variants:
            vid = alternatives.variant_id_of(solution)
            mark = "★" if vid in result.pareto_ids else ""
            protocol = solution.compliance
            status = (f"{protocol.status.value} "
                      f"({len(protocol.failures)}/{len(protocol.conditionals)})")
            print(f"  {vid:<16}{solution.length_m:>7.0f}м{money(solution.cost.total_rub):>14}"
                  f"{solution.risk.score:>8.1f}{status:>16}{mark:>8}")
        print(f"\n  расчёт {elapsed:.1f} с на {len(result.variants)} вариантов")
        return 0

    solution = solver.solve(
        area, building,
        profile=args.profile,
        laying=Laying(args.laying),
        resolution=args.resolution,
    )
    elapsed = time.perf_counter() - started

    if solution is None:
        print("Трасса не найдена", file=sys.stderr)
        return 1

    report(solution)
    print(f"\n  расчёт {elapsed:.1f} с, поле {solution.field_notes['grid']}, "
          f"запретов {solution.field_notes['blocked_share'] * 100:.1f} %")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
