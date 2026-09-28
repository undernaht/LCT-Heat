"""Расчёт по конкурсной модели из командной строки.

    python case_cli.py --input "../ресурсы/распаковано/ТЗ/Датасет скорректированный.geojson" --out result.geojson
    python case_cli.py --input ... --png result.png     # картинка лучшего варианта
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.case import solve as solve_module  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Трассировка по Техническому приложению ЛЦТ 2026")
    parser.add_argument("--input", required=True, help="входной GeoJSON")
    parser.add_argument("--out", help="выходной GeoJSON")
    parser.add_argument("--png", help="картинка лучшего варианта")
    parser.add_argument("--variants", type=int, default=3)
    parser.add_argument("--order", action="append", help="ограничить порядки обработки")
    parser.add_argument("--mode", choices=["2d", "depth"], default="2d", help="режим: 2D или с учётом глубины")
    parser.add_argument("--effort", choices=["standard", "thorough"], default="standard",
                        help="thorough — дополнительно перестановки точек у лучшего варианта")
    args = parser.parse_args()

    result = solve_module.solve_file(
        args.input, args.out, max_variants=args.variants, orders=args.order, mode=args.mode, effort=args.effort,
    )
    case = result.case
    print(f"Район «{case.name}»: {json.dumps(case.stats(), ensure_ascii=False)}")
    for issue in case.issues:
        print(f"  [{issue.level}] {issue.where}: {issue.detail}" + (f" ×{issue.count}" if issue.count > 1 else ""))
    print(f"Поле: {result.notes['field']}; расчёт {result.elapsed_s:.1f} с")
    for line in result.notes.get("improvement", []):
        print(f"  улучшение: {line}")
    for v in result.variants:
        c = v.cost
        print(
            f"\n{v.id} (rank {v.rank}, порядок {v.order}): S = {c.score:.4f}\n"
            f"  длина {c.new_network_length:.1f} м, участков {len(v.network.segments)}, "
            f"камер {len(c.chambers)} ({c.chamber_construction_cost/1e6:.0f} млн), врезок {c.tie_in_count}\n"
            f"  стоимость строительства {c.construction_cost/1e6:.2f} млн, штраф {c.unconnected_penalty/1e6:.0f} млн, "
            f"итого {c.calculated_cost/1e6:.2f} млн\n"
            f"  неподключённых: {c.unconnected or 'нет'}; предельная длина: "
            f"{'ok' if v.diameter_report.ok else 'НАРУШЕНА'}; поднято Ду: {len(v.diameter_report.bumps)}; "
            f"минимальность: {v.notes['minimality'] or 'ok'}"
        )
        for oks_id, reason in v.build.reasons.items():
            print(f"    не подключена {oks_id}: {reason}")
        print(f"  спецпроходов {len(v.special_report.crossings)}, перестроено {len(v.special_report.rebuilt)}, "
              f"ремонтов {len(v.repairs)}, нерешённых нарушений {len(v.notes.get('unresolved_special', []))}")
        for line in v.notes.get("unresolved_special", []):
            print(f"    !! {line}")
        if v.depth_report is not None:
            from app.case import depth as depth_module
            print(f"  глубина: {depth_module.summary(v.depth_report)}")

    if args.png and result.variants:
        from app.case.preview_png import render_png
        best = result.variants[0]
        net = best.network
        xs = [p[0] for s in net.segments.values() for p in s.points]
        ys = [p[1] for s in net.segments.values() for p in s.points]
        bounds = (min(xs) - 40, min(ys) - 40, max(xs) + 40, max(ys) + 40) if xs else None
        render_png(case, net, args.png, bounds=bounds, width_px=2000,
                   title=f"{best.id}: S = {best.cost.score:.4f}, {best.cost.new_network_length:.0f} м, "
                         f"{best.cost.calculated_cost/1e6:.1f} млн ₽")
        print(f"\nкартинка: {args.png}")
    if args.out:
        print(f"выход: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
