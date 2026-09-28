import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApiError, api, formatLength, formatMoney, formatScore, isTransient } from "./api";
import { buildInputLayers, buildResultLayers, key, locateExistingEdge, locateObject, oksLabels } from "./layers";
import type { Highlight } from "./layers";
import { Legend } from "./Legend";
import { MapView } from "./MapView";
import { RESTRICTION_FILL } from "./mapStyle";
import {
  ChambersTable,
  ConnectionsPanel,
  DepthPanel,
  DownloadPanel,
  ExistingImpactPanel,
  KeyFigures,
  LimitChecksPanel,
  ProgressPanel,
  SegmentsTable,
  SpecialPanel,
  ValidationPanel,
  VariantList,
} from "./panels";
import { PassportPanel, ProjectsPanel, SolvePanel } from "./Projects";
import type {
  Effort,
  FeatureCollection,
  Id,
  Job,
  ProjectBrief,
  ProjectDetail,
  Rules,
  SolveMode,
  Summary,
  ValidationFinding,
  ValidationReport,
} from "./types";

const POLL_MS = 2000;

const message = (e: unknown) => String((e as Error)?.message ?? e);

export default function App() {
  const [projects, setProjects] = useState<ProjectBrief[]>([]);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [project, setProject] = useState<ProjectDetail | null>(null);
  const [input, setInput] = useState<FeatureCollection | null>(null);
  const [rules, setRules] = useState<Rules | null>(null);

  const [jobId, setJobId] = useState<string | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [output, setOutput] = useState<FeatureCollection | null>(null);
  const [variantId, setVariantId] = useState<string | null>(null);

  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mapError, setMapError] = useState<string | null>(null);
  /** Ошибка загрузки готового результата (сводка или GeoJSON) — показывается
   *  в правой колонке вместо пустоты. */
  const [resultError, setResultError] = useState<string | null>(null);
  /** Связь с бэкендом потеряна во время опроса задания — опрос продолжается. */
  const [offline, setOffline] = useState<string | null>(null);

  const [showOthers, setShowOthers] = useState(false);
  const [showBasemap, setShowBasemap] = useState(true);
  const [showLabels, setShowLabels] = useState(false);
  const [fitToken, setFitToken] = useState(0);

  /** Протокол независимого валидатора по выходу задания. */
  const [validation, setValidation] = useState<ValidationReport | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [validating, setValidating] = useState(false);
  const [validationRetry, setValidationRetry] = useState(0);
  /** Объект находки, подсвеченный на карте, и сама находка. */
  const [highlight, setHighlight] = useState<Highlight | null>(null);
  const [activeFinding, setActiveFinding] = useState<ValidationFinding | null>(null);

  // --- Справочники и список проектов ---
  const refreshProjects = useCallback(async () => {
    const list = await api.projects();
    setProjects(list);
    return list;
  }, []);

  useEffect(() => {
    // Бэкенд может стартовать позже фронтенда (docker-compose, перезапуск):
    // пока он недоступен, пробуем снова, а не показываем мёртвый экран
    let cancelled = false;
    let timer: number | undefined;
    const load = () => {
      api.rules().then((r) => !cancelled && setRules(r)).catch(() => undefined);
      refreshProjects()
        .then((list) => {
          if (cancelled) return;
          setError(null);
          setProjectId((current) => current ?? list[0]?.id ?? null);
        })
        .catch((e) => {
          if (cancelled) return;
          setError(isTransient(e) ? `${message(e)} — повторю через 5 с` : message(e));
          if (isTransient(e)) timer = window.setTimeout(load, 5000);
        });
    };
    load();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [refreshProjects]);

  // --- Проект: паспорт, вход, задания ---
  useEffect(() => {
    if (!projectId) {
      setProject(null);
      setInput(null);
      setJobId(null);
      return;
    }
    let cancelled = false;
    setError(null);
    setJobId(null);
    setJob(null);
    setSummary(null);
    setOutput(null);
    setVariantId(null);
    Promise.all([api.project(projectId), api.inputGeoJSON(projectId)])
      .then(([detail, geojson]) => {
        if (cancelled) return;
        setProject(detail);
        setInput(geojson);
        // Последнее готовое задание показываем сразу — после перезагрузки страницы
        // результат не должен пропадать
        const byTime = detail.jobs.slice().sort((a, b) => b.created_at - a.created_at);
        const done = byTime.find((j) => j.status === "done");
        const active = byTime.find((j) => j.status === "queued" || j.status === "running");
        // Если готовых нет — показать последнее упавшее, чтобы причина была на экране
        const failed = byTime.find((j) => j.status === "failed");
        setJobId(active?.id ?? done?.id ?? failed?.id ?? null);
      })
      .catch((e) => {
        if (!cancelled) setError(message(e));
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  // --- Задание: опрос и загрузка результата ---
  const loadedJob = useRef<string | null>(null);
  /** Повторная загрузка результата после ошибки (кнопка в правой колонке). */
  const [retryToken, setRetryToken] = useState(0);
  useEffect(() => {
    if (!jobId) {
      setJob(null);
      setSummary(null);
      setOutput(null);
      setVariantId(null);
      setResultError(null);
      setOffline(null);
      loadedJob.current = null;
      return;
    }
    let cancelled = false;
    let timer: number | undefined;

    const finish = async (current: Job) => {
      if (loadedJob.current === current.id) return;
      loadedJob.current = current.id;
      try {
        const [sum, geojson] = await Promise.all([
          current.summary ? Promise.resolve(current.summary) : api.summary(current.id),
          api.outputGeoJSON(current.id),
        ]);
        if (cancelled) return;
        if (!Array.isArray(sum?.variants)) throw new Error("сводка задания без вариантов");
        setSummary(sum);
        setOutput(geojson);
        setResultError(null);
        const best = sum.variants.slice().sort((a, b) => a.rank - b.rank)[0];
        setVariantId(best?.id ?? null);
      } catch (e) {
        if (cancelled) return;
        loadedJob.current = null; // разрешить повторную попытку
        setResultError(message(e));
      }
      // Обновить список заданий проекта и статистику в списке проектов
      api.project(current.project_id).then((detail) => !cancelled && setProject(detail)).catch(() => undefined);
      refreshProjects().catch(() => undefined);
    };

    const tick = async () => {
      try {
        const current = await api.job(jobId);
        if (cancelled) return;
        setOffline(null);
        setJob(current);
        // Строка задания в списке проекта показывает тот же статус и время
        setProject((prev) =>
          prev && prev.jobs.some((j) => j.id === current.id)
            ? { ...prev, jobs: prev.jobs.map((j) => (j.id === current.id ? current : j)) }
            : prev,
        );
        if (current.status === "done") await finish(current);
        else if (current.status === "failed") {
          setSummary(null);
          setOutput(null);
          setVariantId(null);
          api.project(current.project_id).then((detail) => !cancelled && setProject(detail)).catch(() => undefined);
        } else timer = window.setTimeout(tick, POLL_MS);
      } catch (e) {
        if (cancelled) return;
        // Перезапуск бэкенда во время расчёта: опрос не прекращается — после
        // старта сервис пометит задание «failed: сервис перезапущен», и это
        // должно дойти до экрана. Прекращаем только если задания больше нет.
        if (isTransient(e)) {
          setOffline(message(e));
          timer = window.setTimeout(tick, POLL_MS * 2);
        } else {
          const gone = e instanceof ApiError && e.status === 404;
          const text = gone
            ? "задание не найдено: проект удалён или данные сервиса потеряны при перезапуске"
            : message(e);
          setError(text);
          setJob((prev) => (prev ? { ...prev, status: "failed", error: text } : prev));
        }
      }
    };
    setSummary(null);
    setOutput(null);
    setVariantId(null);
    setResultError(null);
    loadedJob.current = null;
    void tick();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [jobId, refreshProjects, retryToken]);

  // --- Протокол валидатора: запрашивается сам после загрузки сводки, интерфейс
  // не блокирует (первый запрос считает отчёт на бэкенде ~1–2 с, дальше кэш) ---
  // Ключ — id готового задания, а не jobId: в кадре смены задания сводка ещё от
  // прежнего, и запрос по новому id ушёл бы в 404 «результат ещё не готов».
  const doneJobId = job?.status === "done" && job.id === jobId ? jobId : null;
  useEffect(() => {
    setValidation(null);
    setValidationError(null);
    setHighlight(null);
    setActiveFinding(null);
    if (!doneJobId || !summary) {
      setValidating(false);
      return;
    }
    let cancelled = false;
    setValidating(true);
    api
      .validation(doneJobId)
      .then((report) => {
        if (cancelled) return;
        if (!Array.isArray(report?.findings)) throw new Error("протокол без списка находок");
        setValidation(report);
      })
      .catch((e) => !cancelled && setValidationError(message(e)))
      .finally(() => !cancelled && setValidating(false));
    return () => {
      cancelled = true;
    };
  }, [doneJobId, summary, validationRetry]);

  // --- Действия ---
  async function upload(file: File, name: string) {
    setUploading(true);
    setError(null);
    try {
      const created = await api.createProject(file, name);
      await refreshProjects();
      setProjectId(created.id);
    } finally {
      setUploading(false);
    }
  }

  async function removeProject(id: string) {
    setError(null);
    try {
      await api.deleteProject(id);
      const list = await refreshProjects();
      if (id === projectId) setProjectId(list[0]?.id ?? null);
    } catch (e) {
      setError(message(e));
    }
  }

  async function solve(mode: SolveMode, maxVariants: number, effort: Effort) {
    if (!projectId) return;
    setError(null);
    try {
      const created = await api.solve(projectId, { mode, max_variants: maxVariants, effort });
      setProject((current) => (current ? { ...current, jobs: [...current.jobs, created] } : current));
      setJobId(created.id);
    } catch (e) {
      setError(message(e));
    }
  }

  // --- Производные данные для карты ---
  const knownTypes = useMemo(
    () => new Set(rules ? Object.keys(rules.restrictions) : Object.keys(RESTRICTION_FILL)),
    [rules],
  );
  const inputLayers = useMemo(() => (input ? buildInputLayers(input, knownTypes) : null), [input, knownTypes]);
  const labels = useMemo(() => oksLabels(inputLayers), [inputLayers]);
  const resultLayers = useMemo(
    () => (output ? buildResultLayers(output, summary, inputLayers) : null),
    [output, summary, inputLayers],
  );
  const variant = useMemo(
    () => summary?.variants.find((v) => v.id === variantId) ?? null,
    [summary, variantId],
  );
  const unconnected = useMemo(
    () => new Set((variant?.unconnected_oks_ids ?? []).map((id) => key(id))),
    [variant],
  );

  /** Смена варианта вручную снимает подсветку находки: ореол чужого варианта
   *  на карте ввёл бы в заблуждение. */
  function selectVariant(id: string) {
    setVariantId(id);
    setHighlight(null);
    setActiveFinding(null);
  }

  /** Клик по находке: выбрать её вариант и подсветить объект на карте. Объекта
   *  с геометрией может не быть (сводка варианта) — тогда только вариант. */
  function pickFinding(finding: ValidationFinding) {
    const known = finding.variant_id && summary?.variants.some((v) => v.id === finding.variant_id);
    const target = known ? finding.variant_id : variantId;
    if (target && target !== variantId) setVariantId(target);
    const found = locateObject(finding.object_id, target, resultLayers, inputLayers);
    setHighlight(found);
    setActiveFinding(found ? finding : null);
    if (!found) console.warn("Объект находки не найден на карте:", finding.object_id, finding.code);
  }

  const highlightedEdgeId =
    highlight?.layer === "network-line" ? key(highlight.feature.properties?.id as Id) : null;

  /** Клик по существующему участку в панели добавочного расхода: подсветить его
   *  на карте; повторный клик по тому же участку снимает подсветку. */
  function pickExistingEdge(edgeId: Id) {
    const found = locateExistingEdge(edgeId, inputLayers);
    setActiveFinding(null);
    setHighlight(found && highlightedEdgeId === key(edgeId) ? null : found);
    if (!found) console.warn("Существующий участок не найден во входе:", edgeId);
  }

  const running = job?.status === "queued" || job?.status === "running";
  const legendTypes = inputLayers?.restrictionTypes ?? [];
  // Расчёт возможен, когда разбор входа не дал ошибок (источник — только
  // предупреждение: бэкенд пишет «на расчёт не влияет») и есть куда подключать
  const blockingIssues = project?.issues.filter((i) => i.level === "error") ?? [];
  const solveBlocked = !project
    ? "проект не выбран"
    : blockingIssues.length
      ? `во входе ошибки: ${blockingIssues.map((i) => i.detail).join("; ")}`
      : project.stats.oks_points === 0
        ? "во входе нет точек подключения"
        : project.stats.network_edges === 0
          ? "во входе нет существующей сети"
          : null;

  return (
    <div className="app">
      <aside className="sidebar sidebar-left">
        <header className="brand">
          <h1>Трассировка подключения к тепловым сетям — ЛЦТ 2026</h1>
          <p>Сервис моделирования трасс подключения перспективных ОКС · район ЗИЛ</p>
        </header>

        <ProjectsPanel
          projects={projects}
          selectedId={projectId}
          busy={uploading}
          onSelect={setProjectId}
          onDelete={removeProject}
          onUpload={upload}
        />

        {error && <p className="error">{error}</p>}

        {project && <PassportPanel stats={project.stats} issues={project.issues} />}

        {project && (
          <SolvePanel
            jobs={project.jobs}
            selectedJobId={jobId}
            disabledReason={solveBlocked}
            onSolve={solve}
            onSelectJob={setJobId}
          />
        )}
      </aside>

      <main className="canvas">
        <div className="map-controls">
          <button
            className={showBasemap ? "chip chip-on" : "chip"}
            onClick={() => setShowBasemap(!showBasemap)}
            title="Растровая подложка OpenStreetMap"
          >
            подложка
          </button>
          <button
            className={showLabels ? "chip chip-on" : "chip"}
            onClick={() => setShowLabels(!showLabels)}
            disabled={!variant}
            title="Подписи Ду и расхода на новых участках"
          >
            подписи Ду
          </button>
          <button
            className={showOthers ? "chip chip-on" : "chip"}
            onClick={() => setShowOthers(!showOthers)}
            disabled={!summary || summary.variants.length < 2}
            title="Показать остальные варианты полупрозрачным пунктиром"
          >
            сравнить варианты
          </button>
          <button className="chip" onClick={() => setFitToken((t) => t + 1)} disabled={!inputLayers?.bbox}>
            показать всё
          </button>
          {highlight && (
            <button
              className="chip chip-highlight"
              onClick={() => {
                setHighlight(null);
                setActiveFinding(null);
              }}
              title="убрать ореол подсвеченного объекта"
            >
              снять подсветку
            </button>
          )}
          {variant && (
            <span className="chip chip-info">
              Вариант {variant.id.replace(/^v/, "")} · S {formatScore(variant.score)} ·{" "}
              {formatMoney(variant.calculated_cost)} · {formatLength(variant.new_network_length)}
            </span>
          )}
        </div>

        <MapView
          input={inputLayers}
          labels={labels}
          result={resultLayers}
          variantId={variantId}
          unconnected={unconnected}
          showOthers={showOthers}
          showBasemap={showBasemap}
          showLabels={showLabels}
          fitToken={fitToken}
          tieInCost={rules?.chambers.tie_in_cost ?? 5_000_000}
          highlight={highlight}
          onError={setMapError}
        />

        <Legend
          restrictionTypes={legendTypes}
          unknownTypes={inputLayers?.unknownTypes ?? []}
          hasResult={Boolean(variant)}
        />

        {running && (
          <div className="map-busy">
            <span className="spinner" /> Строю варианты подключения…
            {job?.elapsed_s !== null && job?.elapsed_s !== undefined && ` ${Math.round(job.elapsed_s)} с`}
            {offline && <span className="map-busy-offline"> · нет связи с сервисом, жду…</span>}
          </div>
        )}

        {mapError && <div className="map-error">{mapError}</div>}
      </main>

      <aside className="sidebar sidebar-right">
        {!job && (
          <section className="panel placeholder">
            <h3>Результат</h3>
            <p className="note" style={{ marginTop: 0 }}>
              {project
                ? "Запустите расчёт — здесь появятся варианты подключения, участки с расходами и Ду, камеры, проверка предельной длины и стоимость."
                : "Загрузите конкурсный набор GeoJSON, чтобы начать."}
            </p>
          </section>
        )}

        {job && job.status !== "done" && <ProgressPanel job={job} offline={offline} />}

        {job?.status === "done" && !summary && (
          <section className="panel progress">
            <h3>Результат</h3>
            {resultError ? (
              <>
                <p className="error">Результат не загрузился: {resultError}</p>
                <button className="primary" onClick={() => setRetryToken((t) => t + 1)}>
                  Повторить загрузку
                </button>
              </>
            ) : (
              <div className="progress-row">
                <span className="spinner spinner-dark" />
                <span>Загружаю сводку и GeoJSON результата…</span>
              </div>
            )}
          </section>
        )}

        {job?.status === "done" && summary && (
          <VariantList
            variants={summary.variants}
            selectedId={variantId}
            elapsed={summary.elapsed_s}
            mode={summary.mode ?? job.mode}
            effort={summary.effort ?? job.effort}
            improvement={summary.improvement}
            rules={rules}
            onSelect={selectVariant}
          />
        )}

        {job?.status === "done" && summary && (
          <ValidationPanel
            report={validation}
            loading={validating}
            error={validationError}
            selectedVariantId={variantId}
            activeFinding={activeFinding}
            onRetry={() => setValidationRetry((t) => t + 1)}
            onPick={pickFinding}
          />
        )}

        {variant && job && (
          <>
            <KeyFigures variant={variant} rules={rules} />
            <SegmentsTable variant={variant} output={output} />
            <ChambersTable variant={variant} output={output} rules={rules} />
            <LimitChecksPanel variant={variant} />
            <SpecialPanel variant={variant} />
            {(summary?.mode ?? job.mode) === "depth" && <DepthPanel variant={variant} />}
            <ConnectionsPanel variant={variant} />
            <ExistingImpactPanel variant={variant} activeEdgeId={highlightedEdgeId} onPick={pickExistingEdge} />
            <DownloadPanel jobId={job.id} mode={summary?.mode ?? job.mode} />
          </>
        )}
      </aside>
    </div>
  );
}
