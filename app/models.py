"""Pydantic models used across phases (see docs/API_DESIGN.md). Grows one
section per phase; no logic here."""

from __future__ import annotations

from pydantic import BaseModel

# --- Phase 1: health ---------------------------------------------------------


class DatabaseHealth(BaseModel):
    path: str
    ok: bool
    tables: int


class LLMHealth(BaseModel):
    provider: str
    model: str
    api_key_present: bool


class HealthResponse(BaseModel):
    status: str
    version: str
    database: DatabaseHealth
    llm: LLMHealth


# --- Phase 2: ingestion ----------------------------------------------------


class IngestResult(BaseModel):
    """Outcome of ``app.ingest.ingest_pdf``. Not an API response yet — a
    service-level return value the Phase 11 API will wrap."""

    document_id: int
    sha256: str
    status: str  # documents.status: 'ingested' | 'failed' | ...
    duplicate: bool
    page_count: int
    pages_text_extracted: int
    pages_low_text: int
    pages_empty: int
    pages_extraction_error: int
    chunk_count: int
