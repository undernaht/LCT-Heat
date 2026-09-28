"""Тесты платы за подключение и выгрузок.

Плата — та самая величина, ради которой считается трасса, поэтому её пороги и
структура проверяются отдельно от сметы работ: это разные вещи.
"""

from __future__ import annotations

import ezdxf
import pytest
from shapely.geometry import LineString, box

from app.domain.enums import BuildingUse, Laying
from app.domain.models import AreaModel, Building, HeatLoad, Railway, Road
from app.domain.enums import SurfaceClass
from app.economics import estimate as economics
from app.economics import fee as fee_module
from app.export import dxf as dxf_export
from app.export import geojson as geojson_export
from app.export import pdf as pdf_export
from app.routing import solver

from .test_routing import synthetic_area


# --- Плата за подключение ---------------------------------------------------


def test_small_load_uses_flat_fee():
    """Не более 0,1 Гкал/ч — 550 ₽, установлено ФЗ-190 ст. 14."""
    fee = fee_module.calculate(0.08, {50: 120.0}, Laying.CHANNEL)

    assert fee.method == "flat"
    assert fee.total_with_vat_rub == 550
    assert not fee.is_estimated          # величина из закона, калибровать нечего


def test_large_load_falls_back_to_individual_project():
    """Свыше 1,5 Гкал/ч плата определяется по индивидуальному проекту."""
    fee = fee_module.calculate(2.4, {200: 400.0}, Laying.CHANNEL)

    assert fee.method == "individual"
    assert fee.lines == []
    assert fee.total_with_vat_rub == 0
    assert "индивидуальн" in fee.note


def test_medium_load_is_two_part_tariff():
    fee = fee_module.calculate(0.464, {80: 450.0}, Laying.CHANNEL)

    assert fee.method == "tariff"
    assert len(fee.lines) == 2
    assert fee.lines[0].unit == "Гкал/ч"
    assert fee.lines[1].unit == "м"
    assert fee.total_with_vat_rub == pytest.approx(fee.total_rub * 1.2)


def test_fee_scales_with_route_length():
    """Ключевая связка: оптимизатор минимизирует ровно то, что платит заявитель."""
    short = fee_module.calculate(0.5, {80: 200.0}, Laying.CHANNEL)
    long = fee_module.calculate(0.5, {80: 600.0}, Laying.CHANNEL)

    assert long.total_rub > short.total_rub
    load_part = short.lines[0].amount_rub
    assert long.lines[0].amount_rub == pytest.approx(load_part)   # ставка за нагрузку та же
    assert long.lines[1].amount_rub == pytest.approx(short.lines[1].amount_rub * 3)


def test_channel_laying_costs_more_than_channelless():
    channel = fee_module.calculate(0.5, {80: 300.0}, Laying.CHANNEL)
    channelless = fee_module.calculate(0.5, {80: 300.0}, Laying.CHANNELLESS)

    assert channel.total_rub > channelless.total_rub


def test_multi_diameter_route_produces_a_line_per_diameter():
    """Структура сразу многодиаметровая — под подключение группы зданий."""
    fee = fee_module.calculate(1.2, {80: 150.0, 150: 400.0}, Laying.CHANNEL)

    assert len(fee.lines) == 3
    assert sum(line.amount_rub for line in fee.lines) == pytest.approx(fee.total_rub)


def test_uncalibrated_rates_are_flagged():
    """Ставки ориентировочные — документ обязан об этом сообщать."""
    fee = fee_module.calculate(0.464, {80: 450.0}, Laying.CHANNEL)
    assert fee.is_estimated


# --- Объединение пересечений ------------------------------------------------


def area_with_railways(*offsets: float) -> AreaModel:
    """Параллельные пути на заданных Y — как двухпутная ж/д из OSM."""
    area = AreaModel(crs="EPSG:32637")
    for index, y in enumerate(offsets):
        area.railways.append(
            Railway(id=f"rw_{index}", geom=LineString([(0.0, y), (300.0, y)]),
                    corridor_width_m=20.0)
        )
    return area


def field_stub():
    from app.costfield.build import build_cost_field, aoi_bounds
    from app.geo.raster import Grid

    area = synthetic_area(wall=False)
    target = area.building("target")
    grid = Grid.covering(aoi_bounds(area, target, 600.0), 4.0, margin=20.0)
    return build_cost_field(area, grid, du_mm=80, laying=Laying.CHANNEL,
                            target_building=target, profile="min_cost")


def test_parallel_tracks_are_one_crossing():
    """Двухпутка приходит из OSM двумя линиями, но прокол под ней один."""
    area = area_with_railways(100.0, 104.0)
    route = LineString([(150.0, 40.0), (150.0, 170.0)])

    crossings = economics.count_crossings(area, route, field_stub())
    rail = [c for c in crossings if c.kind == "hdd_rail"]

    assert len(rail) == 1
    assert "путей: 2" in rail[0].target


def test_distant_tracks_are_separate_crossings():
    area = area_with_railways(60.0, 200.0)
    route = LineString([(150.0, 20.0), (150.0, 260.0)])

    rail = [c for c in economics.count_crossings(area, route, field_stub())
            if c.kind == "hdd_rail"]
    assert len(rail) == 2


def test_grazing_the_right_of_way_is_not_a_crossing():
    """Заход в полосу отвода без пересечения путей — вопрос отступа, не ГНБ.

    Иначе смета получает лишнюю мобилизацию ГНБ за то, что трасса задела угол
    полосы отвода на полтора метра.
    """
    area = area_with_railways(100.0)
    # Трасса идёт в 8 м от оси: внутри полосы отвода (10 м), но пути не пересекает
    route = LineString([(20.0, 92.0), (60.0, 92.0)])

    crossings = economics.count_crossings(area, route, field_stub())
    assert not [c for c in crossings if c.kind == "hdd_rail"]


def test_crossing_the_track_is_charged():
    area = area_with_railways(100.0)
    route = LineString([(150.0, 60.0), (150.0, 140.0)])

    rail = [c for c in economics.count_crossings(area, route, field_stub())
            if c.kind == "hdd_rail"]
    assert len(rail) == 1
    assert rail[0].cost_rub > 0
    assert rail[0].length_m == pytest.approx(20.0, abs=1.0)   # ширина полосы отвода


def test_major_road_crossing_is_charged():
    area = AreaModel(crs="EPSG:32637")
    area.roads.append(
        Road(id="r1", geom=LineString([(0.0, 100.0), (300.0, 100.0)]),
             surface_class=SurfaceClass.STREET_MAJOR, width_m=18.0,
             name="Проспект", requires_closed_crossing=True)
    )
    route = LineString([(150.0, 60.0), (150.0, 140.0)])

    road = [c for c in economics.count_crossings(area, route, field_stub())
            if c.kind == "hdd_road"]
    assert len(road) == 1 and road[0].target == "Проспект"


# --- Выгрузки ---------------------------------------------------------------


@pytest.fixture(scope="module")
def solution_fixture():
    area = synthetic_area(wall=True)
    target = area.building("target")
    solution = solver.solve(area, target, resolution=2.0)
    assert solution is not None
    return solution, area, target


def test_geojson_export_has_route_and_tap(solution_fixture):
    solution, area, _ = solution_fixture
    data = geojson_export.export(solution, area.crs)

    roles = [f["properties"]["role"] for f in data["features"]]
    assert "route" in roles and "tap" in roles

    route = next(f for f in data["features"] if f["properties"]["role"] == "route")
    assert route["geometry"]["type"] == "LineString"
    assert route["properties"]["du_mm"] == solution.du_mm
    assert route["properties"]["compliance"] in {"pass", "conditional", "fail"}


def test_geojson_coordinates_are_degrees(solution_fixture):
    solution, area, _ = solution_fixture
    data = geojson_export.export(solution, area.crs)
    lon, lat = data["features"][0]["geometry"]["coordinates"][0]

    assert -180 <= lon <= 180 and -90 <= lat <= 90


def test_dxf_export_opens_and_has_layers(solution_fixture):
    solution, area, building = solution_fixture
    payload = dxf_export.export(solution, area, building)

    import io

    document = ezdxf.read(io.StringIO(payload.decode("utf-8")))
    names = {layer.dxf.name for layer in document.layers}
    assert {"ТС_ТРАССА", "ТС_ВРЕЗКА", "ЗДАНИЕ_ПОДКЛЮЧАЕМОЕ"} <= names

    model = document.modelspace()
    routes = [e for e in model if e.dxf.layer == "ТС_ТРАССА" and e.dxftype() == "LWPOLYLINE"]
    assert routes


def test_dxf_uses_metres_not_degrees(solution_fixture):
    """В CAD должны быть метры, иначе чертёж бесполезен."""
    solution, area, building = solution_fixture
    payload = dxf_export.export(solution, area, building)

    import io

    document = ezdxf.read(io.StringIO(payload.decode("utf-8")))
    assert document.header["$INSUNITS"] == 6


def test_pdf_export_is_a_pdf(solution_fixture):
    solution, _, building = solution_fixture
    payload = pdf_export.export(solution, building, area_name="test")

    assert payload.startswith(b"%PDF")
    assert len(payload) > 10_000


def test_pdf_contains_the_key_tables(solution_fixture):
    """Протяжённость по диаметрам — вход в расчёт платы, она обязана быть."""
    solution, _, building = solution_fixture
    payload = pdf_export.export(solution, building, area_name="test")

    from pypdf import PdfReader
    import io

    text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(payload)).pages)

    assert "Протяжённость проектируемых сетей" in text
    assert f"Ду{solution.du_mm}" in text
    assert "Плата за подключение" in text
    assert "Протокол нормоконтроля" in text
    assert "не является проектной документацией" in text
