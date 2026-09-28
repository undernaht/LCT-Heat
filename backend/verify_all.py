"""Сквозная проверка: солвер + независимый валидатор по всем наборам.

Один запуск — таблица по конкурсному набору и всем синтетическим сценариям:
подключено / S / ошибки и предупреждения валидатора / время. Это то, что
стоит запускать перед каждой сдачей и после каждой правки солвера.

    python verify_all.py                # все наборы, 1 вариант (быстро)
    python verify_all.py --variants 3   # как в интерфейсе
    python verify_all.py --only roads --only mixed
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.case import solve as solve_module  # noqa: E402
from app.case_validator import validate_files  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
COMPETITION = ROOT / "ресурсы" / "распаковано" / "ТЗ" / "Датасет скорректированный.geojson"
SYNTHETIC_DIR = ROOT / "data" / "cache" / "case"
OUT_DIR = ROOT / "data" / "cache" / "verify"


def datasets(only: list[str] | None) -> list[tuple[str, Path]]:
    items: list[tuple[str, Path]] = []
    if COMPETITION.exists():
        items.append(("конкурсный", COMPETITION))
    for path in sorted(SYNTHETIC_DIR.glob("*.geojson")):
        if "_result" in path.name:
            continue
        items.append((path.stem, path))
    if only:
        items = [(n, p) for n, p in items if n in only]
    return items


def main() -> int:
    parser = argparse.ArgumentParser(description="Солвер + валидатор по всем наборам")
    parser.add_argument("--variants", type=int, default=1)
    parser.add_argument("--only", action="append")
    parser.add_argument("--effort", choices=["standard", "thorough"], default="standard")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    failed = 0
    for name, path in datasets(args.only):
        started = time.perf_counter()
        out = OUT_DIR / f"{path.stem}_result.geojson"
        try:
            result = solve_module.solve_file(path, out, max_variants=args.variants, effort=args.effort)
        except Exception as exc:  # noqa: BLE001 — падение солвера тоже результат прогона
            rows.append((name, "ПАДЕНИЕ", f"{type(exc).__name__}: {exc}"[:60], "", "", "", round(time.perf_counter() - started)))
            failed += 1
            continue
        elapsed = time.perf_counter() - started
        best = result.variants[0]
        report = validate_files(path, out, allow_unconnected=True).to_dict()
        errors = int(report["counts"]["error"])
        warnings = int(report["counts"]["warning"])
        if errors > 0:
            failed += 1
        total = len(result.case.oks)
        connected = total - len(best.cost.unconnected)
        (OUT_DIR / f"{path.stem}_validation.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        rows.append((name, f"{connected}/{total}", f"{best.cost.score:.4f}", str(errors), str(warnings),
                     str(len(best.repairs)), round(elapsed)))

    width = max(len(r[0]) for r in rows) if rows else 10
    print(f"{'набор':<{width}}  {'подкл.':>7}  {'S':>9}  {'ошиб.':>6}  {'предупр.':>8}  {'ремонт':>6}  {'сек':>4}")
    for r in rows:
        print(f"{r[0]:<{width}}  {r[1]:>7}  {r[2]:>9}  {r[3]:>6}  {r[4]:>8}  {r[5]:>6}  {r[6]:>4}")
    print(f"\nвыход: {OUT_DIR}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
