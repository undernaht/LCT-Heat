"""Экспорт варианта в DXF для CAD.

Координаты — в метрической проекции района (UTM), а не в градусах: в CAD должны
быть метры, иначе чертёж бесполезен. Слои названы по-русски, как в проектной
практике, чтобы файл открывался понятным.
"""

from __future__ import annotations

import io

import ezdxf
from ezdxf.enums import TextEntityAlignment

from ..domain.models import AreaModel, Building
from ..routing.alternatives import variant_id_of
from ..routing.solver import RouteSolution

# (имя слоя, цвет ACI, описание)
LAYERS: list[tuple[str, int, str]] = [
    ("ТС_ТРАССА", 1, "Проектируемая тепловая сеть"),
    ("ТС_УЗЛЫ", 30, "Тепловые камеры"),
    ("ТС_ВРЕЗКА", 2, "Точка подключения"),
    ("ТС_СУЩЕСТВУЮЩАЯ", 6, "Существующие тепловые сети"),
    ("СЕТИ_ВОДОПРОВОД", 5, "Водопровод"),
    ("СЕТИ_КАНАЛИЗАЦИЯ", 8, "Канализация"),
    ("СЕТИ_ГАЗ", 2, "Газопровод"),
    ("СЕТИ_КАБЕЛИ", 1, "Силовые кабели и связь"),
    ("ЗДАНИЕ_ПОДКЛЮЧАЕМОЕ", 3, "Перспективное здание"),
    ("ЗДАНИЯ", 9, "Существующая застройка"),
    ("ДОРОГИ", 253, "Улично-дорожная сеть"),
    ("НОРМОКОНТРОЛЬ", 6, "Замечания нормоконтроля"),
]

UTILITY_LAYER = {
    "WATER": "СЕТИ_ВОДОПРОВОД",
    "SEWER": "СЕТИ_КАНАЛИЗАЦИЯ",
    "STORM": "СЕТИ_КАНАЛИЗАЦИЯ",
    "GAS": "СЕТИ_ГАЗ",
    "POWER": "СЕТИ_КАБЕЛИ",
    "COMM": "СЕТИ_КАБЕЛИ",
}

CONTEXT_RADIUS_M = 60.0     # сколько окружения тащим в чертёж


def export(
    solution: RouteSolution,
    area: AreaModel,
    building: Building,
    *,
    context_radius_m: float = CONTEXT_RADIUS_M,
) -> bytes:
    document = ezdxf.new("R2010", setup=True)
    document.header["$INSUNITS"] = 6      # метры
    model = document.modelspace()

    for name, colour, description in LAYERS:
        layer = document.layers.add(name, color=colour)
        # Описание слоя живёт в расширенных данных и поддерживается не везде —
        # если не вышло, слой всё равно валиден.
        try:
            layer.description = description
        except AttributeError:
            pass

    corridor = solution.geometry.buffer(context_radius_m)

    # --- Окружение ---
    for road in area.roads:
        if road.geom.intersects(corridor):
            _add_line(model, road.geom, "ДОРОГИ")

    for other in area.buildings:
        if other.id == building.id or not other.geom.intersects(corridor):
            continue
        _add_polygon(model, other.geom, "ЗДАНИЯ")

    for utility in area.utilities:
        if not utility.geom.intersects(corridor):
            continue
        _add_line(model, utility.geom, UTILITY_LAYER.get(utility.kind.value, "СЕТИ_КАБЕЛИ"))

    for edge in area.heat.edges:
        if edge.geom.intersects(corridor):
            _add_line(model, edge.geom, "ТС_СУЩЕСТВУЮЩАЯ")

    # --- Подключаемое здание ---
    _add_polygon(model, building.geom, "ЗДАНИЕ_ПОДКЛЮЧАЕМОЕ")
    centroid = building.geom.centroid
    _add_text(
        model,
        f"{building.id}  Q = {solution.q_gcal_h:.3f} Гкал/ч",
        (centroid.x, centroid.y),
        "ЗДАНИЕ_ПОДКЛЮЧАЕМОЕ",
        height=3.0,
    )

    # --- Проектируемая трасса ---
    polyline = model.add_lwpolyline(
        [(x, y) for x, y in solution.geometry.coords], dxfattribs={"layer": "ТС_ТРАССА"}
    )
    polyline.dxf.const_width = 0.6

    midpoint = solution.geometry.interpolate(solution.geometry.length / 2.0)
    laying = "канальная" if solution.laying.value == "channel" else "бесканальная"
    _add_text(
        model,
        f"Ду{solution.du_mm}, {laying} прокладка, L = {solution.length_m:.0f} м",
        (midpoint.x, midpoint.y + 4.0),
        "ТС_ТРАССА",
        height=3.0,
    )

    # --- Камеры по нормативному интервалу ---
    interval = solution.geometry.length / max(solution.cost.chambers_count, 1)
    for index in range(1, solution.cost.chambers_count + 1):
        point = solution.geometry.interpolate(min(index * interval, solution.geometry.length))
        model.add_circle((point.x, point.y), radius=1.5, dxfattribs={"layer": "ТС_УЗЛЫ"})

    # --- Точка врезки ---
    tap = solution.connection.point
    model.add_circle((tap.x, tap.y), radius=2.5, dxfattribs={"layer": "ТС_ВРЕЗКА"})
    model.add_line(
        (tap.x - 4, tap.y), (tap.x + 4, tap.y), dxfattribs={"layer": "ТС_ВРЕЗКА"}
    )
    model.add_line(
        (tap.x, tap.y - 4), (tap.x, tap.y + 4), dxfattribs={"layer": "ТС_ВРЕЗКА"}
    )
    _add_text(
        model,
        f"Врезка {solution.connection.node_name or solution.connection.edge_id} "
        f"(Ду{solution.connection.du_mm})",
        (tap.x + 5.0, tap.y + 5.0),
        "ТС_ВРЕЗКА",
        height=3.0,
    )

    # --- Замечания нормоконтроля прямо на чертеже ---
    for check in solution.compliance.checks:
        if check.status.value == "pass":
            continue
        point = solution.geometry.interpolate((check.start_m + check.end_m) / 2.0)
        model.add_circle((point.x, point.y), radius=2.0, dxfattribs={"layer": "НОРМОКОНТРОЛЬ"})
        _add_text(
            model,
            f"{check.title}: норма {check.required_m:.2f}, факт {check.actual_m:.2f} "
            f"(СП 124.13330.2012, {check.clause})",
            (point.x + 3.0, point.y - 3.0),
            "НОРМОКОНТРОЛЬ",
            height=2.0,
        )

    _add_title_block(model, solution, building, area.crs)

    stream = io.StringIO()
    document.write(stream)
    return stream.getvalue().encode("utf-8")


def _add_line(model, geom, layer: str) -> None:
    coords = [(x, y) for x, y in geom.coords]
    if len(coords) >= 2:
        model.add_lwpolyline(coords, dxfattribs={"layer": layer})


def _add_polygon(model, geom, layer: str) -> None:
    coords = [(x, y) for x, y in geom.exterior.coords]
    if len(coords) >= 3:
        model.add_lwpolyline(coords, close=True, dxfattribs={"layer": layer})


def _add_text(model, text: str, position: tuple[float, float], layer: str, *, height: float) -> None:
    entity = model.add_text(text, dxfattribs={"layer": layer, "height": height})
    entity.set_placement(position, align=TextEntityAlignment.LEFT)


def _add_title_block(model, solution: RouteSolution, building: Building, crs_name: str) -> None:
    """Штамп: без него чертёж не читается в отрыве от сервиса."""
    minx, miny, _, _ = solution.geometry.bounds
    x = minx - 40.0
    y = miny - 20.0

    lines = [
        "ПОДКЛЮЧЕНИЕ К ТЕПЛОВЫМ СЕТЯМ — схема трассировки",
        f"Объект: {building.id}, {building.floors or '—'} эт., "
        f"нагрузка {solution.q_gcal_h:.3f} Гкал/ч",
        f"Вариант: {variant_id_of(solution)} ({solution.profile})",
        f"Трасса: L = {solution.length_m:.0f} м, Ду{solution.du_mm}, "
        f"график {solution.schedule}",
        f"Гидравлика: v = {solution.hydraulics.v_m_s:.2f} м/с, "
        f"R = {solution.hydraulics.r_pa_m:.0f} Па/м, "
        f"dP = {solution.hydraulics.dp_bar:.2f} бар",
        f"Точка подключения: {solution.connection.node_name or solution.connection.edge_id}",
        f"Нормоконтроль: {solution.compliance.status.value}, "
        f"нарушений {len(solution.compliance.failures)}, "
        f"условно {len(solution.compliance.conditionals)}",
        f"Система координат: {crs_name}",
        "Сформировано автоматически. Не является проектной документацией.",
    ]

    for index, line in enumerate(lines):
        height = 4.0 if index == 0 else 2.5
        _add_text(model, line, (x, y - index * 5.0), "ТС_ТРАССА", height=height)
