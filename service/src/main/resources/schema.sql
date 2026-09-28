-- Учёт проектов и заданий расчёта (ТЗ §3.2: PostgreSQL как основная БД).
-- Файлы (вход, выход, сводка) лежат на общем томе; здесь — метаданные.
-- Схема идемпотентна: выполняется при каждом старте.

CREATE TABLE IF NOT EXISTS projects (
    id          VARCHAR(32)  PRIMARY KEY,
    name        VARCHAR(255) NOT NULL,
    created_at  TIMESTAMP    NOT NULL,
    input_path  VARCHAR(1024) NOT NULL,
    input_size  BIGINT       NOT NULL DEFAULT 0,
    stats       TEXT,                 -- JSON: паспорт входа из расчётного сервиса
    issues      TEXT                  -- JSON: протокол разбора
);

CREATE TABLE IF NOT EXISTS jobs (
    id            VARCHAR(32)  PRIMARY KEY,
    project_id    VARCHAR(32)  NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    mode          VARCHAR(16)  NOT NULL,
    max_variants  INTEGER      NOT NULL,
    effort        VARCHAR(16)  NOT NULL DEFAULT 'standard',
    status        VARCHAR(16)  NOT NULL,   -- queued | running | done | failed
    created_at    TIMESTAMP    NOT NULL,
    started_at    TIMESTAMP,
    finished_at   TIMESTAMP,
    progress      VARCHAR(255),
    error         TEXT,
    output_path   VARCHAR(1024),
    summary       TEXT                    -- JSON: сводка для интерфейса
);

CREATE INDEX IF NOT EXISTS jobs_project_idx ON jobs(project_id, created_at);
