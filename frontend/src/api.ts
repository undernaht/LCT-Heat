/** Клиент API конкурсной модели. Ходим по относительным путям — Vite
 *  проксирует /api на бэкенд (8010), CORS не нужен. */

import type {
  Effort,
  FeatureCollection,
  Job,
  ProjectBrief,
  ProjectDetail,
  Rules,
  SolveMode,
  Summary,
  UploadResult,
  ValidationReport,
} from "./types";

const BASE = "/api/v1";

/** Ошибка API со статусом: 404 — объекта нет, 0 — сеть/бэкенд недоступны
 *  (перезапуск сервиса), остальное — ответ сервера. */
export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** Временная ошибка — стоит повторить запрос: сеть, прокси без бэкенда, 5xx. */
export const isTransient = (e: unknown): boolean =>
  e instanceof ApiError ? e.status === 0 || e.status >= 500 : e instanceof TypeError;

async function fail(response: Response): Promise<never> {
  let detail = `${response.status} ${response.statusText}`;
  try {
    const body = await response.json();
    if (typeof body?.detail === "string") detail = body.detail;
    else if (body?.detail?.message) detail = String(body.detail.message);
    else if (Array.isArray(body?.detail)) detail = body.detail.map((d: { msg?: string }) => d.msg ?? "").join("; ");
  } catch {
    /* тело не JSON — оставляем статус */
  }
  if (response.status >= 500 || response.status === 0) {
    detail = `сервис расчёта недоступен (${detail})`;
  }
  throw new ApiError(detail, response.status);
}

async function send(path: string, init?: RequestInit): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, init);
  } catch (e) {
    // fetch падает только при сетевой ошибке: бэкенд перезапускается или выключен
    throw new ApiError(`нет связи с сервисом расчёта: ${String((e as Error)?.message ?? e)}`, 0);
  }
  if (!response.ok) await fail(response);
  return response;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await send(path, init);
  return response.json() as Promise<T>;
}

export const api = {
  projects: () => request<ProjectBrief[]>("/projects"),

  project: (id: string) => request<ProjectDetail>(`/projects/${encodeURIComponent(id)}`),

  /** Один совмещённый GeoJSON. Уходит потоком: бэкенд не держит файл в памяти. */
  createProject: async (file: File, name: string): Promise<UploadResult> => {
    const form = new FormData();
    form.append("file", file, file.name);
    form.append("name", name);
    const response = await send("/projects", { method: "POST", body: form });
    return response.json() as Promise<UploadResult>;
  },

  deleteProject: async (id: string): Promise<void> => {
    await send(`/projects/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  inputGeoJSON: (id: string) =>
    request<FeatureCollection>(`/projects/${encodeURIComponent(id)}/input.geojson`),

  solve: (id: string, body: { mode: SolveMode; max_variants: number; effort: Effort }) =>
    request<Job>(`/projects/${encodeURIComponent(id)}/solve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  job: (jobId: string) => request<Job>(`/jobs/${encodeURIComponent(jobId)}`),

  summary: (jobId: string) => request<Summary>(`/jobs/${encodeURIComponent(jobId)}/summary`),

  /** Протокол независимого валидатора. Первый запрос считает отчёт на бэкенде
   *  (~1–2 с), дальше отдаётся кэш рядом с результатом. */
  validation: (jobId: string) =>
    request<ValidationReport>(`/jobs/${encodeURIComponent(jobId)}/validation`),

  outputGeoJSON: (jobId: string) =>
    request<FeatureCollection>(`/jobs/${encodeURIComponent(jobId)}/output.geojson`),

  /** Ссылка на скачивание результата — тот же файл, что проверяет приложение. */
  outputUrl: (jobId: string) => `${BASE}/jobs/${encodeURIComponent(jobId)}/output.geojson`,

  rules: () => request<Rules>("/rules"),
};

// --- Форматирование -------------------------------------------------------------

const ru = "ru-RU";

export function formatMoney(rub: number): string {
  if (Math.abs(rub) >= 1e6) return `${(rub / 1e6).toLocaleString(ru, { maximumFractionDigits: 2 })} млн ₽`;
  if (Math.abs(rub) >= 1e3) return `${Math.round(rub / 1e3).toLocaleString(ru)} тыс ₽`;
  return `${Math.round(rub).toLocaleString(ru)} ₽`;
}

/** Полная сумма в рублях — для подсказок и таблиц, где округление недопустимо. */
export function formatRub(rub: number): string {
  return `${rub.toLocaleString(ru, { maximumFractionDigits: 2 })} ₽`;
}

export function formatLength(m: number): string {
  return m >= 1000
    ? `${(m / 1000).toLocaleString(ru, { maximumFractionDigits: 2 })} км`
    : `${m.toLocaleString(ru, { maximumFractionDigits: 1 })} м`;
}

export function formatNumber(value: number, digits = 2): string {
  return value.toLocaleString(ru, { maximumFractionDigits: digits });
}

export function formatScore(value: number): string {
  return value.toLocaleString(ru, { minimumFractionDigits: 4, maximumFractionDigits: 4 });
}

export function formatDate(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleString(ru, {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}
