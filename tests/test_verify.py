"""Phase 5 — evidence verification + quarantine. Fully deterministic, no LLM.

Synthetic source rows (make_source) + hand-built candidate facts/evidence, plus a
real-PDF deterministic check (test_real_pdf_verification).
"""

from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from app import db
from app.facts import FactError, evidence_chain, mark_reasoning_eligible
from app.models import FactIn
from app.verify import (
    VerifyError,
    _check_numeric_consistency,
    _digits_of,
    _run_ladder,
    normalize_for_compare,
    verification_summary,
    verify_document,
    verify_fact,
)
from tests.conftest import PdfFactory  # noqa: F401  (fixture typing only)

NOW = "2026-01-01T00:00:00Z"


def _mkfact(conn, src, *, quote, cs, ce, page=0, fact_type="semantic",
            ev_quote=None, **over) -> int:
    """Insert one CANDIDATE fact + its (unverified) candidate-evidence row.
    Evidence is inserted via raw SQL so tests can supply deliberately wrong
    offsets. ``cs``/``ce`` are page-relative (make_source chunks start at 0)."""
    over.setdefault("object_raw", "some object")
    if fact_type == "numeric":
        over.setdefault("value_raw", "42 units")
        over.setdefault("numeric_value", 42.0)
        over["value_text"] = None
    else:
        over.setdefault("value_text", over["object_raw"])
    fid = insert_fact_(conn, FactIn(
        document_id=src["document_id"], page_index=page, subject_raw="S",
        predicate="p", fact_type=fact_type, lifecycle_state="CANDIDATE",
        evidence_status="UNVERIFIED", **over,
    ))
    conn.execute(
        "INSERT INTO evidence (fact_id, document_id, page_index, page_id, chunk_id, "
        "char_start, char_end, quote, method, verification_method, numeric_rederivation, "
        "evidence_status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?, 'text_layer','unverified','not_applicable','UNVERIFIED',?)",
        (fid, src["document_id"], page, src["page_ids"][page], src["chunk_ids"][page],
         cs, ce, ev_quote if ev_quote is not None else quote, NOW),
    )
    conn.commit()
    return fid


def insert_fact_(conn, fin):
    from app.facts import insert_fact
    return insert_fact(conn, fin)


# --------------------------------------------------------------------------- #
# migration                                                                  #
# --------------------------------------------------------------------------- #
def test_migration_3_applied(conn):
    assert db.schema_version(conn) == 3
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(evidence)")}
    assert "verified_at" in cols
    # recovered_exact now accepted
    conn.execute("INSERT INTO documents (sha256, stored_path, uploaded_at) VALUES ('m'||?, 'p', ?)",
                 ("x" * 60, NOW))
    doc = conn.execute("SELECT id FROM documents").fetchone()["id"]
    conn.execute("INSERT INTO pages (document_id, page_index, text) VALUES (?, 0, 'txt')", (doc,))
    fid = conn.execute(
        "INSERT INTO facts (document_id, page_index, subject_raw, predicate, object_raw, "
        "fact_type, created_at) VALUES (?,0,'s','p','o','semantic',?)", (doc, NOW),
    ).lastrowid
    conn.execute(
        "INSERT INTO evidence (fact_id, document_id, page_index, quote, verification_method, "
        "created_at) VALUES (?,?,0,'q','recovered_exact',?)", (fid, doc, NOW),
    )
    assert db.schema_version(conn) == 3


def test_migration_3_from_phase4_state(db_path):
    c = db.connect()
    c.executescript(db.SCHEMA_PATH.read_text())
    for _v, sql in db._MIGRATIONS[:2]:
        c.executescript(sql)
    c.execute("PRAGMA user_version = 2")
    c.commit()
    c.close()
    db.init_db()
    c = db.connect()
    try:
        assert db.schema_version(c) == 3
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        c.close()


def test_migration_3_idempotent(db_path):
    db.init_db()
    db.init_db()
    assert db.schema_version(db.connect()) == 3


# --------------------------------------------------------------------------- #
# text helpers                                                               #
# --------------------------------------------------------------------------- #
def test_normalize_is_formatting_only():
    assert normalize_for_compare("₹  8,142\n crore") == "₹ 8,142 crore"
    assert normalize_for_compare("“Rs 42”") == '"Rs 42"'
    # value-changing folds must NOT happen
    assert normalize_for_compare("8,142") != normalize_for_compare("8142")
    assert normalize_for_compare("8142 lakh") != normalize_for_compare("8142 crore")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("₹8,142 crore", 8142.0), ("5%", 5.0), ("42 units", 42.0),
     ("1.4 Mn Tons", 1.4), ("(452)", -452.0), ("two numbers 1 and 2", None)],
)
def test_digits_of(raw, expected):
    assert _digits_of(raw) == expected


# --------------------------------------------------------------------------- #
# exact / normalized / recovered                                             #
# --------------------------------------------------------------------------- #
def test_exact_offsets_verified(conn, db_path, make_source):
    src = make_source(["The company reported strong results this period."])
    fid = _mkfact(conn, src, quote="reported strong results", cs=12, ce=35)
    r = verify_fact(conn, fid)
    assert r.evidence_status == "VERIFIED"
    assert r.verification_method == "exact"
    assert r.lifecycle_state == "GROUNDED"
    ev = conn.execute("SELECT verification_method, evidence_status, verified_at FROM evidence "
                      "WHERE fact_id = ?", (fid,)).fetchone()
    assert ev["verification_method"] == "exact"
    assert ev["evidence_status"] == "VERIFIED"
    assert ev["verified_at"]


@pytest.mark.parametrize(
    "span",
    [
        "Revenue rose  by  ten  per  cent",
        "Revenue rose\nby ten per\ncent",
        "Revenue rose\u00a0by ten\u2009per cent",
        "\u201cRevenue rose by ten per cent\u201d",
    ],
)
def test_normalized_exact(conn, db_path, make_source, span):
    page = f"AAAA {span} BBBB"
    src = make_source([page])
    s = page.index(span)
    quote = normalize_for_compare(span)          # the "cleaned" quote an LLM emits
    fid = _mkfact(conn, src, quote=quote, cs=s, ce=s + len(span))   # correct offsets
    r = verify_fact(conn, fid)
    assert r.evidence_status == "VERIFIED"
    assert r.verification_method == "normalized_exact"


def test_recovered_exact_corrects_span(conn, db_path, make_source):
    src = make_source(["xxxx Revenue grew year on year yyyy"])
    quote = "Revenue grew year on year"
    fid = _mkfact(conn, src, quote=quote, cs=0, ce=4)  # points at "xxxx"
    r = verify_fact(conn, fid)
    assert r.verification_method == "recovered_exact"
    assert r.recovered is True
    ev = conn.execute("SELECT char_start, char_end, quote FROM evidence WHERE fact_id = ?",
                      (fid,)).fetchone()
    assert ev["quote"] == quote                    # claim/quote untouched
    assert src["page_texts"][0][ev["char_start"]:ev["char_end"]] == quote  # span corrected


def test_recovered_exact_is_page_relative_within_chunk(conn, db_path, make_source):
    src = make_source(["PAD PAD PAD Net income increased sharply END"])
    doc = src["document_id"]
    conn.execute("DELETE FROM chunks WHERE document_id = ?", (doc,))
    pt = src["page_texts"][0]
    conn.execute(
        "INSERT INTO chunks (document_id, page_index, seq, char_offset, char_end, text) "
        "VALUES (?, 0, 0, 12, ?, ?)", (doc, len(pt), pt[12:]),
    )
    conn.commit()
    fid = _mkfact(conn, src, quote="Net income increased sharply", cs=0, ce=3)
    r = verify_fact(conn, fid)
    assert r.verification_method == "recovered_exact"
    ev = conn.execute(
        "SELECT char_start, char_end FROM evidence WHERE fact_id = ?", (fid,)
    ).fetchone()
    assert pt[ev["char_start"]:ev["char_end"]] == "Net income increased sharply"
    assert ev["char_start"] == 12                  # chunk_base + local index


def test_ambiguous_quote_not_guessed(conn, db_path, make_source):
    src = make_source(["We saw sales fell sharply here and sales fell sharply again there."])
    fid = _mkfact(conn, src, quote="sales fell sharply", cs=0, ce=6)
    r = verify_fact(conn, fid)
    assert r.evidence_status == "UNVERIFIED"
    assert r.reason == "ambiguous_quote_match"
    assert r.lifecycle_state == "QUARANTINED"


# --------------------------------------------------------------------------- #
# failure cases                                                              #
# --------------------------------------------------------------------------- #
def test_quote_absent_quarantined(conn, db_path, make_source):
    src = make_source(["This page says nothing about the claimed figure at all here."])
    fid = _mkfact(conn, src, quote="a totally fabricated sentence not present", cs=0, ce=10)
    r = verify_fact(conn, fid)
    assert r.evidence_status == "UNVERIFIED"
    assert r.reason == "quote_not_found"
    row = conn.execute("SELECT lifecycle_state, reasoning_eligible, evidence_status FROM facts "
                       "WHERE id = ?", (fid,)).fetchone()
    assert row["lifecycle_state"] == "QUARANTINED"
    assert row["reasoning_eligible"] == 0
    assert row["evidence_status"] == "UNVERIFIED"
    # the failure is recorded, not deleted
    assert conn.execute(
        "SELECT COUNT(*) FROM failures WHERE ref_id = ? AND failure_type = 'grounding_failed'",
        (fid,),
    ).fetchone()[0] == 1


@pytest.mark.parametrize("cs,ce", [(-5, -1), (5000, 6000)])
def test_out_of_range_offsets_fall_through(conn, db_path, make_source, cs, ce):
    # quote present -> recovers; quote absent -> quarantine. Either way no crash.
    src = make_source(["prefix Operating margin improved materially suffix"])
    fid = _mkfact(conn, src, quote="Operating margin improved materially", cs=cs, ce=ce)
    r = verify_fact(conn, fid)
    assert r.verification_method == "recovered_exact"
    assert r.evidence_status == "VERIFIED"


def test_none_offsets(conn, db_path, make_source):
    src = make_source(["Headcount reduced by two hundred over the year overall."])
    fid = _mkfact(conn, src, quote="Headcount reduced by two hundred", cs=None, ce=None)
    r = verify_fact(conn, fid)
    assert r.verification_method == "recovered_exact"  # unique in the chunk


def test_source_unavailable(conn, db_path, make_source):
    src = make_source([""])  # EMPTY page
    fid = _mkfact(conn, src, quote="anything", cs=0, ce=3)
    r = verify_fact(conn, fid)
    assert r.reason == "source_unavailable"
    assert r.lifecycle_state == "QUARANTINED"


def test_missing_evidence_row(conn, db_path, make_source):
    src = make_source(["Some ordinary prose on the page."])
    fid = insert_fact_(conn, FactIn(
        document_id=src["document_id"], page_index=0, subject_raw="s", predicate="p",
        object_raw="o", fact_type="semantic", value_text="o", lifecycle_state="CANDIDATE",
    ))
    conn.commit()
    r = verify_fact(conn, fid)
    assert r.reason == "unsupported_evidence"
    assert r.evidence_id is None
    assert r.lifecycle_state == "QUARANTINED"
    got = conn.execute("SELECT COUNT(*) FROM evidence WHERE fact_id = ?", (fid,)).fetchone()[0]
    assert got == 0


def test_reversed_offsets_blocked_by_check(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO evidence (fact_id, document_id, page_index, char_start, char_end, "
            "quote, created_at) VALUES (1, 1, 0, 9, 3, 'q', ?)", (NOW,),
        )


# --------------------------------------------------------------------------- #
# numeric consistency (§9, §13)                                              #
# --------------------------------------------------------------------------- #
def test_numeric_consistent(conn, db_path, make_source):
    src = make_source(["Revenue was Rs 8,142 crore for the year in review."])
    fid = _mkfact(conn, src, quote="Rs 8,142 crore", cs=12, ce=26, fact_type="numeric",
                  value_raw="Rs 8,142 crore", numeric_value=8142.0, magnitude="crore",
                  currency="INR", object_raw="Rs 8,142 crore")
    r = verify_fact(conn, fid)
    assert r.evidence_status == "VERIFIED"
    assert r.numeric_rederivation == "success"
    assert r.lifecycle_state == "GROUNDED"


def test_numeric_parsed_value_wrong(conn, db_path, make_source):
    src = make_source(["Revenue was Rs 8,142 crore for the year in review."])
    fid = _mkfact(conn, src, quote="Rs 8,142 crore", cs=12, ce=26, fact_type="numeric",
                  value_raw="Rs 8,142 crore", numeric_value=81420.0, magnitude="crore",
                  currency="INR", object_raw="Rs 8,142 crore")
    r = verify_fact(conn, fid)
    assert r.evidence_status == "UNVERIFIED"
    assert r.reason == "numeric_mismatch"
    assert r.numeric_rederivation == "failed"
    row = conn.execute("SELECT lifecycle_state, numeric_value, value_raw FROM facts WHERE id = ?",
                       (fid,)).fetchone()
    assert row["lifecycle_state"] == "QUARANTINED"
    assert row["numeric_value"] == 81420.0          # CLAIM NOT REPAIRED
    assert row["value_raw"] == "Rs 8,142 crore"


def test_numeric_changed_number(conn, db_path, make_source):
    src = make_source(["Revenue was 8142 crore for the year, up sharply on last year."])
    # candidate quote + raw_value_text carry the wrong number 8000
    fid = _mkfact(conn, src, quote="Revenue was 8000 crore", cs=0, ce=22, fact_type="numeric",
                  value_raw="8000 crore", numeric_value=8000.0, magnitude="crore",
                  object_raw="8000 crore")
    r = verify_fact(conn, fid)
    assert r.evidence_status == "UNVERIFIED"           # '8000' isn't in the source
    row = conn.execute("SELECT lifecycle_state FROM facts WHERE id = ?", (fid,)).fetchone()
    assert row["lifecycle_state"] == "QUARANTINED"


@pytest.mark.parametrize(
    ("over", "page", "quote"),
    [
        ({"currency": "USD"}, "Revenue was Rs 8,142 crore this year overall.", "Rs 8,142 crore"),
        ({"magnitude": "lakh"}, "Revenue was Rs 8,142 crore this year overall.", "Rs 8,142 crore"),
        ({"unit_raw": "tonnes"}, "Volume reached 8,142 units this year overall.", "8,142 units"),
    ],
)
def test_numeric_changed_token_quarantined(conn, db_path, make_source, over, page, quote):
    src = make_source([page])
    s = page.index(quote)
    fid = _mkfact(conn, src, quote=quote, cs=s, ce=s + len(quote), fact_type="numeric",
                  value_raw=quote, numeric_value=8142.0, object_raw=quote, **over)
    r = verify_fact(conn, fid)
    assert r.evidence_status == "UNVERIFIED"
    assert r.reason == "numeric_mismatch"


def test_numeric_changed_percentage(conn, db_path, make_source):
    src = make_source(["Margin improved to 5% during the period under review overall."])
    fid = _mkfact(conn, src, quote="5%", cs=18, ce=20, fact_type="numeric",
                  value_raw="5%", numeric_value=5.0, is_percentage=True,
                  percentage_ratio=0.5, object_raw="5%")   # 0.5 != 5 and != 0.05
    r = verify_fact(conn, fid)
    assert r.evidence_status == "UNVERIFIED"
    assert r.reason == "numeric_mismatch"


def test_check_numeric_consistency_unit():
    class Row(dict):
        __getitem__ = dict.get
    from app.config import get_settings
    s = get_settings()
    ok, _ = _check_numeric_consistency(
        Row(value_raw="8,142 crore", numeric_value=8142.0, is_percentage=0,
            percentage_ratio=None, currency=None, magnitude="crore", unit_raw=None),
        "revenue of 8142 crore", s,
    )
    assert ok is True
    bad, why = _check_numeric_consistency(
        Row(value_raw="8,142 crore", numeric_value=99.0, is_percentage=0,
            percentage_ratio=None, currency=None, magnitude="crore", unit_raw=None),
        "revenue of 8142 crore", s,
    )
    assert bad is False and "parsed_value" in why


# --------------------------------------------------------------------------- #
# lifecycle + provenance                                                     #
# --------------------------------------------------------------------------- #
def test_verified_fact_can_become_reasoning_eligible(conn, db_path, make_source):
    src = make_source(["The board approved the annual dividend at its recent meeting."])
    fid = _mkfact(conn, src, quote="approved the annual dividend", cs=10, ce=37)
    verify_fact(conn, fid)
    mark_reasoning_eligible(conn, fid)
    row = conn.execute("SELECT lifecycle_state, reasoning_eligible FROM facts WHERE id = ?",
                       (fid,)).fetchone()
    assert row["lifecycle_state"] == "ELIGIBLE_FOR_REASONING"
    assert row["reasoning_eligible"] == 1


def test_unverified_fact_cannot_become_eligible(conn, db_path, make_source):
    src = make_source(["Prose that does not contain the claimed statement at all."])
    fid = _mkfact(conn, src, quote="a claim absent from the page", cs=0, ce=5)
    verify_fact(conn, fid)
    with pytest.raises(FactError) as e:
        mark_reasoning_eligible(conn, fid)
    assert e.value.code in ("fact_quarantined", "evidence_unverified")
    got = conn.execute("SELECT reasoning_eligible FROM facts WHERE id = ?", (fid,)).fetchone()[0]
    assert got == 0


def test_db_check_blocks_manual_eligible_after_failed_verification(conn, db_path, make_source):
    src = make_source(["Prose lacking the claimed statement entirely on this page."])
    fid = _mkfact(conn, src, quote="claim absent from this page text", cs=0, ce=5)
    verify_fact(conn, fid)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE facts SET reasoning_eligible = 1 WHERE id = ?", (fid,))


def test_quarantine_preserves_extraction_payload(conn, db_path, make_source):
    src = make_source(["Ordinary prose with nothing matching the claim on the page."])
    fid = insert_fact_(conn, FactIn(
        document_id=src["document_id"], page_index=0, subject_raw="s", predicate="p",
        object_raw="o", fact_type="semantic", value_text="o", lifecycle_state="CANDIDATE",
        raw_payload={"subject": "s", "quote": "claimed but absent phrase here"},
    ))
    conn.execute(
        "INSERT INTO evidence (fact_id, document_id, page_index, page_id, chunk_id, char_start, "
        "char_end, quote, method, verification_method, numeric_rederivation, evidence_status, "
        "created_at) VALUES (?,?,0,?,?,0,5,'claimed but absent phrase here','text_layer',"
        "'unverified','not_applicable','UNVERIFIED',?)",
        (fid, src["document_id"], src["page_ids"][0], src["chunk_ids"][0], NOW),
    )
    conn.commit()
    verify_fact(conn, fid)
    import json
    row = conn.execute(
        "SELECT lifecycle_state, raw_payload FROM facts WHERE id = ?", (fid,)
    ).fetchone()
    assert row["lifecycle_state"] == "QUARANTINED"
    assert json.loads(row["raw_payload"])["quote"] == "claimed but absent phrase here"


def test_provenance_chain_preserved(conn, db_path, make_source):
    src = make_source(["The report notes a material improvement in working capital."])
    fid = _mkfact(conn, src, quote="a material improvement in working capital", cs=16, ce=56)
    verify_fact(conn, fid)
    chain = evidence_chain(conn, fid)
    assert chain is not None
    assert chain["document_id"] == src["document_id"]
    assert chain["page_id"] == src["page_ids"][0]
    assert chain["chunk_id"] == src["chunk_ids"][0]


# --------------------------------------------------------------------------- #
# idempotency                                                                #
# --------------------------------------------------------------------------- #
def test_verify_fact_is_idempotent(conn, db_path, make_source):
    src = make_source(["Total assets grew over the course of the financial year here."])
    fid = _mkfact(conn, src, quote="Total assets grew", cs=0, ce=17)
    r1 = verify_fact(conn, fid)
    ev1 = conn.execute("SELECT * FROM evidence WHERE fact_id = ?", (fid,)).fetchone()
    r2 = verify_fact(conn, fid)
    ev2 = conn.execute("SELECT * FROM evidence WHERE fact_id = ?", (fid,)).fetchone()
    assert (r1.evidence_status, r1.verification_method) == \
        (r2.evidence_status, r2.verification_method)
    assert ev1["id"] == ev2["id"]
    n = conn.execute("SELECT COUNT(*) FROM evidence WHERE fact_id = ?", (fid,)).fetchone()[0]
    assert n == 1


def test_verify_fact_idempotent_for_quarantine(conn, db_path, make_source):
    src = make_source(["Page prose with no trace of the claimed sentence anywhere."])
    fid = _mkfact(conn, src, quote="a sentence that is simply not here", cs=0, ce=5)
    verify_fact(conn, fid)
    verify_fact(conn, fid)
    assert conn.execute(
        "SELECT COUNT(*) FROM failures WHERE ref_id = ? AND failure_type = 'grounding_failed'",
        (fid,),
    ).fetchone()[0] == 1   # not duplicated
    n = conn.execute("SELECT COUNT(*) FROM evidence WHERE fact_id = ?", (fid,)).fetchone()[0]
    assert n == 1


# --------------------------------------------------------------------------- #
# batch                                                                      #
# --------------------------------------------------------------------------- #
def test_verify_document_summary_and_isolation(conn, db_path, make_source, monkeypatch):
    src = make_source(["Alpha profit rose. Beta cost fell. Gamma revenue held steady overall."])
    good = _mkfact(conn, src, quote="Alpha profit rose", cs=0, ce=17)
    absent = _mkfact(conn, src, quote="Delta something entirely fabricated here", cs=0, ce=5)
    numeric_ok = _mkfact(conn, src, quote="revenue held steady", cs=32, ce=51,
                         fact_type="numeric", value_raw="steady", numeric_value=None,
                         object_raw="revenue held steady")

    real = verify_fact
    boom_id = absent

    def flaky(conn_, fid, **kw):
        if fid == boom_id:
            raise RuntimeError("kaboom in verify_fact")
        return real(conn_, fid, **kw)

    monkeypatch.setattr("app.verify.verify_fact", flaky)
    summ = verify_document(src["document_id"], database_path=db_path)
    assert summ.status == "done"
    assert summ.examined == 3
    assert summ.errors == 1                       # the flaky one, isolated
    assert summ.verified >= 1                     # good + numeric_ok still processed
    row = conn.execute("SELECT lifecycle_state FROM facts WHERE id = ?", (good,)).fetchone()
    assert row["lifecycle_state"] == "GROUNDED"
    assert conn.execute("SELECT 1 FROM facts WHERE id = ?", (numeric_ok,)).fetchone()


def test_verify_document_unknown(db_path):
    db.init_db()
    with pytest.raises(VerifyError) as e:
        verify_document(999999, database_path=db_path)
    assert e.value.code == "unknown_document"


def test_verification_summary(conn, db_path, make_source):
    src = make_source(["Revenue was Rs 8,142 crore. A separate fabricated line here too."])
    _mkfact(conn, src, quote="Rs 8,142 crore", cs=12, ce=26, fact_type="numeric",
            value_raw="Rs 8,142 crore", numeric_value=8142.0, magnitude="crore",
            currency="INR", object_raw="Rs 8,142 crore")
    _mkfact(conn, src, quote="a line that does not appear on the page", cs=0, ce=5)
    verify_document(src["document_id"], database_path=db_path)
    s = verification_summary(conn, document_id=src["document_id"])
    assert s["evidence_by_method"].get("exact", 0) >= 1
    assert s["evidence_by_status"].get("VERIFIED", 0) >= 1
    assert s["evidence_by_status"].get("UNVERIFIED", 0) >= 1
    assert s["facts_by_lifecycle"].get("GROUNDED", 0) >= 1
    assert s["facts_by_lifecycle"].get("QUARANTINED", 0) >= 1
    assert "quote_not_found" in s["quarantine_reasons"]


# --------------------------------------------------------------------------- #
# _run_ladder direct (reversed / non-storable cases)                         #
# --------------------------------------------------------------------------- #
def test_run_ladder_reversed_offsets_via_dict():
    fact = {"fact_type": "semantic", "value_raw": None, "numeric_value": None,
            "magnitude": None, "currency": None, "is_percentage": 0,
            "percentage_ratio": None, "unit_raw": None}
    ev = {"quote": "the reported figure", "char_start": 20, "char_end": 5}
    from app.config import get_settings
    out = _run_ladder(fact, ev, "abc the reported figure def", 0,
                      "abc the reported figure def", get_settings())
    assert out.verification_method == "recovered_exact"   # offsets ignored, quote found once


# --------------------------------------------------------------------------- #
# real starter PDF (deterministic; no LLM)                                   #
# --------------------------------------------------------------------------- #
def test_real_pdf_verification(conn, db_path):
    import pathlib
    pdf = pathlib.Path("starter-datasets/delhivery/03-delhivery-q4-fy24-earnings-presentation.pdf")
    if not pdf.exists():
        pytest.skip("starter PDF not present")
    from app.ingest import ingest_pdf
    ing = ingest_pdf(str(pdf))
    doc = ing.document_id
    rows = conn.execute(
        "SELECT p.id pid, c.page_index pi, c.id cid, c.char_offset co, c.text ct "
        "FROM pages p JOIN chunks c ON c.document_id = p.document_id "
        "AND c.page_index = p.page_index AND c.seq = 0 "
        "WHERE p.document_id = ? AND length(c.text) > 200 ORDER BY c.page_index LIMIT 3",
        (doc,),
    ).fetchall()
    assert rows, "expected some non-trivial chunks"

    made = []
    for pg in rows:
        ct = pg["ct"]
        s0 = 40
        e0 = min(s0 + 60, len(ct))
        quote = ct[s0:e0]
        fid = insert_fact_(conn, FactIn(
            document_id=doc, page_index=pg["pi"], subject_raw="doc", predicate="states",
            object_raw=quote[:40], fact_type="semantic", value_text=quote[:40],
            lifecycle_state="CANDIDATE",
        ))
        conn.execute(
            "INSERT INTO evidence (fact_id, document_id, page_index, page_id, chunk_id, "
            "char_start, char_end, quote, method, verification_method, numeric_rederivation, "
            "evidence_status, created_at) VALUES (?,?,?,?,?,?,?,?, 'text_layer','unverified',"
            "'not_applicable','UNVERIFIED',?)",
            (fid, doc, pg["pi"], pg["pid"], pg["cid"], pg["co"] + s0, pg["co"] + e0, quote, NOW),
        )
        made.append(("exact", fid))
        fid_bad = insert_fact_(conn, FactIn(
            document_id=doc, page_index=pg["pi"], subject_raw="doc", predicate="states",
            object_raw="fabricated", fact_type="semantic", value_text="fabricated",
            lifecycle_state="CANDIDATE",
        ))
        conn.execute(
            "INSERT INTO evidence (fact_id, document_id, page_index, page_id, chunk_id, "
            "char_start, char_end, quote, method, verification_method, numeric_rederivation, "
            "evidence_status, created_at) VALUES (?,?,?,?,?,0,20,"
            "'ZZ this exact sentence is definitely not in the source ZZ','text_layer',"
            "'unverified','not_applicable','UNVERIFIED',?)",
            (fid_bad, doc, pg["pi"], pg["pid"], pg["cid"], NOW),
        )
        made.append(("bad", fid_bad))
    conn.commit()

    summ = verify_document(doc, database_path=db_path)
    assert summ.status == "done"
    assert summ.examined >= len(made)
    for kind, fid in made:
        row = conn.execute(
            "SELECT lifecycle_state, evidence_status FROM facts WHERE id = ?", (fid,)
        ).fetchone()
        if kind == "exact":
            assert row["evidence_status"] in ("VERIFIED", "PARTIAL")
            assert row["lifecycle_state"] == "GROUNDED"
            assert evidence_chain(conn, fid) is not None
        else:
            assert row["evidence_status"] == "UNVERIFIED"
            assert row["lifecycle_state"] == "QUARANTINED"


def test_factin_reversed_offsets_rejected_at_model():
    with pytest.raises(ValidationError):
        FactIn(document_id=1, page_index=0, subject_raw="s", predicate="p", object_raw="o",
               fact_type="semantic", value_text="o", lifecycle_state="QUARANTINED",
               reasoning_eligible=True)
