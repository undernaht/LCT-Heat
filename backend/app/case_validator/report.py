"""Текст отчёта для консоли. JSON отдаёт сам Report.to_dict()."""

from __future__ import annotations

from app.case_validator.validator import Report

_MARK = {"error": "ОШИБКА", "warning": "предупр.", "info": "инфо"}


def _fmt(value: object) -> str:
    if isinstance(value, float):
        return f"{value:,.4f}".replace(",", " ").rstrip("0").rstrip(".")
    return repr(value)


def format_report(report: Report, max_findings: int | None = None) -> str:
    lines: list[str] = []
    for v in report.variants:
        errors = report.count("error", v.variant_id)
        warnings = report.count("warning", v.variant_id)
        score_declared = f"{v.score_declared:.4f}" if v.score_declared is not None else "—"
        cost_declared = f"{v.calculated_cost_declared / 1e6:.3f}" if v.calculated_cost_declared is not None else "—"
        lines.append(
            f"Вариант {v.variant_id!r} (rank {v.rank}): score заявлен {score_declared}, "
            f"пересчитан {v.score_recomputed:.4f}; ошибок {errors}, предупреждений {warnings}"
        )
        lines.append(
            f"  участков {v.segments}, камер {v.chambers}, техузлов {v.tech_nodes}, врезок {v.tie_in_count}; "
            f"длина {v.length_declared:.2f} м (по геометрии {v.length_recomputed:.2f}); "
            f"итоговая стоимость {cost_declared} млн (пересчёт {v.calculated_cost_recomputed / 1e6:.3f} млн)"
        )
        if v.unconnected_computed or v.unconnected_declared:
            lines.append(f"  неподключённые: заявлено {v.unconnected_declared}, по участкам {v.unconnected_computed}")
    if not report.variants:
        lines.append("Вариантов не найдено")

    shown = report.findings if max_findings is None else report.findings[:max_findings]
    if shown:
        lines.append("")
        lines.append("Находки:")
    for finding in shown:
        where = []
        if finding.variant_id is not None:
            where.append(f"вариант {finding.variant_id!r}")
        if finding.object_id is not None:
            where.append(f"объект {finding.object_id!r}")
        extra = []
        if finding.expected is not None:
            extra.append(f"норма {_fmt(finding.expected)}")
        if finding.actual is not None:
            extra.append(f"факт {_fmt(finding.actual)}")
        tail = f" [{'; '.join(extra)}]" if extra else ""
        lines.append(f"  [{_MARK[finding.severity]}] {finding.code} ({', '.join(where) or 'файл'}): {finding.message}{tail}")
    if max_findings is not None and len(report.findings) > max_findings:
        lines.append(f"  … ещё {len(report.findings) - max_findings} находок (см. JSON)")
    lines.append("")
    counts = ", ".join(f"{s}: {report.count(s)}" for s in ("error", "warning", "info"))
    lines.append(("ПРОВЕРКА ПРОЙДЕНА" if report.ok else "ПРОВЕРКА НЕ ПРОЙДЕНА") + f" ({counts})")
    return "\n".join(lines)
