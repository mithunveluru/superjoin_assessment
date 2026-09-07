"""FastAPI application. Phase 1: startup schema init + a health check.
Pipeline routes (documents, facts, relationships, failures) arrive in Phase 11.
"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__, db
from app.config import get_settings
from app.models import DatabaseHealth, HealthResponse, LLMHealth


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Fact Knowledge Layer", version=__version__, lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()
    conn = db.connect()
    try:
        conn.execute("SELECT 1").fetchone()
        tables = len(db.table_names(conn))
        db_ok = True
    except sqlite3.Error:
        tables, db_ok = 0, False
    finally:
        conn.close()

    return HealthResponse(
        status="ok" if db_ok else "degraded",
        version=__version__,
        database=DatabaseHealth(path=str(settings.database_path), ok=db_ok, tables=tables),
        llm=LLMHealth(
            provider=settings.llm_provider,
            model=settings.llm_model,
            api_key_present=settings.llm_api_key() is not None,
        ),
    )
