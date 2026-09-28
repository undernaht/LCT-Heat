"""Загрузка конфигурации.

Ни одного норматива и ни одной расценки в коде — всё приходит отсюда.
Файлы лежат в `backend/config/`, читаются один раз и кэшируются.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


@dataclass(frozen=True)
class Config:
    normatives: dict[str, Any]
    costs: dict[str, Any]
    loads: dict[str, Any]

    def check(self) -> None:
        """Структурная проверка при старте: лучше упасть здесь, чем в расчёте."""
        required = [
            (self.normatives, "hydraulics", "roughness_m"),
            (self.normatives, "hydraulics", "min_inner_diameter_mm"),
            (self.normatives, "horizontal"),
            (self.normatives, "vertical"),
            (self.costs, "pipe_per_m", "channelless"),
            (self.costs, "earthwork_per_m", "base_by_du"),
            (self.costs, "surface_multiplier"),
            (self.costs, "zone_penalty_per_m"),
            (self.loads, "climate", "t_outside_design_c"),
            (self.loads, "heating_w_per_m2", "residential"),
            (self.loads, "units", "w_to_gcal_h"),
        ]
        for path in required:
            node, keys = path[0], path[1:]
            for key in keys:
                if not isinstance(node, dict) or key not in node:
                    raise ValueError(f"config: отсутствует ключ {'.'.join(keys)}")
                node = node[key]

        # Нормативы обязаны ссылаться на пункт — иначе протокол нормоконтроля
        # нельзя защитить перед согласующим инженером.
        for rule in self.normatives["horizontal"]:
            if not rule.get("clause"):
                raise ValueError(f"normatives: правило {rule.get('id')} без clause")

    @property
    def normatives_verified(self) -> bool:
        return bool(self.normatives.get("meta", {}).get("verified", False))


def _read(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@lru_cache(maxsize=1)
def get_config() -> Config:
    cfg = Config(
        normatives=_read("normatives.yaml"),
        costs=_read("costs.yaml"),
        loads=_read("loads.yaml"),
    )
    cfg.check()
    return cfg
