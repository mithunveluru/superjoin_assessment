"""Phase 3 — canonical fact + evidence + relationship persistence.

No LLM, no PDF extraction, no relationship inference. This module is the durable
write/validate layer that later phases build on. It enforces one invariant the
schema alone cannot:

    a fact becomes ELIGIBLE_FOR_REASONING only if it has evidence that resolves
    FACT -> EVIDENCE -> (CHUNK ->) PAGE -> DOCUMENT.

Vocabularies below mirror the CHECK constraints in app/schema.sql + the
migrations in app/db.py, and the Literal types in app/models.py.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from app.models import EvidenceIn, FactIn, RelationshipIn

FACT_TYPES = frozenset({"numeric", "semantic", "temporal", "categorical"})
LIFECYCLE_STATES = (
    "RAW", "CANDIDATE", "GROUNDED", "NORMALIZED", "ELIGIBLE_FOR_REASONING", "QUARANTINED",
)
MODALITIES = frozenset({"ASSERTED", "HISTORICAL", "ESTIMATED", "FORECAST", "TARGET", "UNCERTAIN"})
EVIDENCE_STATUSES = frozenset({"VERIFIED", "PARTIAL", "UNVERIFIED"})
VERIFICATION_METHODS = frozenset(
    {"exact", "normalized_exact", "fuzzy", "unavailable", "unverified"}
)
RELATIONSHIP_CATEGORIES = frozenset(
    {"CORROBORATES", "CONTRADICTS", "DIFFERENT_CONTEXT", "TEMPORAL_EVOLUTION", "UNCERTAIN"}
)

# lifecycle states whose transition is guarded (use the dedicated helper)
_GUARDED_STATES = {"ELIGIBLE_FOR_REASONING", "QUARANTINED"}


class FactError(RuntimeError):
    """Expected, inspectable persistence-boundary failure. ``code`` is stable."""

    def __init__(self, code: str, *, detail: Any = None):
        self.code = code
        self.detail = detail
        super().__init__(code if detail is None else f"{code}: {detail}")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# facts                                                                      #
# --------------------------------------------------------------------------- #
_FACT_COLUMNS = (
    "document_id", "page_index", "run_id", "raw_extraction_id", "lifecycle_state",
    "quarantine_reason", "subject_raw", "subject_entity_id", "predicate", "predicate_norm",
    "object_raw", "fact_type", "value_raw", "numeric_value", "magnitude", "magnitude_factor",
    "base_value", "currency", "is_percentage", "percentage_ratio", "unit_raw", "unit_norm",
    "value_text", "reporting_period_raw", "reporting_period_start", "reporting_period_end",
    "reporting_period_type", "scope", "qualifiers", "modality", "context_complete",
    "evidence_status", "reasoning_eligible", "extraction_model", "prompt_version",
    "extraction_temperature", "extracted_at", "raw_payload", "created_at",
)


def insert_fact(conn: sqlite3.Connection, fact: FactIn) -> int:
    """Persist one canonical fact. Validates the document/page it points at.
    Does not create evidence — call :func:`attach_evidence` for that."""
    if not conn.execute(
        "SELECT 1 FROM documents WHERE id = ?", (fact.document_id,)
    ).fetchone():
        raise FactError("unknown_document", detail=fact.document_id)
    if not conn.execute(
        "SELECT 1 FROM pages WHERE document_id = ? AND page_index = ?",
        (fact.document_id, fact.page_index),
    ).fetchone():
        raise FactError(
            "page_not_in_document",
            detail=f"document_id={fact.document_id} page_index={fact.page_index}",
        )

    row = {
        "document_id": fact.document_id,
        "page_index": fact.page_index,
        "run_id": fact.run_id,
        "raw_extraction_id": fact.raw_extraction_id,
        "lifecycle_state": fact.lifecycle_state,
        "quarantine_reason": fact.quarantine_reason,
        "subject_raw": fact.subject_raw,
        "subject_entity_id": fact.subject_entity_id,
        "predicate": fact.predicate,
        "predicate_norm": fact.predicate_norm,
        "object_raw": fact.object_raw,
        "fact_type": fact.fact_type,
        "value_raw": fact.value_raw,
        "numeric_value": fact.numeric_value,
        "magnitude": fact.magnitude,
        "magnitude_factor": fact.magnitude_factor,
        "base_value": fact.base_value,
        "currency": fact.currency,
        "is_percentage": int(fact.is_percentage),
        "percentage_ratio": fact.percentage_ratio,
        "unit_raw": fact.unit_raw,
        "unit_norm": fact.unit_norm,
        "value_text": fact.value_text,
        "reporting_period_raw": fact.reporting_period_raw,
        "reporting_period_start": fact.reporting_period_start,
        "reporting_period_end": fact.reporting_period_end,
        "reporting_period_type": fact.reporting_period_type,
        "scope": _json(fact.scope),
        "qualifiers": _json(fact.qualifiers),
        "modality": fact.modality,
        "context_complete": int(fact.context_complete),
        "evidence_status": fact.evidence_status,
        "reasoning_eligible": int(fact.reasoning_eligible),
        "extraction_model": fact.extraction_model,
        "prompt_version": fact.prompt_version,
        "extraction_temperature": fact.extraction_temperature,
        "extracted_at": fact.extracted_at,
        "raw_payload": _json(fact.raw_payload) if not isinstance(fact.raw_payload, str)
        else fact.raw_payload,
        "created_at": _now(),
    }
    placeholders = ", ".join(f":{c}" for c in _FACT_COLUMNS)
    fact_id = conn.execute(
        f"INSERT INTO facts ({', '.join(_FACT_COLUMNS)}) VALUES ({placeholders})", row
    ).lastrowid
    conn.execute(
        "INSERT INTO facts_fts (fact_id, subject_raw, predicate, object_raw, value_text) "
        "VALUES (?, ?, ?, ?, ?)",
        (fact_id, fact.subject_raw, fact.predicate, fact.object_raw, fact.value_text),
    )
    return fact_id


# --------------------------------------------------------------------------- #
# evidence + traceability                                                    #
# --------------------------------------------------------------------------- #
def attach_evidence(conn: sqlite3.Connection, fact_id: int, ev: EvidenceIn) -> int:
    """Attach the single evidence row for a fact and validate the full chain:
    the page belongs to the document, the chunk (if given) belongs to that page,
    and any offsets are inside the page text. Mirrors ``evidence_status`` onto
    the fact so it can later become reasoning-eligible."""
    fact = conn.execute(
        "SELECT document_id FROM facts WHERE id = ?", (fact_id,)
    ).fetchone()
    if fact is None:
        raise FactError("unknown_fact", detail=fact_id)
    if conn.execute("SELECT 1 FROM evidence WHERE fact_id = ?", (fact_id,)).fetchone():
        raise FactError("evidence_exists", detail=fact_id)
    if ev.document_id != fact["document_id"]:
        raise FactError(
            "evidence_document_mismatch",
            detail=f"fact doc={fact['document_id']} evidence doc={ev.document_id}",
        )

    page = conn.execute(
        "SELECT id, text FROM pages WHERE document_id = ? AND page_index = ?",
        (ev.document_id, ev.page_index),
    ).fetchone()
    if page is None:
        raise FactError(
            "page_not_in_document",
            detail=f"document_id={ev.document_id} page_index={ev.page_index}",
        )
    page_id = page["id"]
    if ev.page_id is not None and ev.page_id != page_id:
        raise FactError("page_id_mismatch", detail=f"given={ev.page_id} resolved={page_id}")

    if ev.chunk_id is not None:
        chunk = conn.execute(
            "SELECT document_id, page_index FROM chunks WHERE id = ?", (ev.chunk_id,)
        ).fetchone()
        if chunk is None:
            raise FactError("unknown_chunk", detail=ev.chunk_id)
        if chunk["document_id"] != ev.document_id or chunk["page_index"] != ev.page_index:
            raise FactError(
                "chunk_not_in_page",
                detail=f"chunk doc={chunk['document_id']} page={chunk['page_index']} "
                f"vs evidence doc={ev.document_id} page={ev.page_index}",
            )

    # EvidenceIn already validated that char_start/char_end are paired and start < end
    if ev.char_start is not None and not (
        0 <= ev.char_start < ev.char_end <= len(page["text"])  # type: ignore[operator]
    ):
        raise FactError(
            "invalid_offsets",
            detail=f"start={ev.char_start} end={ev.char_end} page_len={len(page['text'])}",
        )

    evidence_id = conn.execute(
        "INSERT INTO evidence "
        "(fact_id, document_id, page_index, page_id, chunk_id, printed_label, "
        " char_start, char_end, quote, method, verification_method, fuzzy_score, "
        " numeric_rederivation, evidence_status, evidence_score, notes, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            fact_id, ev.document_id, ev.page_index, page_id, ev.chunk_id, ev.printed_label,
            ev.char_start, ev.char_end, ev.quote, ev.method, ev.verification_method,
            ev.fuzzy_score, ev.numeric_rederivation, ev.evidence_status, ev.evidence_score,
            ev.notes, _now(),
        ),
    ).lastrowid
    conn.execute(
        "UPDATE facts SET evidence_status = ? WHERE id = ?", (ev.evidence_status, fact_id)
    )
    return evidence_id


def evidence_chain(conn: sqlite3.Connection, fact_id: int) -> dict[str, int | None] | None:
    """Resolve FACT -> EVIDENCE -> (CHUNK ->) PAGE -> DOCUMENT. Returns the ids
    if the chain is intact, else ``None``."""
    row = conn.execute(
        "SELECT e.id AS evidence_id, e.document_id AS ev_doc, e.page_index AS ev_page, "
        "       e.chunk_id, p.id AS page_id, d.id AS document_id "
        "FROM evidence e "
        "LEFT JOIN pages p ON p.document_id = e.document_id AND p.page_index = e.page_index "
        "LEFT JOIN documents d ON d.id = e.document_id "
        "WHERE e.fact_id = ?",
        (fact_id,),
    ).fetchone()
    if row is None or row["page_id"] is None or row["document_id"] is None:
        return None
    if row["chunk_id"] is not None:
        chunk = conn.execute(
            "SELECT document_id, page_index FROM chunks WHERE id = ?", (row["chunk_id"],)
        ).fetchone()
        if chunk is None or (
            chunk["document_id"] != row["ev_doc"] or chunk["page_index"] != row["ev_page"]
        ):
            return None
    return {
        "fact_id": fact_id,
        "evidence_id": row["evidence_id"],
        "chunk_id": row["chunk_id"],
        "page_id": row["page_id"],
        "document_id": row["document_id"],
    }


# --------------------------------------------------------------------------- #
# lifecycle                                                                  #
# --------------------------------------------------------------------------- #
def set_lifecycle(conn: sqlite3.Connection, fact_id: int, state: str) -> None:
    """Move a fact between the un-guarded lifecycle states
    (RAW/CANDIDATE/GROUNDED/NORMALIZED). Use :func:`mark_reasoning_eligible` and
    :func:`quarantine_fact` for the guarded ones."""
    if state not in LIFECYCLE_STATES:
        raise FactError("invalid_lifecycle_state", detail=state)
    if state == "ELIGIBLE_FOR_REASONING":
        mark_reasoning_eligible(conn, fact_id)
        return
    if state == "QUARANTINED":
        raise FactError("use_quarantine_fact", detail="call quarantine_fact(conn, id, reason)")
    if not conn.execute(
        "UPDATE facts SET lifecycle_state = ? WHERE id = ?", (state, fact_id)
    ).rowcount:
        raise FactError("unknown_fact", detail=fact_id)


def mark_reasoning_eligible(conn: sqlite3.Connection, fact_id: int) -> None:
    """Promote a fact to ELIGIBLE_FOR_REASONING — only if it has a traceable,
    non-UNVERIFIED evidence chain. This is the guard that keeps unsupported
    facts out of the reasoning layer."""
    fact = conn.execute(
        "SELECT lifecycle_state, evidence_status FROM facts WHERE id = ?", (fact_id,)
    ).fetchone()
    if fact is None:
        raise FactError("unknown_fact", detail=fact_id)
    if fact["lifecycle_state"] == "QUARANTINED":
        raise FactError("fact_quarantined", detail=fact_id)
    if evidence_chain(conn, fact_id) is None:
        raise FactError("evidence_required", detail="no evidence chain FACT->...->DOCUMENT")
    if fact["evidence_status"] not in ("VERIFIED", "PARTIAL"):
        raise FactError("evidence_unverified", detail=fact["evidence_status"])
    conn.execute(
        "UPDATE facts SET lifecycle_state = 'ELIGIBLE_FOR_REASONING', reasoning_eligible = 1 "
        "WHERE id = ?",
        (fact_id,),
    )


def quarantine_fact(conn: sqlite3.Connection, fact_id: int, reason: str) -> None:
    """Terminal state for a fact that cannot be grounded/normalized. Kept and
    inspectable; never reasoning-eligible."""
    if not reason:
        raise FactError("quarantine_reason_required")
    if not conn.execute(
        "UPDATE facts SET lifecycle_state = 'QUARANTINED', reasoning_eligible = 0, "
        "quarantine_reason = ? WHERE id = ?",
        (reason, fact_id),
    ).rowcount:
        raise FactError("unknown_fact", detail=fact_id)


# --------------------------------------------------------------------------- #
# relationships (storage only — nothing is inferred in Phase 3)              #
# --------------------------------------------------------------------------- #
def add_relationship(conn: sqlite3.Connection, rel: RelationshipIn) -> int:
    """Persist one fact-to-fact relationship. Stored canonically as
    (min, max) fact id (undirected); self-relationships are rejected; both facts
    must be reasoning-eligible; a repeated pair is a no-op that returns the
    existing row's id (first write wins)."""
    if rel.fact_a_id == rel.fact_b_id:
        raise FactError("self_relationship", detail=rel.fact_a_id)
    lo, hi = sorted((rel.fact_a_id, rel.fact_b_id))

    facts = {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, reasoning_eligible FROM facts WHERE id IN (?, ?)", (lo, hi)
        )
    }
    missing = [x for x in (lo, hi) if x not in facts]
    if missing:
        raise FactError("unknown_fact", detail=missing)
    not_eligible = [x for x in (lo, hi) if not facts[x]["reasoning_eligible"]]
    if not_eligible:
        raise FactError("not_reasoning_eligible", detail=not_eligible)

    existing = conn.execute(
        "SELECT id FROM relationships WHERE fact_a_id = ? AND fact_b_id = ?", (lo, hi)
    ).fetchone()
    if existing:
        return existing["id"]

    return conn.execute(
        "INSERT INTO relationships "
        "(fact_a_id, fact_b_id, category, context_dimension, deterministic_signals, "
        " reasoning, confidence, model_name, prompt_version, run_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            lo, hi, rel.category, rel.context_dimension, _json(rel.deterministic_signals),
            rel.reasoning, rel.confidence, rel.model_name, rel.prompt_version, rel.run_id,
            _now(),
        ),
    ).lastrowid
