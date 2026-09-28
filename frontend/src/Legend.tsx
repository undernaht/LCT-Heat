/** Легенда карты — сворачиваемая. Показывает только те типы ограничений,
 *  которые есть во входе, и шкалу Ду новых участков. */

import { useState } from "react";

import {
  DU_COLOR,
  DU_FALLBACK,
  EXISTING_NETWORK,
  OKS_COLOR,
  RESTRICTION_FILL,
  RESTRICTION_TITLE,
  SOURCE_COLOR,
  UNCONNECTED_COLOR,
  UNKNOWN_FILL,
  UNKNOWN_OUTLINE,
  VARIANT_FALLBACK,
} from "./mapStyle";

type SwatchKind = "line" | "dash" | "fill" | "fill-dashed" | "dot" | "square" | "cross";

interface Row {
  colour: string;
  label: string;
  kind: SwatchKind;
  width?: number;
  border?: string;
}

const LINE_TYPES = new Set(["gas_pipeline", "power_cable"]);

function Swatch({ row }: { row: Row }) {
  switch (row.kind) {
    case "fill":
      return <span className="lg-swatch lg-fill" style={{ background: row.colour }} />;
    case "fill-dashed":
      return (
        <span
          className="lg-swatch lg-fill"
          style={{ background: row.colour, borderStyle: "dashed", borderColor: row.border }}
        />
      );
    case "dot":
      return <span className="lg-swatch lg-dot" style={{ background: row.colour, borderColor: row.border }} />;
    case "square":
      return <span className="lg-swatch lg-square" style={{ background: row.colour, borderColor: row.border }} />;
    case "cross":
      return (
        <span className="lg-swatch lg-dot lg-cross" style={{ background: row.colour }}>
          ×
        </span>
      );
    default:
      return (
        <span className="lg-swatch">
          <span
            className={row.kind === "dash" ? "lg-line lg-dashed" : "lg-line"}
            style={{ background: row.colour, height: row.width ?? 2 }}
          />
        </span>
      );
  }
}

function Group({ title, rows }: { title: string; rows: Row[] }) {
  if (rows.length === 0) return null;
  return (
    <div className="lg-group">
      <div className="lg-title">{title}</div>
      {rows.map((row) => (
        <div key={row.label} className="lg-row">
          <Swatch row={row} />
          <span>{row.label}</span>
        </div>
      ))}
    </div>
  );
}

const INPUT: Row[] = [
  { colour: SOURCE_COLOR, label: "источник тепла", kind: "dot", border: "#78350f" },
  { colour: OKS_COLOR, label: "точка подключения ОКС · расход", kind: "dot", border: "#fff" },
  { colour: EXISTING_NETWORK, label: "существующая сеть (толщина — Ду)", kind: "line", width: 4 },
  { colour: "#ffffff", label: "существующая тепловая камера", kind: "square", border: EXISTING_NETWORK },
];

const RESULT: Row[] = [
  { colour: "#fef08a", label: "новая камера в точке врезки", kind: "square", border: EXISTING_NETWORK },
  { colour: "#fef08a", label: "новая камера (разветвление)", kind: "square", border: "#0f172a" },
  { colour: "#fde047", label: "врезка в существующую камеру", kind: "square", border: EXISTING_NETWORK },
  { colour: "#ffffff", label: "технический узел", kind: "dot", border: "#0f172a" },
  { colour: "#0f172a", label: "спецпроход (пунктир, цвет по Ду)", kind: "dash", width: 3 },
  { colour: VARIANT_FALLBACK, label: "другие варианты (для сравнения)", kind: "dash", width: 2 },
  { colour: UNCONNECTED_COLOR, label: "точка не подключена", kind: "cross" },
];

export function Legend({
  restrictionTypes,
  unknownTypes = [],
  hasResult,
}: {
  restrictionTypes: string[];
  /** Типы, которых нет в правилах приложения (/rules) — как решил App, а не легенда. */
  unknownTypes?: string[];
  hasResult: boolean;
}) {
  const [open, setOpen] = useState(true);

  if (!open) {
    return (
      <button className="legend-toggle" onClick={() => setOpen(true)} title="Показать легенду">
        Легенда
      </button>
    );
  }

  const unknown = new Set(unknownTypes);
  const restrictions: Row[] = restrictionTypes.map((type) => {
    // «Неизвестен» — по правилам приложения; цвет — если для типа он задан,
    // иначе общий цвет неизвестных (так же красит карта)
    const known = !unknown.has(type) && type in RESTRICTION_FILL;
    if (!known) {
      return {
        colour: UNKNOWN_FILL,
        label: unknown.has(type)
          ? `${type} — тип не описан в приложении`
          : `${RESTRICTION_TITLE[type] ?? type}`,
        kind: "fill-dashed",
        border: UNKNOWN_OUTLINE,
      };
    }
    return LINE_TYPES.has(type)
      ? { colour: RESTRICTION_FILL[type], label: RESTRICTION_TITLE[type], kind: "line", width: 3 }
      : { colour: RESTRICTION_FILL[type], label: RESTRICTION_TITLE[type], kind: "fill" };
  });

  return (
    <div className="legend">
      <button className="legend-close" onClick={() => setOpen(false)} title="Свернуть">
        ×
      </button>

      <Group title="Исходные данные" rows={INPUT} />
      <Group title="Пространственные ограничения" rows={restrictions} />
      {hasResult && <Group title="Результат" rows={RESULT} />}

      {hasResult && (
        <div className="lg-group">
          <div className="lg-title">Новые участки — Ду</div>
          <div className="lg-du">
            {Object.entries(DU_COLOR).map(([du, colour]) => (
              <span key={du} className="lg-du-item">
                <i style={{ background: colour }} />
                {du}
              </span>
            ))}
            <span className="lg-du-item">
              <i style={{ background: DU_FALLBACK }} />
              500+
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
