/** Правая колонка: варианты, протокол валидатора, ключевые цифры, таблицы
 *  участков и камер, проверка предельной длины, спецпроходы, неподключённые
 *  точки, выгрузка. */

import { useMemo, useState } from "react";

import { api, formatLength, formatMoney, formatNumber, formatRub, formatScore } from "./api";
import { key, nodeLabel } from "./layers";
import { RESTRICTION_TITLE } from "./mapStyle";
import type {
  FeatureCollection,
  Id,
  Job,
  Laying,
  NodeKind,
  OutChamberProps,
  OutSegmentProps,
  Rules,
  SolveMode,
  ValidationFinding,
  ValidationReport,
  ValidationSeverity,
  VariantSummary,
} from "./types";
import { DEFAULT_SCORE } from "./types";

// --- Сортировка таблиц ----------------------------------------------------------------

type SortDir = "asc" | "desc";

function compare(a: unknown, b: unknown): number {
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a ?? "").localeCompare(String(b ?? ""), "ru", { numeric: true });
}

function useSort<T extends object>(rows: T[], initial: keyof T, initialDir: SortDir = "asc") {
  const [sortKey, setSortKey] = useState<keyof T>(initial);
  const [dir, setDir] = useState<SortDir>(initialDir);
  const sorted = useMemo(() => {
    const sign = dir === "asc" ? 1 : -1;
    return rows.slice().sort((a, b) => compare(a[sortKey], b[sortKey]) * sign);
  }, [rows, sortKey, dir]);
  const toggle = (next: keyof T) => {
    if (next === sortKey) setDir((current) => (current === "asc" ? "desc" : "asc"));
    else {
      setSortKey(next);
      setDir("asc");
    }
  };
  return { sorted, sortKey, dir, toggle };
}

function Th<T>({
  label,
  field,
  sortKey,
  dir,
  onToggle,
  title,
}: {
  label: string;
  field: keyof T;
  sortKey: keyof T;
  dir: SortDir;
  onToggle: (field: keyof T) => void;
  title?: string;
}) {
  const active = field === sortKey;
  return (
    <th
      className={active ? "sortable sorted" : "sortable"}
      onClick={() => onToggle(field)}
      title={title ?? "сортировать"}
    >
      {label}
      {active && <span className="sort-arrow">{dir === "asc" ? "▲" : "▼"}</span>}
    </th>
  );
}

// --- Ход расчёта -----------------------------------------------------------------------

export function ProgressPanel({ job, offline }: { job: Job; offline?: string | null }) {
  return (
    <section className="panel progress">
      <h3>
        Расчёт <span className="muted">{job.mode === "depth" ? "с учётом глубины" : "2D"}</span>
      </h3>
      {job.status === "failed" ? (
        <>
          <p className="error">Расчёт не выполнен: {job.error ?? "причина неизвестна"}</p>
          <p className="note">
            Проект и входные данные сохранены; можно запустить расчёт заново или выбрать другое
            задание в списке слева.
          </p>
        </>
      ) : (
        <>
          <div className="progress-row">
            <span className="spinner spinner-dark" />
            <span>
              {job.status === "queued" ? "В очереди…" : `Считаю: ${job.progress || "…"}`}
            </span>
            {job.elapsed_s !== null && <span className="muted">{formatNumber(job.elapsed_s, 0)} с</span>}
          </div>
          {offline && (
            <p className="error">
              Нет связи с сервисом расчёта ({offline}). Опрос продолжается; если сервис был
              перезапущен, задание будет помечено как прерванное.
            </p>
          )}
          <p className="note">
            Обрабатываются все перспективные ОКС: поле стоимости, деревья по разным порядкам
            точек, спецпроходы, подбор Ду и проверка предельной длины. Интерфейс не блокируется.
          </p>
        </>
      )}
    </section>
  );
}

// --- Варианты --------------------------------------------------------------------------

/** Точки присоединения к существующей сети: новые камеры на существующих
 *  участках плюс существующие камеры с врезками (§2.4 модели). */
export function tieInPoints(variant: VariantSummary): number {
  return variant.nodes.filter(
    (n) => n.kind === "chamber_existing" || (n.kind === "chamber_new" && n.on_existing_network !== null),
  ).length;
}

export function VariantList({
  variants,
  selectedId,
  elapsed,
  mode,
  effort,
  improvement,
  rules,
  onSelect,
}: {
  variants: VariantSummary[];
  selectedId: string | null;
  elapsed: number;
  mode?: SolveMode;
  /** standard | thorough; у старых сводок нет. */
  effort?: string;
  /** Журнал улучшений лучшего варианта (summary.improvement): перестройки веток
   *  при остальных как есть — всегда; перестановки точек — в тщательном режиме. */
  improvement?: string[];
  rules: Rules | null;
  onSelect: (id: string) => void;
}) {
  const sorted = variants.slice().sort((a, b) => a.rank - b.rank);
  const score = rules?.score ?? DEFAULT_SCORE;
  const thorough = effort === "thorough";
  const log = improvement ?? [];
  return (
    <section className="panel">
      <h3>
        Варианты подключения{" "}
        <span className="muted">
          {mode === "depth" ? "с учётом глубины · " : "2D · "}
          {formatNumber(elapsed, 0)} с
          {thorough && (
            <>
              {" "}
              <span className="badge badge-thorough" title="перестановки точек у лучшего варианта">
                тщательно
              </span>
            </>
          )}
        </span>
      </h3>
      {sorted.length === 0 && <p className="error">Сервис не вернул ни одного варианта.</p>}
      <div className="variants">
        {sorted.map((variant) => (
          <button
            key={variant.id}
            type="button"
            className={`variant ${variant.id === selectedId ? "variant-selected" : ""}`}
            onClick={() => onSelect(variant.id)}
          >
            <div className="variant-head">
              <span className={`rank rank-${variant.rank}`}>{variant.rank}</span>
              <span className="variant-title">Вариант {variant.id.replace(/^v/, "")}</span>
              <span className="variant-score" title="показатель S: меньше — лучше">
                S = {formatScore(variant.score)}
              </span>
            </div>
            <div className="variant-numbers">
              <span className="strong" title={formatRub(variant.calculated_cost)}>
                {formatMoney(variant.calculated_cost)}
              </span>
              <span>{formatLength(variant.new_network_length)}</span>
              <span>камер {variant.chambers.length}</span>
              <span title="новые камеры на существующих участках + врезки в существующие камеры">
                точек врезки {tieInPoints(variant)}
              </span>
              <span className={variant.unconnected_oks_ids.length ? "bad" : "ok"}>
                {variant.unconnected_oks_ids.length
                  ? `не подключено ${variant.unconnected_oks_ids.length}`
                  : "все точки подключены"}
              </span>
            </div>
          </button>
        ))}
      </div>
      {sorted.length > 1 && (
        <p className="note">
          Ранжирование только по S = {formatNumber(score.cost_weight, 2)}·C/{formatMoney(score.cost_scale)} +{" "}
          {formatNumber(score.length_weight, 2)}·L/{formatNumber(score.length_scale, 0)} м: стоимость весит{" "}
          {Math.round(score.cost_weight * 100)} %, длина новой сети — {Math.round(score.length_weight * 100)} %.
          Варианты содержательно различаются точками врезки, составом общих участков или маршрутом.
        </p>
      )}
      {log.length > 0 && (
        <details className="improvement">
          <summary>
            Журнал улучшений лучшего варианта <span className="muted">· шагов {log.length}</span>
          </summary>
          <ul className="plain reasons-list">
            {log.map((line, index) => (
              <li key={`${index}-${line}`} className="num-text">
                {line}
              </li>
            ))}
          </ul>
          <p className="note" style={{ marginTop: 6 }}>
            «Ветка заново» — собственная ветка точки снята и проложена заново при остальных как
            есть; «в конец» — тщательный режим: точка переставлена в конец порядка и дерево
            построено заново. Шаг остаётся, если S уменьшился.
          </p>
        </details>
      )}
      {thorough && log.length === 0 && (
        <p className="note">Тщательный режим: перестановки точек лучший вариант не улучшили.</p>
      )}
    </section>
  );
}

// --- Протокол независимого валидатора -----------------------------------------------

const SEVERITY_ORDER: ValidationSeverity[] = ["error", "warning", "info"];
const SEVERITY_TITLE: Record<ValidationSeverity, string> = {
  error: "ошибки",
  warning: "предупреждения",
  info: "пояснения",
};
/** Пересчитанный показатель рядом с заявленным: разница в 4-м знаке — округление
 *  выхода, больше — предмет находки cost.*. */
const scoreDiffers = (declared: number | null, recomputed: number) =>
  declared !== null && Math.abs(declared - recomputed) > 5e-4;

/** «норма/факт» находки: числа с разделителями, строки как есть, списки через запятую. */
function formatFact(value: unknown): string {
  if (value === null || value === undefined || value === "") return "";
  if (typeof value === "number") return formatNumber(value, 3);
  if (Array.isArray(value)) return value.map(formatFact).join(", ");
  return String(value);
}

/** Есть ли у находки объект с геометрией: сводку варианта (v1_summary) и
 *  порядковый номер объекта входа (#12) на карте показать нечем. */
const locatable = (f: ValidationFinding) =>
  f.object_id !== null &&
  f.object_id !== undefined &&
  !/_summary$/.test(String(f.object_id)) &&
  !/^#\d+$/.test(String(f.object_id));

const severityOf = (f: ValidationFinding): ValidationSeverity =>
  (SEVERITY_ORDER as string[]).includes(f.severity) ? (f.severity as ValidationSeverity) : "info";

const variantNo = (id: string | null) => (id ? id.replace(/^v/, "") : "");

export function ValidationPanel({
  report,
  loading,
  error,
  selectedVariantId,
  activeFinding,
  onRetry,
  onPick,
}: {
  report: ValidationReport | null;
  loading: boolean;
  error: string | null;
  selectedVariantId: string | null;
  /** Находка, объект которой сейчас подсвечен на карте. */
  activeFinding: ValidationFinding | null;
  onRetry: () => void;
  onPick: (finding: ValidationFinding) => void;
}) {
  const [levels, setLevels] = useState<Set<ValidationSeverity>>(() => new Set(SEVERITY_ORDER));
  const [onlySelected, setOnlySelected] = useState(false);

  const toggleLevel = (level: ValidationSeverity) =>
    setLevels((current) => {
      const next = new Set(current);
      if (next.has(level)) next.delete(level);
      else next.add(level);
      return next;
    });

  // Группы по коду; порядок групп — по худшему уровню, затем по числу находок
  const groups = useMemo(() => {
    const findings = (report?.findings ?? []).filter(
      (f) =>
        levels.has(severityOf(f)) &&
        (!onlySelected || !selectedVariantId || f.variant_id === selectedVariantId || f.variant_id === null),
    );
    const byCode = new Map<string, ValidationFinding[]>();
    for (const f of findings) byCode.set(f.code, [...(byCode.get(f.code) ?? []), f]);
    const worst = (list: ValidationFinding[]) => Math.min(...list.map((f) => SEVERITY_ORDER.indexOf(severityOf(f))));
    return [...byCode.entries()]
      .map(([code, list]) => ({ code, list, worst: worst(list) }))
      .sort((a, b) => a.worst - b.worst || b.list.length - a.list.length || a.code.localeCompare(b.code));
  }, [report, levels, onlySelected, selectedVariantId]);

  // Пояснений по варианту в отчёте нет (только errors/warnings) — считаем сами
  const infoByVariant = useMemo(() => {
    const counts = new Map<string | null, number>();
    for (const f of report?.findings ?? []) {
      if (severityOf(f) === "info") counts.set(f.variant_id, (counts.get(f.variant_id) ?? 0) + 1);
    }
    return counts;
  }, [report]);

  const shown = groups.reduce((sum, g) => sum + g.list.length, 0);
  const total = report?.findings.length ?? 0;
  const fileLevel = (report?.findings ?? []).filter((f) => f.variant_id === null);
  const countOf = (list: ValidationFinding[], level: ValidationSeverity) =>
    list.filter((f) => severityOf(f) === level).length;

  return (
    <section className="panel validation">
      <h3>
        Проверка по правилам приложения{" "}
        {report && (
          <span className={report.ok ? "badge badge-pass" : "badge badge-fail"}>
            {report.ok ? "Нарушений правил приложения нет" : `Нарушения: ${report.counts.error}`}
          </span>
        )}
        {loading && <span className="muted">проверяю…</span>}
      </h3>

      {loading && (
        <div className="progress-row">
          <span className="spinner spinner-dark" />
          <span>Независимый валидатор пересчитывает выход задания…</span>
        </div>
      )}

      {error && !loading && (
        <>
          <p className="error" style={{ marginTop: 0 }}>
            Протокол не получен: {error}
          </p>
          <button className="secondary" onClick={onRetry}>
            Повторить
          </button>
        </>
      )}

      {report && (
        <>
          <table className="checks dense">
            <thead>
              <tr>
                <th>вариант</th>
                <th className="num">ошибок</th>
                <th className="num">предупр.</th>
                <th className="num">поясн.</th>
                <th className="num" title="показатель S: заявлен в выходе / пересчитан валидатором">
                  S заявл. / пересч.
                </th>
              </tr>
            </thead>
            <tbody>
              {report.variants
                .slice()
                .sort((a, b) => (a.rank ?? 99) - (b.rank ?? 99))
                .map((v) => {
                  const differs = scoreDiffers(v.score_declared, v.score_recomputed);
                  return (
                    <tr key={v.variant_id} className={v.variant_id === selectedVariantId ? "row-selected" : ""}>
                      <td className="nowrap">
                        Вариант {variantNo(v.variant_id)}
                        {v.rank !== null && <span className="muted"> · ранг {v.rank}</span>}
                      </td>
                      <td className={`num ${v.errors ? "bad strong" : "ok"}`}>{v.errors}</td>
                      <td className={`num ${v.warnings ? "warn-text" : ""}`}>{v.warnings}</td>
                      <td className="num">{infoByVariant.get(v.variant_id) ?? 0}</td>
                      <td
                        className={`num ${differs ? "bad" : ""}`}
                        title={
                          differs
                            ? "расхождение больше округления — см. находки cost.*"
                            : "совпадает с точностью до округления выхода"
                        }
                      >
                        {v.score_declared !== null ? formatScore(v.score_declared) : "—"} /{" "}
                        {formatScore(v.score_recomputed)}
                      </td>
                    </tr>
                  );
                })}
              {fileLevel.length > 0 && (
                <tr>
                  <td className="nowrap">файл в целом</td>
                  <td className={`num ${countOf(fileLevel, "error") ? "bad strong" : "ok"}`}>
                    {countOf(fileLevel, "error")}
                  </td>
                  <td className="num">{countOf(fileLevel, "warning")}</td>
                  <td className="num">{countOf(fileLevel, "info")}</td>
                  <td className="num">—</td>
                </tr>
              )}
            </tbody>
          </table>

          <div className="level-filter">
            {SEVERITY_ORDER.map((level) => (
              <button
                key={level}
                type="button"
                className={`level-chip level-${level} ${levels.has(level) ? "on" : ""}`}
                onClick={() => toggleLevel(level)}
                title={levels.has(level) ? "скрыть" : "показать"}
              >
                {SEVERITY_TITLE[level]} {report.counts[level] ?? 0}
              </button>
            ))}
            {report.variants.length > 1 && (
              <button
                type="button"
                className={`level-chip level-variant ${onlySelected ? "on" : ""}`}
                onClick={() => setOnlySelected(!onlySelected)}
                disabled={!selectedVariantId}
              >
                только выбранный вариант
              </button>
            )}
          </div>

          {total === 0 && (
            <p className="note" style={{ marginTop: 0 }}>
              Находок нет: все проверки пройдены.
            </p>
          )}
          {total > 0 && shown === 0 && (
            <p className="note" style={{ marginTop: 0 }}>
              По выбранным фильтрам находок нет (всего {total}).
            </p>
          )}

          {groups.map((group) => (
            <details key={group.code} className="finding-group" open={group.worst === 0}>
              <summary>
                <span className={`finding-dot level-${SEVERITY_ORDER[group.worst]}`} />
                <span className="mono finding-code">{group.code}</span>
                <span className="muted">
                  {group.list.length} ·{" "}
                  {SEVERITY_ORDER.filter((level) => countOf(group.list, level) > 0)
                    .map((level) => `${SEVERITY_TITLE[level]} ${countOf(group.list, level)}`)
                    .join(", ")}
                </span>
              </summary>
              <div className="issues">
                {group.list.map((f, index) => {
                  const level = severityOf(f);
                  const norm = formatFact(f.expected);
                  const fact = formatFact(f.actual);
                  const canLocate = locatable(f);
                  const active = activeFinding === f;
                  const body = (
                    <>
                      <b>
                        {f.variant_id && <span className="finding-variant">вар. {variantNo(f.variant_id)}</span>}
                        {f.object_id !== null && f.object_id !== undefined && (
                          <span className="mono">{String(f.object_id)}</span>
                        )}
                        {canLocate && <span className="finding-locate">{active ? "на карте ✓" : "на карте"}</span>}
                      </b>
                      <span>{f.message}</span>
                      {(norm || fact) && (
                        <span className="finding-fact">
                          {norm && `норма ${norm}`}
                          {norm && fact && " · "}
                          {fact && `факт ${fact}`}
                        </span>
                      )}
                    </>
                  );
                  const className = `issue issue-${level} finding${active ? " finding-active" : ""}`;
                  return canLocate ? (
                    <button
                      key={`${f.code}-${index}`}
                      type="button"
                      className={className}
                      title="выбрать вариант и показать объект на карте"
                      onClick={() => onPick(f)}
                    >
                      {body}
                    </button>
                  ) : (
                    <div key={`${f.code}-${index}`} className={className}>
                      {body}
                    </div>
                  );
                })}
              </div>
            </details>
          ))}
        </>
      )}

      <p className="note">
        Валидатор написан отдельно от солвера по тексту технического приложения: расходы, Ду,
        предельная длина, отступы, спецпроходы, стоимости и показатель пересчитываются заново.
      </p>
    </section>
  );
}

// --- Ключевые цифры -------------------------------------------------------------------

export function KeyFigures({ variant, rules }: { variant: VariantSummary; rules: Rules | null }) {
  const score = rules?.score ?? DEFAULT_SCORE;
  const costTerm = (score.cost_weight * variant.calculated_cost) / score.cost_scale;
  const lengthTerm = (score.length_weight * variant.new_network_length) / score.length_scale;
  const segmentsCost =
    variant.construction_cost - variant.chamber_construction_cost - variant.existing_chamber_tie_in_cost;
  const techNodes = variant.nodes.filter((n) => n.kind === "technical_node").length;
  const specialSegments = variant.segments.filter((s) => s.laying === "special").length;

  const rows: Array<[string, number, string?]> = [
    ["новые участки сети", segmentsCost, `${variant.segments.length} шт.`],
    ["новые тепловые камеры", variant.chamber_construction_cost, `${variant.chambers.length} шт.`],
    [
      "врезки в существующие камеры",
      variant.existing_chamber_tie_in_cost,
      `${variant.existing_chamber_tie_in_count} шт.`,
    ],
  ];
  if (variant.unconnected_penalty > 0) {
    rows.push(["штраф за неподключённые ОКС", variant.unconnected_penalty, `${variant.unconnected_oks_ids.length} шт.`]);
  }
  const max = Math.max(...rows.map((r) => r[1]), 1);

  return (
    <section className="panel key">
      <div className="figures">
        <div className="figure">
          <div className="figure-value">{formatScore(variant.score)}</div>
          <div className="figure-label">показатель S (ранг {variant.rank})</div>
        </div>
        <div className="figure">
          <div className="figure-value" title={formatRub(variant.calculated_cost)}>
            {formatMoney(variant.calculated_cost)}
          </div>
          <div className="figure-label">расчётная стоимость C</div>
        </div>
        <div className="figure">
          <div className="figure-value">{formatLength(variant.new_network_length)}</div>
          <div className="figure-label">длина новой сети L</div>
        </div>
        <div className="figure">
          <div className="figure-value">
            {variant.chambers.length}
            <span className="figure-sep">/</span>
            {techNodes}
            <span className="figure-sep">/</span>
            {specialSegments}
          </div>
          <div className="figure-label">камер / техузлов / спецпроходов</div>
        </div>
      </div>

      <div className="formula">
        <div>
          S = {formatNumber(score.cost_weight, 2)} · C / {formatNumber(score.cost_scale, 0)} +{" "}
          {formatNumber(score.length_weight, 2)} · L / {formatNumber(score.length_scale, 0)}
        </div>
        <div className="muted">
          = {formatNumber(score.cost_weight, 2)} · {formatNumber(variant.calculated_cost, 2)} /{" "}
          {formatNumber(score.cost_scale, 0)} + {formatNumber(score.length_weight, 2)} ·{" "}
          {formatNumber(variant.new_network_length, 3)} / {formatNumber(score.length_scale, 0)}
        </div>
        <div>
          = {formatScore(costTerm)} + {formatScore(lengthTerm)} = <b>{formatScore(costTerm + lengthTerm)}</b>
        </div>
      </div>

      <ul className="bars">
        {rows.map(([title, value, hint]) => (
          <li key={title}>
            <div className="bar-label">
              <span>
                {title}
                {hint && <span className="muted"> · {hint}</span>}
              </span>
              <span className="strong" title={formatRub(value)}>
                {formatMoney(value)}
              </span>
            </div>
            <div className="bar-track">
              <div className="bar-fill" style={{ width: `${(value / max) * 100}%` }} />
            </div>
          </li>
        ))}
        <li>
          <div className="bar-label total-row">
            <span>стоимость строительства</span>
            <span className="strong" title={formatRub(variant.construction_cost)}>
              {formatMoney(variant.construction_cost)}
            </span>
          </div>
        </li>
        <li>
          <div className="bar-label">
            <span className="muted" title="Разъяснение № 14 к техническому приложению: реконструкция существующей сети и камер в актуальной модели не выполняется, дополнительный расход к источнику не распространяется">
              реконструкция существующей сети и камер · не входит в актуальную модель
            </span>
            <span className="muted">—</span>
          </div>
        </li>
      </ul>
    </section>
  );
}

// --- Участки ---------------------------------------------------------------------------

interface SegmentRow {
  id: string;
  from: string;
  to: string;
  flow: number;
  du: number;
  length: number;
  laying: Laying;
  special: string;
  k: number;
  /** Глубина верха габарита, м (режим depth); null в 2D. */
  depthStart: number | null;
  depthEnd: number | null;
  kDepth: number;
  cost: number;
}

const LAYING_SHORT: Record<Laying, string> = { base: "обычн.", special: "спец." };

function publicSegments(output: FeatureCollection | null, variantId: string): Map<string, OutSegmentProps> {
  const map = new Map<string, OutSegmentProps>();
  for (const feature of output?.features ?? []) {
    const p = feature.properties as OutSegmentProps | null;
    if (p?.object_type === "heat_network" && p.variant_id === variantId) map.set(p.internal_id, p);
  }
  return map;
}

function publicChambers(output: FeatureCollection | null, variantId: string): Map<string, OutChamberProps> {
  const map = new Map<string, OutChamberProps>();
  for (const feature of output?.features ?? []) {
    const p = feature.properties as OutChamberProps | null;
    if (p?.object_type === "heat_chamber" && p.variant_id === variantId) map.set(p.internal_id, p);
  }
  return map;
}

/** Имя узла по внутреннему id: вид из сводки, публичный id из выхода. */
function nodeNamer(variant: VariantSummary, output: FeatureCollection | null) {
  const nodes = new Map(variant.nodes.map((n) => [n.id, n]));
  const chambers = publicChambers(output, variant.id);
  const techIds = new Map<string, string>();
  for (const feature of output?.features ?? []) {
    const p = feature.properties as { object_type?: string; variant_id?: string; internal_id?: string; id?: string };
    if (p?.object_type === "technical_node" && p.variant_id === variant.id && p.internal_id && p.id) {
      techIds.set(p.internal_id, p.id);
    }
  }
  return (internalId: string): string => {
    const node = nodes.get(internalId);
    const kind: NodeKind | undefined = node?.kind;
    if (kind === "oks" || kind === "chamber_existing") return nodeLabel(kind, node?.ref ?? internalId);
    if (kind === "chamber_new") return nodeLabel(kind, chambers.get(internalId)?.id ?? internalId);
    if (kind === "technical_node") return nodeLabel(kind, techIds.get(internalId) ?? internalId);
    return internalId;
  };
}

const shortId = (id: string) => id.replace(/^v\d+_/, "");

export function SegmentsTable({ variant, output }: { variant: VariantSummary; output: FeatureCollection | null }) {
  const rows = useMemo<SegmentRow[]>(() => {
    const pub = publicSegments(output, variant.id);
    const name = nodeNamer(variant, output);
    return variant.segments.map((s) => {
      const p = pub.get(s.id);
      return {
        id: p?.id ?? s.id,
        from: name(s.start),
        to: name(s.end),
        flow: s.flow_tph,
        du: s.du,
        length: p?.length ?? s.length_m,
        laying: s.laying,
        special: s.special_types.map((t) => RESTRICTION_TITLE[t] ?? t).join(", "),
        k: s.k_special,
        depthStart: p?.depth_start ?? s.depth_start ?? null,
        depthEnd: p?.depth_end ?? s.depth_end ?? null,
        kDepth: p?.k_depth ?? s.k_depth ?? 1,
        cost: p?.cost ?? s.cost,
      };
    });
  }, [variant, output]);
  const { sorted, sortKey, dir, toggle } = useSort(rows, "id");
  const totalLength = rows.reduce((sum, r) => sum + r.length, 0);
  const totalCost = rows.reduce((sum, r) => sum + r.cost, 0);
  const th = { sortKey, dir, onToggle: toggle };
  const withDepth = rows.some((r) => r.depthStart !== null);

  return (
    <section className="panel">
      <h3>
        Новые участки сети <span className="muted">{rows.length}</span>
      </h3>
      <div className="table-wrap">
        <table className="checks dense">
          <thead>
            <tr>
              <Th<SegmentRow> label="id" field="id" {...th} />
              <Th<SegmentRow> label="от → до" field="from" {...th} />
              <Th<SegmentRow> label="расход, т/ч" field="flow" {...th} />
              <Th<SegmentRow> label="Ду" field="du" {...th} />
              <Th<SegmentRow> label="длина, м" field="length" {...th} />
              <Th<SegmentRow> label="способ" field="laying" {...th} />
              <Th<SegmentRow> label="Kспец" field="k" {...th} />
              {withDepth && <Th<SegmentRow> label="глубина, м" field="depthStart" {...th} title="глубина верха габарита: начало → конец" />}
              {withDepth && <Th<SegmentRow> label="Kгл" field="kDepth" {...th} />}
              <Th<SegmentRow> label="стоимость" field="cost" {...th} />
            </tr>
          </thead>
          <tbody>
            {sorted.map((row) => (
              <tr key={row.id} className={row.laying === "special" ? "row-conditional" : ""}>
                <td className="nowrap mono" title={row.id}>{shortId(row.id)}</td>
                <td>
                  {row.from} → {row.to}
                </td>
                <td className="num">{formatNumber(row.flow, 3)}</td>
                <td className="num strong">{row.du}</td>
                <td className="num">{formatNumber(row.length, 1)}</td>
                <td className="nowrap" title={row.special || undefined}>
                  {LAYING_SHORT[row.laying]}
                  {row.special && <div className="clause">{row.special}</div>}
                </td>
                <td className="num">{row.k !== 1 ? formatNumber(row.k, 2) : "—"}</td>
                {withDepth && (
                  <td className="num">
                    {row.depthStart !== null && row.depthEnd !== null
                      ? `${formatNumber(row.depthStart, 2)} → ${formatNumber(row.depthEnd, 2)}`
                      : "—"}
                  </td>
                )}
                {withDepth && <td className="num">{row.kDepth !== 1 ? formatNumber(row.kDepth, 3) : "—"}</td>}
                <td className="num" title={formatRub(row.cost)}>{formatMoney(row.cost)}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <td colSpan={4}>итого</td>
              <td className="num strong">{formatNumber(totalLength, 1)}</td>
              <td colSpan={withDepth ? 4 : 2} />
              <td className="num strong" title={formatRub(totalCost)}>{formatMoney(totalCost)}</td>
            </tr>
          </tfoot>
        </table>
      </div>
      <p className="note">
        Расход участка — сумма расходов точек подключения дальше по сети; Ду — минимальный
        по таблице 1 приложения с учётом предельной длины и неубывания к месту присоединения.
        Стоимость = длина · ₽/м(Ду) · Kспец{withDepth ? " · Kгл" : ""}. Спецпроходы выделены.
      </p>
    </section>
  );
}

// --- Камеры и врезки -------------------------------------------------------------------

interface ChamberRow {
  id: string;
  du: number;
  cost: number;
  onExisting: string;
  connections: number;
}

export function ChambersTable({
  variant,
  output,
  rules,
}: {
  variant: VariantSummary;
  output: FeatureCollection | null;
  rules: Rules | null;
}) {
  const rows = useMemo<ChamberRow[]>(() => {
    const pub = publicChambers(output, variant.id);
    const nodes = new Map(variant.nodes.map((n) => [n.id, n]));
    return variant.chambers.map((c) => {
      const p = pub.get(c.node_id);
      const node = nodes.get(c.node_id);
      return {
        id: p?.id ?? c.node_id,
        du: c.diameter,
        cost: c.cost,
        onExisting: key(p?.on_existing_network ?? node?.on_existing_network),
        connections: node?.connections_used ?? 0,
      };
    });
  }, [variant, output]);
  const { sorted, sortKey, dir, toggle } = useSort(rows, "id");
  const th = { sortKey, dir, onToggle: toggle };

  const degree = new Map<string, number>();
  for (const s of variant.segments) {
    degree.set(s.start, (degree.get(s.start) ?? 0) + 1);
    degree.set(s.end, (degree.get(s.end) ?? 0) + 1);
  }
  const tieIns = variant.nodes.filter((n) => n.kind === "chamber_existing");
  const tieInCost = rules?.chambers.tie_in_cost ?? 5_000_000;

  return (
    <section className="panel">
      <h3>
        Новые тепловые камеры <span className="muted">{rows.length}</span>
      </h3>
      {rows.length > 0 ? (
        <table className="checks dense">
          <thead>
            <tr>
              <Th<ChamberRow> label="id" field="id" {...th} />
              <Th<ChamberRow> label="Ду" field="du" {...th} />
              <Th<ChamberRow> label="стоимость" field="cost" {...th} />
              <Th<ChamberRow> label="точка врезки" field="onExisting" {...th} title="камера стоит на существующем участке — место присоединения к сети" />
              <Th<ChamberRow> label="примык." field="connections" {...th} title="занято примыканий из 4" />
            </tr>
          </thead>
          <tbody>
            {sorted.map((row) => (
              <tr key={row.id}>
                <td className="nowrap mono" title={row.id}>{shortId(row.id)}</td>
                <td className="num strong">{row.du}</td>
                <td className="num" title={formatRub(row.cost)}>{formatMoney(row.cost)}</td>
                <td className="nowrap">{row.onExisting ? `на участке ${row.onExisting}` : "разветвление"}</td>
                <td className="num">{row.connections} / 4</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="note">Новые камеры не потребовались.</p>
      )}

      <div className="sub">
        Врезки в существующие камеры <span className="muted">{tieIns.length}</span>
      </div>
      {tieIns.length > 0 ? (
        <ul className="plain">
          {tieIns.map((node) => {
            const count = degree.get(node.id) ?? 0;
            return (
              <li key={node.id}>
                ТК {key(node.ref)} — новых участков {count}, занято примыканий {node.connections_used ?? "?"} из 4,{" "}
                <b>{formatMoney(count * tieInCost)}</b>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="note">
          Врезок в существующие камеры нет: камеры ближе {formatNumber(rules?.chambers.reuse_existing_within_m ?? 10, 0)} м к
          месту присоединения не нашлось, в точках присоединения поставлены новые камеры.
        </p>
      )}
      <p className="note">
        Ду и стоимость камеры — по наибольшему Ду примыкающих участков, включая существующий;
        к камере примыкает не более четырёх участков.
      </p>
    </section>
  );
}

// --- Предельная длина --------------------------------------------------------------------

export function LimitChecksPanel({ variant }: { variant: VariantSummary }) {
  const checks = variant.limit_length_checks;
  const failed = checks.filter((c) => !c.ok).length;
  const byOks = new Map<string, typeof checks>();
  for (const check of checks) {
    const k = key(check.oks_id);
    byOks.set(k, [...(byOks.get(k) ?? []), check]);
  }

  return (
    <section className="panel">
      <h3>
        Проверка предельной длины{" "}
        <span className={failed ? "badge badge-fail" : "badge badge-pass"}>
          {failed ? `нарушений ${failed}` : `выполнена · ${checks.length}`}
        </span>
      </h3>
      <p className="note" style={{ marginTop: 0 }}>
        Для каждого пути от точки подключения до места присоединения длина последовательных
        участков одного Ду не должна превышать предел таблицы 1. Камера и техузел новый отсчёт
        не начинают, смена Ду — начинает.
      </p>
      <table className="checks dense">
        <thead>
          <tr>
            <th>точка</th>
            <th>Ду</th>
            <th>отрезок, м</th>
            <th>предел, м</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {[...byOks.entries()]
            .sort((a, b) => compare(Number(a[0]) || a[0], Number(b[0]) || b[0]))
            .flatMap(([oks, list]) =>
              list.map((check, index) => (
                <tr key={`${oks}-${index}`} className={check.ok ? "" : "row-fail"}>
                  <td className="nowrap">{index === 0 ? `#${oks}` : ""}</td>
                  <td className="num strong">{check.du}</td>
                  <td className="num">{formatNumber(check.run_length_m, 1)}</td>
                  <td className="num">{formatNumber(check.limit_m, 0)}</td>
                  <td className={check.ok ? "ok" : "bad"}>{check.ok ? "✓" : "✗"}</td>
                </tr>
              )),
            )}
        </tbody>
      </table>
      {variant.diameter_bumps.length > 0 && (
        <>
          <div className="sub">Поднятия Ду из-за предельной длины</div>
          <ul className="plain reasons-list">
            {variant.diameter_bumps.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </>
      )}
      {variant.minimality.length > 0 && (
        <>
          <div className="sub warn-text">Замечания по минимальности Ду</div>
          <ul className="plain reasons-list">
            {variant.minimality.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

// --- Спецпроходы и ремонты -------------------------------------------------------------

export function SpecialPanel({ variant }: { variant: VariantSummary }) {
  const special = variant.segments.filter((s) => s.laying === "special");
  const hasAnything =
    special.length || variant.special_rebuilt.length || variant.unresolved_special.length || variant.repairs.length;

  return (
    <section className="panel">
      <h3>
        Пространственные ограничения{" "}
        <span className="muted">спецпроходов {variant.special_crossings}</span>
      </h3>
      {!hasAnything && (
        <p className="note" style={{ marginTop: 0 }}>
          Маршруты обходят запретные объекты с отступами; пересечений, требующих специального
          прохода, в этом варианте нет.
        </p>
      )}
      {special.length > 0 && (
        <ul className="plain">
          {special.map((s) => (
            <li key={s.id}>
              <span className="mono">{s.id}</span> — {s.special_types.map((t) => RESTRICTION_TITLE[t] ?? t).join(", ") || "спецпроход"},
              Kспец {formatNumber(s.k_special, 2)}, {formatNumber(s.length_m, 1)} м
            </li>
          ))}
        </ul>
      )}
      {variant.special_rebuilt.length > 0 && (
        <>
          <div className="sub">Перестроенные пересечения</div>
          <ul className="plain reasons-list">
            {variant.special_rebuilt.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </>
      )}
      {variant.unresolved_special.length > 0 && (
        <>
          <div className="sub bad">Пересечения, которые не удалось оформить</div>
          <ul className="plain reasons-list">
            {variant.unresolved_special.map((line) => (
              <li key={line} className="bad">{line}</li>
            ))}
          </ul>
        </>
      )}
      {variant.repairs.length > 0 && (
        <>
          <div className="sub">Ремонты трассы после проверки</div>
          <ul className="plain reasons-list">
            {variant.repairs.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

// --- Неподключённые и запасной подход -------------------------------------------------

export function ConnectionsPanel({ variant }: { variant: VariantSummary }) {
  const oksNodes = variant.nodes.filter((n) => n.kind === "oks");
  const fallback = oksNodes.filter((n) => n.approach_rule === "fallback");
  const unconnected = variant.unconnected_oks_ids;

  return (
    <section className="panel">
      <h3>
        Точки подключения{" "}
        <span className={unconnected.length ? "badge badge-fail" : "badge badge-pass"}>
          {unconnected.length ? `не подключено ${unconnected.length}` : `подключены все ${oksNodes.length}`}
        </span>
      </h3>
      {unconnected.length > 0 && (
        <div className="issues">
          {unconnected.map((id: Id) => (
            <div key={key(id)} className="issue">
              <b>ОКС #{key(id)} — маршрут не найден автоматически</b>
              <span>{variant.unconnected_reasons?.[key(id)] ?? "причина не указана"}</span>
            </div>
          ))}
          <p className="note" style={{ marginTop: 0 }}>
            Частичный результат сохранён; штраф {formatMoney(variant.unconnected_penalty)} учтён в C.
            Отсутствие автоматического маршрута не означает, что подключение невозможно.
          </p>
        </div>
      )}
      {fallback.length > 0 && (
        <>
          <div className="sub">Запасной подход к точке</div>
          <p className="note" style={{ marginTop: 0 }}>
            Для {fallback.map((n) => `#${key(n.ref)}`).join(", ")} финальный участок от ближайшей
            границы полигона не проходит (двор, вогнутый контур); взята следующая по расстоянию
            точка контура. Это отступление от буквы правила помечено в выходе как{" "}
            <code>approach_rule = fallback</code>.
          </p>
        </>
      )}
      {unconnected.length === 0 && fallback.length === 0 && (
        <p className="note" style={{ marginTop: 0 }}>
          Для каждой точки построен один финальный прямой участок от ближайшей границы
          собственного полигона ОКС.
        </p>
      )}
    </section>
  );
}

// --- Существующая сеть: добавочный расход к источнику (ТЗ §4 п. 6) ---------------------

export function ExistingImpactPanel({
  variant,
  activeEdgeId,
  onPick,
}: {
  variant: VariantSummary;
  /** Существующий участок, подсвеченный сейчас на карте. */
  activeEdgeId: string | null;
  onPick: (edgeId: Id) => void;
}) {
  const impact = variant.existing_impact;
  if (impact === undefined) return null; // сводка старого задания — считалось до появления анализа
  const edges = impact?.edges ?? [];
  const peak = edges.reduce((m, e) => Math.max(m, e.share_of_capacity ?? 0), 0);
  const unreached = impact?.unreached_tie_ins ?? [];
  const noData = !impact || !impact.source_found || (edges.length === 0 && unreached.length > 0);
  const badge = noData
    ? { cls: "badge", text: "нет данных" }
    : edges.length === 0
      ? { cls: "badge", text: "нет новых подключений" }
      : peak >= 1
        ? { cls: "badge badge-fail", text: `добавка ${Math.round(peak * 100)}% пропускной` }
        : peak >= 0.5
          ? { cls: "badge badge-conditional", text: `до ${Math.round(peak * 100)}% пропускной` }
          : { cls: "badge badge-pass", text: `до ${Math.round(peak * 100)}% пропускной` };

  return (
    <section className="panel">
      <h3>
        Существующая сеть: добавочный расход к источнику{" "}
        <span className={badge.cls}>{badge.text}</span>
      </h3>
      <p className="note" style={{ marginTop: 0 }}>
        От каждой точки присоединения — кратчайший путь по существующей сети к источнику;
        расход новых подключений суммируется на участках пути. Доля — отношение добавки к
        пропускной способности Ду по таблице 1. Текущие расходы сети во входе не заданы,
        реконструкция в модели не выполняется (разъяснение № 14), поэтому это ориентир, где
        сеть нагружается сильнее всего, а не вывод о реконструкции.
      </p>
      {!impact || !impact.source_found ? (
        <p className="note" style={{ marginTop: 0 }}>
          {impact?.note ?? "источник или существующая сеть отсутствуют — путь к источнику не строится"}
        </p>
      ) : edges.length === 0 ? (
        <p className="note" style={{ marginTop: 0 }}>
          {unreached.length
            ? "Ни от одной точки присоединения источник не достижим по существующей сети."
            : "Новых подключений с расходом нет."}
        </p>
      ) : (
        <table className="checks dense">
          <thead>
            <tr>
              <th>участок</th>
              <th className="num">Ду</th>
              <th className="num">добавка, т/ч</th>
              <th>доля пропускной</th>
              <th className="num">врезок</th>
            </tr>
          </thead>
          <tbody>
            {edges.map((e, index) => {
              const id = key(e.edge_id);
              const share = e.share_of_capacity;
              const active = activeEdgeId === id;
              return (
                <tr
                  key={`${id}-${index}`}
                  className={`row-pick${active ? " row-selected" : ""}${share !== null && share >= 1 ? " row-fail" : ""}`}
                  title="показать участок на карте"
                  role="button"
                  tabIndex={0}
                  aria-pressed={active}
                  onClick={() => onPick(e.edge_id)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      onPick(e.edge_id);
                    }
                  }}
                >
                  <td className="nowrap">
                    <code>{id}</code>
                    <span className="muted"> · {formatNumber(e.length_m, 0)} м</span>
                  </td>
                  <td className="num strong">{e.diameter}</td>
                  <td className="num">{formatNumber(e.added_flow_tph, 2)}</td>
                  <td>
                    {share === null ? (
                      <span className="muted">Ду вне таблицы 1</span>
                    ) : (
                      <div className="share">
                        <div className="bar-track">
                          <div
                            className={`bar-fill${share >= 1 ? " bar-bad" : share >= 0.5 ? " bar-warn" : ""}`}
                            style={{ width: `${Math.min(100, share * 100)}%` }}
                          />
                        </div>
                        <span className="share-text">
                          {Math.round(share * 100)}% из {formatNumber(e.capacity_tph ?? 0, 0)}
                        </span>
                      </div>
                    )}
                  </td>
                  <td className="num" title={e.tie_ins.join(", ")}>{e.tie_ins.length}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {unreached.length > 0 && (
        <>
          <div className="sub warn-text">Источник не достижим по сети</div>
          <ul className="plain reasons-list">
            {unreached.map((label, index) => (
              <li key={`${index}-${label}`}>{label}: существующая сеть от точки присоединения до источника не связна</li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

// --- Глубина (дополнительная задача) ----------------------------------------------------

export function DepthPanel({ variant }: { variant: VariantSummary }) {
  const depth = variant.depth;
  const deepPieces = variant.segments.filter((s) => (s.k_depth ?? 1) > 1);
  const extraCost = deepPieces.reduce((sum, s) => sum + s.cost * (1 - 1 / (s.k_depth ?? 1)), 0);
  return (
    <section className="panel">
      <h3>
        Профиль глубины{" "}
        {depth && (
          <span className={depth.issues.length ? "badge badge-fail" : "badge badge-pass"}>
            {depth.issues.length ? `замечаний ${depth.issues.length}` : "требования выполнены"}
          </span>
        )}
      </h3>
      {depth ? (
        <>
          <dl className="grid">
            <dt>Пересечений с требованием по глубине</dt>
            <dd className="strong">{depth.crossings_with_depth}</dd>
            <dt>Проход под объектом / над объектом</dt>
            <dd>
              {depth.below_object} / {depth.above_object}
            </dd>
            <dt>Глубина верха габарита, м</dt>
            <dd>
              {formatNumber(depth.min_top_depth_m, 2)} … {formatNumber(depth.max_top_depth_m, 2)}
            </dd>
            <dt>Участков разделено техузлами</dt>
            <dd>{depth.pieces_split}</dd>
            <dt>Участков с Kгл &gt; 1</dt>
            <dd>{deepPieces.length}</dd>
            <dt>Удорожание из-за глубины</dt>
            <dd title={formatRub(extraCost)}>{formatMoney(extraCost)}</dd>
          </dl>
          {depth.issues.length > 0 && (
            <ul className="plain reasons-list">
              {depth.issues.map((line) => (
                <li key={line} className="bad">
                  {line}
                </li>
              ))}
            </ul>
          )}
        </>
      ) : (
        <p className="error">Сводка задания не содержит профиля глубины — сервис расчёта нужно перезапустить с актуальным кодом.</p>
      )}
      <p className="note">
        Обычная глубина верха габарита 3,0 м, минимальная 0,7 м; уклон спусков и подъёмов не круче
        0,10. Kгл = 1 + 0,10·(h − 3) при h &gt; 3 м; на участке спуска — среднее по концам. Смена
        глубины и пересечение отметки 3,0 м делят участок техническим узлом.
      </p>
    </section>
  );
}

// --- Выгрузка ----------------------------------------------------------------------------

export function DownloadPanel({ jobId, mode }: { jobId: string; mode?: SolveMode }) {
  return (
    <section className="panel">
      <a className="export" href={api.outputUrl(jobId)} download={`result_${jobId}.geojson`}>
        <span className="export-format">GeoJSON</span>
        <span>
          <b>Скачать результат{mode === "depth" ? " (режим с глубиной)" : ""}</b>
          <span className="muted">
            {" "}
            — один совмещённый файл со всеми вариантами: участки, камеры, техузлы, сводки
          </span>
        </span>
      </a>
      <p className="note">
        Реконструкция существующей сети и камер в актуальной модели приложения не выполняется
        (разъяснение № 14): стоимость реконструкции в C не входит, а добавочный расход по
        существующей сети выше показан справочно и в файл не выгружается.
      </p>
    </section>
  );
}
