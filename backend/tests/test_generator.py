"""Тесты ядра генератора датасета.

Проверяется то, от чего зависит правдоподобие сгенерированной сети:
агрегация нагрузки вверх по дереву, монотонное падение напора от источника
и непрерывность попутных сетей.
"""

from __future__ import annotations

import math

import pytest
from shapely.geometry import LineString

import generate as gen
from app.domain.models import TempSchedule


# --- Граф улиц --------------------------------------------------------------


def chain_graph(lengths: list[float]) -> gen.StreetGraph:
    """Цепочка 0 - 1 - 2 - ... вдоль оси X."""
    graph = gen.StreetGraph()
    x = 0.0
    for i, length in enumerate(lengths):
        graph.add_edge(i, i + 1, (x, 0.0), (x + length, 0.0), "residential")
        x += length
    return graph


def test_dijkstra_finds_shortest_path():
    graph = gen.StreetGraph()
    #  0 --100-- 1 --100-- 2      прямой путь 0->2 длиной 260 хуже, чем 200
    graph.add_edge(0, 1, (0, 0), (100, 0), "residential")
    graph.add_edge(1, 2, (100, 0), (200, 0), "residential")
    graph.add_edge(0, 2, (0, 0), (0, 260), "residential")
    graph.pos[2] = (200, 0)

    dist, prev = graph.dijkstra(0)
    assert dist[2] == pytest.approx(200.0)
    assert prev[2] == 1


def test_dijkstra_prefers_major_roads_when_weighted():
    """Магистрали должны притягивать трассу магистралей — как в реальных схемах."""
    graph = gen.StreetGraph()
    graph.add_edge(0, 1, (0, 0), (0, 100), "primary")
    graph.add_edge(1, 3, (0, 100), (100, 100), "primary")
    graph.add_edge(0, 2, (0, 0), (100, 0), "service")
    graph.add_edge(2, 3, (100, 0), (100, 100), "service")

    preference = {"primary": 0.75, "service": 1.0}
    _, prev = graph.dijkstra(0, lambda length, cls: length * preference.get(cls, 1.0))

    assert prev[3] == 1     # пошли через primary, хотя длина одинаковая


def test_largest_component_ignores_islands():
    graph = chain_graph([50.0, 50.0])
    graph.add_edge(90, 91, (900, 900), (950, 900), "service")   # оторванный кусок

    component = gen.largest_component(graph)
    assert component == {0, 1, 2}


# --- Дерево теплосети -------------------------------------------------------


def test_heat_tree_aggregates_load_downstream():
    """Ребро ближе к источнику несёт сумму нагрузок всех потребителей за ним."""
    graph = chain_graph([100.0, 100.0])
    edge_load, used = gen.build_heat_tree(graph, source=0, consumers={1: 0.4, 2: 0.6})

    assert edge_load[(0, 1)] == pytest.approx(1.0)   # обе нагрузки
    assert edge_load[(1, 2)] == pytest.approx(0.6)   # только дальняя
    assert used == {0, 1, 2}


def test_heat_tree_shares_common_corridor():
    """Два потребителя за общим участком — участок считается один раз с суммой."""
    graph = gen.StreetGraph()
    graph.add_edge(0, 1, (0, 0), (0, 100), "primary")     # общий коридор
    graph.add_edge(1, 2, (0, 100), (-50, 200), "residential")
    graph.add_edge(1, 3, (0, 100), (50, 200), "residential")

    edge_load, _ = gen.build_heat_tree(graph, source=0, consumers={2: 0.3, 3: 0.5})

    assert len(edge_load) == 3
    assert edge_load[(0, 1)] == pytest.approx(0.8)


def test_heat_tree_skips_unreachable_consumers():
    graph = chain_graph([100.0])
    graph.add_edge(50, 51, (5000, 5000), (5100, 5000), "service")

    edge_load, _ = gen.build_heat_tree(graph, source=0, consumers={1: 0.5, 51: 9.9})
    assert set(edge_load) == {(0, 1)}


# --- Диаметры и напор -------------------------------------------------------


def test_diameter_grows_towards_source():
    """Ближе к источнику нагрузка больше — значит и диаметр не меньше."""
    graph = chain_graph([200.0, 200.0])
    edge_load, _ = gen.build_heat_tree(graph, source=0, consumers={1: 3.0, 2: 3.0})
    du, _ = gen.size_and_head(graph, 0, edge_load)

    assert du[(0, 1)] >= du[(1, 2)]


def test_head_decreases_monotonically_from_source():
    """Располагаемый напор обязан падать по мере удаления — на этом стоит фильтр
    кандидатов врезки."""
    graph = chain_graph([300.0, 300.0, 300.0])
    edge_load, _ = gen.build_heat_tree(graph, source=0, consumers={1: 2.0, 2: 2.0, 3: 2.0})
    _, head = gen.size_and_head(graph, 0, edge_load)

    assert head[0] == pytest.approx(gen.SOURCE_HEAD_M)
    assert head[0] > head[1] > head[2] > head[3]
    assert head[3] >= 0.0


def test_capacity_covers_assigned_load():
    """Подобранный диаметр обязан пропускать назначенную на него нагрузку."""
    graph = chain_graph([200.0, 200.0])
    edge_load, _ = gen.build_heat_tree(graph, source=0, consumers={1: 5.0, 2: 4.0})
    du, _ = gen.size_and_head(graph, 0, edge_load)

    from app.hydraulics import diameters

    for key, load in edge_load.items():
        capacity = diameters.capacity_gcal_h(du[key], TempSchedule(130, 70), is_branch=False)
        assert capacity >= load


# --- Попутные сети ----------------------------------------------------------


def test_offset_line_is_parallel_at_given_distance():
    line = LineString([(0, 0), (200, 0)])
    offset = gen.offset_line(line, -3.5)

    assert offset is not None
    assert offset.distance(line) == pytest.approx(3.5, abs=0.1)


def test_offset_line_rejects_degenerate_input():
    assert gen.offset_line(LineString([(0, 0), (0.2, 0)]), -3.5) is None


def test_offset_preserves_length_so_corridor_has_no_gaps():
    """Разрывы в коридоре чужой сети трассировщик воспринял бы как проход —
    артефакт генерации, а не свойство реальности."""
    line = LineString([(0, 0), (100, 0), (200, 40), (320, 40)])
    offset = gen.offset_line(line, -3.5)

    assert offset is not None
    assert offset.length == pytest.approx(line.length, rel=0.1)


@pytest.mark.parametrize(
    ("scope", "road_class", "expected"),
    [
        ("all", "service", True),
        ("all_but_service", "service", False),
        ("all_but_service", "residential", True),
        ("major", "primary", True),
        ("major", "residential", False),
        ("minor", "residential", True),
        ("minor", "primary", False),
    ],
)
def test_road_scope_rules(scope: str, road_class: str, expected: bool):
    assert gen.road_matches(scope, road_class) is expected


# --- Размещение источника ---------------------------------------------------


def test_source_is_placed_on_periphery():
    """Источник — на периферии застройки, а не в её середине."""
    from shapely.geometry import Polygon

    from app.domain.enums import BuildingUse
    from app.domain.models import Building, HeatLoad

    graph = chain_graph([100.0, 100.0, 100.0])
    buildings = [
        Building(
            id=f"b{i}",
            geom=Polygon([(x, -10), (x + 20, -10), (x + 20, 10), (x, 10)]),
            use=BuildingUse.RESIDENTIAL,
            floors=5,
            load=HeatLoad(heating=0.5),
        )
        for i, x in enumerate((0.0, 30.0, 60.0))
    ]

    source = gen.place_source(graph, buildings, component={0, 1, 2, 3})
    assert source == 3          # самый дальний от центра масс застройки
    assert math.dist(graph.pos[source], (0.0, 0.0)) == pytest.approx(300.0)
