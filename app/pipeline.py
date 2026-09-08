"""Phase 11 — full-document pipeline orchestration.

``start(conn, document_id)`` synchronously opens a ``run_type='full'`` row and
returns its id (so the HTTP response can carry a real run id). ``run(document_id,
run_id, …)`` then executes the stages — extract → ground → normalize → resolve →
reason — updating the ``full`` run's ``stage`` and aggregate stats after each and
setting ``documents.status``. Each stage's own phase function still opens its own
sub-run; the ``full`` run is the umbrella ``GET /documents/{id}/status`` reports.

A stage that raises, or reports its own ``status='failed'``, stops the pipeline:
the ``full`` run is marked ``failed`` with the stage + error and
``documents.status='failed'``. Phase 11 adds no new lifecycle rules — it only
sequences Phases 4-9.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any

from app import db
from app.config import Settings, get_settings

log = logging.getLogger("fkl.pipeline")

_STAGES = ("extract", "ground", "normalize", "resolve", "reason")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def start(conn: sqlite3.Connection, document_id: int, *,
          settings: Settings | None = None) -> int:
    """Open the umbrella run row. Caller commits."""
    settings = settings or get_settings()
    run_id = conn.execute(
        "INSERT INTO runs (document_id, run_type, status, stage, started_at, model_name, "
        "settings) VALUES (?, 'full', 'running', 'queued', ?, ?, ?)",
        (document_id, _now(), settings.llm_model,
         json.dumps({"prompt_version": settings.prompt_version,
                     "relationship_prompt_version": settings.relationship_prompt_version})),
    ).lastrowid
    conn.execute("UPDATE documents SET status = 'processing' WHERE id = ?", (document_id,))
    return run_id


def running_full_run(conn: sqlite3.Connection, document_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM runs WHERE document_id = ? AND run_type = 'full' AND status = 'running' "
        "ORDER BY id DESC LIMIT 1", (document_id,),
    ).fetchone()


def _aggregate(conn: sqlite3.Connection, document_id: int, run_id: int, stage: str) -> None:
    row = conn.execute(
        "SELECT "
        "(SELECT COUNT(*) FROM pages WHERE document_id = :d) AS pages, "
        "(SELECT COUNT(*) FROM chunks WHERE document_id = :d) AS chunks, "
        "(SELECT COUNT(*) FROM facts WHERE document_id = :d) AS facts, "
        "(SELECT COUNT(*) FROM facts WHERE document_id = :d AND lifecycle_state IN "
        "  ('GROUNDED','NORMALIZED','ELIGIBLE_FOR_REASONING')) AS grounded, "
        "(SELECT COUNT(*) FROM facts WHERE document_id = :d AND lifecycle_state = 'QUARANTINED') "
        "  AS quarantined, "
        "(SELECT COUNT(*) FROM relationships WHERE fact_a_id IN "
        "  (SELECT id FROM facts WHERE document_id = :d) OR fact_b_id IN "
        "  (SELECT id FROM facts WHERE document_id = :d)) AS rels, "
        "(SELECT COALESCE(SUM(llm_calls), 0) FROM runs WHERE document_id = :d "
        "  AND run_type <> 'full') AS llm_calls, "
        "(SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM runs WHERE document_id = :d "
        "  AND run_type <> 'full') AS cost",
        {"d": document_id},
    ).fetchone()
    conn.execute(
        "UPDATE runs SET stage = ?, pages_processed = ?, chunks_processed = ?, "
        "facts_extracted = ?, facts_grounded = ?, facts_quarantined = ?, "
        "relationships_produced = ?, llm_calls = ?, estimated_cost_usd = ? WHERE id = ?",
        (stage, row["pages"], row["chunks"], row["facts"], row["grounded"],
         row["quarantined"], row["rels"], row["llm_calls"], row["cost"], run_id),
    )


def run(document_id: int, run_id: int, *, database_path: str | None = None,
        settings: Settings | None = None, extractor: Any = None,
        entity_confirmer: Any = None, relationship_confirmer: Any = None) -> dict:
    """Execute the stages for one document. Safe to call from a BackgroundTask."""
    from app.entities import resolve_document
    from app.extract import extract_document
    from app.llm import AnthropicExtractor
    from app.normalize import normalize_document
    from app.reason import reason_document
    from app.verify import verify_document

    settings = settings or get_settings()
    conn = db.connect(database_path)
    failed_stage: str | None = None
    error: str | None = None
    try:
        if extractor is None and settings.llm_api_key():
            try:
                extractor = AnthropicExtractor(settings)
            except Exception as e:  # noqa: BLE001 — surfaced as a stage failure below
                failed_stage, error = "extract", f"extractor init: {e!r}"[:400]

        if failed_stage is None:
            for stage in _STAGES:
                with db.transaction(conn):
                    conn.execute("UPDATE runs SET stage = ? WHERE id = ?", (stage, run_id))
                try:
                    if stage == "extract":
                        res = extract_document(document_id, client=extractor,
                                               database_path=database_path, settings=settings)
                        if res.status == "failed":
                            failed_stage, error = stage, res.error or "extraction failed"
                        elif res.extraction_errors and not res.candidates_persisted:
                            # every chunk errored (bad/missing client, API down) — nothing
                            # to ground. An honest 0-fact extraction (no errors) is fine.
                            failed_stage = stage
                            error = (f"extraction produced no facts "
                                     f"({res.extraction_errors} chunk error(s))")
                    elif stage == "ground":
                        verify_document(document_id, database_path=database_path, settings=settings)
                    elif stage == "normalize":
                        normalize_document(document_id, database_path=database_path,
                                           settings=settings)
                    elif stage == "resolve":
                        resolve_document(document_id, database_path=database_path,
                                         settings=settings, llm=entity_confirmer)
                    elif stage == "reason":
                        reason_document(document_id, database_path=database_path,
                                        settings=settings, llm=relationship_confirmer)
                except Exception as e:  # noqa: BLE001 — isolate the failing stage
                    failed_stage, error = stage, f"{type(e).__name__}: {e}"[:400]
                with db.transaction(conn):
                    _aggregate(conn, document_id, run_id, stage)
                if failed_stage is not None:
                    break

        with db.transaction(conn):
            if failed_stage is None:
                conn.execute(
                    "UPDATE runs SET status = 'done', stage = 'complete', finished_at = ? "
                    "WHERE id = ?", (_now(), run_id),
                )
                conn.execute(
                    "UPDATE documents SET status = 'done', processed_at = ? WHERE id = ?",
                    (_now(), document_id),
                )
            else:
                conn.execute(
                    "UPDATE runs SET status = 'failed', stage = ?, error = ?, finished_at = ? "
                    "WHERE id = ?", (failed_stage, error, _now(), run_id),
                )
                conn.execute(
                    "UPDATE documents SET status = 'failed', status_detail = ? WHERE id = ?",
                    (f"{failed_stage}: {error}"[:500], document_id),
                )
        log.info("pipeline document_id=%s run_id=%s result=%s", document_id, run_id,
                 "done" if failed_stage is None else f"failed@{failed_stage}")
        return {"run_id": run_id, "status": "done" if failed_stage is None else "failed",
                "failed_stage": failed_stage, "error": error}
    finally:
        conn.close()
