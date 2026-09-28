"""Находка валидатора и их накопитель.

Каждая проверка отдаёт не «да/нет», а находку с адресом (вариант, объект) и
числами «норма/факт»: по такому списку можно защищать решение перед
организаторами и искать, кто из двух независимых реализаций прочёл правило
неверно.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

SEVERITIES = ("error", "warning", "info")


@dataclass
class Finding:
    severity: str
    code: str
    message: str
    variant_id: Any = None
    object_id: Any = None
    expected: Any = None
    actual: Any = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Findings:
    """Список находок с короткими методами добавления."""

    def __init__(self) -> None:
        self.items: list[Finding] = []

    def add(self, severity: str, code: str, message: str, *, variant_id: Any = None,
            object_id: Any = None, expected: Any = None, actual: Any = None) -> Finding:
        if severity not in SEVERITIES:
            raise ValueError(severity)
        finding = Finding(severity, code, message, variant_id, object_id, expected, actual)
        self.items.append(finding)
        return finding

    def error(self, code: str, message: str, **kw: Any) -> Finding:
        return self.add("error", code, message, **kw)

    def warning(self, code: str, message: str, **kw: Any) -> Finding:
        return self.add("warning", code, message, **kw)

    def info(self, code: str, message: str, **kw: Any) -> Finding:
        return self.add("info", code, message, **kw)

    def count(self, severity: str, variant_id: Any = ...) -> int:
        return sum(
            1 for f in self.items
            if f.severity == severity and (variant_id is ... or f.variant_id == variant_id)
        )

    @property
    def has_errors(self) -> bool:
        return any(f.severity == "error" for f in self.items)

    def codes(self) -> set[str]:
        return {f.code for f in self.items}
