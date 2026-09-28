"""Протокол нормоконтроля по векторной трассе.

Растр отвечал за поиск. Проверка идёт по итоговой геометрии — иначе мы
проверяли бы собственную дискретизацию, а не реальные расстояния
(docs/04-algorithm.md §8).

Главное разделение:
  трасса ИДЁТ ВДОЛЬ объекта → правило по горизонтали (табл. А.3);
  трасса ПЕРЕСЕКАЕТ объект  → правило по вертикали (табл. А.1), это разрешено.

Различаются они по длине участка вхождения трассы в нормативный коридор
объекта: короткий заход — пересечение или подход к нему, длинный — следование.
Проверять «пересекает ли геометрия» напрямую нельзя: подход к переходу через
улицу неизбежно проходит вплотную к бортовому камню, и посегментная проверка
объявляла бы это нарушением.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from shapely.geometry import LineString, MultiLineString, Point
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from ..config import get_config
from ..domain.enums import ComplianceStatus, Laying
from ..domain.models import AreaModel, Building
from . import rules
from .rules import Clearance, RuleContext

SEARCH_MARGIN = 1.5        # во сколько раз шире норматива ищем соседей
REPORT_NEAR_RATIO = 1.6    # проходящие проверки показываем, если запас невелик


@dataclass
class Check:
    """Одна проверка на одном участке трассы."""

    start_m: float
    end_m: float
    kind: str                    # horizontal | vertical
    rule_id: str
    title: str
    clause: str
    target_id: str
    required_m: float
    actual_m: float
    status: ComplianceStatus
    note: str | None = None

    @property
    def margin_m(self) -> float:
        return self.actual_m - self.required_m

    @property
    def where(self) -> str:
        if self.end_m - self.start_m < 1.0:
            return f"{self.start_m:.0f} м"
        return f"{self.start_m:.0f}–{self.end_m:.0f} м"


@dataclass
class ComplianceReport:
    status: ComplianceStatus
    checks: list[Check] = dc_field(default_factory=list)
    checks_total: int = 0
    length_m: float = 0.0

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status is ComplianceStatus.FAIL]

    @property
    def conditionals(self) -> list[Check]:
        return [c for c in self.checks if c.status is ComplianceStatus.CONDITIONAL]

    def summary(self) -> dict:
        return {
            "status": self.status.value,
            "checks_total": self.checks_total,
            "reported": len(self.checks),
            "failures": len(self.failures),
            "conditionals": len(self.conditionals),
        }


def _setting(key: str) -> float:
    return float(get_config().normatives["compliance"][key])


def _status(actual_m: float, required_m: float) -> ComplianceStatus:
    """pass / conditional / fail.

    СП допускает уменьшение расстояний в стеснённых условиях при защитных
    мероприятиях (Приложение Д), поэтому небольшое отступление — не отказ,
    а `conditional`: можно, но нужны мероприятия и это стоит денег.
    """
    if actual_m >= required_m:
        return ComplianceStatus.PASS
    if actual_m >= required_m * rules.relaxation_ratio():
        return ComplianceStatus.CONDITIONAL
    return ComplianceStatus.FAIL


# --- Глубины ----------------------------------------------------------------


def pipe_depth_range(ctx: RuleContext) -> tuple[float, float]:
    """Верх и низ конструкции тепловой сети от поверхности, м."""
    depths = get_config().normatives["burial_depth_min_m"]
    if ctx.laying is Laying.CHANNELLESS:
        top = float(depths["channelless_top"])
        height = ctx.du_mm / 1000.0 + 0.20      # оболочка ППУ
    else:
        top = float(depths["channel_top"])
        height = ctx.du_mm / 1000.0 + 0.60      # лоток с перекрытием
    return top, top + height


def vertical_clearance(ctx: RuleContext, other_depth_m: float) -> float:
    """Просвет по вертикали между нашей конструкцией и чужой сетью."""
    top, bottom = pipe_depth_range(ctx)
    if other_depth_m > bottom:
        return other_depth_m - bottom
    if other_depth_m < top:
        return top - other_depth_m
    return 0.0


def resolve_crossing(
    ctx: RuleContext, other_depth_m: float, required_m: float
) -> tuple[float, ComplianceStatus, str | None]:
    """Разрешим ли конфликт по вертикали местным заглублением.

    В проекте пересечение решают заглублением или футляром, а не переносом
    трассы, поэтому конфликт — `conditional` с указанием требуемой глубины.
    """
    top, bottom = pipe_depth_range(ctx)
    actual = vertical_clearance(ctx, other_depth_m)
    if actual >= required_m:
        return actual, ComplianceStatus.PASS, None

    height = bottom - top
    minimum_burial = float(get_config().normatives["burial_depth_min_m"]["channel_top"])

    options: list[tuple[float, str]] = []
    deepening = (other_depth_m + required_m) - top
    if deepening > 0:
        options.append((deepening, f"местное заглубление на {deepening:.2f} м (проход ниже)"))
    raised_top = other_depth_m - required_m - height
    if raised_top >= minimum_burial:
        options.append((top - raised_top, f"проход выше, верх на {raised_top:.2f} м"))

    if not options:
        return actual, ComplianceStatus.FAIL, "разойтись по вертикали невозможно"

    adjustment, note = min(options, key=lambda item: item[0])
    if adjustment > _setting("max_local_deepening_m"):
        return actual, ComplianceStatus.FAIL, f"требуется {note}, это слишком глубоко"
    return actual, ComplianceStatus.CONDITIONAL, f"требуется {note}"


# --- Разбор вхождений в коридор ---------------------------------------------


def _rail_target(railway) -> str:
    """Какое правило СП применимо к этому пути."""
    if railway.electrified:
        return "RAIL_ELECTRIFIED"       # 10,75 м от оси
    if railway.gauge_mm and railway.gauge_mm < 1000:
        return "RAIL_750"               # 2,8 м
    return "RAIL_1520"                  # 4,0 м


def _pieces(geometry: BaseGeometry) -> list[LineString]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, MultiLineString):
        return list(geometry.geoms)
    return [g for g in getattr(geometry, "geoms", []) if isinstance(g, LineString)]


def _span(line: LineString, piece: LineString) -> tuple[float, float]:
    start = line.project(Point(piece.coords[0]))
    end = line.project(Point(piece.coords[-1]))
    return (start, end) if start <= end else (end, start)


def _check_linear_object(
    line: LineString,
    target_id: str,
    target_geom: BaseGeometry,
    horizontal_rule: Clearance,
    ctx: RuleContext,
    *,
    vertical_rule: Clearance | None = None,
    depth_m: float | None = None,
) -> list[Check]:
    """Проверить трассу против одного линейного объекта."""
    corridor = target_geom.buffer(horizontal_rule.min_m)
    pieces = _pieces(line.intersection(corridor))
    if not pieces:
        return []

    # Порог обязан масштабироваться от норматива. Перпендикулярное пересечение
    # коридора шириной 2 × норматив даёт участок ровно такой длины, под углом —
    # длиннее. При отступе 4 м от оси ж/д это 8 м, и фиксированный порог 8 м
    # объявлял бы каждое пересечение железной дороги следованием вдоль неё.
    threshold = max(_setting("following_threshold_m"), 3.0 * horizontal_rule.min_m)
    result: list[Check] = []

    for piece in pieces:
        start_m, end_m = _span(line, piece)

        if piece.length > threshold:
            # Следование вдоль — правило по горизонтали
            actual = target_geom.distance(piece)
            result.append(
                Check(
                    start_m=round(start_m, 1), end_m=round(end_m, 1),
                    kind="horizontal", rule_id=horizontal_rule.rule_id,
                    title=horizontal_rule.title, clause=horizontal_rule.clause,
                    target_id=target_id, required_m=horizontal_rule.min_m,
                    actual_m=round(actual, 2), status=_status(actual, horizontal_rule.min_m),
                    note=horizontal_rule.note,
                )
            )
            continue

        # Пересечение или подход к нему — правило по вертикали
        if vertical_rule is None or depth_m is None:
            continue
        actual, status, note = resolve_crossing(ctx, depth_m, vertical_rule.min_m)
        result.append(
            Check(
                start_m=round(start_m, 1), end_m=round(end_m, 1),
                kind="vertical", rule_id=vertical_rule.rule_id,
                title=vertical_rule.title, clause=vertical_rule.clause,
                target_id=target_id, required_m=vertical_rule.min_m,
                actual_m=round(actual, 2), status=status, note=note,
            )
        )

    return result


# --- Основная проверка ------------------------------------------------------


def check_route(
    area: AreaModel,
    line: LineString,
    ctx: RuleContext,
    *,
    target_building: Building | None = None,
) -> ComplianceReport:
    """Прогнать готовую трассу по всем применимым правилам."""
    collected: list[Check] = []
    total = 0

    # --- Здания: пересечь нельзя, только идти вдоль ---
    others = [
        b for b in area.buildings
        if target_building is None or b.id != target_building.id
    ]
    building_rule = rules.horizontal("BUILDING", ctx)
    if building_rule and others:
        tree = STRtree([b.geom for b in others])
        probe = line.buffer(building_rule.min_m * SEARCH_MARGIN)
        for hit in tree.query(probe):
            other = others[hit]
            distance = other.geom.distance(line)
            if distance > building_rule.min_m * SEARCH_MARGIN:
                continue
            total += 1
            nearest = line.project(Point(other.geom.centroid))
            collected.append(
                Check(
                    start_m=round(nearest, 1), end_m=round(nearest, 1),
                    kind="horizontal", rule_id=building_rule.rule_id,
                    title=building_rule.title, clause=building_rule.clause,
                    target_id=other.id, required_m=building_rule.min_m,
                    actual_m=round(distance, 2),
                    status=_status(distance, building_rule.min_m),
                )
            )

    # --- Зелёные насаждения ---
    #
    # Дерево — точечный объект, «идти вдоль» него нельзя, поэтому проверка
    # такая же простая, как для зданий: минимальное расстояние до ствола.
    if area.greenery:
        trees = [g for g in area.greenery if g.is_tree]
        shrubs = [g for g in area.greenery if not g.is_tree]
        for group, target in ((trees, "TREE"), (shrubs, "SHRUB")):
            rule = rules.horizontal(target, ctx)
            if not rule or not group:
                continue
            tree = STRtree([g.geom for g in group])
            for hit in tree.query(line.buffer(rule.min_m * SEARCH_MARGIN)):
                green = group[hit]
                distance = green.geom.distance(line)
                if distance > rule.min_m * SEARCH_MARGIN:
                    continue
                total += 1
                position = line.project(green.geom.centroid)
                collected.append(
                    Check(
                        start_m=round(position, 1), end_m=round(position, 1),
                        kind="horizontal", rule_id=rule.rule_id, title=rule.title,
                        clause=rule.clause, target_id=green.id,
                        required_m=rule.min_m, actual_m=round(distance, 2),
                        status=_status(distance, rule.min_m),
                        note=(
                            "требуется снос с компенсационной стоимостью озеленения"
                            if distance < rule.min_m else None
                        ),
                    )
                )

    # --- Чужие сети ---
    if area.utilities:
        tree = STRtree([u.geom for u in area.utilities])
        for hit in tree.query(line.buffer(5.0)):
            utility = area.utilities[hit]
            horizontal_rule = rules.for_utility(utility, ctx)
            if horizontal_rule is None:
                continue
            checks = _check_linear_object(
                line, utility.id, utility.geom, horizontal_rule, ctx,
                vertical_rule=rules.vertical(utility.kind.value),
                depth_m=utility.depth_m,
            )
            total += len(checks)
            collected.extend(checks)

    # --- Бортовой камень ---
    kerb_rule = rules.horizontal("KERB", ctx)
    if kerb_rule and area.roads:
        carriageways = [(r, r.geom.buffer(r.width_m / 2.0)) for r in area.roads]
        tree = STRtree([poly for _, poly in carriageways])
        for hit in tree.query(line.buffer(kerb_rule.min_m * SEARCH_MARGIN)):
            road, carriageway = carriageways[hit]
            checks = _check_linear_object(
                line, road.name or road.id, carriageway.exterior, kerb_rule, ctx,
                vertical_rule=rules.vertical("ROAD_CATEGORY_I_III")
                if road.requires_closed_crossing else None,
                depth_m=0.0 if road.requires_closed_crossing else None,
            )
            total += len(checks)
            collected.extend(checks)

    # --- Железные дороги ---
    #
    # Правило выбирается по типу пути, а не берётся одно на всех: у
    # электрифицированной дороги отступ 10,75 м против 4,0 м у обычной, у
    # узкой колеи — 2,8 м. Раньше ко всем применялся RAIL_1520, из-за чего
    # признак электрификации читался из данных и никуда не шёл.
    if area.railways:
        tree = STRtree([r.geom for r in area.railways])
        widest = rules.horizontal("RAIL_ELECTRIFIED", ctx)
        probe = line.buffer((widest.min_m if widest else 4.0) * SEARCH_MARGIN)
        for hit in tree.query(probe):
            railway = area.railways[hit]
            target = _rail_target(railway)
            rail_rule = rules.horizontal(target, ctx)
            if rail_rule is None:
                continue
            checks = _check_linear_object(
                line, railway.id, railway.geom, rail_rule, ctx,
                vertical_rule=rules.vertical(target) or rules.vertical("RAIL_1520"),
                depth_m=0.0,
            )
            total += len(checks)
            collected.extend(checks)

    return ComplianceReport(
        status=_overall(collected),
        checks=_select_reported(collected),
        checks_total=total,
        length_m=round(line.length, 1),
    )


def _overall(checks: list[Check]) -> ComplianceStatus:
    if any(c.status is ComplianceStatus.FAIL for c in checks):
        return ComplianceStatus.FAIL
    if any(c.status is ComplianceStatus.CONDITIONAL for c in checks):
        return ComplianceStatus.CONDITIONAL
    return ComplianceStatus.PASS


def _select_reported(checks: list[Check]) -> list[Check]:
    """В протокол идут все нарушения и самая тугая из проходящих проверок по правилу.

    Показывать все несколько сотен «до дома 40 м при норме 2 м» бессмысленно:
    согласующий инженер смотрит на нарушения и на то, где запас минимален.
    """
    reported = [c for c in checks if c.status is not ComplianceStatus.PASS]

    tightest: dict[str, Check] = {}
    for check in checks:
        if check.status is not ComplianceStatus.PASS:
            continue
        if check.actual_m > check.required_m * REPORT_NEAR_RATIO:
            continue
        current = tightest.get(check.rule_id)
        if current is None or check.margin_m < current.margin_m:
            tightest[check.rule_id] = check

    reported.extend(tightest.values())
    return sorted(reported, key=lambda c: (c.status is ComplianceStatus.PASS, c.margin_m))
