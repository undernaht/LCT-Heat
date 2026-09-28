"""Независимый валидатор выходного GeoJSON по Техническому приложению ЛЦТ 2026.

Солвер (app/case) и этот пакет написаны порознь и не делят код: если они
расходятся во мнении о файле, кто-то из них прочёл правила неверно — и это
надо выяснить до того, как файл проверят организаторы.
"""

from app.case_validator.findings import Finding, Findings
from app.case_validator.rules import Rules
from app.case_validator.validator import Report, validate, validate_files

__all__ = ["Finding", "Findings", "Report", "Rules", "validate", "validate_files"]
