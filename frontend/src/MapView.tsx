/** Карта MapLibre: вход (ограничения, существующая сеть, точки подключения) и
 * результат выбранного варианта. Спецификации слоёв — в mapStyle.ts.
 *
 * Подложка — растровые тайлы OSM; при отсутствии сети карта остаётся рабочей:
 * все содержательные слои приходят из нашего API, тайлы только фон.
 *
 * Подписи (точки подключения, Ду участков) — DOM-маркеры, а не symbol-слои с
 * text-field: для текста MapLibre нужен сервер глифов, а маркеры работают
 * офлайн и позиционируются без кадра отрисовки.
 */

import { useEffect, useRef } from "react";
import * as maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type { FeatureCollection, Position } from "geojson";

import type { BBox, Highlight, InputLayers, OksLabel, ResultLayers } from "./layers";
import { bboxOf, midpoint } from "./layers";
import {
  CLICKABLE_LAYERS,
  EXISTING_NETWORK,
  ICONS,
  LAYERS,
  NONE_FILTER,
  RESTRICTION_TITLE,
  SOURCES,
  UNCONNECTED_COLOR,
} from "./mapStyle";
import { formatMoney, formatNumber, formatRub } from "./api";

const BASE_STYLE: maplibregl.StyleSpecification = {
  version: 8,
  sources: {},
  layers: [{ id: "bg", type: "background", paint: { "background-color": "#f7f7f5" } }],
};

interface Props {
  input: InputLayers | null;
  labels: OksLabel[];
  result: ResultLayers | null;
  variantId: string | null;
  /** Точки, не подключённые в выбранном варианте (id строками). */
  unconnected: Set<string>;
  showOthers: boolean;
  showBasemap: boolean;
  showLabels: boolean;
  /** Любое изменение — подогнать карту под границы входа. */
  fitToken: number;
  tieInCost: number;
  /** Объект из протокола валидатора: ореол, подлёт камеры и попап. */
  highlight: Highlight | null;
  onError?: (message: string | null) => void;
}

export function MapView({
  input,
  labels,
  result,
  variantId,
  unconnected,
  showOthers,
  showBasemap,
  showLabels,
  fitToken,
  tieInCost,
  highlight,
  onError,
}: Props) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const loaded = useRef(false);
  const pending = useRef<Array<() => void>>([]);
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;
  const tieInCostRef = useRef(tieInCost);
  tieInCostRef.current = tieInCost;
  const oksMarkers = useRef<maplibregl.Marker[]>([]);
  const segmentMarkers = useRef<maplibregl.Marker[]>([]);
  const popup = useRef<maplibregl.Popup | null>(null);

  /** Выполнить, когда источники и слои созданы (или сразу, если уже). */
  const whenReady = (fn: () => void) => {
    if (loaded.current) fn();
    else pending.current.push(fn);
  };

  // --- Инициализация ---
  useEffect(() => {
    if (!container.current || map.current) return;

    const instance = new maplibregl.Map({
      container: container.current,
      style: BASE_STYLE,
      center: [37.65, 55.69],
      zoom: 14,
      attributionControl: false,
    });
    instance.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    instance.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");
    instance.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-left");

    instance.on("error", (event) => {
      // Недоступные тайлы подложки — не ошибка приложения: содержательные
      // слои приходят из нашего API, а фон необязателен.
      const detail = event as unknown as { sourceId?: string; tile?: unknown; error?: { message?: string } };
      if (detail.sourceId === "basemap" || detail.tile) {
        console.warn("MapLibre: тайл подложки не загрузился", detail.error?.message ?? "");
        return;
      }
      console.error("MapLibre:", detail.error ?? event);
    });
    if (import.meta.env.DEV) {
      (window as unknown as { __map?: maplibregl.Map }).__map = instance;
    }

    // Инициализация вешается на `styledata`, а НЕ на `load`: `load` ждёт первого
    // отрисованного кадра, которого в скрытой вкладке не бывает никогда.
    // Внимание: map.addImage() синхронно порождает ещё один `styledata`, поэтому
    // слушатель снимается ДО добавления значков — иначе setup входит сам в себя
    // и каждый источник пытается добавиться дважды.
    let settingUp = false;
    const setup = () => {
      if (loaded.current || settingUp || !instance.isStyleLoaded()) return;
      settingUp = true;
      instance.off("styledata", setup);

      addIcons(instance);
      const failed: string[] = [];
      for (const [id, spec] of Object.entries(SOURCES)) {
        try {
          instance.addSource(id, spec);
        } catch (error) {
          failed.push(`источник ${id}`);
          console.error(`Источник ${id} не добавлен:`, error);
        }
      }
      // Каждый слой отдельно и в try/catch: один неверный стиль не должен
      // оставлять карту пустой, а причина должна быть видна.
      for (const spec of LAYERS) {
        try {
          instance.addLayer(spec);
        } catch (error) {
          failed.push(spec.id);
          console.error(`Слой ${spec.id} не добавлен:`, error);
        }
      }
      onErrorRef.current?.(failed.length ? `Слои карты не добавились: ${failed.join(", ")}` : null);
      instance.resize();

      // Флаг поднимается ПОСЛЕ добавления слоёв, затем выполняется отложенное.
      loaded.current = true;
      const queue = pending.current;
      pending.current = [];
      queue.forEach((fn) => fn());
    };
    instance.on("styledata", setup);
    setup();

    // Курсор и попапы
    for (const layer of CLICKABLE_LAYERS) {
      instance.on("mouseenter", layer, () => {
        instance.getCanvas().style.cursor = "pointer";
      });
      instance.on("mouseleave", layer, () => {
        instance.getCanvas().style.cursor = "";
      });
    }
    instance.on("click", (event) => {
      if (!loaded.current) return;
      const layers = CLICKABLE_LAYERS.filter((id) => instance.getLayer(id));
      const hits = instance.queryRenderedFeatures(event.point, { layers: [...layers] });
      if (hits.length === 0) return;
      // Верхний слой в CLICKABLE_LAYERS стоит первым — берём самый «верхний» объект
      const priority = (f: maplibregl.MapGeoJSONFeature) =>
        (CLICKABLE_LAYERS as readonly string[]).indexOf(f.layer.id);
      const top = hits.slice().sort((a, b) => priority(a) - priority(b))[0];
      popup.current?.remove();
      popup.current = new maplibregl.Popup({ maxWidth: "340px", closeButton: true })
        .setLngLat(event.lngLat)
        .setHTML(popupHtml(top.layer.id, top.properties ?? {}, tieInCostRef.current))
        .addTo(instance);
    });

    // Если карта так и не инициализировалась — сказать об этом, а не оставлять
    // пустой экран. Самая частая причина: вкладка была скрыта при загрузке.
    const watchdog = window.setTimeout(() => {
      if (loaded.current) return;
      onErrorRef.current?.(
        document.hidden
          ? "Карта не инициализировалась: вкладка была скрыта при загрузке. Откройте вкладку и обновите страницу."
          : "Карта не инициализировалась. Проверьте, что в браузере доступен WebGL, и посмотрите консоль разработчика.",
      );
    }, 8000);

    // Размер flex-контейнера на момент инициализации ещё не посчитан
    const observer = new ResizeObserver(() => instance.resize());
    observer.observe(container.current);

    map.current = instance;
    return () => {
      window.clearTimeout(watchdog);
      observer.disconnect();
      popup.current?.remove();
      oksMarkers.current.forEach((m) => m.remove());
      segmentMarkers.current.forEach((m) => m.remove());
      instance.remove();
      map.current = null;
      loaded.current = false;
      pending.current = [];
    };
  }, []);

  // --- Данные входа ---
  useEffect(() => {
    const instance = map.current;
    if (!instance) return;
    whenReady(() => {
      const empty: FeatureCollection = { type: "FeatureCollection", features: [] };
      setData(instance, "restrictions-poly", input?.restrictionsPoly ?? empty);
      setData(instance, "restrictions-line", input?.restrictionsLine ?? empty);
      setData(instance, "network", input?.network ?? empty);
      setData(instance, "chambers", input?.chambers ?? empty);
      setData(instance, "source-point", input?.source ?? empty);
      setData(instance, "oks", input?.oks ?? empty);
    });
  }, [input]);

  // --- Подписи точек подключения ---
  useEffect(() => {
    const instance = map.current;
    if (!instance) return;
    oksMarkers.current.forEach((m) => m.remove());
    oksMarkers.current = labels.map((label) => {
      const element = document.createElement("div");
      element.className = unconnected.has(label.id) ? "oks-label oks-label-bad" : "oks-label";
      element.textContent = `#${label.id} · ${formatNumber(label.flow_tph, 2)} т/ч`;
      return new maplibregl.Marker({ element, anchor: "left", offset: [9, 0] })
        .setLngLat(label.position as [number, number])
        .addTo(instance);
    });
  }, [labels, unconnected]);

  // --- Результат ---
  useEffect(() => {
    const instance = map.current;
    if (!instance) return;
    whenReady(() => {
      const empty: FeatureCollection = { type: "FeatureCollection", features: [] };
      setData(instance, "result-lines", result?.lines ?? empty);
      setData(instance, "result-nodes", result?.nodes ?? empty);
    });
    popup.current?.remove();
  }, [result]);

  // --- Выбранный вариант и сравнение ---
  useEffect(() => {
    const instance = map.current;
    if (!instance) return;
    whenReady(() => {
      const selected: maplibregl.FilterSpecification = variantId
        ? ["==", ["get", "variant_id"], variantId]
        : NONE_FILTER;
      const base: maplibregl.FilterSpecification = variantId
        ? ["all", ["==", ["get", "variant_id"], variantId], ["==", ["get", "laying_method"], "base"]]
        : NONE_FILTER;
      const special: maplibregl.FilterSpecification = variantId
        ? ["all", ["==", ["get", "variant_id"], variantId], ["==", ["get", "laying_method"], "special"]]
        : NONE_FILTER;
      const others: maplibregl.FilterSpecification =
        variantId && showOthers ? ["!=", ["get", "variant_id"], variantId] : NONE_FILTER;
      setFilter(instance, "result-casing", selected);
      setFilter(instance, "result-base", base);
      setFilter(instance, "result-special", special);
      setFilter(instance, "result-nodes", selected);
      setFilter(instance, "result-alt", others);
    });
  }, [variantId, showOthers]);

  // --- Подписи Ду на участках выбранного варианта ---
  useEffect(() => {
    const instance = map.current;
    if (!instance) return;
    segmentMarkers.current.forEach((m) => m.remove());
    segmentMarkers.current = [];
    if (!showLabels || !result || !variantId) return;
    segmentMarkers.current = result.segmentLabels
      .filter((label) => label.variant_id === variantId)
      .map((label) => {
        const element = document.createElement("div");
        element.className = "seg-label";
        element.textContent = label.text;
        return new maplibregl.Marker({ element, anchor: "center" })
          .setLngLat(label.position as [number, number])
          .addTo(instance);
      });
  }, [result, variantId, showLabels]);

  // --- Подсветка объекта из протокола валидатора ---
  useEffect(() => {
    const instance = map.current;
    if (!instance) return;
    whenReady(() => {
      const empty: FeatureCollection = { type: "FeatureCollection", features: [] };
      setData(instance, "highlight", highlight ? { type: "FeatureCollection", features: [highlight.feature] } : empty);
      popup.current?.remove();
      if (!highlight) return;
      const geometry = highlight.feature.geometry;
      let anchor: Position | null = null;
      if (geometry.type === "Point") {
        anchor = geometry.coordinates;
        instance.easeTo({ center: anchor as [number, number], zoom: Math.max(instance.getZoom(), 17), duration: 500 });
      } else if (geometry.type === "LineString" || geometry.type === "MultiLineString") {
        anchor = midpoint(geometry);
        const bbox = bboxOf({ type: "FeatureCollection", features: [highlight.feature] });
        if (bbox) {
          instance.fitBounds(
            [
              [bbox[0], bbox[1]],
              [bbox[2], bbox[3]],
            ],
            { padding: 120, duration: 500, maxZoom: 18 },
          );
        }
      }
      if (!anchor) return;
      popup.current = new maplibregl.Popup({ maxWidth: "340px", closeButton: true })
        .setLngLat(anchor as [number, number])
        .setHTML(popupHtml(highlight.layer, highlight.feature.properties ?? {}, tieInCostRef.current))
        .addTo(instance);
    });
  }, [highlight]);

  // --- Подложка ---
  useEffect(() => {
    const instance = map.current;
    if (!instance) return;
    whenReady(() => {
      if (instance.getLayer("basemap")) {
        instance.setLayoutProperty("basemap", "visibility", showBasemap ? "visible" : "none");
      }
    });
  }, [showBasemap]);

  // --- Границы ---
  useEffect(() => {
    const instance = map.current;
    const bbox = input?.bbox;
    if (!instance || !bbox) return;
    fit(instance, bbox, fitToken > 0 ? 600 : 0);
  }, [input, fitToken]);

  return <div ref={container} className="map" />;
}

// --- Вспомогательное --------------------------------------------------------------

function setData(map: maplibregl.Map, id: string, data: FeatureCollection) {
  const source = map.getSource(id) as maplibregl.GeoJSONSource | undefined;
  source?.setData(data);
}

function setFilter(map: maplibregl.Map, id: string, filter: maplibregl.FilterSpecification) {
  if (map.getLayer(id)) map.setFilter(id, filter);
}

function fit(map: maplibregl.Map, bbox: BBox, duration: number) {
  const [minLon, minLat, maxLon, maxLat] = bbox;
  map.fitBounds(
    [
      [minLon, minLat],
      [maxLon, maxLat],
    ],
    { padding: 48, duration, maxZoom: 17 },
  );
}

/** Значки узлов рисуются на canvas: спрайт не нужен, работает офлайн. */
function addIcons(map: maplibregl.Map) {
  const make = (size: number, draw: (ctx: CanvasRenderingContext2D, s: number) => void) => {
    const ratio = 2;
    const canvas = document.createElement("canvas");
    canvas.width = size * ratio;
    canvas.height = size * ratio;
    const ctx = canvas.getContext("2d");
    if (!ctx) return null;
    ctx.scale(ratio, ratio);
    draw(ctx, size);
    return ctx.getImageData(0, 0, canvas.width, canvas.height);
  };
  const square = (fill: string, stroke: string, border: number) => (ctx: CanvasRenderingContext2D, s: number) => {
    ctx.fillStyle = fill;
    ctx.strokeStyle = stroke;
    ctx.lineWidth = border;
    const inset = border / 2 + 0.5;
    ctx.fillRect(inset, inset, s - 2 * inset, s - 2 * inset);
    ctx.strokeRect(inset, inset, s - 2 * inset, s - 2 * inset);
  };
  const circle = (fill: string, stroke: string, border: number) => (ctx: CanvasRenderingContext2D, s: number) => {
    ctx.fillStyle = fill;
    ctx.strokeStyle = stroke;
    ctx.lineWidth = border;
    ctx.beginPath();
    ctx.arc(s / 2, s / 2, s / 2 - border / 2 - 0.5, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
  };
  const cross = (ctx: CanvasRenderingContext2D, s: number) => {
    circle(UNCONNECTED_COLOR, "#ffffff", 1.5)(ctx, s);
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 2.2;
    ctx.beginPath();
    ctx.moveTo(s * 0.3, s * 0.3);
    ctx.lineTo(s * 0.7, s * 0.7);
    ctx.moveTo(s * 0.7, s * 0.3);
    ctx.lineTo(s * 0.3, s * 0.7);
    ctx.stroke();
  };

  const icons: Array<[string, number, (ctx: CanvasRenderingContext2D, s: number) => void]> = [
    [ICONS.chamberExisting, 13, square("#ffffff", EXISTING_NETWORK, 2)],
    [ICONS.chamberNew, 15, square("#fef08a", "#0f172a", 2)],
    [ICONS.tieInNew, 17, square("#fef08a", EXISTING_NETWORK, 3)],
    [ICONS.tieIn, 17, square("#fde047", EXISTING_NETWORK, 3)],
    [ICONS.techNode, 9, circle("#ffffff", "#0f172a", 1.5)],
    [ICONS.unconnected, 17, cross],
  ];
  for (const [name, size, draw] of icons) {
    if (map.hasImage(name)) continue;
    const image = make(size, draw);
    if (image) map.addImage(name, image, { pixelRatio: 2 });
  }
}

const esc = (value: unknown) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] ?? c);

const row = (label: string, value: string) =>
  `<div class="pp-row"><span>${esc(label)}</span><b>${value}</b></div>`;

const LAYING_TITLE: Record<string, string> = { base: "обычная", special: "спецпроход" };

/** Разметка попапа по слою и свойствам объекта — как для клика по карте, так и
 *  для подсветки по находке валидатора (там объекта MapLibre нет). */
function popupHtml(layer: string, p: Record<string, unknown>, tieInCost: number): string {
  const num = (v: unknown) => Number(v ?? 0);

  if (layer === "result-base" || layer === "result-special" || layer === "result-alt") {
    const special = p.laying_method === "special";
    // Глубина есть только в режиме с глубиной: в 2D-файле depth_start = null
    const hasDepth = p.depth_start !== null && p.depth_start !== undefined && p.depth_start !== "";
    return (
      `<div class="pp"><div class="pp-title">Новый участок <code>${esc(p.id)}</code>` +
      `<span class="muted"> · вариант ${esc(p.variant_id)}</span></div>` +
      row("расход", `${formatNumber(num(p.flow_tph), 3)} т/ч`) +
      row("условный диаметр", `Ду${esc(p.diameter)}`) +
      row("длина", `${formatNumber(num(p.length), 3)} м`) +
      row("способ прокладки", esc(LAYING_TITLE[String(p.laying_method)] ?? p.laying_method) +
        (special && p.special_types ? ` (${esc(p.special_types)})` : "")) +
      row("Kспец", formatNumber(num(p.k_special ?? 1), 2)) +
      (hasDepth
        ? row("глубина верха, м", `${formatNumber(num(p.depth_start), 2)} → ${formatNumber(num(p.depth_end), 2)}`) +
          row("Kгл", formatNumber(num(p.k_depth ?? 1), 3))
        : "") +
      row("стоимость", `<span title="${esc(formatRub(num(p.cost)))}">${formatMoney(num(p.cost))}</span>`) +
      row("узлы", `${esc(p.start_title)} → ${esc(p.end_title)}`) +
      `</div>`
    );
  }
  if (layer === "result-nodes") {
    switch (p.kind) {
      case "chamber_new":
        return (
          `<div class="pp"><div class="pp-title">Новая тепловая камера <code>${esc(p.id)}</code></div>` +
          row("Ду камеры", `Ду${esc(p.diameter)}`) +
          row("стоимость", `<span title="${esc(formatRub(num(p.cost)))}">${formatMoney(num(p.cost))}</span>`) +
          (p.on_existing_network
            ? row("назначение", "точка врезки") +
              row("на существующем участке", esc(p.on_existing_network)) +
              `<div class="muted">присоединение к сети входит в стоимость камеры, отдельной врезки нет</div>`
            : row("назначение", "разветвление")) +
          `</div>`
        );
      case "technical_node":
        return (
          `<div class="pp"><div class="pp-title">Технический узел <code>${esc(p.id)}</code></div>` +
          `<div class="muted">смена параметра участка; отдельной стоимости нет</div></div>`
        );
      case "tie_in":
        return (
          `<div class="pp"><div class="pp-title">Врезка в существующую камеру ТК ${esc(p.id)}</div>` +
          row("новых участков", esc(p.new_segments)) +
          row("примыканий занято", `${esc(p.connections_used)} из 4`) +
          row("стоимость врезки", `${formatMoney(tieInCost * num(p.new_segments))}`) +
          `</div>`
        );
      case "unconnected":
        return (
          `<div class="pp"><div class="pp-title bad">Точка #${esc(p.id)} не подключена</div>` +
          `<div>${esc(p.reason || "маршрут не найден автоматически")}</div></div>`
        );
      default:
        break;
    }
  }
  if (layer === "chambers-existing") {
    return `<div class="pp"><div class="pp-title">Существующая тепловая камера ТК ${esc(p.id)}</div></div>`;
  }
  if (layer === "oks-points") {
    return (
      `<div class="pp"><div class="pp-title">Точка подключения #${esc(p.id)}</div>` +
      row("расчётный расход", `${formatNumber(num(p.flow_tph), 3)} т/ч`) + `</div>`
    );
  }
  if (layer === "network-line") {
    return (
      `<div class="pp"><div class="pp-title">Существующий участок ${esc(p.id)}</div>` +
      row("условный диаметр", `Ду${esc(p.diameter)}`) + `</div>`
    );
  }
  if (layer === "source-point") {
    return `<div class="pp"><div class="pp-title">Источник ${esc(p.id)}</div><div>${esc(p.name)}</div></div>`;
  }
  if (layer === "restr-fill" || layer === "restr-line" || layer === "restr-line-unknown") {
    const type = String(p.restriction_type);
    return (
      `<div class="pp"><div class="pp-title">${esc(RESTRICTION_TITLE[type] ?? `ограничение «${type}»`)}</div>` +
      row("id", esc(p.id)) +
      (p.address ? row("адрес", esc(p.address)) : "") +
      (p.known ? "" : `<div class="muted">тип не описан в приложении — в расчёте не участвует</div>`) +
      `</div>`
    );
  }
  return `<div class="pp">${esc(layer)}</div>`;
}

