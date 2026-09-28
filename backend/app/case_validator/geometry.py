"""Геометрия трассы: повороты, пересечения, сближения, зигзаги, подход к точке.

Поворот ≤ 90° проверяется и в вершинах LineString, и в узлах вдоль пути от
точки подключения к присоединению — участок, разбитый техузлом, всё равно
одна трасса. Зигзаги в приложении не определены количественно, поэтому их
признаки (§9.7 спецификации) — только предупреждения.
"""

from __future__ import annotations

from shapely.geometry import LineString, Point
from shapely.ops import unary_union

from app.case_validator.findings import Findings
from app.case_validator.geom_utils import dist, exterior_polygons, line_parts, turn_angle, turn_sign
from app.case_validator.model import IdKey, InputData, Restriction, Segment
from app.case_validator.rules import Rules
from app.case_validator.topology import Component, VariantGraph

MAX_TURN_DEG = 90.0
TURN_TOL_DEG = 0.05
NODE_TOL_M = 1e-3
MIN_SEGMENT_M = 3.0        # §9.7 (в): отрезки короче — признак излома
MICRO_EDGE_M = 1.0         # отрезок у узла короче — «хвостик» привязки, а не подход
STEP_LENGTH_M = 10.0       # §9.7 (б): два встречных поворота на отрезке короче
STEP_TURN_DEG = 20.0
PROXIMITY_EXTRA_M = 1.0    # §9.7: сближение новых участков (w1+w2)/2 + 1,0 м
NEAREST_BOUNDARY_TOL_M = 0.5
RAY_M = 200.0              # дальность луча от ближайшей границы при проверке её проходимости
NARROW_CORRIDOR_M = 1.5    # запас за своей полосой до чужой меньше этого — выход узкий (§9.1: T выносится на 1,5 м)


def check_geometry(graph: VariantGraph, inp: InputData, rules: Rules,
                   own: dict[IdKey, list[Restriction]], f: Findings) -> None:
    vid = graph.variant.variant_id
    segments = graph.variant.usable_segments
    for seg in segments:
        _check_vertices(seg, f)
    for comp in graph.components:
        if comp.is_tree:
            _check_node_turns(comp, f)
    _check_pairs(graph, rules, f)
    _check_chamber_spacing(graph, rules, f)
    for node in graph.nodes.values():
        if node.kind == "oks" and node.degree == 1:
            _check_final_segment(node.segments[0], node.key, node.id, inp, rules, own, f, vid)


def _check_vertices(seg: Segment, f: Findings) -> None:
    vid = seg.variant_id
    coords = seg.coords
    for i, (a, b) in enumerate(zip(coords, coords[1:])):
        if dist(a, b) < NODE_TOL_M:
            f.error("geometry.degenerate", f"вырожденный отрезок между вершинами {i} и {i + 1}",
                    variant_id=vid, object_id=seg.id)
            return
    turns = [turn_angle(coords[i - 1], coords[i], coords[i + 1]) for i in range(1, len(coords) - 1)]
    for i, angle in enumerate(turns, start=1):
        if angle > MAX_TURN_DEG + TURN_TOL_DEG:
            f.error("geometry.turn", f"поворот {angle:.2f}° в вершине {i} LineString",
                    variant_id=vid, object_id=seg.id, expected=f"≤ {MAX_TURN_DEG}°", actual=round(angle, 3))
    if seg.is_special:
        if len(coords) != 2:
            f.error("geometry.special_straight",
                    f"специальный проход выполняется одним прямым участком: LineString из {len(coords)} точек",
                    variant_id=vid, object_id=seg.id, expected=2, actual=len(coords))
        return
    # эвристики зигзага — только для обычных участков
    for i in range(1, len(coords) - 2):
        length = dist(coords[i], coords[i + 1])
        if length < MIN_SEGMENT_M:
            f.warning("geometry.short_edge",
                      f"отрезок {i}–{i + 1} длиной {length:.2f} м между поворотами — признак мелкого излома",
                      variant_id=vid, object_id=seg.id, expected=f"≥ {MIN_SEGMENT_M}", actual=round(length, 3))
    if len(coords) > 2:
        for i, label in ((0, "в начале"), (len(coords) - 2, "в конце")):
            length = dist(coords[i], coords[i + 1])
            if length < MICRO_EDGE_M:
                f.warning("geometry.micro_edge",
                          f"отрезок {length:.2f} м {label} участка у узла: микроизлом искажает угол в узле",
                          variant_id=vid, object_id=seg.id, expected=f"≥ {MICRO_EDGE_M}", actual=round(length, 3))
    for i in range(1, len(coords) - 2):
        a1, a2 = turns[i - 1], turns[i]
        s1 = turn_sign(coords[i - 1], coords[i], coords[i + 1])
        s2 = turn_sign(coords[i], coords[i + 1], coords[i + 2])
        length = dist(coords[i], coords[i + 1])
        if s1 * s2 < 0 and a1 > STEP_TURN_DEG and a2 > STEP_TURN_DEG and length < STEP_LENGTH_M:
            f.warning("geometry.step",
                      f"ступенька: повороты {a1:.0f}° и {a2:.0f}° в разные стороны на отрезке {length:.1f} м",
                      variant_id=vid, object_id=seg.id)


def _check_node_turns(comp: Component, f: Findings) -> None:
    """Угол между входящим и исходящим направлением в промежуточных узлах пути."""
    for seg in comp.segments:
        parent = comp.parent.get(seg.key)
        if parent is None:
            continue  # узел присоединения: угол не ограничен
        node = comp.near[seg.key]
        incoming = seg.coords_from(node.key)[::-1]      # приходит в узел
        outgoing = parent.coords_from(node.key)         # уходит из узла к корню
        angle = turn_angle(incoming[-2], incoming[-1], outgoing[1])
        if angle > MAX_TURN_DEG + TURN_TOL_DEG:
            short = min(dist(incoming[-2], incoming[-1]), dist(outgoing[0], outgoing[1]))
            hint = f" (отрезок у узла всего {short:.2f} м)" if short < MICRO_EDGE_M else ""
            f.error("geometry.turn",
                    f"поворот {angle:.2f}° в узле {node.id!r} между участками {seg.id!r} и {parent.id!r}{hint}",
                    variant_id=seg.variant_id, object_id=node.id, expected=f"≤ {MAX_TURN_DEG}°", actual=round(angle, 3))


def _shared_points(a: Segment, b: Segment) -> list[Point]:
    points = []
    for ra in (a.start_ref, a.end_ref):
        for rb in (b.start_ref, b.end_ref):
            if ra.key == rb.key:
                points.append(ra.geom)
    return points


def _linked_points(a: Segment, b: Segment, segments: list[Segment], radius: float) -> list[Point]:
    """Общие узлы пары и узлы, связанные коротким участком (спецпроход между техузлами)."""
    ends_a = {a.start_ref.key: a.start_ref, a.end_ref.key: a.end_ref}
    ends_b = {b.start_ref.key: b.start_ref, b.end_ref.key: b.end_ref}
    keys = set(ends_a) & set(ends_b)
    for c in segments:
        if c is a or c is b or c.geom.length > radius:
            continue
        c_keys = {c.start_ref.key, c.end_ref.key}
        if c_keys & set(ends_a) and c_keys & set(ends_b):
            keys |= c_keys & (set(ends_a) | set(ends_b))
    return [(ends_a.get(k) or ends_b[k]).geom for k in keys]


def _check_chamber_spacing(graph: VariantGraph, rules: Rules, f: Findings) -> None:
    chambers = [n for n in graph.nodes.values() if n.kind == "new_chamber"]
    for i, a in enumerate(chambers):
        for b in chambers[i + 1:]:
            d = dist(a.xy, b.xy)
            wa = max((rules.row(s.diameter).width_m for s in a.segments if rules.row(s.diameter)), default=0.0)
            wb = max((rules.row(s.diameter).width_m for s in b.segments if rules.row(s.diameter)), default=0.0)
            threshold = (wa + wb) / 2.0 + PROXIMITY_EXTRA_M
            if d < threshold - 1e-3:
                f.warning("geometry.chambers_too_close",
                          f"новые камеры {a.id!r} и {b.id!r} в {d:.2f} м друг от друга: сходящиеся ветки объединяются "
                          "в общий участок с одной камерой (§9.7 спецификации)",
                          variant_id=graph.variant.variant_id, object_id=a.id, expected=f"≥ {threshold:.2f} м", actual=round(d, 3))


def _check_pairs(graph: VariantGraph, rules: Rules, f: Findings) -> None:
    segments = graph.variant.usable_segments
    for i, a in enumerate(segments):
        row_a = rules.row(a.diameter)
        for b in segments[i + 1:]:
            if a.geom.distance(b.geom) > 10.0:
                continue
            row_b = rules.row(b.diameter)
            shared = _shared_points(a, b)
            inter = a.geom.intersection(b.geom)
            crossing = False
            if not inter.is_empty:
                pieces = list(getattr(inter, "geoms", [inter]))
                for piece in pieces:
                    if piece.geom_type == "Point" and any(piece.distance(p) <= NODE_TOL_M for p in shared):
                        continue
                    crossing = True
                    where = "накладываются" if piece.geom_type != "Point" else f"в точке ({piece.x:.1f}, {piece.y:.1f})"
                    f.error("geometry.crossing", f"участки {a.id!r} и {b.id!r} пересекаются вне общего узла: {where}",
                            variant_id=a.variant_id, object_id=a.id, actual=b.id)
                    break
            if crossing or row_a is None or row_b is None:
                continue
            threshold = (row_a.width_m + row_b.width_m) / 2.0 + PROXIMITY_EXTRA_M
            if a.geom.distance(b.geom) >= threshold - 1e-3:
                continue
            # «вне узлов»: окрестности общих узлов и узлов, связанных коротким
            # участком (соседи через спецпроход), исключаются
            radius = 2.0 * threshold
            ga, gb = a.geom, b.geom
            for p in _linked_points(a, b, segments, radius):
                disc = p.buffer(radius)
                ga, gb = ga.difference(disc), gb.difference(disc)
            if ga.is_empty or gb.is_empty:
                continue
            d = ga.distance(gb)
            if d < threshold - 1e-3:
                f.warning("geometry.proximity",
                          f"участки {a.id!r} и {b.id!r} сближаются до {d:.2f} м вне общего узла",
                          variant_id=a.variant_id, object_id=a.id, expected=f"≥ {threshold:.2f} м", actual=round(d, 3))


def _check_final_segment(seg: Segment, oks_key: IdKey, oks_id, inp: InputData, rules: Rules,
                         own: dict[IdKey, list[Restriction]], f: Findings, vid) -> None:
    polygons = own.get(oks_key, [])
    if not polygons:
        f.info("geometry.final_segment", "точка подключения не лежит в полигоне ОКС — финальный участок не проверяется",
               variant_id=vid, object_id=oks_id)
        return
    coords = seg.coords_from(oks_key)
    p, v1 = coords[0], coords[1]
    v1_point = Point(v1)
    for r in polygons:
        if any(poly.contains(v1_point) for poly in exterior_polygons(r.geom)):
            f.error("geometry.final_segment",
                    f"первая вершина трассы от точки подключения лежит внутри собственного полигона ОКС {r.id!r}: "
                    "финальный участок должен одной прямой выйти за границу полигона",
                    variant_id=vid, object_id=seg.id)
            return
    point = Point(p)
    exterior_rings = [LineString(poly.exterior.coords) for r in polygons for poly in exterior_polygons(r.geom)]
    d_nearest = min(ring.distance(point) for ring in exterior_rings)
    edge = LineString([p, v1])
    exit_points = []
    for ring in exterior_rings:
        inter = edge.intersection(ring)
        exit_points.extend(getattr(inter, "geoms", [inter]) if not inter.is_empty else [])
    exit_points = [g for g in exit_points if g.geom_type == "Point"]
    if not exit_points:
        return  # точка на стене или снаружи — выход за границу не требуется
    # исключение §2.2 — один прямой участок от границы до точки; если после выхода
    # тот же отрезок снова входит в здание, это уже запрещённое пересечение
    body = unary_union([poly for r in polygons for poly in exterior_polygons(r.geom)])
    inside = [g for g in line_parts(edge.intersection(body)) if g.length > NODE_TOL_M]
    if len(inside) > 1:
        f.error("geometry.final_segment",
                f"финальный участок после выхода из полигона снова входит в собственное здание "
                f"({len(inside)} отрезка внутри): исключение §2.2 действует только до первой границы",
                variant_id=vid, object_id=seg.id, expected=1, actual=len(inside))
        return
    d_exit = min(point.distance(g) for g in exit_points)
    if d_exit > d_nearest + NEAREST_BOUNDARY_TOL_M:
        status, note = _nearest_boundary_status(point, exterior_rings, polygons, seg, inp, rules)
        add = f.info if status == "reenter" else f.warning
        add("geometry.final_segment",
            f"финальный участок выходит из полигона в {d_exit:.2f} м от точки, ближайшая граница — в {d_nearest:.2f} м "
            f"(§2.2: «от ближайшей к точке границы»); {note}",
            variant_id=vid, object_id=seg.id, expected=round(d_nearest, 3), actual=round(d_exit, 3))


def _nearest_boundary_status(point: Point, rings: list[LineString], polygons: list[Restriction],
                             seg: Segment, inp: InputData, rules: Rules) -> tuple[str, str]:
    """Можно ли было выйти от ближайшей границы: прямой луч от неё должен
    выйти из собственной полосы отступа, не войдя снова в здание и не задев
    полосу чужого запрета. Отличает вынужденное отступление от §2.2 от
    необоснованного."""
    ring = min(rings, key=lambda r: r.distance(point))
    b = ring.interpolate(ring.project(point))
    d = dist((point.x, point.y), (b.x, b.y))
    if d < NODE_TOL_M:
        return "ok", "точка на границе"
    ux, uy = (b.x - point.x) / d, (b.y - point.y) / d
    row = rules.row(seg.diameter)
    half = row.width_m / 2.0 if row else 0.0
    own_clearance = rules.oks_clearance(seg.diameter) if row else rules.restrictions["oks"].clearance_m
    own_keys = {r.key for r in polygons}
    own_band = unary_union([r.geom.buffer(own_clearance + half) for r in polygons])
    own_body = unary_union([poly for r in polygons for poly in exterior_polygons(r.geom)])
    ray = LineString([(b.x + ux * NODE_TOL_M, b.y + uy * NODE_TOL_M), (b.x + ux * RAY_M, b.y + uy * RAY_M)])
    reenter = ray.intersection(own_body)
    s_reenter = b.distance(reenter) if not reenter.is_empty else None
    outside = ray.difference(own_band)
    s_exit = b.distance(outside) if not outside.is_empty else None
    if s_exit is None or (s_reenter is not None and s_reenter <= s_exit):
        back = f" через {s_reenter:.1f} м" if s_reenter is not None else ""
        return "reenter", f"ближайшая граница непроходима: прямой участок от неё снова входит в здание{back}"
    probe = LineString([(b.x + ux * NODE_TOL_M, b.y + uy * NODE_TOL_M), (b.x + ux * s_exit, b.y + uy * s_exit)])
    worst: tuple[float, Restriction] | None = None
    for r in inp.restrictions:
        rule = rules.restriction(r.rtype)
        if rule is None or not rule.is_block or r.key in own_keys:
            continue
        band = (rules.oks_clearance(seg.diameter) if r.rtype == "oks" and row else rule.clearance_m) + half
        gap = r.geom.distance(probe) - band
        if worst is None or gap < worst[0]:
            worst = (gap, r)
    if worst is not None and worst[0] < -NODE_TOL_M:
        return "blocked", (f"ближайшая граница заблокирована: полоса отступа {worst[1].rtype} {worst[1].id!r} "
                           f"перекрывает прямой выход (не хватает {-worst[0]:.2f} м)")
    if worst is not None and worst[0] < NARROW_CORRIDOR_M:
        return "narrow", (f"ближайшая граница проходима, но запас до полосы отступа {worst[1].rtype} {worst[1].id!r} "
                          f"всего {worst[0]:.2f} м")
    return "ok", "ближайшая граница проходима — отступление от §2.2 не обосновано"
