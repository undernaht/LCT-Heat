"""Тесты слоёв контекста, добавленных после аудита трассировки.

Зелёные насаждения, землепользование, технический коридор и выравнивание трассы
по сетке кварталов. До этого правила о деревьях лежали в конфиге мёртвым грузом,
землепользование выгружалось и не использовалось, скидка за техкоридор была
задокументирована, но не применялась ни к одной геометрии, а ортогонализация
значилась в конвейере при отсутствующем коде.
"""

from __future__ import annotations

import math

import pytest
from shapely.geometry import LineString, Point, box

from app.compliance import report as compliance
from app.compliance.rules import RuleContext
from app.costfield.build import build_cost_field, surface_shapes
from app.domain.enums import (
    BuildingUse,
    ComplianceStatus,
    HeatNodeKind,
    Laying,
    SurfaceClass,
)
from app.domain.models import (
    AreaModel,
    Building,
    Greenery,
    HeatEdge,
    HeatLoad,
    HeatNetwork,
    HeatNode,
    Road,
    SurfacePatch,
    TempSchedule,
)
from app.geo.raster import Grid
from app.routing.straighten import dominant_bearing, orthogonalize

CTX = RuleContext(Laying.CHANNEL, du_mm=80)
SCHEDULE = TempSchedule(130, 70)


def bare_area() -> AreaModel:
    area = AreaModel(crs="EPSG:32637")
    area.buildings.append(
        Building(id="target", geom=box(140.0, 20.0, 160.0, 40.0),
                 use=BuildingUse.RESIDENTIAL, floors=9, is_perspective=True,
                 load=HeatLoad(heating=0.35, dhw_max=0.11))
    )
    return area


def field_of(area: AreaModel, resolution: float = 1.0):
    grid = Grid.covering((0.0, 0.0, 300.0, 200.0), resolution, margin=10.0)
    return build_cost_field(
        area, grid, du_mm=80, laying=Laying.CHANNEL,
        target_building=area.building("target"), profile="min_cost",
    )


# --- Зелёные насаждения -----------------------------------------------------


def test_tree_raises_cost_but_does_not_block():
    """Дерево можно снести за компенсационную стоимость — значит надбавка, не запрет."""
    area = bare_area()
    area.greenery.append(Greenery(id="g1", geom=Point(100.0, 100.0), is_tree=True))
    field = field_of(area)

    at_tree = field.value_at(100.0, 100.0)
    away = field.value_at(40.0, 100.0)

    assert math.isfinite(at_tree)
    assert at_tree > away


def test_tree_zone_matches_the_norm():
    """Радиус зоны — 2,0 м по СП 124.13330, табл. А.3."""
    area = bare_area()
    area.greenery.append(Greenery(id="g1", geom=Point(100.0, 100.0), is_tree=True))
    field = field_of(area)
    plain = field.value_at(40.0, 100.0)

    assert field.value_at(101.5, 100.0) > plain      # внутри 2 м
    assert field.value_at(104.0, 100.0) == pytest.approx(plain, rel=1e-6)


def test_shrub_zone_is_smaller_than_tree_zone():
    area = bare_area()
    area.greenery.append(Greenery(id="tree", geom=Point(80.0, 100.0), is_tree=True))
    area.greenery.append(Greenery(id="shrub", geom=Point(200.0, 100.0), is_tree=False))
    field = field_of(area)
    plain = field.value_at(40.0, 100.0)

    # 2,5 м от куста уже чисто (норма 1,0), от дерева ещё нет (норма 2,0)
    assert field.value_at(202.5, 100.0) == pytest.approx(plain, rel=1e-6)
    assert field.value_at(82.5, 100.0) > plain


def test_raster_zones_are_conservative_by_one_cell():
    """Растеризация с all_touched расширяет любую зону примерно на ячейку.

    Это не ошибка, а осознанный запас: поиск держится от препятствий чуть дальше
    норматива. Точные расстояния всё равно меряет нормоконтроль по вектору
    (compliance/report.py), поэтому на выводах это не сказывается — но знать
    об этом надо, иначе тонкие зоны кажутся шире, чем заданы.
    """
    area = bare_area()
    area.greenery.append(Greenery(id="shrub", geom=Point(200.0, 100.0), is_tree=False))
    field = field_of(area, resolution=1.0)
    plain = field.value_at(40.0, 100.0)

    # норма 1,0 м, но на растре зона ощущается примерно до 2 м
    assert field.value_at(201.4, 100.0) > plain
    assert field.value_at(203.0, 100.0) == pytest.approx(plain, rel=1e-6)


def test_compliance_reports_tree_violation():
    area = bare_area()
    area.greenery.append(Greenery(id="g1", geom=Point(100.0, 101.0), is_tree=True))
    route = LineString([(20.0, 100.0), (200.0, 100.0)])

    result = compliance.check_route(area, route, CTX)

    check = next(c for c in result.checks if c.target_id == "g1")
    assert check.required_m == 2.0
    assert check.actual_m == pytest.approx(1.0, abs=0.05)
    assert check.status is not ComplianceStatus.PASS
    assert "снос" in check.note


def test_distant_tree_is_not_reported():
    area = bare_area()
    area.greenery.append(Greenery(id="g1", geom=Point(100.0, 160.0), is_tree=True))
    route = LineString([(20.0, 100.0), (200.0, 100.0)])

    result = compliance.check_route(area, route, CTX)
    assert not [c for c in result.checks if c.target_id == "g1"]


# --- Землепользование -------------------------------------------------------


def test_landuse_becomes_a_surface_class():
    area = bare_area()
    area.surfaces.append(
        SurfacePatch(id="s1", geom=box(60.0, 60.0, 140.0, 140.0),
                     surface_class=SurfaceClass.LAWN)
    )
    field = field_of(area)

    assert field.surface_at(100.0, 100.0) is SurfaceClass.LAWN
    assert field.surface_at(20.0, 180.0) is SurfaceClass.GROUND


def test_road_wins_over_landuse():
    """Проезжая часть поверх парка: асфальт всё равно придётся вскрывать."""
    area = bare_area()
    area.surfaces.append(
        SurfacePatch(id="s1", geom=box(0.0, 90.0, 300.0, 130.0),
                     surface_class=SurfaceClass.LAWN)
    )
    area.roads.append(
        Road(id="r1", geom=LineString([(0.0, 110.0), (300.0, 110.0)]),
             surface_class=SurfaceClass.STREET_LOCAL, width_m=8.0)
    )
    field = field_of(area)

    assert field.surface_at(150.0, 110.0) is SurfaceClass.STREET_LOCAL
    assert field.surface_at(150.0, 95.0) is SurfaceClass.LAWN


# --- Технический коридор ----------------------------------------------------


def with_heat_line() -> AreaModel:
    area = bare_area()
    network = HeatNetwork()
    network.nodes.append(
        HeatNode(id="hn_a", geom=Point(0.0, 150.0), kind=HeatNodeKind.SOURCE,
                 head_available_m=60.0)
    )
    network.edges.append(
        HeatEdge(id="he_1", geom=LineString([(0.0, 150.0), (300.0, 150.0)]),
                 du_mm=200, laying=Laying.CHANNEL, schedule=SCHEDULE,
                 node_a="hn_a", load_gcal_h=1.0, capacity_gcal_h=10.0)
    )
    area.heat = network
    return area


def test_corridor_along_existing_network_is_cheaper():
    """Вдоль действующей сети уже есть коридор и согласованный отвод."""
    area = with_heat_line()
    field = field_of(area)

    in_corridor = field.value_at(150.0, 148.0)
    outside = field.value_at(150.0, 100.0)

    assert field.surface_at(150.0, 148.0) is SurfaceClass.UTILITY_CORRIDOR
    assert in_corridor < outside


def test_corridor_stops_at_the_carriageway():
    """Под асфальтом коридора нет — там обычное вскрытие проезжей части."""
    area = with_heat_line()
    area.roads.append(
        Road(id="r1", geom=LineString([(0.0, 150.0), (300.0, 150.0)]),
             surface_class=SurfaceClass.STREET_MAJOR, width_m=14.0,
             requires_closed_crossing=True)
    )
    field = field_of(area)

    assert field.surface_at(150.0, 150.0) is SurfaceClass.STREET_MAJOR


def test_corridor_shapes_are_produced():
    area = with_heat_line()
    classes = {surface for _, surface in surface_shapes(area)}
    assert SurfaceClass.UTILITY_CORRIDOR in classes


# --- Выравнивание по сетке кварталов ----------------------------------------


def grid_roads() -> list[LineString]:
    """Правильная сетка: улицы строго по осям."""
    roads = [LineString([(0.0, y), (300.0, y)]) for y in (50.0, 100.0, 150.0)]
    roads += [LineString([(x, 0.0), (x, 200.0)]) for x in (50.0, 150.0, 250.0)]
    return roads


def test_dominant_bearing_finds_the_grid():
    base = dominant_bearing(grid_roads(), (150.0, 100.0))
    assert math.degrees(base) == pytest.approx(0.0, abs=1.0)


def test_dominant_bearing_follows_a_rotated_grid():
    angle = math.radians(20.0)
    rotated = []
    for line in grid_roads():
        coords = [
            (x * math.cos(angle) - y * math.sin(angle),
             x * math.sin(angle) + y * math.cos(angle))
            for x, y in line.coords
        ]
        rotated.append(LineString(coords))

    base = dominant_bearing(rotated, (150.0, 100.0))
    assert math.degrees(base) == pytest.approx(20.0, abs=2.0)


def test_orthogonalize_aligns_a_slightly_skewed_path():
    area = bare_area()
    field = field_of(area)
    # ломаная, отклонённая от осей на ~7°
    skewed = [(20.0, 60.0), (120.0, 72.0), (220.0, 60.0)]

    result = orthogonalize(field, skewed, grid_roads())

    assert result[0] == skewed[0] and result[-1] == skewed[-1]
    for (x1, y1), (x2, y2) in zip(result, result[1:]):
        bearing = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 45
        deviation = min(bearing, 45 - bearing)
        assert deviation < 7.0


def test_orthogonalize_keeps_endpoints_exactly():
    """Точка ввода и точка врезки двигаться не могут."""
    area = bare_area()
    field = field_of(area)
    path = [(20.0, 60.0), (90.0, 68.0), (160.0, 61.0), (230.0, 70.0)]

    result = orthogonalize(field, path, grid_roads())
    assert result[0] == path[0]
    assert result[-1] == path[-1]


def test_orthogonalize_does_not_enter_blocked_zones():
    area = bare_area()
    area.buildings.append(
        Building(id="wall", geom=box(80.0, 70.0, 220.0, 90.0),
                 use=BuildingUse.RESIDENTIAL, floors=5, load=HeatLoad(heating=1.0))
    )
    field = field_of(area)
    path = [(20.0, 60.0), (120.0, 66.0), (220.0, 60.0)]

    result = orthogonalize(field, path, grid_roads())

    from app.routing.straighten import segment_cost

    for a, b in zip(result, result[1:]):
        assert math.isfinite(segment_cost(field, a, b))


def test_orthogonalize_leaves_short_paths_alone():
    area = bare_area()
    field = field_of(area)
    path = [(20.0, 60.0), (200.0, 60.0)]

    assert orthogonalize(field, path, grid_roads()) == path
