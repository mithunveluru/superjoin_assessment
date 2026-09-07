"""Phase 3 — canonical fact + evidence + relationship persistence model.

Covers migration 2, fact persistence (all four fact types, raw + normalized
numeric representation, raw_payload), the evidence chain + offset validation,
the lifecycle state machine, the reasoning-eligibility / evidence invariant, and
relationship storage. All data is synthetic — no starter PDFs.
"""

from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from app import db
from app.facts import (
    FactError,
    add_relationship,
    attach_evidence,
    evidence_chain,
    insert_fact,
    mark_reasoning_eligible,
    quarantine_fact,
    set_lifecycle,
)
from app.models import EvidenceIn, FactIn, RelationshipIn

NOW = "2026-01-01T00:00:00Z"


def _fact(src: dict, page: int = 0, **over) -> FactIn:
    base = {
        "document_id": src["document_id"],
        "page_index": page,
        "subject_raw": "Acme Corp",
        "predicate": "revenue",
        "object_raw": "some value",
        "fact_type": "semantic",
        "value_text": "some value",
    }
    base.update(over)
    return FactIn(**base)


def _grounded_eligible_fact(conn, src, page=0, **over) -> int:
    """Insert a fact + valid VERIFIED evidence + promote it to eligible."""
    fid = insert_fact(conn, _fact(src, page=page, **over))
    text = src["page_texts"][page]
    attach_evidence(
        conn, fid,
        EvidenceIn(
            document_id=src["document_id"], page_index=page,
            chunk_id=src["chunk_ids"][page], quote=text[:5],
            char_start=0, char_end=5,
            verification_method="exact", evidence_status="VERIFIED",
        ),
    )
    set_lifecycle(conn, fid, "GROUNDED")
    mark_reasoning_eligible(conn, fid)
    return fid


# --------------------------------------------------------------------------- #
# schema / migration                                                         #
# --------------------------------------------------------------------------- #
def test_migration_2_applied(conn):
    assert db.schema_version(conn) == db.CURRENT_SCHEMA_VERSION
    fcols = {r["name"] for r in conn.execute("PRAGMA table_info(facts)")}
    assert "raw_payload" in fcols
    ecols = {r["name"] for r in conn.execute("PRAGMA table_info(evidence)")}
    assert {"page_id", "chunk_id"} <= ecols
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_migration_idempotent(db_path):
    db.init_db()
    db.init_db()
    c = db.connect()
    try:
        assert db.schema_version(c) == db.CURRENT_SCHEMA_VERSION
    finally:
        c.close()


def test_migration_from_genesis_only_state(db_path):
    # simulate a Phase-1 database: genesis schema, user_version 0
    c = db.connect()
    c.executescript(db.SCHEMA_PATH.read_text())
    c.commit()
    assert db.schema_version(c) == 0
    c.close()
    db.init_db()  # must climb to 2
    c = db.connect()
    try:
        assert db.schema_version(c) == db.CURRENT_SCHEMA_VERSION
        assert "raw_payload" in {r["name"] for r in c.execute("PRAGMA table_info(facts)")}
        assert "page_id" in {r["name"] for r in c.execute("PRAGMA table_info(evidence)")}
    finally:
        c.close()


def test_migration_from_phase2_state(db_path):
    # genesis + migration 1 only, then init_db must apply migration 2
    c = db.connect()
    c.executescript(db.SCHEMA_PATH.read_text())
    c.executescript(db._MIGRATIONS[0][1])
    c.execute("PRAGMA user_version = 1")
    c.commit()
    c.close()
    db.init_db()
    c = db.connect()
    try:
        assert db.schema_version(c) == db.CURRENT_SCHEMA_VERSION
    finally:
        c.close()


def test_widened_checks_and_new_constraints(conn):
    conn.execute("INSERT INTO documents (sha256, stored_path, uploaded_at) VALUES (?,?,?)",
                 ("a" * 64, "p", NOW))
    doc = conn.execute("SELECT id FROM documents").fetchone()["id"]
    conn.execute("INSERT INTO pages (document_id, page_index, text, char_count) VALUES (?,0,?,?)",
                 (doc, "hello world", 11))

    def mk_fact(ft, life="CANDIDATE"):
        return conn.execute(
            "INSERT INTO facts (document_id, page_index, subject_raw, predicate, object_raw, "
            "fact_type, lifecycle_state, created_at) VALUES (?,0,?,?,?,?,?,?)",
            (doc, "s", "p", "o", ft, life, NOW),
        ).lastrowid

    for ft in ("numeric", "semantic", "temporal", "categorical"):
        mk_fact(ft)
    mk_fact("numeric", "RAW")
    with pytest.raises(sqlite3.IntegrityError):
        mk_fact("bogus")
    with pytest.raises(sqlite3.IntegrityError):
        mk_fact("numeric", "NOT_A_STATE")

    fid = mk_fact("semantic")
    # start >= end rejected
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO evidence (fact_id, document_id, page_index, char_start, char_end, "
            "quote, created_at) VALUES (?,?,0,5,5,?,?)",
            (fid, doc, "q", NOW),
        )
    # 'unavailable' verification method accepted
    conn.execute(
        "INSERT INTO evidence (fact_id, document_id, page_index, verification_method, quote, "
        "created_at) VALUES (?,?,0,'unavailable',?,?)",
        (fid, doc, "q", NOW),
    )
    # the two Phase-1 cross-column invariants survive the rebuild
    f2 = mk_fact("semantic")  # still UNVERIFIED
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE facts SET reasoning_eligible = 1 WHERE id = ?", (f2,))
    f3 = mk_fact("semantic", "QUARANTINED")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE facts SET reasoning_eligible = 1 WHERE id = ?", (f3,))


# --------------------------------------------------------------------------- #
# fact persistence                                                           #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("ft", ["numeric", "semantic", "temporal", "categorical"])
def test_insert_each_fact_type(conn, make_source, ft):
    src = make_source(["page zero text"])
    over = {"fact_type": ft}
    if ft == "numeric":
        over |= {"value_raw": "42 units", "numeric_value": 42.0, "value_text": None}
    fid = insert_fact(conn, _fact(src, **over))
    row = conn.execute("SELECT fact_type FROM facts WHERE id = ?", (fid,)).fetchone()
    assert row["fact_type"] == ft


def test_numeric_representation_raw_and_normalized_preserved(conn, make_source):
    src = make_source(["revenue figure page"])
    fid = insert_fact(conn, _fact(
        src, fact_type="numeric", value_text=None,
        value_raw="₹8,142 crore", numeric_value=8142.0,
        magnitude="crore", magnitude_factor=1e7, base_value=8.142e10, currency="INR",
        unit_raw="₹ crore", unit_norm="INR",
    ))
    r = conn.execute(
        "SELECT value_raw, numeric_value, magnitude, magnitude_factor, base_value, currency, "
        "unit_raw, unit_norm FROM facts WHERE id = ?", (fid,)
    ).fetchone()
    assert r["value_raw"] == "₹8,142 crore"      # source expression NOT discarded
    assert r["numeric_value"] == 8142.0
    assert r["magnitude"] == "crore"
    assert r["base_value"] == 8.142e10
    assert r["currency"] == "INR"


def test_percentage_representation(conn, make_source):
    src = make_source(["margin page"])
    fid = insert_fact(conn, _fact(
        src, fact_type="numeric", value_text=None,
        value_raw="5%", numeric_value=5.0, is_percentage=True, percentage_ratio=0.05,
        unit_raw="%", unit_norm="ratio",
    ))
    r = conn.execute(
        "SELECT value_raw, is_percentage, percentage_ratio FROM facts WHERE id = ?", (fid,)
    ).fetchone()
    assert r["value_raw"] == "5%"
    assert r["is_percentage"] == 1
    assert r["percentage_ratio"] == 0.05


def test_numeric_fact_inconsistent_base_value_rejected():
    with pytest.raises(ValidationError):
        FactIn(
            document_id=1, page_index=0, subject_raw="s", predicate="p", object_raw="o",
            fact_type="numeric", numeric_value=100.0, magnitude_factor=1e6, base_value=999.0,
        )


def test_numeric_fact_without_value_rejected():
    with pytest.raises(ValidationError):
        FactIn(document_id=1, page_index=0, subject_raw="s", predicate="p",
               object_raw="o", fact_type="numeric")


def test_raw_payload_roundtrip(conn, make_source):
    src = make_source(["p"])
    payload = {"subject": "Acme", "raw": "₹8,142 crore", "confidence": None}
    fid = insert_fact(conn, _fact(src, raw_payload=payload))
    import json
    stored = conn.execute("SELECT raw_payload FROM facts WHERE id = ?", (fid,)).fetchone()[0]
    assert json.loads(stored) == payload


def test_fact_fts_populated(conn, make_source):
    src = make_source(["p"])
    fid = insert_fact(conn, _fact(src, predicate="net revenue"))
    hit = conn.execute(
        "SELECT fact_id FROM facts_fts WHERE facts_fts MATCH ?", ("revenue",)
    ).fetchone()
    assert hit["fact_id"] == fid


def test_insert_fact_unknown_document(conn):
    with pytest.raises(FactError) as e:
        insert_fact(conn, FactIn(document_id=9999, page_index=0, subject_raw="s",
                                 predicate="p", object_raw="o", fact_type="semantic"))
    assert e.value.code == "unknown_document"


def test_insert_fact_page_not_in_document(conn, make_source):
    src = make_source(["only page 0"])
    with pytest.raises(FactError) as e:
        insert_fact(conn, _fact(src, page=7))
    assert e.value.code == "page_not_in_document"


# --------------------------------------------------------------------------- #
# evidence + chain                                                           #
# --------------------------------------------------------------------------- #
def test_valid_evidence_chain(conn, make_source):
    src = make_source(["Alpha page zero text", "Bravo page one text"])
    fid = insert_fact(conn, _fact(src, page=1))
    eid = attach_evidence(conn, fid, EvidenceIn(
        document_id=src["document_id"], page_index=1, chunk_id=src["chunk_ids"][1],
        quote="Bravo", char_start=0, char_end=5,
        verification_method="exact", evidence_status="VERIFIED",
    ))
    chain = evidence_chain(conn, fid)
    assert chain == {
        "fact_id": fid, "evidence_id": eid,
        "chunk_id": src["chunk_ids"][1], "page_id": src["page_ids"][1],
        "document_id": src["document_id"],
    }
    # evidence_status mirrored onto the fact
    assert conn.execute(
        "SELECT evidence_status FROM facts WHERE id = ?", (fid,)
    ).fetchone()["evidence_status"] == "VERIFIED"


def test_evidence_page_not_in_document(conn, make_source):
    src = make_source(["only one page"])
    fid = insert_fact(conn, _fact(src))
    with pytest.raises(FactError) as e:
        attach_evidence(conn, fid, EvidenceIn(
            document_id=src["document_id"], page_index=99, quote="x"))
    assert e.value.code == "page_not_in_document"


def test_evidence_chunk_not_in_page(conn, make_source):
    src = make_source(["page zero", "page one"])
    fid = insert_fact(conn, _fact(src, page=0))
    with pytest.raises(FactError) as e:
        attach_evidence(conn, fid, EvidenceIn(
            document_id=src["document_id"], page_index=0,
            chunk_id=src["chunk_ids"][1],  # chunk belongs to page 1, not page 0
            quote="page"))
    assert e.value.code == "chunk_not_in_page"


def test_evidence_offsets_out_of_range(conn, make_source):
    src = make_source(["short"])  # len 5
    fid = insert_fact(conn, _fact(src))
    with pytest.raises(FactError) as e:
        attach_evidence(conn, fid, EvidenceIn(
            document_id=src["document_id"], page_index=0, quote="short",
            char_start=0, char_end=99))
    assert e.value.code == "invalid_offsets"


def test_evidence_start_not_before_end_rejected_at_model():
    with pytest.raises(ValidationError):
        EvidenceIn(document_id=1, page_index=0, quote="q", char_start=5, char_end=5)


def test_evidence_offsets_must_be_paired():
    with pytest.raises(ValidationError):
        EvidenceIn(document_id=1, page_index=0, quote="q", char_start=0)


def test_attach_evidence_twice_rejected(conn, make_source):
    src = make_source(["some page text here"])
    fid = insert_fact(conn, _fact(src))
    attach_evidence(conn, fid, EvidenceIn(
        document_id=src["document_id"], page_index=0, quote="some"))
    with pytest.raises(FactError) as e:
        attach_evidence(conn, fid, EvidenceIn(
            document_id=src["document_id"], page_index=0, quote="page"))
    assert e.value.code == "evidence_exists"


def test_evidence_document_mismatch(conn, make_source):
    a = make_source(["doc a page"])
    b = make_source(["doc b page"])
    fid = insert_fact(conn, _fact(a))
    with pytest.raises(FactError) as e:
        attach_evidence(conn, fid, EvidenceIn(
            document_id=b["document_id"], page_index=0, quote="doc"))
    assert e.value.code == "evidence_document_mismatch"


# --------------------------------------------------------------------------- #
# lifecycle + the evidence invariant                                         #
# --------------------------------------------------------------------------- #
def test_unguarded_lifecycle_states(conn, make_source):
    src = make_source(["p"])
    fid = insert_fact(conn, _fact(src))
    for state in ("RAW", "CANDIDATE", "GROUNDED", "NORMALIZED"):
        set_lifecycle(conn, fid, state)
        assert conn.execute(
            "SELECT lifecycle_state FROM facts WHERE id = ?", (fid,)
        ).fetchone()["lifecycle_state"] == state


def test_invalid_lifecycle_state_rejected(conn, make_source):
    src = make_source(["p"])
    fid = insert_fact(conn, _fact(src))
    with pytest.raises(FactError) as e:
        set_lifecycle(conn, fid, "BOGUS")
    assert e.value.code == "invalid_lifecycle_state"


def test_missing_evidence_blocks_eligibility(conn, make_source):
    src = make_source(["p"])
    fid = insert_fact(conn, _fact(src))
    with pytest.raises(FactError) as e:
        mark_reasoning_eligible(conn, fid)
    assert e.value.code == "evidence_required"
    row = conn.execute(
        "SELECT lifecycle_state, reasoning_eligible FROM facts WHERE id = ?", (fid,)
    ).fetchone()
    assert row["lifecycle_state"] != "ELIGIBLE_FOR_REASONING"
    assert row["reasoning_eligible"] == 0


def test_unverified_evidence_blocks_eligibility(conn, make_source):
    src = make_source(["some page text"])
    fid = insert_fact(conn, _fact(src))
    attach_evidence(conn, fid, EvidenceIn(
        document_id=src["document_id"], page_index=0, quote="some",
        verification_method="unverified", evidence_status="UNVERIFIED"))
    with pytest.raises(FactError) as e:
        mark_reasoning_eligible(conn, fid)
    assert e.value.code == "evidence_unverified"
    assert conn.execute(
        "SELECT reasoning_eligible FROM facts WHERE id = ?", (fid,)
    ).fetchone()["reasoning_eligible"] == 0


def test_grounded_fact_with_valid_evidence_becomes_eligible(conn, make_source):
    src = make_source(["evidence bearing page text"])
    fid = _grounded_eligible_fact(conn, src)
    row = conn.execute(
        "SELECT lifecycle_state, reasoning_eligible FROM facts WHERE id = ?", (fid,)
    ).fetchone()
    assert row["lifecycle_state"] == "ELIGIBLE_FOR_REASONING"
    assert row["reasoning_eligible"] == 1


def test_partial_evidence_can_become_eligible(conn, make_source):
    src = make_source(["fuzzy match page text"])
    fid = insert_fact(conn, _fact(src))
    attach_evidence(conn, fid, EvidenceIn(
        document_id=src["document_id"], page_index=0, chunk_id=src["chunk_ids"][0],
        quote="fuzzy", char_start=0, char_end=5,
        verification_method="fuzzy", fuzzy_score=0.93, evidence_status="PARTIAL"))
    mark_reasoning_eligible(conn, fid)
    assert conn.execute(
        "SELECT reasoning_eligible FROM facts WHERE id = ?", (fid,)
    ).fetchone()["reasoning_eligible"] == 1


def test_quarantine_sets_state_and_blocks_eligibility(conn, make_source):
    src = make_source(["p"])
    fid = insert_fact(conn, _fact(src))
    quarantine_fact(conn, fid, "extractor produced an unsupported claim")
    row = conn.execute(
        "SELECT lifecycle_state, reasoning_eligible, quarantine_reason FROM facts WHERE id = ?",
        (fid,),
    ).fetchone()
    assert row["lifecycle_state"] == "QUARANTINED"
    assert row["reasoning_eligible"] == 0
    assert "unsupported" in row["quarantine_reason"]
    with pytest.raises(FactError) as e:
        mark_reasoning_eligible(conn, fid)
    assert e.value.code == "fact_quarantined"


def test_set_lifecycle_quarantine_needs_dedicated_helper(conn, make_source):
    src = make_source(["p"])
    fid = insert_fact(conn, _fact(src))
    with pytest.raises(FactError) as e:
        set_lifecycle(conn, fid, "QUARANTINED")
    assert e.value.code == "use_quarantine_fact"


# --------------------------------------------------------------------------- #
# relationships (storage only)                                               #
# --------------------------------------------------------------------------- #
def test_all_five_relationship_categories_persist(conn, make_source):
    src = make_source(["page for relationship facts " * 3])
    anchor = _grounded_eligible_fact(conn, src)
    cats = ["CORROBORATES", "CONTRADICTS", "DIFFERENT_CONTEXT", "TEMPORAL_EVOLUTION", "UNCERTAIN"]
    for cat in cats:
        other = _grounded_eligible_fact(conn, src, predicate=f"metric {cat}")
        add_relationship(conn, RelationshipIn(
            fact_a_id=anchor, fact_b_id=other, category=cat, reasoning=f"stored {cat}"))
    stored = {
        r["category"]
        for r in conn.execute("SELECT category FROM relationships")
    }
    assert stored == set(cats)


def test_invalid_relationship_category_rejected():
    with pytest.raises(ValidationError):
        RelationshipIn(fact_a_id=1, fact_b_id=2, category="RECONCILES")


def test_self_relationship_rejected(conn, make_source):
    src = make_source(["p " * 5])
    fid = _grounded_eligible_fact(conn, src)
    with pytest.raises(FactError) as e:
        add_relationship(conn, RelationshipIn(
            fact_a_id=fid, fact_b_id=fid, category="CORROBORATES"))
    assert e.value.code == "self_relationship"


def test_relationship_pair_stored_canonically(conn, make_source):
    src = make_source(["p " * 5])
    f1 = _grounded_eligible_fact(conn, src)
    f2 = _grounded_eligible_fact(conn, src, predicate="other metric")
    lo, hi = sorted((f1, f2))
    add_relationship(conn, RelationshipIn(
        fact_a_id=hi, fact_b_id=lo, category="CORROBORATES"))  # reversed on input
    row = conn.execute("SELECT fact_a_id, fact_b_id FROM relationships").fetchone()
    assert (row["fact_a_id"], row["fact_b_id"]) == (lo, hi)


def test_duplicate_relationship_is_deterministic_noop(conn, make_source):
    src = make_source(["p " * 5])
    f1 = _grounded_eligible_fact(conn, src)
    f2 = _grounded_eligible_fact(conn, src, predicate="other metric")
    rid1 = add_relationship(conn, RelationshipIn(
        fact_a_id=f1, fact_b_id=f2, category="CORROBORATES"))
    rid2 = add_relationship(conn, RelationshipIn(
        fact_a_id=f2, fact_b_id=f1, category="CONTRADICTS"))  # same pair, different category
    assert rid1 == rid2
    rows = conn.execute("SELECT category FROM relationships").fetchall()
    assert len(rows) == 1
    assert rows[0]["category"] == "CORROBORATES"  # first write wins


def test_relationship_requires_reasoning_eligible_facts(conn, make_source):
    src = make_source(["p " * 5])
    f1 = insert_fact(conn, _fact(src))                 # CANDIDATE, not eligible
    f2 = _grounded_eligible_fact(conn, src, predicate="other")
    with pytest.raises(FactError) as e:
        add_relationship(conn, RelationshipIn(
            fact_a_id=f1, fact_b_id=f2, category="CORROBORATES"))
    assert e.value.code == "not_reasoning_eligible"


def test_relationship_unknown_fact(conn, make_source):
    src = make_source(["p " * 5])
    f1 = _grounded_eligible_fact(conn, src)
    with pytest.raises(FactError) as e:
        add_relationship(conn, RelationshipIn(
            fact_a_id=f1, fact_b_id=999999, category="CORROBORATES"))
    assert e.value.code == "unknown_fact"
