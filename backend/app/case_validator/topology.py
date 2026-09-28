"""Граф новых участков варианта и проверки §2.1, §2.4 приложения.

Граф строится по ссылкам start_node_id/end_node_id (совпадение с геометрией
проверено раньше). Требования: лес; в каждой компоненте ровно одно место
присоединения к существующей сети; точки подключения — листья; не более
четырёх примыканий к камере с учётом существующих; правило 10 м до
существующей камеры. Число примыканий существующей камеры считается по
геометрии входа (§9.12 спецификации) — в файле их никто не объявляет.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shapely.geometry import Point

from app.case_validator.findings import Findings
from app.case_validator.model import IdKey, InputData, InputLine, NodeRef, Segment, Variant
from app.case_validator.rules import Rules

LENIENT_ATTACH_TOL_M = 3.0   # §9.12: концы участков в 0,5–3 м от камеры — неоднозначны
NEAR_LINE_WARN_M = 2.0       # камера рядом с сетью, но не на ней


@dataclass
class Node:
    ref: NodeRef
    segments: list[Segment] = field(default_factory=list)   # с повтором для петли
    on_lines: list[InputLine] = field(default_factory=list)  # существующие линии в допуске
    near_lines: list[InputLine] = field(default_factory=list)  # в мягком допуске (§9.12)

    @property
    def key(self) -> IdKey:
        return self.ref.key

    @property
    def id(self) -> Any:
        return self.ref.id

    @property
    def kind(self) -> str:
        return self.ref.kind

    @property
    def xy(self) -> tuple[float, float]:
        return self.ref.xy

    @property
    def degree(self) -> int:
        return len(self.segments)

    @property
    def is_attachment(self) -> bool:
        return self.kind == "existing_chamber" or (self.kind == "new_chamber" and bool(self.on_lines))


@dataclass
class Component:
    nodes: list[Node]
    segments: list[Segment]
    roots: list[Node]
    has_cycle: bool
    root: Node | None = None
    # дерево, если корень единственный и цикла нет
    parent: dict[IdKey, Segment | None] = field(default_factory=dict)  # участок → участок ближе к корню
    near: dict[IdKey, Node] = field(default_factory=dict)              # узел участка со стороны корня
    far: dict[IdKey, Node] = field(default_factory=dict)               # узел со стороны точек
    children: dict[IdKey, list[Segment]] = field(default_factory=dict)
    downstream_oks: dict[IdKey, set[IdKey]] = field(default_factory=dict)
    leaf_paths: dict[IdKey, list[Segment]] = field(default_factory=dict)  # ОКС → путь к корню

    @property
    def is_tree(self) -> bool:
        return self.root is not None


@dataclass
class VariantGraph:
    variant: Variant
    nodes: dict[IdKey, Node]
    components: list[Component]
    component_of: dict[IdKey, Component]   # участок → компонента

    def node_of(self, ref: NodeRef) -> Node:
        return self.nodes[ref.key]

    def tree_of(self, seg: Segment) -> Component | None:
        comp = self.component_of.get(seg.key)
        return comp if comp is not None and comp.is_tree else None


# --- построение -------------------------------------------------------------


def existing_occupancy(point: Point, lines: list[InputLine], tol: float) -> tuple[int, list[InputLine]]:
    """Сколько примыканий занимают существующие линии у точки (§9.12)."""
    occupied = 0
    touching: list[InputLine] = []
    for line in lines:
        if line.geom.distance(point) > tol:
            continue
        touching.append(line)
        ends = sum(1 for c in (line.geom.coords[0], line.geom.coords[-1]) if Point(c).distance(point) <= tol)
        occupied += ends if ends else 2
    return occupied, touching


def build_graph(variant: Variant, inp: InputData, rules: Rules) -> VariantGraph:
    nodes: dict[IdKey, Node] = {}
    lines = list(inp.lines.values())

    def node_for(ref: NodeRef) -> Node:
        node = nodes.get(ref.key)
        if node is None:
            node = Node(ref)
            _, node.on_lines = existing_occupancy(ref.geom, lines, rules.attach_tol_m)
            _, node.near_lines = existing_occupancy(ref.geom, lines, LENIENT_ATTACH_TOL_M)
            nodes[ref.key] = node
        return node

    segments = variant.usable_segments
    parent_uf: dict[IdKey, IdKey] = {}

    def find(k: IdKey) -> IdKey:
        while parent_uf[k] != k:
            parent_uf[k] = parent_uf[parent_uf[k]]
            k = parent_uf[k]
        return k

    for seg in segments:
        a = node_for(seg.start_ref)
        b = node_for(seg.end_ref)
        a.segments.append(seg)
        b.segments.append(seg)
        parent_uf.setdefault(a.key, a.key)
        parent_uf.setdefault(b.key, b.key)
        parent_uf[find(a.key)] = find(b.key)

    groups: dict[IdKey, list[Node]] = {}
    for node in nodes.values():
        groups.setdefault(find(node.key), []).append(node)

    components: list[Component] = []
    component_of: dict[IdKey, Component] = {}
    for members in groups.values():
        member_keys = {n.key for n in members}
        comp_segments = list({s.key: s for n in members for s in n.segments}.values())
        comp_segments.sort(key=lambda s: s.index)
        roots = [n for n in members if n.is_attachment]
        has_cycle = len(comp_segments) >= len(member_keys)
        comp = Component(sorted(members, key=lambda n: repr(n.id)), comp_segments, roots, has_cycle)
        if len(roots) == 1 and not has_cycle:
            _root_tree(comp, roots[0])
        components.append(comp)
        for s in comp_segments:
            component_of[s.key] = comp
    return VariantGraph(variant, nodes, components, component_of)


def _root_tree(comp: Component, root: Node) -> None:
    comp.root = root
    node_by_key = {n.key: n for n in comp.nodes}
    visited = {root.key}
    stack: list[tuple[Node, Segment | None]] = [(root, None)]
    order: list[Segment] = []
    while stack:
        node, via = stack.pop()
        for seg in node.segments:
            if via is not None and seg.key == via.key:
                continue
            other = node_by_key[seg.other_ref(node.key).key]
            comp.near[seg.key] = node
            comp.far[seg.key] = other
            comp.parent[seg.key] = via
            comp.children.setdefault(seg.key, [])
            if via is not None:
                comp.children[via.key].append(seg)
            order.append(seg)
            if other.key not in visited:
                visited.add(other.key)
                stack.append((other, seg))
    for seg in reversed(order):
        far = comp.far[seg.key]
        down: set[IdKey] = {far.key} if far.kind == "oks" else set()
        for child in comp.children[seg.key]:
            down |= comp.downstream_oks[child.key]
        comp.downstream_oks[seg.key] = down
    for node in comp.nodes:
        if node.kind != "oks" or node.degree != 1:
            continue
        path: list[Segment] = []
        seg: Segment | None = node.segments[0]
        while seg is not None:
            path.append(seg)
            seg = comp.parent.get(seg.key)
        comp.leaf_paths[node.key] = path


# --- проверки ---------------------------------------------------------------


def check_topology(graph: VariantGraph, inp: InputData, rules: Rules, f: Findings) -> None:
    variant = graph.variant
    vid = variant.variant_id
    lines = list(inp.lines.values())

    for seg in variant.usable_segments:
        if seg.start_ref.key == seg.end_ref.key:
            f.error("topology.loop", "участок начинается и заканчивается в одном узле", variant_id=vid, object_id=seg.id)

    for comp in graph.components:
        oks_ids = [n.id for n in comp.nodes if n.kind == "oks"]
        if not comp.roots:
            f.error("topology.no_attachment",
                    f"компонента из {len(comp.segments)} участков не присоединена к существующей сети "
                    f"(нет камеры на существующей линии в допуске {rules.attach_tol_m} м и нет существующей камеры); точки: {oks_ids}",
                    variant_id=vid, object_id=comp.segments[0].id if comp.segments else None, actual=oks_ids)
        elif len(comp.roots) > 1:
            f.error("topology.multiple_attachments",
                    f"компонента присоединена к существующей сети в {len(comp.roots)} местах — замкнутый маршрут через существующую сеть",
                    variant_id=vid, object_id=comp.segments[0].id if comp.segments else None,
                    expected=1, actual=[n.id for n in comp.roots])
        if comp.has_cycle:
            f.error("topology.cycle", "новые участки образуют цикл (замкнутые маршруты не допускаются)",
                    variant_id=vid, object_id=comp.segments[0].id if comp.segments else None,
                    actual=[s.id for s in comp.segments])

    for node in graph.nodes.values():
        if node.kind == "oks":
            if node.degree != 1:
                f.error("topology.oks_degree", "точка подключения должна быть листом с одним примыкающим участком",
                        variant_id=vid, object_id=node.id, expected=1, actual=node.degree)
        elif node.kind == "tech":
            _check_tech_node(node, graph, rules, f)
        elif node.kind == "new_chamber":
            _check_new_chamber(node, graph, inp, lines, rules, f)
        elif node.kind == "existing_chamber":
            _check_existing_chamber(node, lines, rules, f)

    # камеры и техузлы без единого участка
    used = set(graph.nodes)
    for chamber in variant.chambers.values():
        if chamber.key not in used:
            f.error("topology.orphan", "новая камера не примыкает ни к одному участку", variant_id=vid, object_id=chamber.id)
    for tech in variant.tech_nodes.values():
        if tech.key not in used:
            f.error("topology.orphan", "технический узел не примыкает ни к одному участку", variant_id=vid, object_id=tech.id)


def _check_tech_node(node: Node, graph: VariantGraph, rules: Rules, f: Findings) -> None:
    vid = graph.variant.variant_id
    if node.degree != 2:
        f.error("topology.tech_degree", "к техническому узлу должны примыкать ровно два участка",
                variant_id=vid, object_id=node.id, expected=2, actual=node.degree)
    if node.on_lines:
        f.error("topology.tech_on_network",
                f"технический узел лежит на существующей линии {[ln.id for ln in node.on_lines]}: присоединение выполняется только через тепловую камеру",
                variant_id=vid, object_id=node.id)


def _check_new_chamber(node: Node, graph: VariantGraph, inp: InputData, lines: list[InputLine],
                       rules: Rules, f: Findings) -> None:
    vid = graph.variant.variant_id
    point = Point(node.xy)
    new_degree = node.degree
    occupied, _ = existing_occupancy(point, lines, rules.attach_tol_m)
    total = new_degree + occupied
    if total > rules.max_connections:
        f.error("topology.max_connections",
                f"к новой камере примыкает {total} участков (новых {new_degree}, существующих {occupied})",
                variant_id=vid, object_id=node.id, expected=f"≤ {rules.max_connections}", actual=total)
    if new_degree == 0:
        return
    if not node.is_attachment:
        if new_degree == 1:
            f.error("topology.dangling", "камера с одним участком вне существующей сети — висячий конец трассы",
                    variant_id=vid, object_id=node.id)
        elif new_degree == 2:
            f.warning("topology.unjustified_chamber",
                      "камера без разветвления и без присоединения: поворот камеры не требует, смена параметров — технический узел",
                      variant_id=vid, object_id=node.id)
        if node.near_lines:
            d = min(ln.geom.distance(point) for ln in node.near_lines)
            if d <= NEAR_LINE_WARN_M:
                f.warning("topology.near_network",
                          f"камера в {d:.2f} м от существующей линии, но не на ней (допуск {rules.attach_tol_m} м) — присоединение не засчитано",
                          variant_id=vid, object_id=node.id, expected=f"≤ {rules.attach_tol_m}", actual=round(d, 3))
        return

    # камера присоединения: правило 10 м и совпадение с существующей камерой
    for chamber in inp.chambers.values():
        d = chamber.geom.distance(point)
        if d <= rules.attach_tol_m:
            f.error("topology.chamber_duplicate",
                    f"новая камера совпадает с существующей камерой {chamber.id!r} ({d:.2f} м) — нужно присоединяться к ней",
                    variant_id=vid, object_id=node.id)
            continue
        if d > rules.reuse_within_m + 1e-6:
            continue
        strict, _ = existing_occupancy(chamber.geom, lines, rules.attach_tol_m)
        lenient, _ = existing_occupancy(chamber.geom, lines, LENIENT_ATTACH_TOL_M)
        fits_strict = strict + new_degree <= rules.max_connections
        fits_lenient = lenient + new_degree <= rules.max_connections
        message = (
            f"новая камера в {d:.2f} м от существующей камеры {chamber.id!r}, у которой хватило бы примыканий "
            f"(занято {strict}, новых {new_degree}, предел {rules.max_connections}): по §2.4 используется существующая камера"
        )
        if fits_strict and fits_lenient:
            f.error("topology.reuse_existing_chamber", message, variant_id=vid, object_id=node.id,
                    expected=f"> {rules.reuse_within_m} м или камера заполнена", actual=round(d, 3))
        elif fits_strict or fits_lenient:
            f.warning("topology.reuse_existing_chamber",
                      message + f"; число занятых примыканий неоднозначно (по допуску 0,5 м — {strict}, по 3 м — {lenient})",
                      variant_id=vid, object_id=node.id, actual=round(d, 3))


def _check_existing_chamber(node: Node, lines: list[InputLine], rules: Rules, f: Findings) -> None:
    vid = node.segments[0].variant_id if node.segments else None
    point = Point(node.xy)
    strict, _ = existing_occupancy(point, lines, rules.attach_tol_m)
    lenient, _ = existing_occupancy(point, lines, LENIENT_ATTACH_TOL_M)
    new_degree = node.degree
    if strict + new_degree > rules.max_connections:
        f.error("topology.max_connections",
                f"к существующей камере примыкает {strict + new_degree} участков (существующих {strict}, новых {new_degree})",
                variant_id=vid, object_id=node.id, expected=f"≤ {rules.max_connections}", actual=strict + new_degree)
    elif lenient + new_degree > rules.max_connections:
        f.warning("topology.max_connections",
                  f"по мягкому допуску 3 м у существующей камеры {lenient} примыканий, с новыми {new_degree} — больше предела",
                  variant_id=vid, object_id=node.id, expected=f"≤ {rules.max_connections}", actual=lenient + new_degree)


def check_technical_node_reasons(graph: VariantGraph, seg_crossings: dict[IdKey, frozenset], f: Findings) -> None:
    """Технический узел оправдан только сменой параметра участка (§2.1)."""
    vid = graph.variant.variant_id
    for node in graph.nodes.values():
        if node.kind != "tech" or node.degree != 2:
            continue
        a, b = node.segments
        same = (
            a.laying_method == b.laying_method
            and a.depth_start == b.depth_start and a.depth_end == b.depth_end
            and seg_crossings.get(a.key, frozenset()) == seg_crossings.get(b.key, frozenset())
        )
        if same:
            f.warning("topology.unjustified_tech_node",
                      "технический узел между участками с одинаковыми способом прокладки, глубиной и набором пересекаемых объектов",
                      variant_id=vid, object_id=node.id)
