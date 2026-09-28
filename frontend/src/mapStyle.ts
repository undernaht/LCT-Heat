/** Источники и слои карты — чистые данные без обращения к браузеру.
 *
 * Модуль намеренно не импортирует maplibre-gl (только типы): его читает и
 * MapView.tsx, и `scripts/validate-style.mjs`, который прогоняет те же слои
 * через валидатор стиля в Node. Невалидный слой MapLibre не роняет приложение —
 * он просто молча не добавляется, а карта остаётся пустой; поэтому проверка
 * без браузера обязательна.
 *
 * Грабли: `line-dasharray` не принимает выражений от данных, только от зума.
 * Поэтому пунктирные слои (спецпроходы, чужие варианты, неизвестные типы
 * ограничений) разведены с сплошными по отдельным слоям с фильтрами.
 */

import type {
  ExpressionSpecification,
  FilterSpecification,
  LayerSpecification,
  SourceSpecification,
} from "maplibre-gl";

export const OSM_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";

/** Цвета ограничений по типу — те же, что в backend/app/case/preview.py. */
export const RESTRICTION_FILL: Record<string, string> = {
  oks: "#d6d3d1",
  water: "#93c5fd",
  railway: "#a16207",
  road: "#e7e5e4",
  tram_tracks: "#c4b5fd",
  park: "#86efac",
  social_area: "#fde68a",
  prohibited_site: "#fca5a5",
  gas_pipeline: "#f97316",
  power_cable: "#facc15",
};
export const UNKNOWN_FILL = "#f0abfc";
export const UNKNOWN_OUTLINE = "#a21caf";

export const RESTRICTION_TITLE: Record<string, string> = {
  oks: "ОКС (здания)",
  water: "водные объекты",
  railway: "железная дорога",
  road: "автодорога",
  tram_tracks: "трамвайные пути",
  park: "парк, озеленение",
  social_area: "социальная территория",
  prohibited_site: "запретная площадка",
  gas_pipeline: "газопровод",
  power_cable: "силовой кабель",
};

/** Шкала условных диаметров новых участков (preview.py DU_COLOR). */
export const DU_COLOR: Record<number, string> = {
  50: "#22c55e",
  65: "#16a34a",
  80: "#15803d",
  100: "#0ea5e9",
  125: "#0284c7",
  150: "#1d4ed8",
  200: "#7c3aed",
  250: "#a21caf",
  300: "#be185d",
  400: "#b91c1c",
};
export const DU_FALLBACK = "#0f172a";

export const EXISTING_NETWORK = "#7f1d1d";
export const SOURCE_COLOR = "#f59e0b";
export const OKS_COLOR = "#16a34a";
export const UNCONNECTED_COLOR = "#dc2626";

/** Цвета «чужих» вариантов при сравнении — по порядковому номеру варианта. */
export const VARIANT_COLOR: Record<string, string> = {
  v1: "#0369a1",
  v2: "#7c3aed",
  v3: "#b45309",
};
export const VARIANT_FALLBACK = "#475569";

/** Фильтр «ничего»: до выбора варианта результирующие слои пусты. */
export const NONE_FILTER: FilterSpecification = ["==", ["get", "variant_id"], "__none__"];

function matchExpr(
  property: string,
  table: Record<string | number, string>,
  fallback: string,
): ExpressionSpecification {
  const pairs: Array<string | number> = [];
  for (const [key, colour] of Object.entries(table)) {
    pairs.push(Number.isNaN(Number(key)) ? key : Number(key), colour);
  }
  return ["match", ["get", property], ...pairs, fallback] as unknown as ExpressionSpecification;
}

const FILL_COLOR = matchExpr("restriction_type", RESTRICTION_FILL, UNKNOWN_FILL);
const OUTLINE_COLOR: ExpressionSpecification = [
  "match", ["get", "restriction_type"],
  "oks", "#a8a29e",
  "water", "#3b82f6",
  "railway", "#713f12",
  "road", "#a8a29e",
  "tram_tracks", "#7c3aed",
  "park", "#15803d",
  "social_area", "#b45309",
  "prohibited_site", "#b91c1c",
  "gas_pipeline", "#c2410c",
  "power_cable", "#a16207",
  UNKNOWN_OUTLINE,
];
const DU_LINE_COLOR = matchExpr("diameter", DU_COLOR, DU_FALLBACK);
const VARIANT_LINE_COLOR = matchExpr("variant_id", VARIANT_COLOR, VARIANT_FALLBACK);

/** Толщина новых участков по Ду. */
const DU_WIDTH: ExpressionSpecification = [
  "interpolate", ["linear"], ["get", "diameter"],
  50, 2.5, 200, 4.5, 400, 6.5, 1400, 9,
];
/** Толщина существующей сети по Ду. */
const NET_WIDTH: ExpressionSpecification = [
  "interpolate", ["linear"], ["get", "diameter"],
  50, 1.5, 300, 3.5, 600, 5.5, 1400, 8,
];

const ROUND = { "line-cap": "round" as const, "line-join": "round" as const };

const empty = (): SourceSpecification => ({
  type: "geojson",
  data: { type: "FeatureCollection", features: [] },
});

export const SOURCES: Record<string, SourceSpecification> = {
  basemap: {
    type: "raster",
    tiles: [OSM_TILES],
    tileSize: 256,
    maxzoom: 19,
    attribution: "Подложка © участники OpenStreetMap (ODbL)",
  },
  "restrictions-poly": empty(),
  "restrictions-line": empty(),
  network: empty(),
  chambers: empty(),
  "source-point": empty(),
  oks: empty(),
  "result-lines": empty(),
  "result-nodes": empty(),
  /** Один подсвеченный объект (находка валидатора): линия или точка. */
  highlight: empty(),
};

/** Цвет ореола подсветки — жёлтый, не занят ни одним типом объектов на карте. */
export const HIGHLIGHT_COLOR = "#facc15";

/** Имена растровых значков, которые MapView регистрирует через map.addImage(). */
export const ICONS = {
  chamberExisting: "chamber-existing",
  chamberNew: "chamber-new",
  /** Новая камера на существующем участке — точка врезки. */
  tieInNew: "chamber-tie-in-new",
  /** Врезка в существующую камеру. */
  tieIn: "chamber-tie-in",
  techNode: "tech-node",
  unconnected: "oks-unconnected",
} as const;

/** Порядок массива — порядок отрисовки: первое внизу. */
export const LAYERS: LayerSpecification[] = [
  {
    id: "basemap", type: "raster", source: "basemap",
    paint: { "raster-saturation": -0.65, "raster-opacity": 0.85 },
  },

  // --- ограничения ---
  {
    id: "restr-fill", type: "fill", source: "restrictions-poly",
    paint: { "fill-color": FILL_COLOR, "fill-opacity": 0.55 },
  },
  {
    id: "restr-outline", type: "line", source: "restrictions-poly",
    filter: ["==", ["get", "known"], true],
    paint: { "line-color": OUTLINE_COLOR, "line-width": 0.8, "line-opacity": 0.8 },
  },
  {
    id: "restr-outline-unknown", type: "line", source: "restrictions-poly",
    filter: ["==", ["get", "known"], false],
    paint: { "line-color": UNKNOWN_OUTLINE, "line-width": 1.4, "line-dasharray": [2, 1.5] },
  },
  {
    id: "restr-line", type: "line", source: "restrictions-line",
    filter: ["==", ["get", "known"], true],
    layout: ROUND,
    paint: {
      "line-color": FILL_COLOR,
      "line-width": ["interpolate", ["linear"], ["zoom"], 13, 1.5, 18, 4.5],
      "line-opacity": 0.9,
    },
  },
  {
    id: "restr-line-unknown", type: "line", source: "restrictions-line",
    filter: ["==", ["get", "known"], false],
    layout: ROUND,
    paint: { "line-color": UNKNOWN_OUTLINE, "line-width": 2, "line-dasharray": [2, 1.5] },
  },

  // --- существующая сеть ---
  {
    id: "network-casing", type: "line", source: "network",
    layout: ROUND,
    paint: { "line-color": "#ffffff", "line-width": ["+", NET_WIDTH, 2.5], "line-opacity": 0.75 },
  },
  {
    id: "network-line", type: "line", source: "network",
    layout: ROUND,
    paint: { "line-color": EXISTING_NETWORK, "line-width": NET_WIDTH, "line-opacity": 0.92 },
  },

  // --- результат: чужие варианты полупрозрачным пунктиром ---
  {
    id: "result-alt", type: "line", source: "result-lines",
    filter: NONE_FILTER,
    layout: ROUND,
    paint: {
      "line-color": VARIANT_LINE_COLOR,
      "line-width": 3,
      "line-opacity": 0.5,
      "line-dasharray": [2, 2],
    },
  },

  // --- подсветка объекта из протокола валидатора: ореол под результатом ---
  {
    id: "highlight-line", type: "line", source: "highlight",
    filter: ["==", ["geometry-type"], "LineString"],
    layout: ROUND,
    paint: { "line-color": HIGHLIGHT_COLOR, "line-width": ["+", DU_WIDTH, 12], "line-opacity": 0.8 },
  },

  // --- результат: выбранный вариант ---
  {
    id: "result-casing", type: "line", source: "result-lines",
    filter: NONE_FILTER,
    layout: ROUND,
    paint: { "line-color": "#ffffff", "line-width": ["+", DU_WIDTH, 3], "line-opacity": 0.9 },
  },
  {
    id: "result-base", type: "line", source: "result-lines",
    filter: NONE_FILTER,
    layout: ROUND,
    paint: { "line-color": DU_LINE_COLOR, "line-width": DU_WIDTH },
  },
  {
    id: "result-special", type: "line", source: "result-lines",
    filter: NONE_FILTER,
    layout: { "line-cap": "butt", "line-join": "round" },
    paint: { "line-color": DU_LINE_COLOR, "line-width": DU_WIDTH, "line-dasharray": [1.6, 1.2] },
  },

  // --- точки ---
  {
    id: "highlight-point", type: "circle", source: "highlight",
    filter: ["==", ["geometry-type"], "Point"],
    paint: {
      "circle-radius": 16,
      "circle-color": HIGHLIGHT_COLOR,
      "circle-opacity": 0.75,
      "circle-stroke-color": "#a16207",
      "circle-stroke-width": 1.5,
    },
  },
  {
    id: "chambers-existing", type: "symbol", source: "chambers",
    layout: {
      "icon-image": ICONS.chamberExisting,
      "icon-allow-overlap": true,
      "icon-ignore-placement": true,
    },
  },
  {
    id: "source-point", type: "circle", source: "source-point",
    paint: {
      "circle-radius": 9,
      "circle-color": SOURCE_COLOR,
      "circle-stroke-color": "#78350f",
      "circle-stroke-width": 2,
    },
  },
  {
    id: "oks-points", type: "circle", source: "oks",
    paint: {
      "circle-radius": 6,
      "circle-color": OKS_COLOR,
      "circle-stroke-color": "#ffffff",
      "circle-stroke-width": 1.5,
    },
  },
  {
    id: "result-nodes", type: "symbol", source: "result-nodes",
    filter: NONE_FILTER,
    layout: {
      "icon-image": ["get", "icon"],
      "icon-allow-overlap": true,
      "icon-ignore-placement": true,
    },
  },
];

/** Слои, по которым можно кликнуть и получить попап. Порядок — приоритет:
 *  узлы лежат поверх линий, поэтому при попадании в оба берётся узел. */
export const CLICKABLE_LAYERS = [
  "result-nodes",
  "oks-points",
  "source-point",
  "chambers-existing",
  "result-base",
  "result-special",
  "result-alt",
  "network-line",
  "restr-line",
  "restr-line-unknown",
  "restr-fill",
] as const;
