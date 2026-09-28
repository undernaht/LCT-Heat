"""Протокол импорта геоданных.

До появления этого модуля любая неожиданность во входных данных приводила к
одному из двух исходов: голому `500 Internal Server Error` (отсутствует поле
`id`, неизвестное значение `use`) или к молчаливой потере объектов
(`MultiPolygon`, `properties: null`, отсутствующий файл слоя). Второе хуже
первого: сервис отвечал успехом на данных, половину которых выбросил.

Теперь всё, что не удалось прочитать, попадает сюда и доходит до API.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


@dataclass
class ImportIssue:
    layer: str
    kind: str          # missing_file | bad_geometry | missing_field | bad_value | empty
    detail: str
    count: int = 1

    @property
    def is_fatal(self) -> bool:
        """Мешает ли расчёту. Отсутствие зелени — нет, отсутствие зданий — да."""
        return self.kind == "missing_file" and self.layer in FATAL_LAYERS


FATAL_LAYERS = {"buildings_load", "gen_heat_edges"}


@dataclass
class ImportReport:
    source_crs: str = "EPSG:4326"
    metric_crs: str = ""
    loaded: dict[str, int] = field(default_factory=dict)
    issues: list[ImportIssue] = field(default_factory=list)

    def add(self, layer: str, kind: str, detail: str) -> None:
        for issue in self.issues:
            if issue.layer == layer and issue.kind == kind and issue.detail == detail:
                issue.count += 1
                return
        self.issues.append(ImportIssue(layer=layer, kind=kind, detail=detail))

    def count(self, layer: str, loaded: int) -> None:
        self.loaded[layer] = loaded

    # Виды замечаний, означающие ПОТЕРЮ данных. Остальные — «файла нет»,
    # «слой пуст», «величина рассчитана» — информационные: объектов не убыло.
    LOSSY_KINDS = frozenset({
        "bad_geometry", "missing_field", "bad_value", "duplicate", "unrecognised",
    })

    @property
    def dropped(self) -> int:
        return sum(
            issue.count for issue in self.issues if issue.kind in self.LOSSY_KINDS
        )

    @property
    def ok(self) -> bool:
        return not any(issue.is_fatal for issue in self.issues)

    def summary(self) -> dict:
        return {
            "source_crs": self.source_crs,
            "metric_crs": self.metric_crs,
            "loaded": self.loaded,
            "dropped": self.dropped,
            "fatal": not self.ok,
            "issues": [
                {
                    "layer": issue.layer,
                    "kind": issue.kind,
                    "detail": issue.detail,
                    "count": issue.count,
                }
                for issue in sorted(self.issues, key=lambda i: -i.count)
            ],
            "by_kind": dict(Counter(issue.kind for issue in self.issues)),
        }
