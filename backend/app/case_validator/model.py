"""Типизированная модель входа и выхода после разбора GeoJSON.

Разбор терпим к мусору: объект с битой геометрией или атрибутами попадает в
модель помеченным непригодным (`usable = False`), чтобы одна ошибка структуры
не обрушила остальные проверки и не породила лавину вторичных находок.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from shapely.geometry import LineString, Point
from shapely.geometry.base import BaseGeometry

IdKey = tuple[str, Any]


def is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_id(value: Any) -> bool:
    return isinstance(value, str) or is_number(value)


def id_key(value: Any) -> IdKey:
    """Ключ сравнения идентификаторов: тип сохраняется, 1 и "1" — разные id."""
    if isinstance(value, str):
        return ("s", value)
    if is_number(value):
        return ("n", value)
    return ("x", repr(value))


def id_text(value: Any) -> str:
    """Строковое представление для «мягкого» сравнения 1 ↔ "1" ↔ 1.0."""
    if is_number(value) and float(value).is_integer():
        return str(int(value))
    return str(value)


# --- вход -------------------------------------------------------------------


@dataclass
class InputLine:
    id: Any
    key: IdKey
    geom: LineString
    diameter: int | None


@dataclass
class InputPoint:
    """Существующая камера или точка подключения ОКС."""

    id: Any
    key: IdKey
    lonlat: tuple[float, float]
    geom: Point
    flow_tph: float | None = None


@dataclass
class Restriction:
    id: Any
    key: IdKey
    rtype: str
    geom: BaseGeometry


@dataclass
class InputData:
    lines: dict[IdKey, InputLine] = field(default_factory=dict)
    chambers: dict[IdKey, InputPoint] = field(default_factory=dict)
    oks: dict[IdKey, InputPoint] = field(default_factory=dict)
    restrictions: list[Restriction] = field(default_factory=list)
    keys: set[IdKey] = field(default_factory=set)          # все id входа
    kinds: dict[IdKey, str] = field(default_factory=dict)  # id → object_type
    text_ids: dict[str, list[Any]] = field(default_factory=dict)


# --- выход ------------------------------------------------------------------


@dataclass
class NodeRef:
    """Узел, на который ссылается участок."""

    kind: str  # oks | existing_chamber | new_chamber | tech
    key: IdKey
    id: Any
    lonlat: tuple[float, float]
    geom: Point

    @property
    def xy(self) -> tuple[float, float]:
        return (self.geom.x, self.geom.y)


@dataclass
class Segment:
    id: Any
    key: IdKey
    variant_id: Any
    index: int
    lonlat: list[tuple[float, float]] | None
    geom: LineString | None
    start_id: Any = None
    end_id: Any = None
    flow_tph: float | None = None
    diameter: int | None = None       # только Ду из таблицы 1
    length: float | None = None
    laying_method: str | None = None  # base | special
    depth_start: float | None = None
    depth_end: float | None = None
    cost: float | None = None
    start_ref: NodeRef | None = None
    end_ref: NodeRef | None = None

    @property
    def usable(self) -> bool:
        return self.geom is not None and self.start_ref is not None and self.end_ref is not None

    @property
    def coords(self) -> list[tuple[float, float]]:
        return [(x, y) for x, y in self.geom.coords] if self.geom is not None else []

    @property
    def is_special(self) -> bool:
        return self.laying_method == "special"

    def other_ref(self, key: IdKey) -> NodeRef | None:
        if self.start_ref is not None and self.start_ref.key == key:
            return self.end_ref
        return self.start_ref

    def coords_from(self, key: IdKey) -> list[tuple[float, float]]:
        """Координаты, ориентированные так, что узел `key` — первый."""
        coords = self.coords
        if self.end_ref is not None and self.end_ref.key == key and not (
            self.start_ref is not None and self.start_ref.key == key
        ):
            return coords[::-1]
        return coords


@dataclass
class NewChamber:
    id: Any
    key: IdKey
    variant_id: Any
    index: int
    lonlat: tuple[float, float] | None
    geom: Point | None
    diameter: int | None = None
    cost: float | None = None


@dataclass
class TechNode:
    id: Any
    key: IdKey
    variant_id: Any
    index: int
    lonlat: tuple[float, float] | None
    geom: Point | None


@dataclass
class Summary:
    id: Any
    key: IdKey
    variant_id: Any
    index: int
    rank: int | None = None
    construction_cost: float | None = None
    chamber_construction_cost: float | None = None
    existing_chamber_tie_in_count: int | None = None
    existing_chamber_tie_in_cost: float | None = None
    unconnected_penalty: float | None = None
    calculated_cost: float | None = None
    new_network_length: float | None = None
    score: float | None = None
    unconnected_oks_ids: list[Any] | None = None


@dataclass
class Variant:
    variant_id: Any
    key: IdKey
    segments: dict[IdKey, Segment] = field(default_factory=dict)
    chambers: dict[IdKey, NewChamber] = field(default_factory=dict)
    tech_nodes: dict[IdKey, TechNode] = field(default_factory=dict)
    summaries: list[Summary] = field(default_factory=list)

    @property
    def summary(self) -> Summary | None:
        return self.summaries[0] if len(self.summaries) == 1 else None

    @property
    def usable_segments(self) -> list[Segment]:
        return [s for s in self.segments.values() if s.usable]


@dataclass
class OutputData:
    variants: dict[IdKey, Variant] = field(default_factory=dict)
