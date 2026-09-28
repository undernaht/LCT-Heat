/** Подготовка GeoJSON для карты: вход и результат раскладываются по источникам.
 *
 * Чистые функции без MapLibre — считаются в useMemo и легко проверяются.
 * Всё, чего нет в выходном файле, но нужно на карте (врезки в существующие
 * камеры, неподключённые точки), собирается здесь из сводки и входа.
 */

import type { Feature, FeatureCollection, Geometry, LineString, MultiLineString, Position } from "geojson";

import { ICONS } from "./mapStyle";
import type {
  Id,
  NodeKind,
  NodeSummary,
  OutChamberProps,
  OutSegmentProps,
  OutTechNodeProps,
  Summary,
  VariantSummary,
} from "./types";

export type BBox = [number, number, number, number];

export interface InputLayers {
  restrictionsPoly: FeatureCollection;
  restrictionsLine: FeatureCollection;
  network: FeatureCollection;
  chambers: FeatureCollection;
  source: FeatureCollection;
  oks: FeatureCollection;
  bbox: BBox | null;
  /** Типы ограничений, встретившиеся во входе (для легенды). */
  restrictionTypes: string[];
  unknownTypes: string[];
}

export interface OksLabel {
  id: string;
  position: Position;
  flow_tph: number;
}

export interface SegmentLabel {
  variant_id: string;
  position: Position;
  text: string;
}

export interface ResultLayers {
  lines: FeatureCollection;
  nodes: FeatureCollection;
  segmentLabels: SegmentLabel[];
}

/** Объект для подсветки на карте: сам объект (с уже подготовленными для попапа
 *  свойствами) и слой, в стиле которого показать попап. */
export interface Highlight {
  layer: "result-base" | "result-nodes" | "oks-points" | "chambers-existing" | "network-line";
  feature: Feature;
}

/** Найти объект по id из протокола валидатора: сначала среди новых участков и
 *  узлов варианта (v1_net_3, v1_chamber_5, врезка в ТК по входному id), потом
 *  среди точек подключения, существующих камер и участков входа. null —
 *  объекта с геометрией нет (например, v1_summary). */
export function locateObject(
  objectId: Id | null | undefined,
  variantId: string | null,
  result: ResultLayers | null,
  input: InputLayers | null,
): Highlight | null {
  const id = key(objectId);
  if (!id) return null;
  const inVariant = (f: Feature) =>
    key(f.properties?.id as Id) === id && (!variantId || f.properties?.variant_id === variantId);
  const line = result?.lines.features.find(inVariant);
  if (line) return { layer: "result-base", feature: line };
  const node = result?.nodes.features.find(inVariant);
  if (node) return { layer: "result-nodes", feature: node };
  const oks = input?.oks.features.find((f) => key(f.properties?.id as Id) === id);
  if (oks) return { layer: "oks-points", feature: oks };
  const chamber = input?.chambers.features.find((f) => key(f.properties?.id as Id) === id);
  if (chamber) return { layer: "chambers-existing", feature: chamber };
  const edge = input?.network.features.find((f) => key(f.properties?.id as Id) === id);
  if (edge) return { layer: "network-line", feature: edge };
  return null;
}

/** Существующий участок входа по id — для панели добавочного расхода. */
export function locateExistingEdge(edgeId: Id, input: InputLayers | null): Highlight | null {
  const id = key(edgeId);
  const edge = input?.network.features.find((f) => key(f.properties?.id as Id) === id);
  return edge ? { layer: "network-line", feature: edge } : null;
}

const fc = (features: Feature[]): FeatureCollection => ({ type: "FeatureCollection", features });

/** Объекты коллекции или пусто: бэкенд принимает любой JSON (паспорт покажет
 *  ошибку «ожидался FeatureCollection»), а карта не должна падать на нём. */
const featuresOf = (collection: FeatureCollection | null | undefined): Feature[] =>
  Array.isArray(collection?.features) ? collection.features.filter((f) => f && typeof f === "object") : [];

export const key = (id: Id | null | undefined): string => (id === null || id === undefined ? "" : String(id));

const VARIANT_PREFIX = /^v\d+_/;

/** Короткое имя узла для таблиц и попапов: «v1_chamber_2» → «новая ТК 2». */
export function nodeLabel(kind: NodeKind | undefined, publicId: Id): string {
  const id = key(publicId);
  const short = id.replace(VARIANT_PREFIX, "");
  switch (kind) {
    case "oks":
      return `ОКС #${id}`;
    case "chamber_existing":
      return `сущ. ТК ${id}`;
    case "chamber_new":
      return `новая ТК ${short.replace(/^chamber_/, "")}`;
    case "technical_node":
      return `техузел ${short.replace(/^tn_/, "")}`;
    default:
      if (short.startsWith("chamber_")) return `новая ТК ${short.slice(8)}`;
      if (short.startsWith("tn_")) return `техузел ${short.slice(3)}`;
      return id;
  }
}

/** Виды узлов по внутреннему id участка: (start, end) — из сводки варианта. */
export function segmentNodeKinds(variant: VariantSummary): Map<string, [NodeKind | undefined, NodeKind | undefined]> {
  const kinds = new Map(variant.nodes.map((n) => [n.id, n.kind] as const));
  return new Map(variant.segments.map((s) => [s.id, [kinds.get(s.start), kinds.get(s.end)]]));
}

function walk(geometry: Geometry | null, visit: (p: Position) => void): void {
  if (!geometry) return;
  switch (geometry.type) {
    case "Point":
      visit(geometry.coordinates);
      break;
    case "MultiPoint":
    case "LineString":
      geometry.coordinates.forEach(visit);
      break;
    case "MultiLineString":
    case "Polygon":
      geometry.coordinates.forEach((ring) => ring.forEach(visit));
      break;
    case "MultiPolygon":
      geometry.coordinates.forEach((poly) => poly.forEach((ring) => ring.forEach(visit)));
      break;
    case "GeometryCollection":
      geometry.geometries.forEach((g) => walk(g, visit));
      break;
  }
}

export function bboxOf(collection: FeatureCollection): BBox | null {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const feature of featuresOf(collection)) {
    walk(feature.geometry, ([x, y]) => {
      if (x < minX) minX = x;
      if (y < minY) minY = y;
      if (x > maxX) maxX = x;
      if (y > maxY) maxY = y;
    });
  }
  return Number.isFinite(minX) ? [minX, minY, maxX, maxY] : null;
}

/** Точка на середине ломаной по длине (в градусах с поправкой на широту). */
export function midpoint(geometry: LineString | MultiLineString): Position | null {
  const lines = geometry.type === "LineString" ? [geometry.coordinates] : geometry.coordinates;
  const coords = lines.flat();
  if (coords.length === 0) return null;
  if (coords.length === 1) return coords[0];
  const kx = Math.cos((coords[0][1] * Math.PI) / 180);
  const seg: number[] = [];
  let total = 0;
  for (let i = 1; i < coords.length; i += 1) {
    const dx = (coords[i][0] - coords[i - 1][0]) * kx;
    const dy = coords[i][1] - coords[i - 1][1];
    const d = Math.hypot(dx, dy);
    seg.push(d);
    total += d;
  }
  let rest = total / 2;
  for (let i = 0; i < seg.length; i += 1) {
    if (rest <= seg[i] || i === seg.length - 1) {
      const t = seg[i] > 0 ? rest / seg[i] : 0;
      const [x1, y1] = coords[i];
      const [x2, y2] = coords[i + 1];
      return [x1 + (x2 - x1) * t, y1 + (y2 - y1) * t];
    }
    rest -= seg[i];
  }
  return coords[0];
}

export function buildInputLayers(input: FeatureCollection, knownTypes: Set<string>): InputLayers {
  const poly: Feature[] = [];
  const line: Feature[] = [];
  const network: Feature[] = [];
  const chambers: Feature[] = [];
  const source: Feature[] = [];
  const oks: Feature[] = [];
  const types = new Set<string>();
  const unknown = new Set<string>();

  for (const feature of featuresOf(input)) {
    const props = feature.properties ?? {};
    const geometry = feature.geometry;
    if (!geometry || typeof geometry !== "object" || !geometry.type) continue;
    const id = key(props.id as Id);
    switch (props.object_type) {
      case "restriction": {
        const type = String(props.restriction_type ?? "");
        const known = knownTypes.has(type);
        types.add(type);
        if (!known) unknown.add(type);
        const out: Feature = {
          type: "Feature",
          geometry,
          properties: { id, restriction_type: type, known, address: props.address ?? "" },
        };
        if (geometry.type === "Polygon" || geometry.type === "MultiPolygon") poly.push(out);
        else line.push(out);
        break;
      }
      case "heat_network":
        network.push({
          type: "Feature",
          geometry,
          properties: { id, diameter: Number(props.diameter) || 100 },
        });
        break;
      case "heat_chamber":
        chambers.push({ type: "Feature", geometry, properties: { id } });
        break;
      case "source":
        source.push({ type: "Feature", geometry, properties: { id, name: props.name ?? "" } });
        break;
      case "oks_connection_point":
        oks.push({
          type: "Feature",
          geometry,
          properties: { id, flow_tph: Number(props.flow_tph) || 0 },
        });
        break;
      default:
        break;
    }
  }

  return {
    restrictionsPoly: fc(poly),
    restrictionsLine: fc(line),
    network: fc(network),
    chambers: fc(chambers),
    source: fc(source),
    oks: fc(oks),
    bbox: bboxOf(input),
    restrictionTypes: [...types].sort(),
    unknownTypes: [...unknown].sort(),
  };
}

export function oksLabels(layers: InputLayers | null): OksLabel[] {
  if (!layers) return [];
  return layers.oks.features
    .filter((f) => f.geometry?.type === "Point")
    .map((f) => ({
      id: String(f.properties?.id ?? ""),
      position: (f.geometry as { coordinates: Position }).coordinates,
      flow_tph: Number(f.properties?.flow_tph ?? 0),
    }));
}

function pointOf(collection: FeatureCollection, id: string): Position | null {
  const feature = collection.features.find((f) => key(f.properties?.id as Id) === id);
  return feature && feature.geometry?.type === "Point" ? feature.geometry.coordinates : null;
}

/** Новые участки и узлы всех вариантов; фильтрация по варианту — на карте. */
export function buildResultLayers(
  output: FeatureCollection,
  summary: Summary | null,
  input: InputLayers | null,
): ResultLayers {
  const lines: Feature[] = [];
  const nodes: Feature[] = [];
  const labels: SegmentLabel[] = [];
  const kindsByVariant = new Map(
    (summary?.variants ?? []).map((v) => [v.id, segmentNodeKinds(v)] as const),
  );

  for (const feature of featuresOf(output)) {
    const props = feature.properties ?? {};
    const geometry = feature.geometry;
    if (!geometry) continue;
    if (props.object_type === "heat_network") {
      const p = props as OutSegmentProps;
      const [startKind, endKind] = kindsByVariant.get(p.variant_id)?.get(p.internal_id) ?? [undefined, undefined];
      lines.push({
        type: "Feature",
        geometry,
        properties: {
          id: p.id,
          variant_id: p.variant_id,
          internal_id: p.internal_id,
          start_node_id: key(p.start_node_id),
          end_node_id: key(p.end_node_id),
          start_title: nodeLabel(startKind, p.start_node_id),
          end_title: nodeLabel(endKind, p.end_node_id),
          flow_tph: p.flow_tph,
          diameter: Number(p.diameter) || 100,
          length: p.length,
          laying_method: p.laying_method,
          k_special: p.k_special ?? 1,
          special_types: Array.isArray(p.special_types) ? p.special_types.join(", ") : "",
          depth_start: p.depth_start ?? null,
          depth_end: p.depth_end ?? null,
          k_depth: p.k_depth ?? 1,
          cost: p.cost,
        },
      });
      if (geometry.type === "LineString" || geometry.type === "MultiLineString") {
        const position = midpoint(geometry);
        if (position) {
          labels.push({
            variant_id: p.variant_id,
            position,
            text: `Ду${p.diameter} · ${Number(p.flow_tph).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} т/ч`,
          });
        }
      }
    } else if (props.object_type === "heat_chamber") {
      const p = props as OutChamberProps;
      // Камера на существующем участке — точка врезки (§2.4): свой значок,
      // чтобы места присоединения читались на карте без попапа
      const onExisting = key(p.on_existing_network);
      nodes.push({
        type: "Feature",
        geometry,
        properties: {
          id: p.id,
          variant_id: p.variant_id,
          kind: "chamber_new",
          icon: onExisting ? ICONS.tieInNew : ICONS.chamberNew,
          diameter: p.diameter,
          cost: p.cost,
          on_existing_network: onExisting,
        },
      });
    } else if (props.object_type === "technical_node") {
      const p = props as OutTechNodeProps;
      nodes.push({
        type: "Feature",
        geometry,
        properties: { id: p.id, variant_id: p.variant_id, kind: "technical_node", icon: ICONS.techNode },
      });
    }
  }

  if (summary && input) {
    for (const variant of summary.variants) {
      const degree = new Map<string, number>();
      for (const segment of variant.segments) {
        degree.set(segment.start, (degree.get(segment.start) ?? 0) + 1);
        degree.set(segment.end, (degree.get(segment.end) ?? 0) + 1);
      }
      for (const node of variant.nodes as NodeSummary[]) {
        if (node.kind !== "chamber_existing") continue;
        const ref = key(node.ref);
        const position = pointOf(input.chambers, ref);
        if (!position) continue;
        nodes.push({
          type: "Feature",
          geometry: { type: "Point", coordinates: position },
          properties: {
            id: ref,
            variant_id: variant.id,
            kind: "tie_in",
            icon: ICONS.tieIn,
            connections_used: node.connections_used ?? 0,
            new_segments: degree.get(node.id) ?? 0,
          },
        });
      }
      for (const oksId of variant.unconnected_oks_ids) {
        const ref = key(oksId);
        const position = pointOf(input.oks, ref);
        if (!position) continue;
        nodes.push({
          type: "Feature",
          geometry: { type: "Point", coordinates: position },
          properties: {
            id: ref,
            variant_id: variant.id,
            kind: "unconnected",
            icon: ICONS.unconnected,
            reason: variant.unconnected_reasons?.[ref] ?? "",
          },
        });
      }
    }
  }

  return { lines: fc(lines), nodes: fc(nodes), segmentLabels: labels };
}
