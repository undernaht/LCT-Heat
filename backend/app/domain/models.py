"""Доменная модель района.

Правило: `geom` внутри модели ВСЕГДА в метрической проекции (см. geo/crs.py).
Перепроецирование происходит ровно в двух местах — на входе ingest и на выходе export.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shapely.geometry import LineString, Point, Polygon

from .enums import BuildingUse, HeatNodeKind, Laying, SurfaceClass, UtilityKind


@dataclass(frozen=True)
class TempSchedule:
    """Температурный график. Δt определяет расход при заданной нагрузке."""

    supply_c: float
    return_c: float

    @property
    def delta_t(self) -> float:
        return self.supply_c - self.return_c

    @classmethod
    def parse(cls, text: str) -> TempSchedule:
        """'130/70' -> TempSchedule(130, 70)"""
        supply, ret = text.split("/")
        return cls(float(supply), float(ret))

    def __str__(self) -> str:
        return f"{self.supply_c:g}/{self.return_c:g}"


@dataclass
class HeatLoad:
    """Тепловая нагрузка, Гкал/ч."""

    heating: float = 0.0
    ventilation: float = 0.0
    dhw_max: float = 0.0
    source: str = "estimated"      # estimated | manual | design

    @property
    def total(self) -> float:
        return self.heating + self.ventilation + self.dhw_max


@dataclass
class Building:
    id: str
    geom: Polygon
    use: BuildingUse = BuildingUse.OTHER
    floors: int | None = None
    height_m: float | None = None
    built_year: int | None = None
    is_perspective: bool = False
    load: HeatLoad | None = None
    entry_point: Point | None = None    # точка ввода (ИТП)
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def footprint_m2(self) -> float:
        return self.geom.area


@dataclass
class HeatNode:
    id: str
    geom: Point
    kind: HeatNodeKind
    name: str | None = None
    capacity_gcal_h: float | None = None       # для источников
    reserve_gcal_h: float | None = None        # свободная мощность источника
    head_available_m: float | None = None      # располагаемый напор
    # Статический пьезометрический уровень, м над уровнем моря. От него зависят
    # проверки невскипания вверху и предельного давления внизу — они про
    # абсолютные отметки, а не про разность напоров.
    static_head_m: float | None = None


@dataclass
class HeatEdge:
    id: str
    geom: LineString
    du_mm: int
    laying: Laying = Laying.CHANNEL
    schedule: TempSchedule = field(default_factory=lambda: TempSchedule(130, 70))
    node_a: str | None = None
    node_b: str | None = None
    load_gcal_h: float = 0.0                   # текущая транзитная нагрузка
    capacity_gcal_h: float | None = None       # предельная по гидравлике
    is_main: bool = False                      # магистраль или распределительная

    @property
    def reserve_gcal_h(self) -> float:
        if self.capacity_gcal_h is None:
            return 0.0
        return max(0.0, self.capacity_gcal_h - self.load_gcal_h)


@dataclass
class HeatNetwork:
    nodes: list[HeatNode] = field(default_factory=list)
    edges: list[HeatEdge] = field(default_factory=list)

    def node(self, node_id: str) -> HeatNode | None:
        return next((n for n in self.nodes if n.id == node_id), None)


@dataclass
class Utility:
    """Чужая инженерная сеть — препятствие с нормативным отступом."""

    id: str
    geom: LineString
    kind: UtilityKind
    pressure_class: str | None = None    # газ: '<=0.3' | '0.3-0.6' | '0.6-1.2'
    voltage_kv: float | None = None      # кабели
    depth_m: float | None = None         # глубина заложения — для проверки пересечений
    diameter_mm: int | None = None


@dataclass
class Road:
    id: str
    geom: LineString
    surface_class: SurfaceClass
    width_m: float
    name: str | None = None
    requires_closed_crossing: bool = False   # магистраль: только прокол/ГНБ


@dataclass
class Railway:
    id: str
    geom: LineString
    gauge_mm: int = 1520
    electrified: bool = False
    corridor_width_m: float = 20.0


@dataclass
class SurfacePatch:
    """Участок покрытия — задаёт множитель земляных работ."""

    id: str
    geom: Polygon
    surface_class: SurfaceClass


@dataclass
class Parcel:
    """Земельный участок. Чужая собственность — «стоимость согласований»."""

    id: str
    geom: Polygon
    cadastral_number: str | None = None
    is_public: bool = False
    is_protected_zone: bool = False      # ЗОУИТ
    prohibits_laying: bool = False


@dataclass
class Greenery:
    id: str
    geom: Point | Polygon
    is_tree: bool = True                 # дерево (2,0 м) или кустарник (1,0 м)


@dataclass
class WaterBody:
    id: str
    geom: Polygon
    name: str | None = None


@dataclass
class AreaModel:
    """Всё, что нужно для расчёта по одному району. Живёт в памяти."""

    crs: str
    # Протокол импорта: что прочитано, что отброшено и почему. Заполняется
    # ingest'ом и доходит до API — молчаливая потеря объектов недопустима.
    import_report: Any = None
    # Рельеф. Необязателен: без него всё считается, только профиль недоступен.
    terrain: Any = None
    buildings: list[Building] = field(default_factory=list)
    heat: HeatNetwork = field(default_factory=HeatNetwork)
    utilities: list[Utility] = field(default_factory=list)
    roads: list[Road] = field(default_factory=list)
    railways: list[Railway] = field(default_factory=list)
    surfaces: list[SurfacePatch] = field(default_factory=list)
    greenery: list[Greenery] = field(default_factory=list)
    parcels: list[Parcel] = field(default_factory=list)
    water: list[WaterBody] = field(default_factory=list)

    def building(self, building_id: str) -> Building | None:
        return next((b for b in self.buildings if b.id == building_id), None)

    def stats(self) -> dict[str, int]:
        return {
            "buildings": len(self.buildings),
            "heat_nodes": len(self.heat.nodes),
            "heat_edges": len(self.heat.edges),
            "utilities": len(self.utilities),
            "roads": len(self.roads),
            "railways": len(self.railways),
            "surfaces": len(self.surfaces),
            "greenery": len(self.greenery),
            "parcels": len(self.parcels),
            "water": len(self.water),
        }
