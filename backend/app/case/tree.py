"""Дерево подключения всех точек: жадное наращивание.

Точки обрабатываются по очереди. Каждая ищет самый дешёвый (в единицах S)
способ присоединиться: к существующей сети, к существующей камере или к уже
построенным веткам. Так общие участки возникают сами: если ветка соседа рядом,
присоединиться к ней дешевле, чем тянуть свою до магистрали, — и показатель
это поощряет, потому что длина общего участка считается один раз.

Порядок точек влияет на результат; варианты строятся из разных порядков
(solve.py) и сравниваются по S; локальный поиск — improve.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field as dc_field
from typing import Any

from shapely.geometry import GeometryCollection, LineString, Point

from . import costing, diameters
from .field import CaseField
from .geometry import Obstacles, remove_short_segments, soften_sharp_turns, straighten, turn_angle_deg
from .model import CaseInput
from .network import (
    NODE_CHAMBER_EXISTING,
    NODE_CHAMBER_NEW,
    NODE_OKS,
    SNAP_TO_VERTEX_M,
    Network,
    Node,
    Segment,
    snap_point_to_line,
)
from .route import JoinOption, TreeIndex, Wave
from .targets import ApproachTarget

Point2 = tuple[float, float]

# Поворот у выхода из зоны ОКС круче 90°: до этого угла — срезка угла,
# круче — П-образный выход (наружу, вбок, дальше по маршруту).
CHORD_MAX_TURN_DEG = 150.0
CHORD_M = 4.0
SIDE_STEP_M = (6.0, 12.0)


# Сколько запасных выходов из полигона сравнивать по S
FALLBACK_PLANS = 3
# Сколько раз искать другое место присоединения, если фиксация не удалась
RETRY_ROUNDS = 2
# Присоединение ближе этого к камере дерева уходит в саму камеру (§9.7: сходящиеся
# ветки объединяются в общий участок с одной камерой)
MERGE_CHAMBERS_M = 3.0
# Перестройка ветки: окно волны по длине нынешней ветки — дальше улучшения не ищем
REBUILD_RADIUS_FACTOR = 1.5
REBUILD_RADIUS_EXTRA_M = 30.0
REBUILD_RADIUS_MIN_M = 60.0
# Наложение новой ветки на другую ветку или сеть длиннее этого — отказ от плана
OVERLAP_TOLERANCE_M = 0.05
# Перестройка ветки принимается только при выигрыше не меньше этого (в единицах S)
MIN_GAIN_S = 1e-4


@dataclass
class _Plan:
    target: ApproachTarget
    points: list[Point2]
    join: JoinOption
    du: int
    score: float
    single_leg: bool = False            # ветка = только финальный участок (цель на стволе)
    leg_from: Point2 | None = None


@dataclass
class BuildResult:
    network: Network
    unconnected: list[Any]                     # id точек без маршрута
    reasons: dict[Any, str] = dc_field(default_factory=dict)
    joins: dict[str, JoinOption] = dc_field(default_factory=dict)   # oks node id → как присоединились
    notes: dict[str, Any] = dc_field(default_factory=dict)


class TreeBuilder:
    def __init__(self, case: CaseInput, field: CaseField):
        self.case = case
        self.field = field
        self.rules = field.rules
        self.net = Network()
        self.index = TreeIndex(field)
        self.wave = Wave(case, field, self.index)
        self.existing_nodes: dict[Any, Node] = {}   # id существующей камеры → узел
        self.unconnected: list[Any] = []
        self.reasons: dict[Any, str] = {}
        self.joins: dict[str, JoinOption] = {}
        self.flow_by_oks_node: dict[str, float] = {}
        self.step_of_oks: dict[str, int] = {}      # узел точки → шаг, на котором построена её ветка
        self._radius_hint: float | None = None     # окно волны при перестройке ветки (по её длине)
        self._last_attempt = None

    # --- публичное ---

    def build(self, targets: list[list[ApproachTarget]]) -> BuildResult:
        for candidates in targets:
            self.connect(candidates)
        return self.result()

    def result(self) -> BuildResult:
        return BuildResult(
            network=self.net, unconnected=list(self.unconnected), reasons=dict(self.reasons),
            joins=dict(self.joins), notes={"examined": sum(1 for _ in self.joins)},
        )

    def proxy_score(self) -> float:
        """S дерева до спецпроходов и выгрузки — для сравнения локальных перестроек."""
        diameters.assign(self.net, self.rules, self.flow_by_oks_node)
        return costing.price(self.net, self.case, self.rules, self.unconnected).score

    def rebuild_leaf(self, oks_node_id: str, candidates: list[ApproachTarget]) -> tuple[bool, float, float]:
        """Снять собственную ветку точки и проложить её заново при остальных ветках как есть.

        Жадное дерево строит ветку, когда соседних веток ещё нет; после того как
        они появились, у точки может найтись присоединение дешевле. Ветка снимается
        до первого узла, где к ней примыкает что-то ещё (или до точки присоединения),
        точка подключается заново той же процедурой, и результат принимается,
        только если S дерева уменьшился; иначе всё откатывается к снимку.
        Возвращает (принято, S до, S после попытки).
        """
        before = self.proxy_score()
        snap = self._snapshot()
        oks_node = self.net.nodes[oks_node_id]
        own_length = sum(s.length for s in self._own_branch(oks_node))
        self._detach(oks_node)
        # Ищем только то, что может быть дешевле нынешней ветки: окно волны — по её длине
        self._radius_hint = max(REBUILD_RADIUS_MIN_M, own_length * REBUILD_RADIUS_FACTOR + REBUILD_RADIUS_EXTRA_M)
        try:
            connected = self.connect(candidates)
        finally:
            self._radius_hint = None
        if connected:
            after = self.proxy_score()
            if after < before - MIN_GAIN_S:
                return True, before, after
        else:
            after = math.inf
        self._restore(snap)
        # Ветка проверена при нынешнем окружении: снова смотреть её стоит, только
        # если рядом появится что-то новое (шаг больше нынешнего)
        self.net.step += 1
        self.step_of_oks[oks_node_id] = self.net.step
        return False, before, after

    def _own_branch(self, oks_node: Node) -> list[Segment]:
        """Участки от точки до первого узла, который держит что-то ещё."""
        net = self.net
        own: list[Segment] = []
        node = oks_node
        incident = net.incident(node.id)
        while incident:
            segment = incident[0]
            own.append(segment)
            node = net.nodes[segment.other(node.id)]
            if node.is_tie_in_point or node.kind == NODE_OKS or net.degree(node.id) != 2:
                break
            incident = [s for s in net.incident(node.id) if s.id != segment.id]
        return own

    def connect(self, candidates: list[ApproachTarget], *, evaluate: bool = True) -> bool:
        """Подключить точку, перебирая кандидатов выхода из её полигона.

        Ближайшая граница, если она проходима, берётся всегда — так велит
        приложение. Если она отвергнута, среди запасных кандидатов выбирается
        лучший по S, а не первый попавшийся: «ближайшая достижимая» — уже наша
        трактовка, и внутри неё нет причин брать дорогой выход во двор.

        `evaluate` — сравнивать запасные планы не по оценке маршрута, а по S
        всего дерева после пробной фиксации: присоединение в другом месте
        меняет Ду и камеры соседей, оценка маршрута этого не видит. Стоит
        доли секунды на план, на конкурсном наборе даёт −0,3…0,5 % S.
        """
        oks = candidates[0].oks
        spec = self.rules.du_for_flow(oks.flow_tph)
        if spec is None:
            self._fail(oks.id, "расход выше пропускной способности Ду1400")
            return False
        du = spec.du
        self.net.step += 1

        reasons: list[str] = []
        excluded: list[Point2] = []
        for _round in range(RETRY_ROUNDS + 1):
            plans: list[_Plan] = []
            for target in candidates:
                outcome = self._plan_via(target, du, excluded)
                if isinstance(outcome, str):
                    reasons.append(outcome)
                    continue
                if target.is_nearest:
                    plans = [outcome]
                    break
                plans.append(outcome)
                if len(plans) >= FALLBACK_PLANS:
                    break
            if not plans:
                break
            for plan in self._rank_plans(plans, evaluate):
                if self._commit(plan):
                    return True
            # Ни одну точку присоединения не удалось зафиксировать (переполнилась
            # камера, не вышел угол) — исключаем эти места и ищем другие
            excluded.extend(plan.points[-1] for plan in plans)
            reasons.append("точка присоединения потеряла допустимость")
        self._fail(oks.id, "; ".join(dict.fromkeys(reasons)) or "нет достижимой цели")
        return False

    def _rank_plans(self, plans: list[_Plan], evaluate: bool) -> list[_Plan]:
        """Порядок фиксации планов: по оценке маршрута или по S дерева после пробной фиксации."""
        by_estimate = sorted(plans, key=lambda p: p.score)
        if not evaluate or len(plans) < 2:
            return by_estimate
        scored: list[tuple[float, int, _Plan]] = []
        for index, plan in enumerate(by_estimate):
            snap = self._snapshot()
            actual = self.proxy_score() if self._commit(plan) else math.inf
            self._restore(snap)
            scored.append((actual, index, plan))
        scored.sort()
        return [plan for _, _, plan in scored]

    def _plan_via(self, target: ApproachTarget, du: int, exclude: list[Point2] | None = None):
        """Маршрут от цели до присоединения без изменения дерева."""
        oks = target.oks
        attempt = self.wave.run(target.target, du, oks.flow_tph, exclude=exclude, radius_m=self._radius_hint)
        if attempt is None:
            return "волна не достигла ни сети, ни дерева"
        self._last_attempt = attempt

        obstacles = self._obstacles()
        points = straighten(self.field, attempt.points, obstacles)
        points = soften_sharp_turns(points, obstacles)
        if len(points) < 2:
            # Цель легла прямо на ствол дерева или сеть: вся ветка — один
            # финальный участок, камера ставится в его конце
            if target.final_leg is None or attempt.join.kind not in ("branch", "network", "branch_node", "chamber"):
                return "вырожденный маршрут"
            oks_point = (oks.geom.x, oks.geom.y)
            score = self.rules.score_per_m(du) * target.final_leg.length + attempt.join.join_score
            return _Plan(target=target, points=[points[0]], join=attempt.join, du=du, score=score, single_leg=True,
                         leg_from=oks_point)

        # Поворот в точке выхода из зоны ОКС: финальный участок прямой, значит
        # угол между ним и первым отрезком трассы тоже должен быть ≤ 90°.
        if target.final_leg is not None:
            fixed = self._fix_exit_turn(target, points, obstacles, du)
            if fixed is None:
                return "у выхода из зоны ОКС не получается поворот ≤ 90°"
            points, attempt = fixed

        points = remove_short_segments(
            points, obstacles, min_segment_m=float(self.rules.search["min_segment_m"]),
            max_turn_deg=self.rules.max_turn_deg,
        )
        # Оценка в единицах S: сама трасса по факту плюс узел присоединения
        length = sum(math.dist(a, b) for a, b in zip(points, points[1:]))
        if target.final_leg is not None:
            length += target.final_leg.length
        score = self.rules.score_per_m(du) * length + attempt.join.join_score
        return _Plan(target=target, points=points, join=attempt.join, du=du, score=score)

    def _commit(self, plan: "_Plan") -> bool:
        target, points, oks, du = plan.target, list(plan.points), plan.target.oks, plan.du
        if plan.single_leg and plan.leg_from is not None:
            # Угол между финальным участком и стволом проверяем как для обычной ветки,
            # но сдвигать точку нельзя: участок обязан идти от ближайшей границы
            aligned = self._align_branch_join([plan.leg_from, points[-1]], plan.join, final_leg=True) \
                if plan.join.kind in ("branch", "branch_node") else [plan.leg_from, points[-1]]
            if aligned is None or len(aligned) != 2 or math.dist(aligned[-1], points[-1]) > 1.0:
                return False
            points = [aligned[-1]]
        elif plan.join.kind in ("branch", "branch_node"):
            points = self._align_branch_join(points, plan.join)
            if points is None:
                return False
            # Выравнивание сдвинуло конец ветки: у короткой ветки это меняет и
            # угол у выхода из зоны ОКС — проверяем заново
            if target.final_leg is not None:
                points = self._chord_at_exit(target, points)
                if points is None:
                    return False
        nodes_before = set(self.net.nodes)
        join_node = self._attach(plan.join, points[-1], du)
        if join_node is None:
            return False
        created = join_node.id not in nodes_before
        shifted = math.dist(points[-1], join_node.point)
        points[-1] = join_node.point

        oks_point = (oks.geom.x, oks.geom.y)
        full = ([oks_point] + points) if target.final_leg is not None else points
        full = _dedupe(full)
        # Конец финального участка ушёл в соседнюю камеру дальше допуска —
        # участок перестал идти от ближайшей границы
        if plan.single_leg and shifted > 1.0:
            self._undo_attach(plan.join, join_node, created=created)
            return False
        if len(full) < 2 or self._overlaps_existing(full):
            self._undo_attach(plan.join, join_node, created=created)
            return False
        oks_node = self.net.add_node(oks_point, NODE_OKS, ref=oks.id, approach_rule=target.approach_rule)
        segment = self.net.add_segment(full, oks_node.id, join_node.id)
        # Конец обычной ветки ушёл в соседнюю камеру: угол в ней не проверялся —
        # чинится подходом под 45° сразу, а не ремонтом всего дерева
        if shifted > 1e-6 and not self._fix_turn_at_join(segment, join_node):
            self.net.remove_segment(segment.id)
            self.net.remove_node(oks_node.id)
            self._undo_attach(plan.join, join_node, created=created)
            return False
        self.joins[oks_node.id] = plan.join
        self.flow_by_oks_node[oks_node.id] = oks.flow_tph
        self.step_of_oks[oks_node.id] = self.net.step

        # Ду назначаются сразу: волна следующей точки должна видеть настоящую
        # стоимость камеры на ветке, а она зависит от Ду ветки.
        diameters.assign(self.net, self.rules, self.flow_by_oks_node)
        self._refresh_index()
        return True

    # --- внутреннее ---

    def _overlaps_existing(self, points: list[Point2]) -> bool:
        """Новая ветка не может лежать на другой ветке или на существующей сети
        (касание в узле — точка, наложение — отрезок)."""
        line = LineString(points)
        if line.intersection(self.field.network_geom).length > OVERLAP_TOLERANCE_M:
            return True
        return any(line.intersection(s.line).length > OVERLAP_TOLERANCE_M for s in self.net.segments.values())

    def _undo_attach(self, join: JoinOption, node: Node, *, created: bool = True) -> None:
        """Откатить узел присоединения, созданный `_attach`, если ветка не состоялась.

        `created` — узел появился в этом `_attach` (разрез ствола, камера на сети,
        новый узел существующей камеры); чужой узел, к которому лишь присоединялись,
        не трогается — только снимается учтённая врезка.
        """
        if join.kind == "chamber" and node.existing is not None:
            node.tie_ins = max(0, node.tie_ins - 1)
            self.wave.release_tie_in(node.existing)
            if created and self.net.degree(node.id) == 0:
                self.net.remove_node(node.id)
                self.existing_nodes.pop(node.ref, None)
        elif node.kind == NODE_CHAMBER_NEW and created:
            degree = self.net.degree(node.id)
            if degree == 0:
                self.net.remove_node(node.id)
            elif degree == 2 and node.on_edge is None and self._merge_angle_ok(node):
                self.net.merge_through(node.id)      # разрез ствола без ветки не нужен

    def _fix_turn_at_join(self, branch: Segment, node: Node) -> bool:
        """Угол пути ветка → узел → ствол в камере дерева, куда ушёл конец ветки:
        при превышении — подход под 45° (как в path_turn_issues); False — не вышло."""
        if node.kind != NODE_CHAMBER_NEW:
            return True
        trunk = self._downstream_segment(node.id)
        if trunk is None or trunk.id == branch.id:
            return True
        others = [s.line for s in self.net.segments.values() if s.id != branch.id]
        obstacles = Obstacles(self.field.blocked_geom, [self.field.network_geom, *others])
        return _fix_turn_at_node(self.net, node, branch, trunk, self.rules.max_turn_deg, obstacles) is None

    def _snapshot(self):
        return (
            self.net.snapshot(), dict(self.existing_nodes), dict(self.joins), dict(self.flow_by_oks_node),
            list(self.unconnected), dict(self.reasons), dict(self.wave.extra_connections), dict(self.step_of_oks),
        )

    def _restore(self, snap) -> None:
        net_snap, existing_nodes, joins, flows, unconnected, reasons, extra, steps = snap
        self.net.restore(net_snap)
        # узлы в снимке — копии: привязка «камера входа → узел» переводится на них
        self.existing_nodes = {cid: self.net.nodes[n.id] for cid, n in existing_nodes.items() if n.id in self.net.nodes}
        self.joins, self.flow_by_oks_node, self.step_of_oks = joins, flows, steps
        self.unconnected, self.reasons = unconnected, reasons
        self.wave.set_tie_ins(extra)
        self._refresh_index()

    def _detach(self, oks_node: Node) -> Node | None:
        """Удалить ветку точки до первого узла, который держит что-то ещё; вернуть его."""
        net = self.net
        node = oks_node
        incident = net.incident(node.id)
        if not incident:
            net.remove_node(node.id)
            return None
        segment = incident[0]
        while True:
            nxt = net.nodes[segment.other(node.id)]
            net.remove_segment(segment.id)
            net.remove_node(node.id)
            node = nxt
            remaining = net.incident(node.id)
            if node.is_tie_in_point or node.kind == NODE_OKS or len(remaining) != 1:
                break
            segment = remaining[0]       # проходной узел — ветка идёт дальше

        self.joins.pop(oks_node.id, None)
        self.flow_by_oks_node.pop(oks_node.id, None)
        self.step_of_oks.pop(oks_node.id, None)

        if node.kind == NODE_CHAMBER_EXISTING and node.existing is not None:
            node.tie_ins = max(0, node.tie_ins - 1)
            self.wave.release_tie_in(node.existing)
            if net.degree(node.id) == 0:
                net.remove_node(node.id)
                self.existing_nodes.pop(node.ref, None)
                node = None
        elif node.kind == NODE_CHAMBER_NEW:
            degree = net.degree(node.id)
            if degree == 0:
                net.remove_node(node.id)          # камера врезки держала только эту ветку
                node = None
            elif degree == 2 and node.on_edge is None and self._merge_angle_ok(node):
                net.merge_through(node.id)        # камера без разветвления не нужна
                node = None
        diameters.assign(net, self.rules, self.flow_by_oks_node)
        self._refresh_index()
        return node

    def _merge_angle_ok(self, node: Node) -> bool:
        """Слить участки в проходной камере можно, если угол в ней проходит как поворот вершины."""
        first, second = self.net.incident(node.id)
        a = _oriented_points(first, node.id)[1]
        b = _oriented_points(second, node.id)[1]
        return turn_angle_deg(a, node.point, b) <= self.rules.max_turn_deg + 1e-6

    def _fail(self, oks_id: Any, reason: str) -> None:
        self.unconnected.append(oks_id)
        self.reasons[oks_id] = reason

    def _obstacles(self, *, blocked: bool = True) -> Obstacles:
        lines = [self.field.network_geom] + [s.line for s in self.net.segments.values()]
        return Obstacles(self.field.blocked_geom if blocked else GeometryCollection(), lines)

    def _chord_at_exit(self, target: ApproachTarget, points: list[Point2]) -> list[Point2] | None:
        """Срезка угла у выхода из зоны ОКС, если он стал круче предела."""
        oks_point = (target.oks.geom.x, target.oks.geom.y)
        limit = self.rules.max_turn_deg
        if len(points) < 2 or turn_angle_deg(oks_point, points[0], points[1]) <= limit + 1e-6:
            return points
        ux, uy = target.direction
        vx, vy = points[1][0] - points[0][0], points[1][1] - points[0][1]
        vn = math.hypot(vx, vy) or 1.0
        vx, vy = vx / vn, vy / vn
        obstacles = self._obstacles()
        for chord in (CHORD_M, CHORD_M / 2):
            q = (points[0][0] + chord * (ux + vx), points[0][1] + chord * (uy + vy))
            if (obstacles.segment_ok(points[0], q) and obstacles.segment_ok(q, points[1])
                    and turn_angle_deg(oks_point, points[0], q) <= limit
                    and turn_angle_deg(points[0], q, points[1]) <= limit):
                return [points[0], q] + points[1:]
        return None

    def _fix_exit_turn(self, target: ApproachTarget, points, obstacles, du):
        """Поворот у выхода из зоны ОКС ≤ 90°: срезка угла или П-образный выход.

        Возвращает (точки, попытка волны) или None.
        """
        oks_point = (target.oks.geom.x, target.oks.geom.y)
        ux, uy = target.direction
        limit = self.rules.max_turn_deg
        theta = turn_angle_deg(oks_point, points[0], points[1])
        if theta <= limit + 1e-6:
            return points, self._last_attempt

        if theta <= CHORD_MAX_TURN_DEG:
            # Срезка: из T идём по биссектрисе между «наружу» и направлением трассы,
            # затем возвращаемся на неё. Оба новых поворота — по θ/2.
            vx, vy = points[1][0] - points[0][0], points[1][1] - points[0][1]
            vn = math.hypot(vx, vy) or 1.0
            vx, vy = vx / vn, vy / vn
            q = (points[0][0] + CHORD_M * (ux + vx), points[0][1] + CHORD_M * (uy + vy))
            if (obstacles.segment_ok(points[0], q) and obstacles.segment_ok(q, points[1])
                    and turn_angle_deg(oks_point, points[0], q) <= limit
                    and turn_angle_deg(points[0], q, points[1]) <= limit):
                return [points[0], q] + points[1:], self._last_attempt

        # П-образный выход: наружу на CHORD_M, вбок на w, дальше волна заново
        side = 1 if (ux * (points[1][1] - points[0][1]) - uy * (points[1][0] - points[0][0])) > 0 else -1
        for w in SIDE_STEP_M:
            for sign in (side, -side):
                nx, ny = -uy * sign, ux * sign
                out = (points[0][0] + ux * CHORD_M, points[0][1] + uy * CHORD_M)
                anchor = (out[0] + nx * w, out[1] + ny * w)
                if not self.field.grid.contains(*anchor):
                    continue
                row, col = self.field.grid.rowcol(*anchor)
                if self.field.blocked[row, col] or not self.field.reachable[row, col]:
                    continue
                if not (obstacles.segment_ok(points[0], out) and obstacles.segment_ok(out, anchor)):
                    continue
                attempt = self.wave.run(Point(*anchor), du, target.oks.flow_tph)
                if attempt is None:
                    continue
                rest = soften_sharp_turns(straighten(self.field, attempt.points, obstacles), obstacles)
                if len(rest) < 2 or turn_angle_deg(out, anchor, rest[1]) > limit + 1e-6:
                    continue
                self._last_attempt = attempt
                return [points[0], out] + rest, attempt
        return None

    def _attach(self, join: JoinOption, at: Point2, du: int) -> Node | None:
        rules = self.rules
        if join.kind == "network":
            assert join.edge is not None
            snapped = join.edge.geom.interpolate(join.edge.geom.project(Point(at)))
            node = self.net.add_node(
                (snapped.x, snapped.y), NODE_CHAMBER_NEW, on_edge=join.edge,
                existing_connections=self._existing_connections_at(snapped),
            )
            if self.net.free_connections(node, rules.max_connections) <= 0:
                self.net.remove_node(node.id)
                return None
            return node

        if join.kind == "chamber":
            assert join.chamber is not None
            node = self.existing_nodes.get(join.chamber.id)
            if node is None:
                node = self.net.add_node(
                    (join.chamber.geom.x, join.chamber.geom.y), NODE_CHAMBER_EXISTING,
                    ref=join.chamber.id, existing=join.chamber,
                )
                self.existing_nodes[join.chamber.id] = node
            if self.net.free_connections(node, rules.max_connections) <= 0:
                return None
            node.tie_ins += 1
            self.wave.note_tie_in(join.chamber)
            return node

        if join.kind == "branch_node":
            node = self.net.nodes.get(join.node_id)
            if node is None or self.net.free_connections(node, rules.max_connections) <= 0:
                return None
            return node

        if join.kind == "branch":
            segment = min(self.net.segments.values(), key=lambda s: s.line.distance(Point(at)))
            if segment.line.distance(Point(at)) > self.field.grid.resolution * 2.5:
                return None
            # Камера дерева в паре метров — присоединяемся к ней, а не ставим вторую рядом
            for node_id in (segment.start, segment.end):
                node = self.net.nodes[node_id]
                if node.kind == NODE_CHAMBER_NEW and math.dist(node.point, at) <= MERGE_CHAMBERS_M:
                    if self.net.free_connections(node, rules.max_connections) > 0:
                        return node
            node = self.net.split_segment(segment, at, NODE_CHAMBER_NEW)
            if self.net.free_connections(node, rules.max_connections) <= 0:
                return None
            return node

        return None

    def _align_branch_join(self, points: list[Point2], join: JoinOption, *, final_leg: bool = False) -> list[Point2] | None:
        """Поворот ≤ 90° и в камере разветвления — вдоль пути к присоединению.

        Ветка, подошедшая к стволу «против потока», дала бы на пути от своей
        точки к врезке поворот больше 90°. Два способа исправить: сдвинуть
        точку присоединения по стволу к врезке или подойти под 45° через
        дополнительную вершину рядом со стволом.

        `final_leg` — выравнивается сам финальный участок (цель легла на ствол):
        он идёт из точки внутри здания через собственную зону ОКС, поэтому
        запреты для него не проверяются — только чужие линии.
        """
        if len(points) < 2:
            return points
        at = Point(*points[-1])
        if join.kind == "branch_node":
            node = self.net.nodes.get(join.node_id)
            if node is None:
                return None
            trunk = self._downstream_segment(node.id)
            if trunk is None:
                return points
            line = trunk.line
            base = line.project(Point(*node.point))
            toward_root = 1.0 if trunk.start == node.id else -1.0
            shifts: tuple[float, ...] = (0.0,)
        else:
            trunk = min(self.net.segments.values(), key=lambda s: s.line.distance(at))
            line = trunk.line
            base = line.project(at)
            root_side = trunk.start if self._closer_to_root(trunk.start, trunk.end) else trunk.end
            toward_root = -1.0 if root_side == trunk.start else 1.0
            shifts = (0.0, 3.0, 6.0, 10.0, 15.0, 22.0, 30.0)

        obstacles = self._obstacles(blocked=not final_leg)
        limit = self.rules.max_turn_deg
        prev = points[-2]
        before_prev = points[-3] if len(points) >= 3 else None
        cumulative = [0.0]
        for a, b in zip(trunk.points, trunk.points[1:]):
            cumulative.append(cumulative[-1] + math.dist(a, b))

        def ahead_of(d: float) -> Point2:
            """Первая вершина ствола за точкой присоединения в сторону корня —
            фактическое звено, а не хорда: вершина ствола в метре от точки
            присоединения иначе даёт непроверенный поворот."""
            if toward_root > 0:
                for i, c in enumerate(cumulative):
                    if c > d + 0.05:
                        return trunk.points[i]
                return trunk.points[-1]
            for i in range(len(cumulative) - 1, -1, -1):
                if cumulative[i] < d - 0.05:
                    return trunk.points[i]
            return trunk.points[0]

        def bend_ok(at: Point2) -> bool:
            """Сдвиг конца ветки меняет и поворот в предыдущей вершине."""
            return before_prev is None or turn_angle_deg(before_prev, prev, at) <= limit + 1e-6

        # 1. сдвиг точки присоединения по стволу (с прилипанием к вершине ствола,
        #    чтобы разрез не оставил микроотрезка)
        for shift in shifts:
            d = base + toward_root * shift
            if d < 0.0 or d > line.length:
                break
            candidate = line.interpolate(d)
            cand = snap_point_to_line(trunk.points, (candidate.x, candidate.y), SNAP_TO_VERTEX_M) \
                if join.kind == "branch" else (candidate.x, candidate.y)
            d_cand = line.project(Point(*cand))
            if math.dist(prev, cand) < 0.5:
                continue
            if turn_angle_deg(prev, cand, ahead_of(d_cand)) <= limit + 1e-6 and bend_ok(cand) \
                    and obstacles.segment_ok(prev, cand):
                return points[:-1] + [cand]

        # 2. подход под 45°: дополнительная вершина Q со стороны ветки
        for shift in shifts:
            d = base + toward_root * shift
            if d < 0.0 or d > line.length:
                break
            raw = line.interpolate(d)
            jx, jy = snap_point_to_line(trunk.points, (raw.x, raw.y), SNAP_TO_VERTEX_M) \
                if join.kind == "branch" else (raw.x, raw.y)
            joint = Point(jx, jy)
            ahead = ahead_of(line.project(joint))
            tx, ty = ahead[0] - joint.x, ahead[1] - joint.y
            tn = math.hypot(tx, ty) or 1.0
            tx, ty = tx / tn, ty / tn
            side = 1.0 if (tx * (prev[1] - joint.y) - ty * (prev[0] - joint.x)) > 0 else -1.0
            nx, ny = -ty * side, tx * side
            for h in (4.0, 7.0, 11.0):
                q = (joint.x - tx * h + nx * h, joint.y - ty * h + ny * h)
                row, col = self.field.grid.rowcol(*q)
                if not self.field.grid.contains(*q) or self.field.blocked[row, col]:
                    continue
                if math.dist(prev, q) < 0.5:
                    continue
                if turn_angle_deg(prev, q, (joint.x, joint.y)) > limit + 1e-6 or not bend_ok(q):
                    continue
                if obstacles.segment_ok(prev, q) and obstacles.segment_ok(q, (joint.x, joint.y)):
                    return points[:-1] + [q, (joint.x, joint.y)]
        return None

    def _downstream_segment(self, node_id: str):
        """Участок, уходящий от узла в сторону присоединения."""
        roots = self.net.tie_in_nodes()
        for root in roots:
            for s, near, far in self.net.oriented_from_root(root.id):
                if far == node_id:
                    return s
        return None

    def _closer_to_root(self, a: str, b: str) -> bool:
        """Какой из двух узлов участка ближе к присоединению (по дереву)."""
        roots = self.net.tie_in_nodes()
        for root in roots:
            for s, near, far in self.net.oriented_from_root(root.id):
                if {near, far} == {a, b}:
                    return near == a
        return True

    def _existing_connections_at(self, point: Point) -> int:
        """§9.4: концы существующих участков в допуске плюс 2 за каждый участок, внутри которого точка."""
        tol = self.rules.attach_tolerance_m
        count = 0
        for edge in self.case.network:
            ends = [Point(edge.geom.coords[0]), Point(edge.geom.coords[-1])]
            touching = sum(1 for end in ends if end.distance(point) <= tol)
            if touching:
                count += touching
            elif edge.geom.distance(point) <= tol:
                count += 2
        return count

    def _refresh_index(self) -> None:
        """Переиндексировать дерево для волны: ветки с их Ду и свободные примыкания камер."""
        self.index.node_cells.clear()
        self.index.node_free.clear()
        for node in self.net.nodes.values():
            if node.kind != NODE_CHAMBER_NEW:
                continue   # существующие камеры волна ведёт сама
            free = self.net.free_connections(node, self.rules.max_connections)
            self.index.add_node(node.id, Point(*node.point), free)
        self.index.reset()
        for segment in self.net.segments.values():
            self.index.add_branch(segment.id, segment.line, segment.du or self.field.du_plan)


def path_turn_issues(net: Network, rules, blocked_geom, network_geom) -> list[tuple[str, float, Point2]]:
    """Повороты круче предела в узлах вдоль путей к присоединению — с попыткой исправить.

    В камере разветвления путь идёт из ветки в ствол. Если угол между ними
    больше предела, ветке даётся подход под 45° через дополнительную вершину
    (как при присоединении). Что не исправилось — возвращается для ремонта.
    """
    limit = rules.max_turn_deg
    issues: list[tuple[str, float, Point2]] = []
    for root in net.tie_in_nodes():
        oriented = list(net.oriented_from_root(root.id))
        downstream = {far: seg for seg, near, far in oriented}      # участок от узла к корню
        for seg, near, far in oriented:
            node = net.nodes[far]
            if node.kind == NODE_OKS:
                continue
            trunk = downstream.get(far)
            if trunk is None:
                continue
            for branch, _n, _f in oriented:
                if _n != far:
                    continue                                        # ветки, уходящие от узла
                others = [s.line for s in net.segments.values() if s.id != branch.id]
                obstacles = Obstacles(blocked_geom, [network_geom, *others])
                fixed = _fix_turn_at_node(net, node, branch, trunk, limit, obstacles)
                if fixed is not None and fixed > limit + 1e-6:
                    issues.append((node.id, fixed, node.point))
    return issues


def _oriented_points(seg: Segment, from_node: str) -> list[Point2]:
    return list(seg.points) if seg.start == from_node else list(reversed(seg.points))


def _fix_turn_at_node(net: Network, node: Node, branch: Segment, trunk: Segment, limit: float, obstacles: Obstacles):
    """Угол пути ветка → узел → ствол. Возвращает оставшийся угол или None, если всё в норме."""
    b_pts = _oriented_points(branch, node.id)        # от узла наружу по ветке
    t_pts = _oriented_points(trunk, node.id)         # от узла к корню
    if len(b_pts) < 2 or len(t_pts) < 2:
        return None
    turn = turn_angle_deg(b_pts[1], node.point, t_pts[1])
    if turn <= limit + 1e-6:
        return None
    # Подход под 45°: вершина Q рядом с узлом со стороны ветки
    tx, ty = t_pts[1][0] - node.point[0], t_pts[1][1] - node.point[1]
    tn = math.hypot(tx, ty) or 1.0
    tx, ty = tx / tn, ty / tn
    prev = b_pts[1]
    side = 1.0 if (tx * (prev[1] - node.point[1]) - ty * (prev[0] - node.point[0])) > 0 else -1.0
    nx, ny = -ty * side, tx * side
    for h in (3.0, 5.0, 8.0):
        q = (node.point[0] - tx * h + nx * h, node.point[1] - ty * h + ny * h)
        if math.dist(prev, q) < 0.5:
            continue
        if turn_angle_deg(prev, q, node.point) > limit + 1e-6:
            continue
        if len(b_pts) >= 3 and turn_angle_deg(b_pts[2], prev, q) > limit + 1e-6:
            continue
        if not (obstacles.segment_ok(prev, q) and obstacles.segment_ok(q, node.point)):
            continue
        new_pts = [node.point, q] + b_pts[1:]
        branch.points = new_pts if branch.start == node.id else list(reversed(new_pts))
        return None
    return turn


def _dedupe(points: list[Point2]) -> list[Point2]:
    result: list[Point2] = []
    for p in points:
        if not result or math.dist(result[-1], p) > 1e-6:
            result.append(p)
    return result


def build_tree(case: CaseInput, field: CaseField, targets: list[ApproachTarget]) -> BuildResult:
    return TreeBuilder(case, field).build(targets)
