/** Типы ответов API конкурсной модели. Зеркалят backend/app/case/api.py и
 *  jobs.summarize(); выходной GeoJSON — backend/app/case/output.py. */

import type { FeatureCollection as GeoJSONFeatureCollection } from "geojson";

/** Идентификаторы во входе могут быть числами или строками; тип сохраняется. */
export type Id = string | number;

export type FeatureCollection = GeoJSONFeatureCollection;

// --- Проекты --------------------------------------------------------------------

export interface ProjectStats {
  source: boolean;
  network_edges: number;
  network_length_m: number;
  chambers: number;
  oks_points: number;
  total_flow_tph: number;
  restrictions: Record<string, number>;
  unknown_restrictions: number;
  issues: number;
}

export type IssueLevel = "error" | "warning" | "info";

export interface Issue {
  level: IssueLevel | string;
  where: string;
  detail: string;
  count: number;
}

export interface ProjectBrief {
  id: string;
  name: string;
  created_at: number;
  stats: ProjectStats;
  issues: number;
  jobs: string[];
}

export interface ProjectDetail {
  id: string;
  name: string;
  created_at: number;
  stats: ProjectStats;
  issues: Issue[];
  jobs: Job[];
}

export interface UploadResult {
  id: string;
  name: string;
  stats: ProjectStats;
  issues: Issue[];
}

// --- Задания --------------------------------------------------------------------

export type SolveMode = "2d" | "depth";
/** Тщательность: standard — три порядка точек; thorough — плюс перестановки
 *  точек у лучшего варианта (дольше в 2–3 раза). */
export type Effort = "standard" | "thorough";
export type JobStatus = "queued" | "running" | "done" | "failed";

export interface Job {
  id: string;
  project_id: string;
  mode: SolveMode;
  max_variants: number;
  /** У заданий, созданных до появления поля, отсутствует. */
  effort?: Effort | string;
  status: JobStatus;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  progress: string;
  error: string | null;
  output_path: string | null;
  summary: Summary | null;
  elapsed_s: number | null;
}

// --- Сводка (jobs.summarize) ------------------------------------------------------

export type Laying = "base" | "special";

export interface SegmentSummary {
  /** Внутренний id участка (`internal_id` в GeoJSON). */
  id: string;
  /** Внутренние id узлов начала и конца. */
  start: string;
  end: string;
  flow_tph: number;
  du: number;
  length_m: number;
  laying: Laying;
  k_special: number;
  special_types: string[];
  /** Глубина верха габарита в начале и конце, м — только в режиме с глубиной. */
  depth_start?: number | null;
  depth_end?: number | null;
  /** Kгл — 1 в 2D-режиме. */
  k_depth?: number;
  cost: number;
}

/** depth.summary(): итог профиля глубины по варианту (только режим depth). */
export interface DepthSummary {
  crossings_with_depth: number;
  below_object: number;
  above_object: number;
  max_top_depth_m: number;
  min_top_depth_m: number;
  pieces_split: number;
  issues: string[];
}

export interface LimitCheck {
  oks_id: Id;
  du: number;
  run_length_m: number;
  limit_m: number;
  ok: boolean;
}

export type NodeKind = "oks" | "chamber_new" | "chamber_existing" | "technical_node";

export interface NodeSummary {
  /** Внутренний id узла. */
  id: string;
  kind: NodeKind;
  /** Входной id точки подключения или существующей камеры. */
  ref: Id | null;
  approach_rule: "nearest" | "fallback" | "";
  on_existing_network: Id | null;
  connections_used: number | null;
}

export interface ChamberSummary {
  node_id: string;
  diameter: number;
  cost: number;
}

export interface VariantSummary {
  id: string;
  rank: number;
  order: string;
  score: number;
  construction_cost: number;
  chamber_construction_cost: number;
  existing_chamber_tie_in_count: number;
  existing_chamber_tie_in_cost: number;
  unconnected_penalty: number;
  calculated_cost: number;
  new_network_length: number;
  unconnected_oks_ids: Id[];
  unconnected_reasons: Record<string, string>;
  chambers: ChamberSummary[];
  segments: SegmentSummary[];
  limit_length_checks: LimitCheck[];
  diameter_bumps: string[];
  special_crossings: number;
  special_rebuilt: string[];
  repairs: string[];
  unresolved_special: string[];
  minimality: string[];
  /** null в 2D-режиме; у сводок старых заданий поля нет. */
  depth?: DepthSummary | null;
  nodes: NodeSummary[];
  /** Добавочный расход по существующей сети; у сводок старых заданий поля нет. */
  existing_impact?: ExistingImpact | null;
}

/** Существующий участок, по которому к источнику пойдёт расход новых подключений. */
export interface ExistingImpactEdge {
  edge_id: Id;
  diameter: number;
  length_m: number;
  added_flow_tph: number;
  /** null — Ду участка нет в таблице 1. */
  capacity_tph: number | null;
  share_of_capacity: number | null;
  /** Подписи точек присоединения: «камера 12», «участок 7». */
  tie_ins: string[];
}

export interface ExistingImpact {
  source_found: boolean;
  unreached_tie_ins: string[];
  note: string;
  edges: ExistingImpactEdge[];
}

export interface Summary {
  elapsed_s: number;
  /** Режим и тщательность расчёта; у сводок старых заданий отсутствуют. */
  mode?: SolveMode;
  effort?: Effort | string;
  /** Журнал улучшения лучшего варианта — строки вида «ветка 4 заново: S 12,9378 → 12,9296»
   *  (перестройка веток, всегда) и «4 в конец: S 12,9688 → 12,9061» (перестановки,
   *  тщательный режим); пуст, если улучшений не нашлось. */
  improvement?: string[];
  field: Record<string, unknown>;
  stats: ProjectStats;
  variants: VariantSummary[];
}

// --- Выходной GeoJSON (§7 приложения) ---------------------------------------------

export interface OutSegmentProps {
  id: string;
  object_type: "heat_network";
  variant_id: string;
  start_node_id: Id;
  end_node_id: Id;
  flow_tph: number;
  diameter: number;
  length: number;
  laying_method: Laying;
  depth_start: number | null;
  depth_end: number | null;
  cost: number;
  k_special: number;
  k_depth: number;
  special_types: string[];
  special_ids: Id[];
  internal_id: string;
}

export interface OutChamberProps {
  id: string;
  object_type: "heat_chamber";
  variant_id: string;
  diameter: number;
  cost: number;
  on_existing_network: Id | null;
  internal_id: string;
}

export interface OutTechNodeProps {
  id: string;
  object_type: "technical_node";
  variant_id: string;
  internal_id: string;
}

// --- Протокол независимого валидатора (/jobs/{id}/validation) ---------------------
// Зеркалит backend/app/case_validator: Report.to_dict(), Finding.to_dict(),
// costs.VariantTotals. Валидатор написан отдельно от солвера по тексту приложения.

export type ValidationSeverity = "error" | "warning" | "info";

export interface ValidationFinding {
  severity: ValidationSeverity | string;
  /** Код правила вида geometry.final_segment — группа.проверка. */
  code: string;
  message: string;
  /** null — находка по файлу в целом (структура входа/выхода). */
  variant_id: string | null;
  /** id объекта из выхода (v1_net_3, v1_chamber_5, v1_tn_1, v1_summary) либо
   *  входной id точки подключения или существующей камеры; null — без адреса. */
  object_id: Id | null;
  /** «норма» и «факт»: число, строка вида «≤ 90.0°», список или null. */
  expected: unknown;
  actual: unknown;
}

export interface ValidationVariant {
  variant_id: string;
  segments: number;
  chambers: number;
  tech_nodes: number;
  length_declared: number;
  length_recomputed: number;
  construction_cost_recomputed: number;
  chamber_cost_recomputed: number;
  tie_in_count: number;
  unconnected_computed: Id[];
  unconnected_declared: Id[] | null;
  penalty_recomputed: number;
  calculated_cost_recomputed: number;
  score_recomputed: number;
  score_declared: number | null;
  calculated_cost_declared: number | null;
  rank: number | null;
  errors: number;
  warnings: number;
}

export interface ValidationReport {
  /** true — ни одной находки уровня error. */
  ok: boolean;
  input: string | null;
  output: string | null;
  counts: Record<ValidationSeverity, number>;
  variants: ValidationVariant[];
  findings: ValidationFinding[];
  meta: Record<string, unknown>;
}

// --- Таблицы приложения (/rules) ---------------------------------------------------

export interface Rules {
  meta: { source: string; calc_crs: string; input_crs: string };
  /** Ду → [пропускная способность т/ч, предельная длина м, ₽/м, ширина м, высота м]. */
  diameters: Record<string, [number, number, number, number, number]>;
  chambers: {
    cost_by_max_du: Array<[number, number, number]>;
    max_connections: number;
    reuse_existing_within_m: number;
    tie_in_cost: number;
  };
  penalty: { fixed: number; per_tph: number };
  score: { cost_weight: number; cost_scale: number; length_weight: number; length_scale: number };
  restrictions: Record<string, { rule: string; clearance_m?: number; k_special?: number }>;
}

/** Веса показателя по умолчанию — на случай, если /rules недоступен. */
export const DEFAULT_SCORE: Rules["score"] = {
  cost_weight: 0.7,
  cost_scale: 25_000_000,
  length_weight: 0.3,
  length_scale: 100,
};
