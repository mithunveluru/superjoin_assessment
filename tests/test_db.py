"""Schema is created correctly, connection pragmas are enforced, and the
invariants from docs/DATA_MODEL.md hold at the storage layer."""

from __future__ import annotations

import sqlite3

import pytest

from app import db

EXPECTED_TABLES = {
    "documents",
    "pages",
    "chunks",
    "runs",
    "raw_extractions",
    "entities",
    "entity_aliases",
    "facts",
    "facts_fts",
    "evidence",
    "relationships",
    "failures",
}

NOW = "2026-09-07T00:00:00Z"


def _insert_document(conn: sqlite3.Connection, sha: str = "a" * 64) -> int:
    cur = conn.execute(
        "INSERT INTO documents (sha256, stored_path, uploaded_at) VALUES (?, ?, ?)",
        (sha, f"uploads/{sha}.pdf", NOW),
    )
    return cur.lastrowid


def _insert_fact(conn: sqlite3.Connection, doc_id: int, **overrides) -> int:
    row = {
        "document_id": doc_id,
        "page_index": 0,
        "subject_raw": "Subject",
        "predicate": "predicate",
        "object_raw": "object",
        "fact_type": "semantic",
        "created_at": NOW,
    }
    row.update(overrides)
    cols = ", ".join(row)
    ph = ", ".join(["?"] * len(row))
    cur = conn.execute(f"INSERT INTO facts ({cols}) VALUES ({ph})", tuple(row.values()))
    return cur.lastrowid


# --- schema ---------------------------------------------------------------------
def test_all_tables_present(conn):
    assert set(db.table_names(conn)) == EXPECTED_TABLES


def test_init_db_is_idempotent(db_path):
    db.init_db()
    db.init_db()  # must not raise
    c = db.connect()
    try:
        assert set(db.table_names(c)) == EXPECTED_TABLES
    finally:
        c.close()


def test_expected_indexes_present(conn):
    idx = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'ix_%'"
        )
    }
    for expected in (
        "ix_documents_status",
        "ix_facts_lifecycle",
        "ix_facts_reasoning",
        "ix_relationships_category",
        "ix_failures_type",
        "ix_entity_aliases_norm",
    ):
        assert expected in idx


# --- connection pragmas -------------------------------------------------------
def test_foreign_keys_enforced(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_wal_mode_on_file_db(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


# --- foreign keys ----------------------------------------------------------------
def test_fk_violation_raises(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO pages (document_id, page_index, text) VALUES (?, ?, ?)",
            (999, 0, "orphan page"),
        )


# --- CHECK constraints ---------------------------------------------------------
def test_bad_document_status_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO documents (sha256, stored_path, uploaded_at, status) "
            "VALUES (?, ?, ?, ?)",
            ("b" * 64, "uploads/x.pdf", NOW, "not-a-status"),
        )


def test_bad_lifecycle_state_rejected(conn):
    doc = _insert_document(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_fact(conn, doc, lifecycle_state="BOGUS")


def test_quarantined_fact_cannot_be_reasoning_eligible(conn):
    doc = _insert_document(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_fact(
            conn, doc, lifecycle_state="QUARANTINED", reasoning_eligible=1,
            evidence_status="PARTIAL",
        )


def test_eligible_fact_cannot_have_unverified_evidence(conn):
    doc = _insert_document(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_fact(
            conn, doc, lifecycle_state="ELIGIBLE_FOR_REASONING",
            reasoning_eligible=1, evidence_status="UNVERIFIED",
        )


def test_relationship_pair_order_enforced(conn):
    doc = _insert_document(conn)
    f1 = _insert_fact(conn, doc, evidence_status="VERIFIED")
    f2 = _insert_fact(conn, doc, evidence_status="VERIFIED")
    lo, hi = sorted((f1, f2))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO relationships "
            "(fact_a_id, fact_b_id, category, deterministic_signals, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (hi, lo, "CORROBORATES", "{}", NOW),
        )
    # correct order is accepted
    conn.execute(
        "INSERT INTO relationships "
        "(fact_a_id, fact_b_id, category, deterministic_signals, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (lo, hi, "CORROBORATES", "{}", NOW),
    )


def test_bad_relationship_category_rejected(conn):
    doc = _insert_document(conn)
    f1 = _insert_fact(conn, doc, evidence_status="VERIFIED")
    f2 = _insert_fact(conn, doc, evidence_status="VERIFIED")
    lo, hi = sorted((f1, f2))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO relationships "
            "(fact_a_id, fact_b_id, category, deterministic_signals, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (lo, hi, "RECONCILES", "{}", NOW),  # old taxonomy, no longer valid
        )


# --- basic round-trip + transactions -----------------------------------------
def test_document_round_trip(conn):
    conn.execute(
        "INSERT INTO documents "
        "(sha256, stored_path, original_filename, title, publisher, disclosure_type, "
        " document_date, publication_date, data_vintage, fy_convention, "
        " fy_convention_source, page_count, uploaded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "c" * 64, "uploads/c.pdf", "some report.pdf", "Some Report", "Some Org",
            "annual report", "2025-05-25", "2025-05-29", "provisional estimates",
            "apr-mar", "detected", 100, NOW,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM documents WHERE sha256 = ?", ("c" * 64,)).fetchone()
    assert row["title"] == "Some Report"
    assert row["data_vintage"] == "provisional estimates"
    assert row["fy_convention"] == "apr-mar"
    assert row["fy_convention_source"] == "detected"
    assert row["status"] == "uploaded"  # default applied


def test_transaction_rolls_back_on_error(conn):
    with pytest.raises(RuntimeError), db.transaction(conn):
        _insert_document(conn, sha="d" * 64)
        raise RuntimeError("boom")
    count = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE sha256 = ?", ("d" * 64,)
    ).fetchone()[0]
    assert count == 0


def test_facts_fts_is_queryable(conn):
    doc = _insert_document(conn)
    fid = _insert_fact(conn, doc, subject_raw="Acme Corp", predicate="revenue", object_raw="x")
    conn.execute(
        "INSERT INTO facts_fts (fact_id, subject_raw, predicate, object_raw, value_text) "
        "VALUES (?, ?, ?, ?, ?)",
        (fid, "Acme Corp", "revenue", "x", None),
    )
    conn.commit()
    hit = conn.execute(
        "SELECT fact_id FROM facts_fts WHERE facts_fts MATCH ?", ("revenue",)
    ).fetchone()
    assert hit["fact_id"] == fid
