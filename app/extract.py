"""Phase 4 — candidate fact extraction.

INPUT:  documents -> pages -> chunks  (Phase 2)
OUTPUT: candidate facts             (Phase 3 schema, lifecycle_state='CANDIDATE')

What this does NOT do (later phases): verify the quote against the source text,
normalize numbers/units/dates, resolve entities, infer relationships, decide
truth. Extracted facts are NEVER reasoning-eligible.

Design: the LLM proposes structure; deterministic code here validates the
structural contract and persists what passes. A malformed candidate is recorded
(payload preserved) and skipped; one bad chunk does not abort the document.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from datetime import UTC, datetime
from typing import Any

from app import db
from app.config import Settings, get_settings
from app.facts import FACT_TYPES, attach_evidence, insert_fact
from app.models import EvidenceIn, ExtractionRunResult, FactIn, LLMExtraction, RawCandidate

log = logging.getLogger("fkl.extract")

_PERIOD_TYPES = {
    "instant", "quarter", "half_year", "fiscal_year", "calendar_year", "range", "unknown",
}
# LLM modality (lowercase, may say 'projected') -> facts.modality (uppercase CHECK set)
_MODALITY_MAP = {
    None: "ASSERTED", "": "ASSERTED", "asserted": "ASSERTED", "historical": "HISTORICAL",
    "estimated": "ESTIMATED", "forecast": "FORECAST", "projected": "FORECAST",
    "target": "TARGET", "uncertain": "UNCERTAIN",
}


class ExtractError(RuntimeError):
    def __init__(self, code: str, *, detail: Any = None, run_id: int | None = None):
        self.code = code
        self.detail = detail
        self.run_id = run_id
        super().__init__(code if detail is None else f"{code}: {detail}")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _doc_header(doc: sqlite3.Row) -> str:
    """Generic, this-document-only metadata to orient the model. Never names a
    corpus, entity, or expected answer."""
    parts = [f"document_id={doc['id']}"]
    for col in ("disclosure_type", "publisher", "document_date", "publication_date",
                "fy_convention"):
        val = doc[col]  # SELECT * -> every documents column is present
        if val and val != "unknown":
            parts.append(f"{col}={val}")
    return "Document metadata: " + ", ".join(parts)


# --------------------------------------------------------------------------- #
# deterministic validation of one candidate                                  #
# --------------------------------------------------------------------------- #
def _validate_candidate(cand: RawCandidate, chunk_len: int) -> str | None:
    """Return None if the candidate satisfies the structural contract, else a
    short rejection-reason code."""
    if not cand.subject.strip():
        return "missing_subject"
    if not cand.predicate.strip():
        return "missing_predicate"
    if not cand.object.strip():
        return "missing_object"
    if not cand.quote.strip():
        return "missing_quote"
    if cand.fact_type not in FACT_TYPES:
        return "invalid_fact_type"
    if cand.modality is not None and cand.modality.lower() not in _MODALITY_MAP:
        return "invalid_modality"
    if cand.period_type is not None and cand.period_type.lower() not in _PERIOD_TYPES:
        return "invalid_period_type"

    s, e = cand.char_start, cand.char_end
    if not isinstance(s, int) or not isinstance(e, int):
        return "invalid_offsets"
    if not (0 <= s < e <= chunk_len):
        return "invalid_offsets"

    if cand.fact_type == "numeric":
        if cand.raw_value_text is None and cand.parsed_value is None:
            return "numeric_missing_value"
        for v in (cand.parsed_value, cand.ratio):
            if v is not None and (math.isnan(v) or math.isinf(v)):
                return "numeric_unparseable"
    return None


def _candidate_identity(cand: RawCandidate) -> tuple:
    return (
        cand.subject.strip().lower(), cand.predicate.strip().lower(),
        cand.object.strip().lower(), cand.char_start, cand.char_end,
    )


def _candidate_to_fact_in(
    cand: RawCandidate, *, document_id: int, page_index: int, run_id: int,
    raw_extraction_id: int, model: str, prompt_version: str,
) -> FactIn:
    numeric = cand.fact_type == "numeric"
    return FactIn(
        document_id=document_id,
        page_index=page_index,
        subject_raw=cand.subject,
        predicate=cand.predicate,
        predicate_norm=cand.predicate.strip().lower(),
        object_raw=cand.object,
        fact_type=cand.fact_type,
        value_text=None if numeric else cand.object,
        value_raw=(cand.raw_value_text or cand.object) if numeric else None,
        numeric_value=cand.parsed_value if numeric else None,
        magnitude=cand.magnitude if numeric else None,
        currency=cand.currency if numeric else None,
        is_percentage=bool(cand.percentage) if numeric else False,
        percentage_ratio=cand.ratio if numeric else None,
        unit_raw=cand.unit if numeric else None,
        reporting_period_raw=cand.reporting_period,
        reporting_period_type=(cand.period_type.lower() if cand.period_type else None),
        scope={"raw": cand.scope} if cand.scope else None,
        qualifiers=cand.qualifiers or None,
        modality=_MODALITY_MAP[(cand.modality or "").lower() or None],
        lifecycle_state="CANDIDATE",
        evidence_status="UNVERIFIED",
        reasoning_eligible=False,
        run_id=run_id,
        raw_extraction_id=raw_extraction_id,
        raw_payload=cand.model_dump(),
        extraction_model=model,
        prompt_version=prompt_version,
        extracted_at=_now(),
    )


# --------------------------------------------------------------------------- #
# persistence helpers                                                        #
# --------------------------------------------------------------------------- #
def _record_failure(conn, run_id, document_id, failure_type, reason, ref_table,
                    ref_id, detail):
    conn.execute(
        "INSERT INTO failures "
        "(run_id, document_id, failure_type, ref_table, ref_id, reason, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, document_id, failure_type, ref_table, ref_id, reason,
         json.dumps(detail, ensure_ascii=False, default=str), _now()),
    )


def _abort_consecutive(conn, run_id, document_id, n, reason):
    """Record why the run stopped early. Caller must hold a transaction.

    A provider that is down, unauthorised or out of quota fails every remaining
    chunk the same way, and each failure costs the SDK's full retry/backoff
    ladder — a 300-chunk document took ~4 hours to produce nothing. Stopping at
    the first sustained run of failures keeps that honest and fast.
    """
    _record_failure(
        conn, run_id, document_id, "run_error", "consecutive_chunk_errors",
        "documents", document_id, {"consecutive": n, "last_reason": reason},
    )


def _insert_raw_extraction(conn, *, run_id, document_id, page_index, chunk_id, result):
    parsed = result.parsed
    return conn.execute(
        "INSERT INTO raw_extractions "
        "(run_id, document_id, page_index, chunk_id, model_name, prompt_version, "
        " temperature, request_settings, raw_response, item_count, parse_error, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
        (
            run_id, document_id, page_index, chunk_id, result.model, result.prompt_version,
            json.dumps({"stop_reason": result.stop_reason}),
            result.raw_text or "",
            len(parsed.facts) if parsed is not None else None,
            result.error_detail if result.error_code else None,
            _now(),
        ),
    ).lastrowid


# --------------------------------------------------------------------------- #
# public entry point                                                         #
# --------------------------------------------------------------------------- #
def extract_document(
    document_id: int,
    *,
    client: Any = None,
    database_path: str | None = None,
    settings: Settings | None = None,
) -> ExtractionRunResult:
    """Run candidate extraction over every chunk of one ingested document.

    ``client`` is any object exposing ``.model``, ``.prompt_version`` and
    ``.extract(chunk_text, doc_header) -> LLMExtraction``. If omitted, a real
    ``Extractor`` is constructed (needs the SDK + an API key).
    """
    settings = settings or get_settings()
    if client is None:
        from app.llm import Extractor

        client = Extractor(settings)

    conn = db.connect(database_path)
    run_id: int | None = None
    try:
        doc = conn.execute(
            "SELECT * FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        if doc is None:
            raise ExtractError("unknown_document", detail=document_id)
        if doc["status"] not in ("ingested", "processing", "done"):
            raise ExtractError("document_not_ingested", detail=doc["status"])

        chunks = conn.execute(
            "SELECT id, page_index, seq, char_offset, char_end, text FROM chunks "
            "WHERE document_id = ? ORDER BY page_index, seq",
            (document_id,),
        ).fetchall()

        with db.transaction(conn):
            run_id = conn.execute(
                "INSERT INTO runs "
                "(document_id, run_type, status, stage, started_at, model_name, "
                " prompt_versions, settings) "
                "VALUES (?, 'extract', 'running', 'extract', ?, ?, ?, ?)",
                (
                    document_id, _now(), client.model,
                    json.dumps({"extract": client.prompt_version}),
                    json.dumps({
                        "max_tokens": settings.llm_max_tokens,
                        "temperature": settings.llm_temperature,
                        "max_llm_calls_per_doc": settings.max_llm_calls_per_doc,
                    }),
                ),
            ).lastrowid

        header = _doc_header(doc)
        counts = {
            "chunks_processed": 0, "candidates_generated": 0, "candidates_persisted": 0,
            "candidates_rejected": 0, "extraction_errors": 0,
        }
        facts_by_type: dict[str, int] = {}
        cost = 0.0
        consecutive_errors = 0

        for chunk in chunks:
            if counts["chunks_processed"] >= settings.max_llm_calls_per_doc:
                with db.transaction(conn):
                    _record_failure(
                        conn, run_id, document_id, "run_error", "max_llm_calls_per_doc",
                        "documents", document_id,
                        {"limit": settings.max_llm_calls_per_doc},
                    )
                break

            counts["chunks_processed"] += 1
            try:
                result: LLMExtraction = client.extract(chunk["text"], header)
            except Exception as e:  # noqa: BLE001 - isolate a failing chunk, keep going
                counts["extraction_errors"] += 1
                consecutive_errors += 1
                with db.transaction(conn):
                    _record_failure(
                        conn, run_id, document_id, "run_error", "llm_client_exception",
                        "chunks", chunk["id"], {"error": repr(e)[:300]},
                    )
                    if consecutive_errors >= settings.extract_consecutive_error_limit:
                        _abort_consecutive(conn, run_id, document_id, consecutive_errors,
                                           "llm_client_exception")
                        break
                continue
            cost += (result.input_tokens * settings.llm_input_cost_per_token
                     + result.output_tokens * settings.llm_output_cost_per_token)

            if result.error_code == "auth":
                raise ExtractError("llm_auth_failed", detail=result.error_detail,
                                   run_id=run_id)

            with db.transaction(conn):
                raw_id = _insert_raw_extraction(
                    conn, run_id=run_id, document_id=document_id,
                    page_index=chunk["page_index"], chunk_id=chunk["id"], result=result,
                )
                if result.error_code in ("api_error", "rate_limit", "timeout", "refusal"):
                    counts["extraction_errors"] += 1
                    consecutive_errors += 1
                    _record_failure(
                        conn, run_id, document_id, "run_error", result.error_code,
                        "raw_extractions", raw_id,
                        {"chunk_id": chunk["id"], "detail": result.error_detail},
                    )
                    if consecutive_errors >= settings.extract_consecutive_error_limit:
                        _abort_consecutive(conn, run_id, document_id, consecutive_errors,
                                           result.error_code)
                        break
                    continue
                consecutive_errors = 0
                if result.error_code in ("malformed_response", "truncated_response"):
                    _record_failure(
                        conn, run_id, document_id, "extraction_unparsed",
                        result.error_code, "raw_extractions", raw_id,
                        {"chunk_id": chunk["id"], "detail": result.error_detail},
                    )
                    continue

                candidates = result.parsed.facts if result.parsed else []
                counts["candidates_generated"] += len(candidates)
                seen: set[tuple] = set()
                for i, cand in enumerate(candidates):
                    reason = _validate_candidate(cand, len(chunk["text"]))
                    if reason is None:
                        identity = _candidate_identity(cand)
                        if identity in seen:
                            reason = "duplicate_in_chunk"
                        else:
                            seen.add(identity)
                    if reason is not None:
                        counts["candidates_rejected"] += 1
                        _record_failure(
                            conn, run_id, document_id, "extraction_unparsed", reason,
                            "raw_extractions", raw_id,
                            {"chunk_id": chunk["id"], "candidate_index": i,
                             "candidate": cand.model_dump()},
                        )
                        continue

                    fact_in = _candidate_to_fact_in(
                        cand, document_id=document_id, page_index=chunk["page_index"],
                        run_id=run_id, raw_extraction_id=raw_id, model=result.model,
                        prompt_version=result.prompt_version,
                    )
                    fact_id = insert_fact(conn, fact_in)
                    # candidate citation, NOT verified — page-relative offsets
                    attach_evidence(conn, fact_id, EvidenceIn(
                        document_id=document_id, page_index=chunk["page_index"],
                        chunk_id=chunk["id"],
                        char_start=chunk["char_offset"] + cand.char_start,
                        char_end=chunk["char_offset"] + cand.char_end,
                        quote=cand.quote, method="text_layer",
                        verification_method="unverified", evidence_status="UNVERIFIED",
                        notes="candidate citation from extraction; not yet verified (Phase 5)",
                    ))
                    counts["candidates_persisted"] += 1
                    facts_by_type[cand.fact_type] = facts_by_type.get(cand.fact_type, 0) + 1

        with db.transaction(conn):
            conn.execute(
                "UPDATE runs SET status = 'done', stage = 'complete', finished_at = ?, "
                "chunks_processed = ?, facts_extracted = ?, llm_calls = ?, "
                "estimated_cost_usd = ? WHERE id = ?",
                (_now(), counts["chunks_processed"], counts["candidates_persisted"],
                 counts["chunks_processed"], round(cost, 6), run_id),
            )

        log.info(
            "extracted document_id=%s chunks=%s generated=%s persisted=%s rejected=%s errors=%s",
            document_id, counts["chunks_processed"], counts["candidates_generated"],
            counts["candidates_persisted"], counts["candidates_rejected"],
            counts["extraction_errors"],
        )
        return ExtractionRunResult(
            run_id=run_id, document_id=document_id, status="done",
            facts_by_type=facts_by_type, estimated_cost_usd=round(cost, 6), **counts,
        )

    except ExtractError as e:
        if run_id is not None:
            _mark_run_failed(conn, run_id, document_id, e.code)
            e.run_id = run_id
        raise
    except Exception as e:  # noqa: BLE001 - convert to an inspectable ExtractError
        if run_id is not None:
            _mark_run_failed(conn, run_id, document_id, f"unexpected: {e!r}")
        raise ExtractError("extraction_failed", detail=repr(e)[:200], run_id=run_id) from e
    finally:
        conn.close()


def _mark_run_failed(conn, run_id: int, document_id: int, reason: str) -> None:
    reason = reason[:500]
    try:
        with db.transaction(conn):
            conn.execute(
                "UPDATE runs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                (reason, _now(), run_id),
            )
            _record_failure(conn, run_id, document_id, "run_error", reason,
                            "documents", document_id, {"reason": reason})
    except Exception:  # noqa: BLE001 - bookkeeping must not mask the real error
        log.exception("failed to record extraction-run failure run_id=%s", run_id)


# --------------------------------------------------------------------------- #
# observability (Phase 4 = lightweight; not the Phase 10 eval framework)     #
# --------------------------------------------------------------------------- #
def extraction_summary(conn: sqlite3.Connection, *, document_id: int | None = None,
                       run_id: int | None = None) -> dict[str, Any]:
    """Counts for inspection: chunks processed, candidates generated / persisted /
    rejected, extraction errors, facts by type, facts by document."""
    where, params = [], []
    if run_id is not None:
        where.append("run_id = ?")
        params.append(run_id)
    if document_id is not None:
        where.append("document_id = ?")
        params.append(document_id)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    chunks = conn.execute(
        f"SELECT COUNT(*) FROM raw_extractions{clause}", params
    ).fetchone()[0]
    generated = conn.execute(
        f"SELECT COALESCE(SUM(item_count), 0) FROM raw_extractions{clause}", params
    ).fetchone()[0]
    persisted = conn.execute(
        f"SELECT COUNT(*) FROM facts{clause}", params
    ).fetchone()[0]
    rejected = conn.execute(
        f"SELECT COUNT(*) FROM failures{clause} "
        f"{'AND' if where else 'WHERE'} failure_type = 'extraction_unparsed'",
        params,
    ).fetchone()[0]
    errors = conn.execute(
        f"SELECT COUNT(*) FROM failures{clause} "
        f"{'AND' if where else 'WHERE'} failure_type = 'run_error'",
        params,
    ).fetchone()[0]
    by_type = {
        r["fact_type"]: r["n"]
        for r in conn.execute(
            f"SELECT fact_type, COUNT(*) AS n FROM facts{clause} GROUP BY fact_type", params
        )
    }
    by_doc = {
        r["document_id"]: r["n"]
        for r in conn.execute(
            f"SELECT document_id, COUNT(*) AS n FROM facts{clause} GROUP BY document_id", params
        )
    }
    return {
        "chunks_processed": chunks,
        "candidates_generated": generated,
        "candidates_persisted": persisted,
        "candidates_rejected": rejected,
        "extraction_errors": errors,
        "facts_by_type": by_type,
        "facts_by_document": by_doc,
    }
