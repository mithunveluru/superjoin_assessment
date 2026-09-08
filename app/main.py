"""FastAPI application.

Phase 1 shipped startup schema init + ``/health``. Phase 11 adds the read/write
API over the Phase 2-10 storage: documents (upload / list / detail / process /
status), facts, relationships, entities, and the failure surface. No auth — local
prototype (noted in the README).
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__, db, pipeline, queries
from app.config import get_settings
from app.ingest import IngestError, ingest_pdf
from app.models import (
    DatabaseHealth,
    DocumentOut,
    DocumentsPage,
    EntitiesPage,
    EntityOut,
    FactOut,
    FactsPage,
    FailuresPage,
    HealthResponse,
    LLMHealth,
    ProcessOut,
    RelationshipOut,
    RelationshipsPage,
    StatusOut,
)

STATIC_DIR = Path(__file__).with_name("static")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Fact Knowledge Layer", version=__version__, lifespan=lifespan)


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message


@app.exception_handler(APIError)
async def _api_error_handler(_: Request, exc: APIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}}
    )


def get_conn() -> Iterator[sqlite3.Connection]:
    # check_same_thread=False: Starlette may run this generator's teardown on a
    # different threadpool thread than its setup. One request, used sequentially.
    conn = db.connect(check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def _found(value, code: str, message: str):
    if value is None:
        raise APIError(404, code, message)
    return value


# --------------------------------------------------------------------------- #
# health                                                                     #
# --------------------------------------------------------------------------- #
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
        llm=LLMHealth(provider=settings.llm_provider, model=settings.llm_model,
                      api_key_present=settings.llm_api_key() is not None),
    )


# --------------------------------------------------------------------------- #
# documents                                                                  #
# --------------------------------------------------------------------------- #
_INGEST_STATUS = {
    "not_a_pdf": (400, "invalid_pdf"), "corrupt_pdf": (400, "invalid_pdf"),
    "empty_file": (400, "invalid_pdf"), "empty_pdf": (400, "invalid_pdf"),
    "file_unreadable": (400, "invalid_pdf"), "file_not_found": (400, "invalid_pdf"),
    "encrypted_pdf": (400, "encrypted_pdf"), "file_too_large": (413, "file_too_large"),
    "too_many_pages": (422, "too_many_pages"), "too_many_chunks": (422, "too_many_chunks"),
}


@app.post("/documents", response_model=DocumentOut, status_code=201)
def upload_document(file: UploadFile, conn: Conn):
    # sync def on purpose: the sqlite connection from get_conn is bound to this
    # request's worker thread, so route + dependency must share it.
    name = file.filename or "upload.pdf"
    if not name.lower().endswith(".pdf"):
        raise APIError(400, "invalid_pdf", "expected a .pdf file")
    tmp_dir = Path(tempfile.mkdtemp(prefix="fkl-upload-"))
    tmp = tmp_dir / Path(name).name
    try:
        tmp.write_bytes(file.file.read())
        try:
            res = ingest_pdf(str(tmp), original_filename=name)
        except IngestError as e:
            status, code = _INGEST_STATUS.get(e.code, (400, "invalid_pdf"))
            raise APIError(status, code, e.detail or e.code) from e
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    out = queries.document_out(conn, res.document_id, duplicate=res.duplicate)
    return JSONResponse(content=out, status_code=200 if res.duplicate else 201)


@app.get("/documents", response_model=DocumentsPage)
def list_documents(conn: Conn, status: str | None = None,
                   limit: int | None = None, offset: int | None = None):
    return queries.list_documents(conn, status=status, limit=limit, offset=offset)


@app.get("/documents/{document_id}", response_model=DocumentOut)
def get_document(document_id: int, conn: Conn):
    return _found(queries.document_out(conn, document_id, detail=True),
                  "document_not_found", f"no document {document_id}")


@app.post("/documents/{document_id}/process", response_model=ProcessOut, status_code=202)
def process_document(document_id: int, background_tasks: BackgroundTasks,
                     conn: Conn):
    if conn.execute("SELECT 1 FROM documents WHERE id = ?", (document_id,)).fetchone() is None:
        raise APIError(404, "document_not_found", f"no document {document_id}")
    existing = pipeline.running_full_run(conn, document_id)
    if existing:
        return ProcessOut(run_id=existing["id"], document_id=document_id,
                          status=existing["status"], stage=existing["stage"],
                          started_at=existing["started_at"])
    with db.transaction(conn):
        run_id = pipeline.start(conn, document_id)
    row = conn.execute("SELECT stage, started_at FROM runs WHERE id = ?", (run_id,)).fetchone()
    background_tasks.add_task(
        pipeline.run, document_id, run_id, database_path=str(get_settings().database_path)
    )
    return ProcessOut(run_id=run_id, document_id=document_id, status="running",
                      stage=row["stage"], started_at=row["started_at"])


@app.get("/documents/{document_id}/status", response_model=StatusOut)
def document_status(document_id: int, conn: Conn):
    return _found(queries.status_out(conn, document_id),
                  "document_not_found", f"no document {document_id}")


# --------------------------------------------------------------------------- #
# facts                                                                      #
# --------------------------------------------------------------------------- #
@app.get("/facts", response_model=FactsPage)
def list_facts(
    conn: Conn,
    document_id: int | None = None, entity_id: int | None = None, entity: str | None = None,
    predicate: str | None = None, type: str | None = None, lifecycle_state: str | None = None,
    evidence_status: str | None = None, modality: str | None = None,
    reasoning_eligible: bool | None = None, q: str | None = None,
    sort: str = Query("recent"), limit: int | None = None, offset: int | None = None,
):
    filters = {
        "document_id": document_id, "entity_id": entity_id, "entity": entity,
        "predicate": predicate, "type": type, "lifecycle_state": lifecycle_state,
        "evidence_status": evidence_status, "modality": modality,
        "reasoning_eligible": reasoning_eligible, "q": q,
    }
    return queries.list_facts(conn, filters=filters, limit=limit, offset=offset, sort=sort)


@app.get("/facts/{fact_id}", response_model=FactOut)
def get_fact(fact_id: int, conn: Conn):
    return _found(queries.fact_out(conn, fact_id, detail=True),
                  "fact_not_found", f"no fact {fact_id}")


# --------------------------------------------------------------------------- #
# relationships                                                              #
# --------------------------------------------------------------------------- #
@app.get("/relationships", response_model=RelationshipsPage)
def list_relationships(
    conn: Conn,
    category: str | None = None, context_dimension: str | None = None,
    validation_action: str | None = None, document_id: int | None = None,
    entity_id: int | None = None, min_confidence: float | None = None,
    llm_used: bool | None = None, sort: str = Query("confidence"),
    limit: int | None = None, offset: int | None = None,
):
    filters = {
        "category": category, "context_dimension": context_dimension,
        "validation_action": validation_action, "document_id": document_id,
        "entity_id": entity_id, "min_confidence": min_confidence, "llm_used": llm_used,
    }
    return queries.list_relationships(conn, filters=filters, limit=limit, offset=offset, sort=sort)


@app.get("/relationships/{relationship_id}", response_model=RelationshipOut)
def get_relationship(relationship_id: int, conn: Conn):
    return _found(queries.relationship_out(conn, relationship_id, detail=True),
                  "relationship_not_found", f"no relationship {relationship_id}")


# --------------------------------------------------------------------------- #
# entities                                                                   #
# --------------------------------------------------------------------------- #
@app.get("/entities", response_model=EntitiesPage)
def list_entities(conn: Conn, type: str | None = None,
                  q: str | None = None, limit: int | None = None, offset: int | None = None):
    return queries.list_entities(conn, entity_type=type, q=q, limit=limit, offset=offset)


@app.get("/entities/{entity_id}", response_model=EntityOut)
def get_entity(entity_id: int, conn: Conn):
    return _found(queries.entity_out(conn, entity_id, detail=True),
                  "entity_not_found", f"no entity {entity_id}")


# --------------------------------------------------------------------------- #
# failures                                                                   #
# --------------------------------------------------------------------------- #
@app.get("/failures", response_model=FailuresPage)
def list_failures(conn: Conn, document_id: int | None = None,
                  failure_type: str | None = None, limit: int | None = None,
                  offset: int | None = None):
    return queries.list_failures(conn, document_id=document_id, failure_type=failure_type,
                                 limit=limit, offset=offset)


# --------------------------------------------------------------------------- #
# static UI (Phase 12 — framework-free single page over the API)             #
# --------------------------------------------------------------------------- #
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


@app.get("/", include_in_schema=False)
def root():
    index = STATIC_DIR / "index.html"
    if index.is_file():
        return FileResponse(index)
    return JSONResponse({"service": "fact-knowledge-layer", "version": __version__,
                         "docs": "/docs"})
