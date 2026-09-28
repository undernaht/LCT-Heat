/** Левая колонка: загрузка конкурсного набора, список проектов, паспорт входа,
 *  протокол разбора и запуск расчёта.
 *
 * Паспорт и протокол — не украшение: молчаливая потеря половины объектов при
 * зелёном ответе хуже честного отказа, а на защите первый вопрос — «что вы
 * прочитали из нашего файла». */

import { useRef, useState } from "react";

import { formatDate, formatLength, formatNumber } from "./api";
import { RESTRICTION_TITLE } from "./mapStyle";
import type { Effort, Issue, Job, ProjectBrief, ProjectStats, SolveMode } from "./types";

// --- Проекты и загрузка -------------------------------------------------------------

export function ProjectsPanel({
  projects,
  selectedId,
  busy,
  onSelect,
  onDelete,
  onUpload,
}: {
  projects: ProjectBrief[];
  selectedId: string | null;
  busy: boolean;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onUpload: (file: File, name: string) => Promise<void>;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);

  function pick(list: FileList | null) {
    const picked = list?.[0];
    if (!picked) return;
    setFile(picked);
    setError(null);
    if (!name.trim()) setName(picked.name.replace(/\.[^.]+$/, ""));
  }

  async function send() {
    if (!file) return;
    setError(null);
    try {
      await onUpload(file, name.trim() || file.name.replace(/\.[^.]+$/, ""));
      setFile(null);
      setName("");
      if (input.current) input.current.value = "";
    } catch (e) {
      setError(String((e as Error).message ?? e));
    }
  }

  return (
    <section className="panel">
      <h3>Конкурсный набор</h3>

      <div
        className={`dropzone ${file ? "filled" : ""} ${dragging ? "dragging" : ""}`}
        onClick={() => input.current?.click()}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          pick(event.dataTransfer.files);
        }}
      >
        <input
          ref={input}
          type="file"
          accept=".geojson,.json,application/geo+json,application/json"
          hidden
          onChange={(event) => pick(event.target.files)}
        />
        {file ? (
          <>
            <b>{file.name}</b>
            <span className="muted">{(file.size / 1024 / 1024).toFixed(2)} МБ</span>
          </>
        ) : (
          <>
            <b>Перетащите совмещённый GeoJSON</b>
            <span className="muted">либо нажмите, чтобы выбрать файл</span>
          </>
        )}
      </div>

      <label className="field">
        <span>Название проекта</span>
        <input
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="например, ЗИЛ — конкурсный набор"
        />
      </label>

      <button className="primary" onClick={send} disabled={busy || !file}>
        {busy ? "Загружаю…" : "Загрузить и разобрать"}
      </button>
      {error && <p className="error">{error}</p>}

      {projects.length > 0 && (
        <ul className="project-list">
          {projects.map((project) => (
            <li
              key={project.id}
              className={project.id === selectedId ? "project-row selected" : "project-row"}
            >
              <button type="button" className="project-pick" onClick={() => onSelect(project.id)}>
                <span className="project-name">{project.name}</span>
                <span className="project-meta">
                  {formatDate(project.created_at)} · точек {project.stats.oks_points} · расчётов{" "}
                  {project.jobs.length}
                </span>
              </button>
              <button
                type="button"
                className="project-delete"
                title="Удалить проект"
                onClick={() => {
                  if (window.confirm(`Удалить проект «${project.name}» вместе с расчётами?`)) onDelete(project.id);
                }}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

// --- Паспорт входа --------------------------------------------------------------------

const LEVEL_TITLE: Record<string, string> = {
  error: "ошибки",
  warning: "предупреждения",
  info: "замечания",
};
const LEVEL_ORDER = ["error", "warning", "info"];

export function PassportPanel({ stats, issues }: { stats: ProjectStats; issues: Issue[] }) {
  const byLevel = new Map<string, Issue[]>();
  for (const issue of issues) {
    const list = byLevel.get(issue.level) ?? [];
    list.push(issue);
    byLevel.set(issue.level, list);
  }
  const levels = [...byLevel.keys()].sort(
    (a, b) => (LEVEL_ORDER.indexOf(a) + 1 || 99) - (LEVEL_ORDER.indexOf(b) + 1 || 99),
  );
  const restrictions = Object.entries(stats.restrictions).sort((a, b) => b[1] - a[1]);

  return (
    <section className="panel">
      <h3>Паспорт входа</h3>
      <dl className="grid">
        <dt>Источник тепла</dt>
        <dd className={stats.source ? "ok" : "bad"}>{stats.source ? "есть" : "не найден"}</dd>
        <dt>Участков существующей сети</dt>
        <dd>
          {stats.network_edges} · {formatLength(stats.network_length_m)}
        </dd>
        <dt>Существующих камер</dt>
        <dd>{stats.chambers}</dd>
        <dt>Точек подключения ОКС</dt>
        <dd className="strong">{stats.oks_points}</dd>
        <dt>Суммарный расход</dt>
        <dd className="strong">{formatNumber(stats.total_flow_tph, 2)} т/ч</dd>
      </dl>

      <div className="sub">Ограничения по типам</div>
      <table className="checks">
        <tbody>
          {restrictions.map(([type, count]) => (
            <tr key={type}>
              <td>
                {RESTRICTION_TITLE[type] ?? type}
                <span className="muted"> {type}</span>
              </td>
              <td className="nowrap strong">{count}</td>
            </tr>
          ))}
          {stats.unknown_restrictions > 0 && (
            <tr className="row-conditional">
              <td>типы, не описанные в приложении (в расчёте не участвуют)</td>
              <td className="nowrap strong">{stats.unknown_restrictions}</td>
            </tr>
          )}
        </tbody>
      </table>

      {issues.length > 0 ? (
        <details open={byLevel.has("error")}>
          <summary>
            Протокол разбора ({issues.length}
            {levels.map((level) => ` · ${LEVEL_TITLE[level] ?? level} ${byLevel.get(level)?.length ?? 0}`).join("")})
          </summary>
          {levels.map((level) => (
            <div key={level} className="issues">
              {(byLevel.get(level) ?? []).map((issue, index) => (
                <div key={`${issue.where}-${index}`} className={`issue issue-${issue.level}`}>
                  <b>
                    {issue.where}
                    {issue.count > 1 && <span className="muted"> ×{issue.count}</span>}
                  </b>
                  <span>{issue.detail}</span>
                </div>
              ))}
            </div>
          ))}
        </details>
      ) : (
        <p className="note">Замечаний к входу нет.</p>
      )}
    </section>
  );
}

// --- Расчёт -----------------------------------------------------------------------------

const STATUS_TITLE: Record<string, string> = {
  queued: "в очереди",
  running: "считается",
  done: "готово",
  failed: "ошибка",
};

const MODE_SHORT: Record<string, string> = { "2d": "2D", depth: "глубина" };

export function SolvePanel({
  jobs,
  selectedJobId,
  disabledReason,
  onSolve,
  onSelectJob,
}: {
  jobs: Job[];
  selectedJobId: string | null;
  /** Почему расчёт недоступен; null — доступен. */
  disabledReason: string | null;
  onSolve: (mode: SolveMode, maxVariants: number, effort: Effort) => void;
  onSelectJob: (id: string) => void;
}) {
  const [mode, setMode] = useState<SolveMode>("2d");
  const [maxVariants, setMaxVariants] = useState(3);
  const [effort, setEffort] = useState<Effort>("standard");
  const active = jobs.some((job) => job.status === "queued" || job.status === "running");

  return (
    <section className="panel">
      <h3>Расчёт</h3>
      <div className="controls">
        <label className="field small">
          <span>Режим</span>
          <select value={mode} onChange={(event) => setMode(event.target.value as SolveMode)}>
            <option value="2d">2D — обязательная часть</option>
            <option value="depth">с учётом глубины</option>
          </select>
        </label>
        <label className="field small">
          <span>Вариантов</span>
          <select value={maxVariants} onChange={(event) => setMaxVariants(Number(event.target.value))}>
            <option value={1}>1</option>
            <option value={2}>2</option>
            <option value={3}>до 3</option>
          </select>
        </label>
      </div>
      <label className="field">
        <span>Тщательность</span>
        <select value={effort} onChange={(event) => setEffort(event.target.value as Effort)}>
          <option value="standard">стандартно</option>
          <option value="thorough">тщательно (дольше в 2–3 раза: плюс перестановки точек)</option>
        </select>
      </label>
      <button
        className="primary"
        onClick={() => onSolve(mode, maxVariants, effort)}
        disabled={Boolean(disabledReason) || active}
        title={disabledReason ?? undefined}
      >
        {active ? "Идёт расчёт…" : "Построить варианты подключения"}
      </button>
      {disabledReason && !active && <p className="error">Расчёт недоступен: {disabledReason}</p>}
      <p className="note">
        Все перспективные ОКС обрабатываются за один запуск, маршруты и точки врезки
        выбираются автоматически. Три варианта — около минуты; режим с учётом глубины —
        отдельный расчёт и отдельный набор вариантов. Тщательный режим дополнительно
        переставляет точки в порядке подключения у лучшего варианта.
      </p>

      {jobs.length > 0 && (
        <ul className="job-list">
          {jobs
            .slice()
            .sort((a, b) => b.created_at - a.created_at)
            .map((job) => (
              <li key={job.id}>
                <button
                  type="button"
                  className={job.id === selectedJobId ? "job-row selected" : "job-row"}
                  onClick={() => onSelectJob(job.id)}
                >
                  <span className={`badge badge-${job.status}`}>{STATUS_TITLE[job.status] ?? job.status}</span>
                  <span>
                    {formatDate(job.created_at)} · {MODE_SHORT[job.mode] ?? job.mode} · вариантов {job.max_variants}
                    {job.effort === "thorough" && (
                      <>
                        {" "}
                        <span className="badge badge-thorough" title="перестановки точек у лучшего варианта">
                          тщательно
                        </span>
                      </>
                    )}
                  </span>
                  {job.elapsed_s !== null && <span className="muted">{formatNumber(job.elapsed_s, 0)} с</span>}
                </button>
              </li>
            ))}
        </ul>
      )}
    </section>
  );
}
