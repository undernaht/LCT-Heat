"""Проверка выходного GeoJSON по Техническому приложению из командной строки.

    python validate_cli.py --input "../ресурсы/распаковано/ТЗ/Датасет скорректированный.geojson" \
        --output result.geojson [--json report.json] [--allow-unconnected] [--max-findings N]

Код выхода 1, если есть ошибки.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.case_validator.report import format_report  # noqa: E402
from app.case_validator.validator import validate_files  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Независимая проверка результата по Техническому приложению ЛЦТ 2026")
    parser.add_argument("--input", required=True, help="входной GeoJSON (набор организаторов)")
    parser.add_argument("--output", required=True, help="выходной GeoJSON (результат сервиса)")
    parser.add_argument("--json", help="куда записать отчёт в JSON")
    parser.add_argument("--rules", help="альтернативный case_rules.yaml")
    parser.add_argument("--allow-unconnected", action="store_true",
                        help="неподключённые точки считать предупреждением, а не ошибкой")
    parser.add_argument("--max-findings", type=int, help="сколько находок печатать в консоль")
    args = parser.parse_args()

    report = validate_files(args.input, args.output, args.rules, args.allow_unconnected)
    print(format_report(report, args.max_findings))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, ensure_ascii=False, indent=2)
        print(f"отчёт: {args.json}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
