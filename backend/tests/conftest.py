"""Общая настройка тестов.

Скрипты генерации живут в `data/`, а не в пакете `app`, но их ядро тестируется
здесь же — иначе датасет остаётся единственной непроверяемой частью проекта.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "data"))
