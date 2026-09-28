"""Одна волна от точки подключения до всего, к чему можно присоединиться.

Кандидаты присоединения — три вида ячеек:
* существующая сеть: новая камера на участке (её Ду — по наибольшему из
  примыкающих, включая сам участок, §9.3);
* существующая камера с свободными примыканиями: врезка (только если точка
  присоединения ближе 10 м — §2.4; поэтому ячейки сети в этом радиусе
  из кандидатов первого вида убираются);
* уже построенные ветки: разветвление в новой камере, а если ветка в этом
  месте уже разветвляется — без новой камеры, пока есть свободные примыкания.

Путь усекается в первой точке, где он касается сети или дерева: если бы
дальше было дешевле, волна выбрала бы ту ячейку сама (§9.4 спецификации).

Волна считается в окне вокруг точки: кандидаты дальше полутора расстояний до
сети заведомо проигрывают, а полный район — это миллионы ячеек на каждую точку.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from shapely.geometry import LineString, Point
from skimage.graph import MCP_Geometric

from ..geo import raster
from .field import CaseField
from .model import CaseInput, ExistingChamber, ExistingEdge

Point2 = tuple[float, float]
Cell = tuple[int, int]


@dataclass
class JoinOption:
    kind: str                     # network | chamber | branch | branch_node
    cell: Cell
    point: Point
    wave_score: float             # стоимость пути в единицах S
    join_score: float             # камера / врезка в единицах S
    edge: ExistingEdge | None = None
    chamber: ExistingChamber | None = None
    branch_id: Any = None
    node_id: Any = None
    chamber_du: int | None = None

    @property
    def total(self) -> float:
        return self.wave_score + self.join_score


@dataclass
class RouteResult:
    points: list[Point2]          # от цели (снаружи зоны ОКС) до точки присоединения
    join: JoinOption
    examined: int


BURN_CACHE_MAX = 4000   # растров веток в кеше индекса; при переполнении кеш сбрасывается


class TreeIndex:
    """Растровое представление уже построенного дерева для волны."""

    def __init__(self, field: CaseField):
        self.field = field
        self.branch_of = np.full(field.grid.shape, -1, dtype=np.int32)   # индекс ветки
        self.branches: list[tuple[Any, LineString, int]] = []            # (id, линия, Ду)
        self.node_cells: dict[Cell, Any] = {}                             # ячейка → id узла с камерой
        self.node_free: dict[Any, int] = {}                               # свободные примыкания узла
        # Растр ветки по её геометрии: индекс перестраивается целиком после каждого
        # присоединения и отката, а геометрия большинства участков не меняется —
        # растеризовать их заново незачем (на 68 точках это 3/4 времени варианта)
        self._burned: dict[bytes, tuple[slice, slice, np.ndarray]] = {}

    def reset(self) -> None:
        self.branch_of[:] = -1
        self.branches = []

    def add_branch(self, branch_id: Any, line: LineString, du: int) -> None:
        index = len(self.branches)
        self.branches.append((branch_id, line, du))
        # Ветка занимает и соседние ячейки: путь, прошедший в метре от неё,
        # считается присоединившимся — иначе две ветки лягут бок о бок.
        key = line.wkb
        burned = self._burned.get(key)
        if burned is None:
            if len(self._burned) >= BURN_CACHE_MAX:
                self._burned.clear()
            burned = self._burned[key] = raster.burn_mask_window(
                self.field.grid, [line.buffer(self.field.grid.resolution * 1.01)]
            )
        rows, cols, mask = burned
        view = self.branch_of[rows, cols]
        view[mask & (view < 0)] = index

    def add_node(self, node_id: Any, point: Point, free_connections: int) -> None:
        cell = self.field.grid.rowcol(point.x, point.y)
        self.node_cells[cell] = node_id
        self.node_free[node_id] = free_connections

    @property
    def any(self) -> bool:
        return bool(self.branches)


def _network_index(case: CaseInput, field: CaseField, extra: dict[Any, int] | None = None):
    """Ячейки существующей сети → участок; ячейки камер; запрет ячеек у камер."""
    grid = field.grid
    extra = extra or {}
    edge_of = np.full(grid.shape, -1, dtype=np.int32)
    for index, edge in enumerate(case.network):
        rows, cols, mask = raster.burn_mask_window(grid, [edge.geom])
        view = edge_of[rows, cols]
        view[mask & (view < 0)] = index

    usable = [
        c for c in case.chambers
        if c.connections + extra.get(c.id, 0) < field.rules.max_connections
    ]
    chamber_cells: dict[Cell, ExistingChamber] = {}
    near_chamber = np.zeros(grid.shape, dtype=bool)
    radius = field.rules.reuse_chamber_within_m
    for chamber in usable:
        chamber_cells[grid.rowcol(chamber.geom.x, chamber.geom.y)] = chamber
        rows, cols, mask = raster.burn_mask_window(grid, [chamber.geom.buffer(radius)])
        near_chamber[rows, cols] |= mask
    return edge_of, chamber_cells, near_chamber


class Wave:
    """Волна от одной точки; кандидаты оцениваются по накопленной стоимости."""

    def __init__(self, case: CaseInput, field: CaseField, tree: TreeIndex):
        self.case, self.field, self.tree = case, field, tree
        self.extra_connections: dict[Any, int] = {}
        self.edge_of, self.chamber_cells, self.near_chamber = _network_index(case, field, self.extra_connections)

    def note_tie_in(self, chamber: ExistingChamber) -> None:
        """Наша врезка заняла примыкание: заполненная камера выбывает из кандидатов."""
        self.extra_connections[chamber.id] = self.extra_connections.get(chamber.id, 0) + 1
        if chamber.connections + self.extra_connections[chamber.id] >= self.field.rules.max_connections:
            self._reindex_network()

    def release_tie_in(self, chamber: ExistingChamber) -> None:
        """Врезка снята (ветка перестраивается): заполненная камера возвращается в кандидаты."""
        was_full = self._is_full(chamber)
        self.extra_connections[chamber.id] = max(0, self.extra_connections.get(chamber.id, 0) - 1)
        if was_full and not self._is_full(chamber):
            self._reindex_network()

    def set_tie_ins(self, extra: dict[Any, int]) -> None:
        """Восстановить занятость камер из снимка; индекс сети — если изменился набор заполненных."""
        before = {c.id for c in self.case.chambers if self._is_full(c)}
        self.extra_connections = dict(extra)
        after = {c.id for c in self.case.chambers if self._is_full(c)}
        if before != after:
            self._reindex_network()

    def _is_full(self, chamber: ExistingChamber) -> bool:
        return chamber.connections + self.extra_connections.get(chamber.id, 0) >= self.field.rules.max_connections

    def _reindex_network(self) -> None:
        self.edge_of, self.chamber_cells, self.near_chamber = _network_index(
            self.case, self.field, self.extra_connections
        )

    # --- стоимость узла присоединения по видам кандидатов ---

    def _edge_join_scores(self, du: int) -> np.ndarray:
        rules = self.field.rules
        include = rules.chamber_diameter_includes_existing
        return np.array([
            rules.score_of_cost(rules.chamber_cost(max(e.diameter, du) if include else du))
            for e in self.case.network
        ] or [0.0], dtype=np.float64)

    def _branch_join_scores(self, du: int) -> np.ndarray:
        rules = self.field.rules
        return np.array([
            rules.score_of_cost(rules.chamber_cost(max(branch_du, du)))
            for _, _, branch_du in self.tree.branches
        ] or [0.0], dtype=np.float64)

    def run(self, start: Point, du: int, flow_tph: float, *, radius_m: float | None = None,
            exclude: list[Point2] | None = None, exclude_radius_m: float = 4.0) -> RouteResult | None:
        """`exclude` — точки, около которых присоединяться нельзя (неудачные попытки)."""
        grid = self.field.grid
        rules = self.field.rules
        weight = self.field.weights_for(du)
        start_cell = grid.rowcol(start.x, start.y)
        if not np.isfinite(weight[start_cell]):
            return None

        # Старт уже на сети или на ветке дерева (например, ветка соседа прошла
        # через точку выхода): маршрут вырожденный, присоединение — здесь же.
        # Иначе волна пошла бы вдоль ветки до следующей ячейки и легла на неё.
        if not (exclude and any(math.dist((start.x, start.y), e) <= exclude_radius_m for e in exclude)):
            immediate = self._option_at(start_cell, du, 0.0)
            if immediate is not None:
                return RouteResult(points=[(start.x, start.y)], join=immediate, examined=1)

        if radius_m is None:
            radius_m = max(200.0, start.distance(self.field.network_geom) * 1.6 + 100.0)
        cells = int(radius_m / grid.resolution)
        r0, r1 = max(0, start_cell[0] - cells), min(grid.height, start_cell[0] + cells + 1)
        c0, c1 = max(0, start_cell[1] - cells), min(grid.width, start_cell[1] + cells + 1)
        rows, cols = slice(r0, r1), slice(c0, c1)

        mcp = MCP_Geometric(weight[rows, cols], fully_connected=True)
        # Стоимость в единицах S, шаг сетки в метрах: MCP считает в ячейках
        local, _ = mcp.find_costs([(start_cell[0] - r0, start_cell[1] - c0)])
        local = local * grid.resolution
        finite = np.isfinite(local)
        if exclude:
            for point in exclude:
                er, ec = grid.rowcol(*point)
                k = int(exclude_radius_m / grid.resolution) + 1
                rr0, rr1 = max(r0, er - k) - r0, min(r1, er + k + 1) - r0
                cc0, cc1 = max(c0, ec - k) - c0, min(c1, ec + k + 1) - c0
                if rr1 > rr0 and cc1 > cc0:
                    finite[rr0:rr1, cc0:cc1] = False

        best: JoinOption | None = None
        examined = 0

        def consider(option: JoinOption) -> None:
            nonlocal best
            if best is None or option.total < best.total:
                best = option

        # --- существующая сеть: новая камера (векторно) ---
        edge_w = self.edge_of[rows, cols]
        mask = (edge_w >= 0) & finite & ~self.near_chamber[rows, cols]
        if mask.any():
            join_scores = self._edge_join_scores(du)
            totals = local[mask] + join_scores[edge_w[mask]]
            k = int(np.argmin(totals))
            rr, cc = np.nonzero(mask)
            cell = (int(rr[k]) + r0, int(cc[k]) + c0)
            edge_index = int(edge_w[rr[k], cc[k]])
            edge = self.case.network[edge_index]
            chamber_du = max(edge.diameter, du) if rules.chamber_diameter_includes_existing else du
            consider(JoinOption("network", cell, Point(*grid.xy(*cell)), float(local[rr[k], cc[k]]),
                                float(join_scores[edge_index]), edge=edge, chamber_du=chamber_du))
            examined += int(mask.sum())

        # --- существующая камера: врезка ---
        for cell, chamber in self.chamber_cells.items():
            lr, lc = cell[0] - r0, cell[1] - c0
            if not (0 <= lr < r1 - r0 and 0 <= lc < c1 - c0) or not finite[lr, lc]:
                continue
            consider(JoinOption("chamber", cell, chamber.geom, float(local[lr, lc]),
                                rules.score_of_cost(rules.tie_in_cost), chamber=chamber))
            examined += 1

        # --- дерево: узлы с камерами без новой камеры ---
        for cell, node_id in self.tree.node_cells.items():
            if self.tree.node_free.get(node_id, 0) <= 0:
                continue
            lr, lc = cell[0] - r0, cell[1] - c0
            if not (0 <= lr < r1 - r0 and 0 <= lc < c1 - c0) or not finite[lr, lc]:
                continue
            consider(JoinOption("branch_node", cell, Point(*grid.xy(*cell)), float(local[lr, lc]), 0.0, node_id=node_id))
            examined += 1

        # --- дерево: ветки — новая камера (векторно) ---
        if self.tree.any:
            branch_w = self.tree.branch_of[rows, cols]
            mask = (branch_w >= 0) & finite
            if mask.any():
                join_scores = self._branch_join_scores(du)
                totals = local[mask] + join_scores[branch_w[mask]]
                k = int(np.argmin(totals))
                rr, cc = np.nonzero(mask)
                cell = (int(rr[k]) + r0, int(cc[k]) + c0)
                branch_index = int(branch_w[rr[k], cc[k]])
                branch_id, _, branch_du = self.tree.branches[branch_index]
                consider(JoinOption("branch", cell, Point(*grid.xy(*cell)), float(local[rr[k], cc[k]]),
                                    float(join_scores[branch_index]),
                                    branch_id=branch_id, chamber_du=max(branch_du, du)))
                examined += int(mask.sum())

        if best is None:
            return None

        # Усечение в первой точке касания сети или дерева. Касание сети в 10 м
        # от свободной существующей камеры (§2.4) — не место для новой камеры:
        # маршрут перенацеливается на эту камеру; на подходе к ней ячейки сети
        # рядом с ней касанием не считаются.
        for _attempt in range(3):
            path_local = mcp.traceback((best.cell[0] - r0, best.cell[1] - c0))
            path_cells = [(r + r0, c + c0) for r, c in path_local]
            cut = None
            for index, (r, c) in enumerate(path_cells[1:], start=1):
                on_network = self.edge_of[r, c] >= 0
                if on_network and self.near_chamber[r, c] and best.kind == "chamber"                         and best.chamber is not None and self._near(best.chamber, (r, c)):
                    continue
                if on_network or (r, c) in self.chamber_cells or self.tree.branch_of[r, c] >= 0:
                    cut = index
                    break
            if cut is None or cut >= len(path_cells) - 1:
                break
            r, c = path_cells[cut]
            option = self._option_at((r, c), du, float(local[r - r0, c - c0]))
            if option is not None:
                best = option
                path_cells = path_cells[: cut + 1]
                break
            redirect = self._chamber_near((r, c))
            if redirect is None:
                return None
            lr, lc = redirect[0] - r0, redirect[1] - c0
            if not (0 <= lr < r1 - r0 and 0 <= lc < c1 - c0) or not finite[lr, lc]:
                return None
            best = JoinOption("chamber", redirect, self.chamber_cells[redirect].geom, float(local[lr, lc]),
                              rules.score_of_cost(rules.tie_in_cost), chamber=self.chamber_cells[redirect])
        else:
            return None

        points = [grid.xy(r, c) for r, c in path_cells]
        points[0] = (start.x, start.y)
        points[-1] = (best.point.x, best.point.y)
        return RouteResult(points=points, join=best, examined=examined)

    def _near(self, chamber: ExistingChamber, cell: Cell) -> bool:
        x, y = self.field.grid.xy(*cell)
        return chamber.geom.distance(Point(x, y)) <= self.field.rules.reuse_chamber_within_m + self.field.grid.resolution

    def _chamber_near(self, cell: Cell) -> Cell | None:
        """Ячейка ближайшей свободной существующей камеры в радиусе правила 10 м."""
        x, y = self.field.grid.xy(*cell)
        point = Point(x, y)
        limit = self.field.rules.reuse_chamber_within_m + self.field.grid.resolution
        best_cell, best_d = None, limit
        for chamber_cell, chamber in self.chamber_cells.items():
            d = chamber.geom.distance(point)
            if d <= best_d:
                best_cell, best_d = chamber_cell, d
        return best_cell

    def _option_at(self, cell: Cell, du: int, wave_score: float) -> JoinOption | None:
        rules = self.field.rules
        grid = self.field.grid
        if cell in self.chamber_cells:
            chamber = self.chamber_cells[cell]
            return JoinOption("chamber", cell, chamber.geom, wave_score,
                              rules.score_of_cost(rules.tie_in_cost), chamber=chamber)
        if self.edge_of[cell] >= 0:
            if self.near_chamber[cell]:
                return None   # §2.4: в 10 м от свободной существующей камеры новая камера не ставится
            edge = self.case.network[int(self.edge_of[cell])]
            chamber_du = max(edge.diameter, du) if rules.chamber_diameter_includes_existing else du
            return JoinOption("network", cell, Point(*grid.xy(*cell)), wave_score,
                              rules.score_of_cost(rules.chamber_cost(chamber_du)), edge=edge, chamber_du=chamber_du)
        if cell in self.tree.node_cells and self.tree.node_free.get(self.tree.node_cells[cell], 0) > 0:
            return JoinOption("branch_node", cell, Point(*grid.xy(*cell)), wave_score, 0.0,
                              node_id=self.tree.node_cells[cell])
        if self.tree.branch_of[cell] >= 0:
            branch_id, _, branch_du = self.tree.branches[int(self.tree.branch_of[cell])]
            chamber_du = max(branch_du, du)
            return JoinOption("branch", cell, Point(*grid.xy(*cell)), wave_score,
                              rules.score_of_cost(rules.chamber_cost(chamber_du)), branch_id=branch_id, chamber_du=chamber_du)
        return None
