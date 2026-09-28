"""Разбор входа и выхода; проверки структуры по §7 приложения.

Здесь GeoJSON превращается в модель, а всё, что не разбирается, становится
находкой. Вход — файл организаторов, поэтому его аномалии только
предупреждения; выход проверяется строго: типы атрибутов, уникальность id,
состав вариантов, ранжирование и ссылки участков на узлы.
"""

from __future__ import annotations

from typing import Any

from shapely import make_valid
from shapely.geometry import LineString, Point, shape

from app.case_validator.findings import Findings
from app.case_validator.model import (
    IdKey,
    InputData,
    InputLine,
    InputPoint,
    NewChamber,
    NodeRef,
    OutputData,
    Restriction,
    Segment,
    Summary,
    TechNode,
    Variant,
    id_key,
    id_text,
    is_id,
    is_integer,
    is_number,
)
from app.case_validator.rules import Rules
from app.geo.crs import to_metric

CALC_CRS = "EPSG:32637"
COINCIDE_TOL_DEG = 1e-8
MAX_VARIANTS = 3

INPUT_TYPES = {"source", "heat_network", "heat_chamber", "oks_connection_point", "restriction"}
OUTPUT_TYPES = {"heat_network", "heat_chamber", "technical_node", "variant_summary"}
SUMMARY_NUMBERS = (
    "construction_cost",
    "chamber_construction_cost",
    "existing_chamber_tie_in_cost",
    "unconnected_penalty",
    "calculated_cost",
    "new_network_length",
    "score",
)


# --- геометрия --------------------------------------------------------------


def _position_ok(pos: Any) -> bool:
    return (
        isinstance(pos, (list, tuple))
        and len(pos) >= 2
        and all(is_number(c) for c in pos[:2])
    )


def parse_point(geometry: Any) -> tuple[tuple[float, float], Point] | None:
    if not isinstance(geometry, dict) or geometry.get("type") != "Point":
        return None
    pos = geometry.get("coordinates")
    if not _position_ok(pos):
        return None
    lonlat = (float(pos[0]), float(pos[1]))
    return lonlat, to_metric(Point(lonlat), CALC_CRS)


def parse_linestring(geometry: Any) -> tuple[list[tuple[float, float]], LineString] | None:
    if not isinstance(geometry, dict) or geometry.get("type") != "LineString":
        return None
    coords = geometry.get("coordinates")
    if not isinstance(coords, list) or len(coords) < 2 or not all(_position_ok(p) for p in coords):
        return None
    lonlat = [(float(p[0]), float(p[1])) for p in coords]
    return lonlat, to_metric(LineString(lonlat), CALC_CRS)


# --- вход -------------------------------------------------------------------


def parse_input(geojson: Any, rules: Rules, f: Findings) -> InputData:
    inp = InputData()
    features = geojson.get("features") if isinstance(geojson, dict) else None
    if not isinstance(features, list):
        f.error("input.structure", "входной файл — не FeatureCollection со списком features")
        return inp

    unknown_types: dict[str, int] = {}
    for index, feat in enumerate(features):
        props = feat.get("properties") if isinstance(feat, dict) else None
        if not isinstance(props, dict):
            f.warning("input.feature", f"вход: объект #{index} без properties пропущен")
            continue
        otype = props.get("object_type")
        oid = props.get("id")
        if not is_id(oid):
            f.warning("input.id", f"вход: объект #{index} ({otype}) без id пропущен")
            continue
        key = id_key(oid)
        if key in inp.keys:
            f.warning("input.id", "вход: повторяющийся id", object_id=oid)
        inp.keys.add(key)
        inp.kinds[key] = str(otype)
        inp.text_ids.setdefault(id_text(oid), []).append(oid)
        geometry = feat.get("geometry")

        if otype == "heat_network":
            parsed = parse_linestring(geometry)
            if parsed is None:
                f.warning("input.geometry", "вход: heat_network без LineString пропущен", object_id=oid)
                continue
            diameter = props.get("diameter")
            if not is_integer(diameter) or rules.row(diameter) is None:
                f.warning(
                    "input.diameter",
                    "вход: у существующего участка нет Ду из таблицы 1 — габарит принят нулевым",
                    object_id=oid, actual=diameter,
                )
                diameter = None
            inp.lines[key] = InputLine(oid, key, parsed[1], diameter)
        elif otype == "heat_chamber":
            parsed = parse_point(geometry)
            if parsed is None:
                f.warning("input.geometry", "вход: heat_chamber без Point пропущена", object_id=oid)
                continue
            inp.chambers[key] = InputPoint(oid, key, parsed[0], parsed[1])
        elif otype == "oks_connection_point":
            parsed = parse_point(geometry)
            if parsed is None:
                f.warning("input.geometry", "вход: точка подключения без Point пропущена", object_id=oid)
                continue
            flow = props.get("flow_tph")
            if not is_number(flow) and _number_from_text(flow) is not None:
                f.warning("input.flow", f"вход: flow_tph записан строкой {flow!r}, прочитан как число", object_id=oid)
                flow = _number_from_text(flow)
            if not is_number(flow) or flow < 0:
                f.warning("input.flow", "вход: у точки подключения нет flow_tph — принят 0", object_id=oid)
                flow = 0.0
            inp.oks[key] = InputPoint(oid, key, parsed[0], parsed[1], float(flow))
        elif otype == "restriction":
            rtype = props.get("restriction_type")
            try:
                geom = to_metric(shape(geometry), CALC_CRS)
            except Exception:  # любая битая геометрия — предупреждение, не падение
                f.warning("input.geometry", "вход: ограничение с неразборной геометрией пропущено", object_id=oid)
                continue
            if geom.is_empty:
                continue
            if not geom.is_valid:
                geom = make_valid(geom)
                f.info("input.geometry", "вход: невалидный полигон ограничения исправлен make_valid", object_id=oid)
            canonical = rules.canonical_type(rtype)
            if rules.restriction(canonical) is None:
                unknown_types[str(rtype)] = unknown_types.get(str(rtype), 0) + 1
            inp.restrictions.append(Restriction(oid, key, canonical, geom))
        elif otype == "source":
            pass
        else:
            f.info("input.object_type", f"вход: объект неизвестного типа «{otype}» не участвует в проверке", object_id=oid)

    for rtype, n in sorted(unknown_types.items()):
        f.info(
            "restriction.unknown_type",
            f"ограничения типа «{rtype}» ({n} шт.) не входят в таблицу 2 и не проверяются",
            actual=rtype,
        )
    return inp


# --- выход ------------------------------------------------------------------


class _Ctx:
    """Адрес находки: вариант и объект."""

    def __init__(self, f: Findings, variant_id: Any, object_id: Any) -> None:
        self.f = f
        self.variant_id = variant_id
        self.object_id = object_id

    def error(self, code: str, message: str, **kw: Any) -> None:
        self.f.error(code, message, variant_id=self.variant_id, object_id=self.object_id, **kw)

    def warning(self, code: str, message: str, **kw: Any) -> None:
        self.f.warning(code, message, variant_id=self.variant_id, object_id=self.object_id, **kw)


def _number(props: dict, name: str, ctx: _Ctx, *, minimum: float | None = None) -> float | None:
    if name not in props:
        ctx.error("structure.attribute", f"отсутствует обязательный атрибут {name}", expected=name)
        return None
    value = props[name]
    if not is_number(value):
        # Число строкой («24,87») — не по §1 приложения, но входные файлы из рабочих
        # систем так делают; читаем, как солвер, с предупреждением, а не отказом
        parsed = _number_from_text(value)
        if parsed is None:
            ctx.error("structure.attribute", f"{name} должен быть числом", expected="number", actual=repr(value))
            return None
        ctx.warning("structure.attribute", f"{name} записан строкой, прочитан как {parsed}",
                    expected="number", actual=repr(value))
        value = parsed
    if minimum is not None and value < minimum:
        ctx.error("structure.attribute", f"{name} меньше допустимого", expected=f">= {minimum}", actual=value)
        return None
    return float(value)


def _number_from_text(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, str):
        return None
    try:
        return float(value.strip().replace(",", "."))
    except ValueError:
        return None


def _integer(props: dict, name: str, ctx: _Ctx) -> int | None:
    if name not in props:
        ctx.error("structure.attribute", f"отсутствует обязательный атрибут {name}", expected=name)
        return None
    value = props[name]
    if not is_integer(value):
        ctx.error("structure.attribute", f"{name} должен быть целым числом", expected="integer", actual=repr(value))
        return None
    return value


def _depth(props: dict, name: str, ctx: _Ctx) -> float | None:
    if name not in props:
        ctx.error("structure.attribute", f"отсутствует обязательный атрибут {name} (null в 2D-режиме)", expected=name)
        return None
    value = props[name]
    if value is None:
        return None
    if not is_number(value):
        ctx.error("structure.attribute", f"{name} должен быть числом или null", actual=repr(value))
        return None
    return float(value)


def _ref_id(props: dict, name: str, ctx: _Ctx) -> Any:
    if name not in props or not is_id(props[name]):
        ctx.error("structure.attribute", f"{name} отсутствует или не является строкой/числом", expected=name,
                  actual=repr(props.get(name)))
        return None
    return props[name]


def _diameter(props: dict, ctx: _Ctx, rules: Rules) -> int | None:
    value = _integer(props, "diameter", ctx)
    if value is None:
        return None
    if rules.row(value) is None:
        ctx.error("structure.diameter", "Ду отсутствует в таблице 1", expected=[r.du for r in rules.rows], actual=value)
        return None
    return value


def parse_output(geojson: Any, inp: InputData, rules: Rules, f: Findings) -> OutputData:
    out = OutputData()
    if not isinstance(geojson, dict) or geojson.get("type") != "FeatureCollection":
        f.error("structure.collection", "выходной файл должен быть FeatureCollection", actual=type(geojson).__name__)
        return out
    features = geojson.get("features")
    if not isinstance(features, list):
        f.error("structure.collection", "в выходном файле нет списка features")
        return out

    seen: dict[IdKey, Any] = {}
    seen_text: dict[str, Any] = {}

    def variant_for(vid: Any) -> Variant:
        key = id_key(vid)
        if key not in out.variants:
            out.variants[key] = Variant(vid, key)
        return out.variants[key]

    for index, feat in enumerate(features):
        props = feat.get("properties") if isinstance(feat, dict) else None
        if not isinstance(props, dict):
            f.error("structure.feature", f"объект #{index} без properties", object_id=f"#{index}")
            continue
        otype = props.get("object_type")
        oid = props.get("id") if is_id(props.get("id")) else None
        vid = props.get("variant_id") if is_id(props.get("variant_id")) else None
        ctx = _Ctx(f, vid, oid if oid is not None else f"#{index}")
        if otype not in OUTPUT_TYPES:
            ctx.error("structure.object_type", "недопустимый object_type", expected=sorted(OUTPUT_TYPES), actual=otype)
            continue
        if oid is None:
            ctx.error("structure.id", "id отсутствует или не строка/число", actual=repr(props.get("id")))
            continue
        if vid is None:
            ctx.error("structure.variant_id", "variant_id отсутствует или не строка/число", actual=repr(props.get("variant_id")))
            continue
        key = id_key(oid)
        if key in seen:
            ctx.error("structure.id_unique", "id повторяется в выходном файле (в том числе между вариантами)", actual=oid)
        elif key in inp.keys:
            ctx.error("structure.id_unique", "id выходного объекта совпадает с id входного", actual=oid)
        else:
            text = id_text(oid)
            if text in seen_text or text in inp.text_ids:
                ctx.warning("structure.id_unique", "id совпадает с другим id при приведении к строке", actual=oid)
            seen_text[text] = oid
        seen[key] = oid
        variant = variant_for(vid)
        geometry = feat.get("geometry")

        if otype == "heat_network":
            parsed = parse_linestring(geometry)
            if parsed is None:
                ctx.error("structure.geometry", "heat_network должен быть LineString не менее чем из двух позиций")
            seg = Segment(oid, key, vid, index, parsed[0] if parsed else None, parsed[1] if parsed else None)
            seg.start_id = _ref_id(props, "start_node_id", ctx)
            seg.end_id = _ref_id(props, "end_node_id", ctx)
            seg.flow_tph = _number(props, "flow_tph", ctx, minimum=0.0)
            seg.diameter = _diameter(props, ctx, rules)
            if "length" in props:
                seg.length = _number(props, "length", ctx, minimum=0.0)
            else:
                # пример §7.3 приложения выгружен без length, хотя §7.2 его требует
                ctx.warning("structure.attribute", "length отсутствует — принята длина по геометрии в EPSG:32637")
            method = props.get("laying_method")
            if method not in ("base", "special"):
                ctx.error("structure.attribute", "laying_method должен быть base или special", actual=repr(method))
            else:
                seg.laying_method = method
            seg.depth_start = _depth(props, "depth_start", ctx)
            seg.depth_end = _depth(props, "depth_end", ctx)
            if (seg.depth_start is None) != (seg.depth_end is None):
                ctx.error("structure.depth", "глубина задана только на одном конце участка")
            seg.cost = _number(props, "cost", ctx, minimum=0.0)
            variant.segments[key] = seg
        elif otype == "heat_chamber":
            parsed = parse_point(geometry)
            if parsed is None:
                ctx.error("structure.geometry", "heat_chamber должна быть Point")
            chamber = NewChamber(oid, key, vid, index, parsed[0] if parsed else None, parsed[1] if parsed else None)
            chamber.diameter = _diameter(props, ctx, rules)
            chamber.cost = _number(props, "cost", ctx, minimum=0.0)
            variant.chambers[key] = chamber
        elif otype == "technical_node":
            parsed = parse_point(geometry)
            if parsed is None:
                ctx.error("structure.geometry", "technical_node должен быть Point")
            variant.tech_nodes[key] = TechNode(oid, key, vid, index, parsed[0] if parsed else None,
                                               parsed[1] if parsed else None)
        else:
            if geometry is not None:
                ctx.warning("structure.geometry", "у variant_summary ожидается geometry: null", actual=geometry.get("type") if isinstance(geometry, dict) else repr(geometry))
            summary = Summary(oid, key, vid, index)
            summary.rank = _integer(props, "rank", ctx)
            summary.existing_chamber_tie_in_count = _integer(props, "existing_chamber_tie_in_count", ctx)
            for name in SUMMARY_NUMBERS:
                setattr(summary, name, _number(props, name, ctx))
            ids = props.get("unconnected_oks_ids", ...)
            if ids is ...:
                ctx.error("structure.attribute", "отсутствует обязательный атрибут unconnected_oks_ids")
            elif not isinstance(ids, list):
                ctx.error("structure.attribute", "unconnected_oks_ids должен быть массивом (пустым, а не null)", actual=repr(ids))
            elif not all(is_id(v) for v in ids):
                ctx.error("structure.attribute", "unconnected_oks_ids содержит не строку/число", actual=ids)
            else:
                summary.unconnected_oks_ids = list(ids)
            variant.summaries.append(summary)

    _check_variants(out, f)
    _resolve_references(out, inp, f)
    return out


def _check_variants(out: OutputData, f: Findings) -> None:
    n = len(out.variants)
    if n == 0:
        f.error("structure.variants", "в файле нет ни одного варианта")
        return
    if n > MAX_VARIANTS:
        f.error("structure.variants", "вариантов больше допустимого", expected=f"1..{MAX_VARIANTS}", actual=n)
    ranked: list[tuple[Any, int, float]] = []
    for variant in out.variants.values():
        if len(variant.summaries) != 1:
            f.error("structure.summary", "на вариант должна приходиться ровно одна variant_summary",
                    variant_id=variant.variant_id, expected=1, actual=len(variant.summaries))
            continue
        summary = variant.summaries[0]
        if not variant.segments and not variant.chambers:
            f.info("structure.summary", "вариант без новых участков", variant_id=variant.variant_id)
        if summary.rank is not None and summary.score is not None:
            ranked.append((variant.variant_id, summary.rank, summary.score))
    if len(ranked) != n:
        return
    ranks = sorted(r for _, r, _ in ranked)
    if ranks != list(range(1, n + 1)):
        f.error("structure.rank", "rank должен быть перестановкой 1..N", expected=list(range(1, n + 1)), actual=ranks)
        return
    for vid_a, rank_a, score_a in ranked:
        for vid_b, rank_b, score_b in ranked:
            if score_a < score_b - 1e-9 and rank_a > rank_b:
                f.error("structure.rank", f"rank не согласован со score: у {vid_a!r} score меньше, чем у {vid_b!r}, а rank больше",
                        variant_id=vid_a, expected=f"rank < {rank_b}", actual=rank_a)


def _resolve_references(out: OutputData, inp: InputData, f: Findings) -> None:
    for variant in out.variants.values():
        others = [v for v in out.variants.values() if v is not variant]
        for seg in variant.segments.values():
            seg.start_ref = _resolve(seg, seg.start_id, "start_node_id", variant, others, inp, f)
            seg.end_ref = _resolve(seg, seg.end_id, "end_node_id", variant, others, inp, f)
            if seg.lonlat is None:
                continue
            for ref, lonlat, what in ((seg.start_ref, seg.lonlat[0], "начало"), (seg.end_ref, seg.lonlat[-1], "конец")):
                if ref is None:
                    continue
                _check_coincidence(seg, ref, lonlat, what, f)


def _lookup(key: IdKey, variant: Variant, inp: InputData) -> NodeRef | None:
    if key in inp.oks:
        p = inp.oks[key]
        return NodeRef("oks", key, p.id, p.lonlat, p.geom)
    if key in inp.chambers:
        p = inp.chambers[key]
        return NodeRef("existing_chamber", key, p.id, p.lonlat, p.geom)
    if key in variant.chambers:
        c = variant.chambers[key]
        if c.geom is None:
            return None
        return NodeRef("new_chamber", key, c.id, c.lonlat, c.geom)
    if key in variant.tech_nodes:
        t = variant.tech_nodes[key]
        if t.geom is None:
            return None
        return NodeRef("tech", key, t.id, t.lonlat, t.geom)
    return None


def _resolve(seg: Segment, ref_id: Any, name: str, variant: Variant, others: list[Variant],
             inp: InputData, f: Findings) -> NodeRef | None:
    if ref_id is None:
        return None
    key = id_key(ref_id)
    ctx = _Ctx(f, seg.variant_id, seg.id)
    ref = _lookup(key, variant, inp)
    if ref is not None:
        return ref
    if key in variant.chambers or key in variant.tech_nodes:
        return None  # узел есть, но без геометрии — ошибка уже записана
    for other in others:
        if key in other.chambers or key in other.tech_nodes:
            ctx.error("structure.reference", f"{name} ссылается на узел другого варианта ({other.variant_id!r})", actual=ref_id)
            return None
    if key in inp.keys:
        ctx.error("structure.reference", f"{name} ссылается на входной объект недопустимого типа",
                  expected="oks_connection_point | heat_chamber", actual=f"{ref_id!r}: {inp.kinds.get(key)}")
        return None
    if key in variant.segments or any(s.key == key for s in variant.summaries):
        ctx.error("structure.reference", f"{name} ссылается не на узел", actual=ref_id)
        return None
    # мягкое совпадение по строке: 1 ↔ "1"
    for candidate in inp.text_ids.get(id_text(ref_id), []):
        ref = _lookup(id_key(candidate), variant, inp)
        if ref is not None:
            ctx.warning("structure.reference", f"{name} совпадает с id узла только по строковому представлению — тип id должен сохраняться",
                        expected=repr(candidate), actual=repr(ref_id))
            return ref
    for node in list(variant.chambers.values()) + list(variant.tech_nodes.values()):
        if id_text(node.id) == id_text(ref_id):
            ref = _lookup(node.key, variant, inp)
            if ref is not None:
                ctx.warning("structure.reference", f"{name} совпадает с id узла только по строковому представлению",
                            expected=repr(node.id), actual=repr(ref_id))
                return ref
    ctx.error("structure.reference", f"{name} не найден среди точек подключения, камер и технических узлов варианта", actual=ref_id)
    return None


def _check_coincidence(seg: Segment, ref: NodeRef, lonlat: tuple[float, float], what: str, f: Findings) -> None:
    dlon = abs(lonlat[0] - ref.lonlat[0])
    dlat = abs(lonlat[1] - ref.lonlat[1])
    if dlon <= COINCIDE_TOL_DEG and dlat <= COINCIDE_TOL_DEG:
        if ref.kind in ("oks", "existing_chamber") and lonlat != ref.lonlat:
            f.warning("structure.node_coincidence",
                      f"{what} участка не совпадает побитово с координатами входного узла {ref.id!r} (ожидается точное копирование)",
                      variant_id=seg.variant_id, object_id=seg.id, expected=list(ref.lonlat), actual=list(lonlat))
        return
    metric = Point(seg.geom.coords[0] if what == "начало" else seg.geom.coords[-1]).distance(ref.geom)
    f.error("structure.node_coincidence",
            f"{what} участка не совпадает с узлом {ref.id!r}: расхождение {metric:.3f} м",
            variant_id=seg.variant_id, object_id=seg.id, expected=f"≤ {COINCIDE_TOL_DEG}°", actual=max(dlon, dlat))
