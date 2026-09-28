/**
 * Проверка спецификаций слоёв карты без браузера.
 *
 * Нужна потому, что невалидный стиль MapLibre не роняет приложение: слой просто
 * не добавляется, карта остаётся пустой, и в консоли ничего внятного нет.
 * Так уже поймали `line-dasharray` с выражением от данных — эта опция принимает
 * только выражения от зума.
 *
 * Слои и источники берутся из src/mapStyle.ts напрямую — это модуль без
 * обращений к браузеру, Node читает его со стрипом типов (Node ≥ 22.6).
 *
 *   node scripts/validate-style.mjs
 */

import { spawnSync } from "node:child_process";

const STRIP = "--experimental-strip-types";
if (!process.execArgv.some((arg) => arg.includes("strip-types"))) {
  // Перезапуск с флагом стрипа типов: так скрипт не дублирует выражения слоёв
  const result = spawnSync(process.execPath, [STRIP, "--no-warnings", ...process.argv.slice(1)], {
    stdio: "inherit",
  });
  process.exit(result.status ?? 1);
}

const { validateStyleMin } = await import("@maplibre/maplibre-gl-style-spec");
const { LAYERS, SOURCES, CLICKABLE_LAYERS, ICONS } = await import("../src/mapStyle.ts");

const style = { version: 8, sources: SOURCES, layers: LAYERS };
const errors = validateStyleMin(style);

console.log(`Проверено слоёв: ${LAYERS.length}`);
for (const layer of LAYERS) console.log(`   ${layer.id} (${layer.type}) ← ${layer.source}`);

// Ссылки на источники, кликабельные слои и значки должны существовать
const layerIds = new Set(LAYERS.map((layer) => layer.id));
for (const layer of LAYERS) {
  if (!(layer.source in SOURCES)) errors.push({ message: `слой ${layer.id}: нет источника ${layer.source}` });
}
for (const id of CLICKABLE_LAYERS) {
  if (!layerIds.has(id)) errors.push({ message: `CLICKABLE_LAYERS: нет слоя ${id}` });
}
const icons = new Set(Object.values(ICONS));
for (const layer of LAYERS) {
  const image = layer.layout?.["icon-image"];
  if (typeof image === "string" && !icons.has(image)) {
    errors.push({ message: `слой ${layer.id}: значок ${image} не описан в ICONS` });
  }
}

if (errors.length) {
  console.error(`\nОшибок: ${errors.length}`);
  for (const error of errors) console.error(`   ${error.message}`);
  process.exit(1);
}

console.log("\nВсе слои валидны.");
