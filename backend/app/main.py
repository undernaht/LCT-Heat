"""Точка входа FastAPI.

    backend/.venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8010

Документация — http://localhost:8010/docs
Порт 8010, а не 8000: на машине разработки 8000 часто уже занят.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.routers import router as legacy_router
from .case.api import router as case_router
from .config import get_config


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Конфиги проверяются на старте: лучше упасть здесь, чем в середине расчёта."""
    config = get_config()
    if not config.normatives_verified:
        # Без эмодзи: на русской консоли Windows (cp1251) print с ними падает
        # с UnicodeEncodeError и роняет запуск сервера целиком.
        logging.getLogger("heat-network").warning(
            "normatives.yaml: meta.verified = false — "
            "значения СП не сверены с действующей редакцией"
        )
    yield


app = FastAPI(
    lifespan=lifespan,
    title="Трассировка тепловых сетей",
    description=(
        "Сервис моделирования трассировки тепловых сетей для технологического "
        "присоединения новых зданий. Кейс ЛЦТ 2026."
    ),
    version="0.2.0",
)

# Фронтенд поднимается отдельным процессом на 5173 — иначе браузер не пустит.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Конкурсная модель (Техническое приложение ЛЦТ 2026) — основной API.
app.include_router(case_router, prefix="/api/v1")
# Прежняя модель по СП 124.13330 — оставлена как расширение, не основной путь.
app.include_router(legacy_router, prefix="/api/legacy/v1")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
