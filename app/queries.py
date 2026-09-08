"""Phase 11 — read/shaping layer for the HTTP API.

Pure query + dict-shaping over the Phase 1-10 tables. No writes, no LLM. Returns
plain ``dict``s in the shapes ``docs/API_DESIGN.md`` describes; ``app.main`` wraps
them in the pydantic response models. Storage stays ``sqlite3`` rows — no ORM.

ponytail: fact shaping does one small follow-up query per fact (evidence, entity).
Fine for a few thousand facts; batch if a corpus ever dwarfs that.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

_CATEGORY_LABEL = {
    "CORROBORATES": "Corroborates",
    "CONTRADICTS": "Contradicts",
    "DIFFERENT_CONTEXT": "Reconciled by context",
    "TEMPORAL_EVOLUTION": "Temporal evolution",
    "UNCERTAIN": "Uncertain",
}

_FACT_TYPE = {"numeric", "semantic", "temporal", "categorical"}
_LIFECYCLE = {"RAW", "CANDIDATE", "GROUNDED", "NORMALIZED",
              "ELIGIBLE_FOR_REASONING", "QUARANTINED"}
_EVIDENCE_STATUS = {"VERIFIED", "PARTIAL", "UNVERIFIED"}
_MODALITY = {"ASSERTED", "HISTORICAL", "ESTIMATED", "FORECAST", "TARGET", "UNCERTAIN"}
_REL_CATEGORY = set(_CATEGORY_LABEL)


def _loads(value: str | None) -> Any:
    if value in (None, ""):
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def _clamp_page(limit: int | None, offset: int | None) -> tuple[int, int]:
    lim = 50 if limit is None else max(1, min(int(limit), 200))
    off = 0 if offset is None else max(0, int(offset))
    return lim, off


# --------------------------------------------------------------------------- #
# facts                                                                      #
# --------------------------------------------------------------------------- #
_FACT_COLS = (
    "f.id, f.document_id, f.page_index, f.lifecycle_state, f.reasoning_eligible, "
    "f.subject_raw, f.subject_entity_id, f.predicate, f.predicate_norm, f.object_raw, "
    "f.fact_type, f.value_raw, f.numeric_value, f.magnitude, f.magnitude_factor, "
    "f.base_value, f.currency, f.is_percentage, f.percentage_ratio, f.unit_raw, "
    "f.unit_norm, f.value_text, f.reporting_period_raw, f.reporting_period_start, "
    "f.reporting_period_end, f.reporting_period_type, f.scope, f.qualifiers, f.modality, "
    "f.context_complete, f.evidence_status, f.extraction_model, f.prompt_version, "
    "f.extraction_temperature, f.raw_extraction_id, "
    "d.title AS document_title, d.publisher, d.disclosure_type, d.publication_date, "
    "d.data_vintage"
)


def _shape_fact(conn: sqlite3.Connection, r: sqlite3.Row) -> dict:
    printed_label = conn.execute(
        "SELECT printed_label FROM pages WHERE document_id = ? AND page_index = ?",
        (r["document_id"], r["page_index"]),
    ).fetchone()
    entity = None
    if r["subject_entity_id"] is not None:
        e = conn.execute(
            "SELECT id, canonical_label FROM entities WHERE id = ?", (r["subject_entity_id"],)
        ).fetchone()
        if e:
            entity = {"id": e["id"], "canonical_label": e["canonical_label"]}

    ev_row = conn.execute(
        "SELECT page_index, printed_label, char_start, char_end, quote, method, "
        "verification_method, fuzzy_score, numeric_rederivation, evidence_status, "
        "evidence_score, notes FROM evidence WHERE fact_id = ?", (r["id"],),
    ).fetchone()
    evidence = dict(ev_row) if ev_row else None

    numeric = None
    if r["fact_type"] == "numeric":
        numeric = {
            "value_raw": r["value_raw"], "numeric_value": r["numeric_value"],
            "magnitude": r["magnitude"], "magnitude_factor": r["magnitude_factor"],
            "base_value": r["base_value"], "currency": r["currency"],
            "is_percentage": bool(r["is_percentage"]),
            "percentage_ratio": r["percentage_ratio"],
            "unit_raw": r["unit_raw"], "unit_norm": r["unit_norm"],
        }

    return {
        "id": r["id"],
        "document_id": r["document_id"],
        "document_title": r["document_title"],
        "publisher": r["publisher"],
        "disclosure_type": r["disclosure_type"],
        "publication_date": r["publication_date"],
        "data_vintage": r["data_vintage"],
        "page_index": r["page_index"],
        "printed_label": printed_label["printed_label"] if printed_label else None,
        "lifecycle_state": r["lifecycle_state"],
        "reasoning_eligible": bool(r["reasoning_eligible"]),
        "subject_raw": r["subject_raw"],
        "entity": entity,
        "predicate": r["predicate"],
        "predicate_norm": r["predicate_norm"],
        "object_raw": r["object_raw"],
        "fact_type": r["fact_type"],
        "numeric": numeric,
        "value_text": r["value_text"],
        "reporting_period": {
            "raw": r["reporting_period_raw"], "start": r["reporting_period_start"],
            "end": r["reporting_period_end"], "type": r["reporting_period_type"],
        },
        "scope": _loads(r["scope"]),
        "qualifiers": _loads(r["qualifiers"]) or [],
        "modality": r["modality"],
        "context_complete": bool(r["context_complete"]),
        "evidence_status": r["evidence_status"],
        "evidence": evidence,
        "repro": {
            "extraction_model": r["extraction_model"],
            "prompt_version": r["prompt_version"],
            "extraction_temperature": r["extraction_temperature"],
        },
    }


def fact_out(conn: sqlite3.Connection, fact_id: int, *, detail: bool = False) -> dict | None:
    r = conn.execute(
        f"SELECT {_FACT_COLS} FROM facts f JOIN documents d ON d.id = f.document_id "
        f"WHERE f.id = ?", (fact_id,),
    ).fetchone()
    if r is None:
        return None
    out = _shape_fact(conn, r)
    if not detail:
        return out

    span = conn.execute(
        "SELECT p.text, e.char_start, e.char_end FROM evidence e "
        "JOIN pages p ON p.document_id = e.document_id AND p.page_index = e.page_index "
        "WHERE e.fact_id = ?", (fact_id,),
    ).fetchone()
    if span and span["char_start"] is not None:
        lo = max(0, span["char_start"] - 200)
        hi = min(len(span["text"]), span["char_end"] + 200)
        out["context_window"] = span["text"][lo:hi]
    if r["raw_extraction_id"] is not None:
        raw = conn.execute(
            "SELECT model_name, prompt_version, raw_response FROM raw_extractions WHERE id = ?",
            (r["raw_extraction_id"],),
        ).fetchone()
        if raw:
            out["raw_extraction"] = dict(raw)
    out["relationships"] = [
        {"id": x["id"], "category": x["category"], "context_dimension": x["context_dimension"],
         "confidence": x["confidence"], "other_fact_id": x["fact_b_id"] if x["fact_a_id"] == fact_id
         else x["fact_a_id"]}
        for x in conn.execute(
            "SELECT id, fact_a_id, fact_b_id, category, context_dimension, confidence "
            "FROM relationships WHERE fact_a_id = ? OR fact_b_id = ? ORDER BY id",
            (fact_id, fact_id),
        )
    ]
    return out


def list_facts(conn: sqlite3.Connection, *, filters: dict[str, Any],
               limit: int | None = None, offset: int | None = None,
               sort: str = "recent") -> dict:
    where: list[str] = []
    params: list[Any] = []
    f = filters

    if f.get("document_id") is not None:
        where.append("f.document_id = ?")
        params.append(f["document_id"])
    if f.get("entity_id") is not None:
        where.append("f.subject_entity_id = ?")
        params.append(f["entity_id"])
    if f.get("entity"):
        where.append(
            "f.subject_entity_id IN (SELECT id FROM entities WHERE canonical_label LIKE ? "
            "UNION SELECT entity_id FROM entity_aliases WHERE surface LIKE ?)"
        )
        params += [f"%{f['entity']}%", f"%{f['entity']}%"]
    if f.get("predicate"):
        where.append("(f.predicate LIKE ? OR f.predicate_norm LIKE ?)")
        params += [f"%{f['predicate']}%", f"%{f['predicate']}%"]
    if f.get("type") in _FACT_TYPE:
        where.append("f.fact_type = ?")
        params.append(f["type"])
    if f.get("lifecycle_state") in _LIFECYCLE:
        where.append("f.lifecycle_state = ?")
        params.append(f["lifecycle_state"])
    if f.get("evidence_status") in _EVIDENCE_STATUS:
        where.append("f.evidence_status = ?")
        params.append(f["evidence_status"])
    if f.get("modality") in _MODALITY:
        where.append("f.modality = ?")
        params.append(f["modality"])
    if f.get("reasoning_eligible") is not None:
        where.append("f.reasoning_eligible = ?")
        params.append(1 if f["reasoning_eligible"] else 0)
    if f.get("q"):
        where.append('f.id IN (SELECT fact_id FROM facts_fts WHERE facts_fts MATCH ?)')
        params.append(f'"{f["q"]}"')

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(
        f"SELECT COUNT(*) FROM facts f{clause}", params
    ).fetchone()[0]

    order = "f.page_index ASC, f.id ASC" if sort == "page" else "f.id DESC"
    lim, off = _clamp_page(limit, offset)
    rows = conn.execute(
        f"SELECT {_FACT_COLS} FROM facts f JOIN documents d ON d.id = f.document_id"
        f"{clause} ORDER BY {order} LIMIT ? OFFSET ?", [*params, lim, off],
    ).fetchall()
    return {"items": [_shape_fact(conn, r) for r in rows], "total": total}


# --------------------------------------------------------------------------- #
# relationships                                                              #
# --------------------------------------------------------------------------- #
def relationship_out(conn: sqlite3.Connection, rel_id: int, *, detail: bool = False) -> dict | None:
    r = conn.execute("SELECT * FROM relationships WHERE id = ?", (rel_id,)).fetchone()
    if r is None:
        return None
    return _shape_relationship(conn, r, detail=detail)


def _shape_relationship(conn: sqlite3.Connection, r: sqlite3.Row, *,
                        detail: bool = False) -> dict:
    return {
        "id": r["id"],
        "category": r["category"],
        "category_label": _CATEGORY_LABEL.get(r["category"], r["category"]),
        "context_dimension": r["context_dimension"],
        "fact_a": fact_out(conn, r["fact_a_id"], detail=detail),
        "fact_b": fact_out(conn, r["fact_b_id"], detail=detail),
        "deterministic_signals": _loads(r["deterministic_signals"]) or {},
        "llm_used": bool(r["llm_used"]),
        "llm_proposed_category": r["llm_proposed_category"],
        "validation_action": r["validation_action"],
        "validation_notes": r["validation_notes"],
        "reasoning": r["reasoning"],
        "confidence": r["confidence"],
        "created_at": r["created_at"],
    }


def list_relationships(conn: sqlite3.Connection, *, filters: dict[str, Any],
                       limit: int | None = None, offset: int | None = None,
                       sort: str = "confidence") -> dict:
    where: list[str] = []
    params: list[Any] = []
    f = filters

    if f.get("category") in _REL_CATEGORY:
        where.append("r.category = ?")
        params.append(f["category"])
    if f.get("context_dimension"):
        where.append("r.context_dimension = ?")
        params.append(f["context_dimension"])
    if f.get("validation_action"):
        where.append("r.validation_action = ?")
        params.append(f["validation_action"])
    if f.get("llm_used") is not None:
        where.append("r.llm_used = ?")
        params.append(1 if f["llm_used"] else 0)
    if f.get("min_confidence") is not None:
        where.append("r.confidence >= ?")
        params.append(f["min_confidence"])
    if f.get("document_id") is not None:
        where.append(
            "(r.fact_a_id IN (SELECT id FROM facts WHERE document_id = ?) "
            "OR r.fact_b_id IN (SELECT id FROM facts WHERE document_id = ?))"
        )
        params += [f["document_id"], f["document_id"]]
    if f.get("entity_id") is not None:
        where.append(
            "(r.fact_a_id IN (SELECT id FROM facts WHERE subject_entity_id = ?) "
            "OR r.fact_b_id IN (SELECT id FROM facts WHERE subject_entity_id = ?))"
        )
        params += [f["entity_id"], f["entity_id"]]

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM relationships r{clause}", params).fetchone()[0]
    order = "r.id DESC" if sort == "recent" else "r.confidence DESC NULLS LAST, r.id DESC"
    lim, off = _clamp_page(limit, offset)
    rows = conn.execute(
        f"SELECT r.* FROM relationships r{clause} ORDER BY {order} LIMIT ? OFFSET ?",
        [*params, lim, off],
    ).fetchall()
    return {"items": [_shape_relationship(conn, r) for r in rows], "total": total}


# --------------------------------------------------------------------------- #
# documents / runs                                                           #
# --------------------------------------------------------------------------- #
_RUN_STAT_COLS = ("pages_processed", "chunks_processed", "facts_extracted", "facts_grounded",
                  "facts_quarantined", "relationships_produced", "llm_calls")


def _shape_run(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    stats = {c: r[c] for c in _RUN_STAT_COLS}
    stats["estimated_cost_usd"] = r["estimated_cost_usd"]
    return {
        "run_id": r["id"], "run_type": r["run_type"], "status": r["status"],
        "stage": r["stage"], "stats": stats, "model_name": r["model_name"],
        "prompt_versions": _loads(r["prompt_versions"]),
        "started_at": r["started_at"], "finished_at": r["finished_at"], "error": r["error"],
    }


def _doc_counts(conn: sqlite3.Connection, document_id: int) -> dict:
    def one(sql: str, *p: Any) -> int:
        return conn.execute(sql, p).fetchone()[0]

    by_cat = {r["category"]: r["n"] for r in conn.execute(
        "SELECT category, COUNT(*) AS n FROM relationships r "
        "WHERE r.fact_a_id IN (SELECT id FROM facts WHERE document_id = ?) "
        "OR r.fact_b_id IN (SELECT id FROM facts WHERE document_id = ?) GROUP BY category",
        (document_id, document_id),
    )}
    return {
        "pages": one("SELECT COUNT(*) FROM pages WHERE document_id = ?", document_id),
        "facts": one("SELECT COUNT(*) FROM facts WHERE document_id = ?", document_id),
        "facts_unverified": one(
            "SELECT COUNT(*) FROM facts WHERE document_id = ? AND evidence_status = 'UNVERIFIED'",
            document_id),
        "facts_quarantined": one(
            "SELECT COUNT(*) FROM facts WHERE document_id = ? AND lifecycle_state = 'QUARANTINED'",
            document_id),
        "eligible_facts": one(
            "SELECT COUNT(*) FROM facts WHERE document_id = ? "
            "AND lifecycle_state = 'ELIGIBLE_FOR_REASONING'", document_id),
        "relationships": sum(by_cat.values()),
        "by_category": by_cat,
        "failures": one("SELECT COUNT(*) FROM failures WHERE document_id = ?", document_id),
    }


def document_out(conn: sqlite3.Connection, document_id: int, *,
                 detail: bool = False, duplicate: bool = False) -> dict | None:
    d = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    if d is None:
        return None
    out = {
        "id": d["id"], "sha256": d["sha256"], "original_filename": d["original_filename"],
        "title": d["title"], "publisher": d["publisher"], "disclosure_type": d["disclosure_type"],
        "document_date": d["document_date"], "publication_date": d["publication_date"],
        "data_vintage": d["data_vintage"], "fy_convention": d["fy_convention"],
        "fy_convention_source": d["fy_convention_source"], "page_count": d["page_count"],
        "status": d["status"], "status_detail": d["status_detail"], "duplicate": duplicate,
        "uploaded_at": d["uploaded_at"], "processed_at": d["processed_at"],
        "counts": _doc_counts(conn, document_id),
    }
    if detail:
        out["latest_run"] = _shape_run(latest_run(conn, document_id))
    return out


def latest_run(conn: sqlite3.Connection, document_id: int,
               run_type: str | None = None) -> sqlite3.Row | None:
    if run_type:
        return conn.execute(
            "SELECT * FROM runs WHERE document_id = ? AND run_type = ? ORDER BY id DESC LIMIT 1",
            (document_id, run_type),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM runs WHERE document_id = ? ORDER BY id DESC LIMIT 1", (document_id,)
    ).fetchone()


def list_documents(conn: sqlite3.Connection, *, status: str | None = None,
                   limit: int | None = None, offset: int | None = None) -> dict:
    where = " WHERE status = ?" if status else ""
    params: list[Any] = [status] if status else []
    total = conn.execute(f"SELECT COUNT(*) FROM documents{where}", params).fetchone()[0]
    lim, off = _clamp_page(limit, offset)
    ids = [r["id"] for r in conn.execute(
        f"SELECT id FROM documents{where} ORDER BY id DESC LIMIT ? OFFSET ?",
        [*params, lim, off],
    )]
    return {"items": [document_out(conn, i) for i in ids], "total": total}


def status_out(conn: sqlite3.Connection, document_id: int) -> dict | None:
    d = conn.execute("SELECT status FROM documents WHERE id = ?", (document_id,)).fetchone()
    if d is None:
        return None
    run = latest_run(conn, document_id, "full") or latest_run(conn, document_id)
    return {"document_id": document_id, "status": d["status"], "run": _shape_run(run)}


# --------------------------------------------------------------------------- #
# entities                                                                   #
# --------------------------------------------------------------------------- #
def _shape_entity(conn: sqlite3.Connection, e: sqlite3.Row, *, detail: bool = False) -> dict:
    alias_count = conn.execute(
        "SELECT COUNT(*) FROM entity_aliases WHERE entity_id = ?", (e["id"],)
    ).fetchone()[0]
    fact_count = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE subject_entity_id = ?", (e["id"],)
    ).fetchone()[0]
    out = {
        "id": e["id"], "canonical_label": e["canonical_label"], "entity_type": e["entity_type"],
        "normalization_key": e["normalization_key"], "alias_count": alias_count,
        "fact_count": fact_count, "resolution_method": e["resolution_method"],
        "resolution_score": e["resolution_score"], "llm_confirmed": bool(e["llm_confirmed"]),
        "llm_confidence": e["llm_confidence"],
    }
    if detail:
        out["aliases"] = [
            {"surface": a["surface"], "normalized": a["normalized"],
             "match_method": a["match_method"], "score": a["score"],
             "source_fact_id": a["source_fact_id"]}
            for a in conn.execute(
                "SELECT surface, normalized, match_method, score, source_fact_id "
                "FROM entity_aliases WHERE entity_id = ? ORDER BY id", (e["id"],))
        ]
        sample_ids = [x["id"] for x in conn.execute(
            "SELECT id FROM facts WHERE subject_entity_id = ? ORDER BY id LIMIT 10", (e["id"],))]
        out["sample_facts"] = [fact_out(conn, i) for i in sample_ids]
    return out


def entity_out(conn: sqlite3.Connection, entity_id: int, *, detail: bool = False) -> dict | None:
    e = conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
    return _shape_entity(conn, e, detail=detail) if e else None


def list_entities(conn: sqlite3.Connection, *, entity_type: str | None = None,
                  q: str | None = None, limit: int | None = None,
                  offset: int | None = None) -> dict:
    where: list[str] = []
    params: list[Any] = []
    if entity_type:
        where.append("entity_type = ?")
        params.append(entity_type)
    if q:
        where.append("canonical_label LIKE ?")
        params.append(f"%{q}%")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM entities{clause}", params).fetchone()[0]
    lim, off = _clamp_page(limit, offset)
    ids = [r["id"] for r in conn.execute(
        f"SELECT id FROM entities{clause} ORDER BY id LIMIT ? OFFSET ?", [*params, lim, off])]
    return {"items": [entity_out(conn, i) for i in ids], "total": total}


# --------------------------------------------------------------------------- #
# failures                                                                   #
# --------------------------------------------------------------------------- #
def list_failures(conn: sqlite3.Connection, *, document_id: int | None = None,
                  failure_type: str | None = None, limit: int | None = None,
                  offset: int | None = None) -> dict:
    where: list[str] = []
    params: list[Any] = []
    if document_id is not None:
        where.append("document_id = ?")
        params.append(document_id)
    if failure_type:
        where.append("failure_type = ?")
        params.append(failure_type)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM failures{clause}", params).fetchone()[0]
    counts_by_type = {r["failure_type"]: r["n"] for r in conn.execute(
        f"SELECT failure_type, COUNT(*) AS n FROM failures{clause} GROUP BY failure_type", params)}
    lim, off = _clamp_page(limit, offset)
    rows = conn.execute(
        f"SELECT * FROM failures{clause} ORDER BY id DESC LIMIT ? OFFSET ?", [*params, lim, off],
    ).fetchall()

    items = []
    for r in rows:
        item = {
            "id": r["id"], "failure_type": r["failure_type"], "reason": r["reason"],
            "ref_table": r["ref_table"], "ref_id": r["ref_id"], "detail": _loads(r["detail"]),
            "document_id": r["document_id"], "run_id": r["run_id"], "created_at": r["created_at"],
        }
        if r["ref_table"] == "facts" and r["ref_id"]:
            item["fact"] = fact_out(conn, r["ref_id"])
        elif r["ref_table"] == "relationships" and r["ref_id"]:
            item["relationship"] = relationship_out(conn, r["ref_id"])
        items.append(item)
    return {"items": items, "total": total, "counts_by_type": counts_by_type}
