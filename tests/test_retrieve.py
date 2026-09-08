"""Phase 8 (Stage A) — bounded candidate-pair retrieval.

Deterministic lexical + entity blocking over reasoning-eligible facts. No LLM,
no embeddings, no network. Phase 8 retrieves candidates; it never classifies a
relationship and never changes a fact's lifecycle.
"""

from __future__ import annotations

import pathlib

import pytest

from app.config import Settings
from app.facts import insert_fact
from app.models import FactIn
from app.retrieve import retrieval_summary, retrieve_candidates

NOW = "2026-01-01T00:00:00Z"


def _entity(conn, label) -> int:
    return conn.execute(
        "INSERT INTO entities (canonical_label, created_at) VALUES (?, ?)", (label, NOW)
    ).lastrowid


def _fact(conn, src, *, subject, predicate, obj="x", fact_type="semantic",
          entity_id=None, state="ELIGIBLE_FOR_REASONING", ev="VERIFIED", **over) -> int:
    eligible = state == "ELIGIBLE_FOR_REASONING"
    if fact_type == "numeric":
        over.setdefault("value_raw", obj)
        over["value_text"] = None
    else:
        over.setdefault("value_text", obj)
    return insert_fact(conn, FactIn(
        document_id=src["document_id"], page_index=0, subject_raw=subject,
        predicate=predicate, predicate_norm=predicate.lower(), object_raw=obj,
        fact_type=fact_type, subject_entity_id=entity_id, lifecycle_state=state,
        evidence_status=ev, reasoning_eligible=eligible, **over,
    ))


def _pairset(pairs):
    return {(p.fact_a_id, p.fact_b_id) for p in pairs}


# --------------------------------------------------------------------------- #
# retrieval                                                                  #
# --------------------------------------------------------------------------- #
def test_same_entity_same_predicate_retrieved(conn, make_source):
    src = make_source(["p"])
    e = _entity(conn, "Company X")
    a = _fact(conn, src, subject="Company X", predicate="revenue", entity_id=e)
    b = _fact(conn, src, subject="Company X", predicate="revenue", entity_id=e)
    conn.commit()
    pairs = retrieve_candidates(conn)
    assert _pairset(pairs) == {(a, b)}
    assert "entity" in pairs[0].retrieval_methods
    assert "predicate_exact" in pairs[0].retrieval_methods


def test_same_entity_different_period_retrieved(conn, make_source):
    src = make_source(["p"])
    e = _entity(conn, "Company X")
    a = _fact(conn, src, subject="Company X", predicate="revenue", entity_id=e,
              reporting_period_start="2023-04-01", reporting_period_end="2024-04-01",
              reporting_period_type="fiscal_year")
    b = _fact(conn, src, subject="Company X", predicate="revenue", entity_id=e,
              reporting_period_start="2024-04-01", reporting_period_end="2025-04-01",
              reporting_period_type="fiscal_year")
    conn.commit()
    pairs = retrieve_candidates(conn)
    assert _pairset(pairs) == {(a, b)}
    assert pairs[0].signals.period_relation == "adjacent"  # a signal, not an exclusion


def test_same_entity_related_predicate_retrieved(conn, make_source):
    src = make_source(["p"])
    e = _entity(conn, "Company X")
    a = _fact(conn, src, subject="Company X", predicate="employees", entity_id=e)
    b = _fact(conn, src, subject="Company X", predicate="headcount", entity_id=e)
    conn.commit()
    pairs = retrieve_candidates(conn)
    assert _pairset(pairs) == {(a, b)}  # retrieved on the entity block despite 0 shared tokens


def test_unrelated_entities_not_retrieved(conn, make_source):
    src = make_source(["p"])
    e1, e2 = _entity(conn, "Company X"), _entity(conn, "Ministry Y")
    _fact(conn, src, subject="Company X", predicate="revenue", obj="crore", entity_id=e1)
    _fact(conn, src, subject="Ministry Y", predicate="fiscal deficit", obj="percent", entity_id=e2)
    conn.commit()
    assert retrieve_candidates(conn) == []


def test_no_self_pairs(conn, make_source):
    src = make_source(["p"])
    e = _entity(conn, "Company X")
    _fact(conn, src, subject="Company X", predicate="revenue", entity_id=e)
    conn.commit()
    assert retrieve_candidates(conn) == []


def test_pair_is_canonical_and_deduped(conn, make_source):
    src = make_source(["p"])
    e = _entity(conn, "Company X")
    a = _fact(conn, src, subject="Company X", predicate="revenue", entity_id=e)
    b = _fact(conn, src, subject="Company X", predicate="revenue", entity_id=e)
    conn.commit()
    pairs = retrieve_candidates(conn)
    assert len(pairs) == 1
    assert pairs[0].fact_a_id < pairs[0].fact_b_id
    assert (pairs[0].fact_a_id, pairs[0].fact_b_id) == (min(a, b), max(a, b))


def test_cross_document_retrieval(conn, make_source):
    src1, src2 = make_source(["p"]), make_source(["p"])
    e = _entity(conn, "Company X")
    a = _fact(conn, src1, subject="Company X", predicate="revenue", entity_id=e)
    b = _fact(conn, src2, subject="Company X", predicate="revenue", entity_id=e)
    conn.commit()
    pairs = retrieve_candidates(conn)
    assert _pairset(pairs) == {(a, b)}
    assert pairs[0].signals.same_document is False


def test_deterministic(conn, make_source):
    src1, src2 = make_source(["p"]), make_source(["p"])
    e = _entity(conn, "Company X")
    for i in range(4):
        _fact(conn, src1 if i % 2 else src2, subject="Company X",
              predicate="revenue" if i < 3 else "total revenue", entity_id=e)
    conn.commit()
    r1 = retrieve_candidates(conn)
    r2 = retrieve_candidates(conn)
    assert [(p.fact_a_id, p.fact_b_id, p.retrieval_score, p.retrieval_methods) for p in r1] == \
           [(p.fact_a_id, p.fact_b_id, p.retrieval_score, p.retrieval_methods) for p in r2]
    assert [p.signals.model_dump() for p in r1] == [p.signals.model_dump() for p in r2]


def test_top_k_bounds_and_changes_output(conn, make_source):
    src = make_source(["p"])
    e = _entity(conn, "Company X")
    ids = [_fact(conn, src, subject="Company X", predicate="revenue", entity_id=e)
           for _ in range(5)]
    conn.commit()
    full = retrieve_candidates(conn, settings=Settings(retrieval_top_k=15))
    assert _pairset(full) == {(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]}  # all 10

    capped = retrieve_candidates(conn, settings=Settings(retrieval_top_k=1))
    assert len(capped) < len(full)
    assert len(capped) <= 1 * 5  # top_k * n_eligible


def test_empty_and_single(conn, make_source):
    src = make_source(["p"])
    assert retrieve_candidates(conn) == []
    _fact(conn, src, subject="Company X", predicate="revenue", entity_id=_entity(conn, "X"))
    conn.commit()
    assert retrieve_candidates(conn) == []


# --------------------------------------------------------------------------- #
# lifecycle safety                                                           #
# --------------------------------------------------------------------------- #
def test_only_eligible_facts_participate(conn, make_source):
    src = make_source(["p"])
    e = _entity(conn, "Company X")
    elig = _fact(conn, src, subject="Company X", predicate="revenue", entity_id=e)
    _fact(conn, src, subject="Company X", predicate="revenue", entity_id=e,
          state="NORMALIZED", ev="VERIFIED")
    _fact(conn, src, subject="Company X", predicate="revenue", entity_id=e,
          state="CANDIDATE", ev="UNVERIFIED")
    _fact(conn, src, subject="Company X", predicate="revenue", entity_id=e,
          state="QUARANTINED", ev="UNVERIFIED")
    conn.commit()
    pairs = retrieve_candidates(conn)
    assert pairs == []  # only one eligible fact -> no pair
    assert elig  # referenced


def test_phase8_never_mutates_lifecycle(conn, make_source):
    src = make_source(["p"])
    e = _entity(conn, "Company X")
    _fact(conn, src, subject="Company X", predicate="revenue", entity_id=e)
    _fact(conn, src, subject="Company X", predicate="revenue", entity_id=e)
    conn.commit()
    before = conn.execute(
        "SELECT id, lifecycle_state, evidence_status, reasoning_eligible FROM facts ORDER BY id"
    ).fetchall()
    retrieve_candidates(conn)
    after = conn.execute(
        "SELECT id, lifecycle_state, evidence_status, reasoning_eligible FROM facts ORDER BY id"
    ).fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]
    assert conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# a DATASET_ANALYSIS candidate (C1 shape) — retrieved, NOT classified        #
# --------------------------------------------------------------------------- #
def test_c1_shape_retrieved_not_classified(conn, make_source):
    src_ar, src_deck = make_source(["p"]), make_source(["p"])
    e = _entity(conn, "Company X")
    a = _fact(conn, src_ar, subject="Company X", predicate="revenue from operations",
              obj="81,415.38 million", fact_type="numeric", base_value=8.141538e10,
              numeric_value=81415.38, currency="INR", entity_id=e,
              reporting_period_start="2023-04-01", reporting_period_end="2024-04-01",
              reporting_period_type="fiscal_year")
    b = _fact(conn, src_deck, subject="Company X", predicate="revenue from services",
              obj="8,142 Cr", fact_type="numeric", base_value=8.142e10,
              numeric_value=8142.0, currency="INR", entity_id=e,
              reporting_period_start="2023-04-01", reporting_period_end="2024-04-01",
              reporting_period_type="fiscal_year")
    conn.commit()
    pairs = retrieve_candidates(conn)
    assert _pairset(pairs) == {(a, b)}
    p = pairs[0]
    assert not hasattr(p, "category")           # Phase 8 does not classify
    assert p.signals.same_document is False
    assert p.signals.numeric_comparable
    assert p.signals.base_value_delta_pct < 0.01
    assert p.signals.unit_equivalent is True


def test_retrieval_summary(conn, make_source):
    src = make_source(["p"])
    e = _entity(conn, "Company X")
    _fact(conn, src, subject="Company X", predicate="revenue", entity_id=e)
    _fact(conn, src, subject="Company X", predicate="revenue", entity_id=e)
    conn.commit()
    pairs = retrieve_candidates(conn)
    s = retrieval_summary(conn, pairs)
    assert s["facts_eligible"] == 2
    assert s["candidate_pairs"] == 1
    assert s["candidate_pairs"] <= s["max_possible"]
    assert s["config"]["top_k"] == Settings().retrieval_top_k


def test_module_has_no_corpus_strings():
    for mod in ("app/retrieve.py", "app/signals.py"):
        text = pathlib.Path(mod).read_text(encoding="utf-8").lower()
        for needle in ("delhivery", "ssn logistics", "sahil", "reserve bank", "rbi",
                       "economic survey", "imf", "crore"):
            assert needle not in text


# --------------------------------------------------------------------------- #
# real starter PDF (deterministic; no LLM)                                    #
# --------------------------------------------------------------------------- #
def test_real_pdf_smoke(conn, db_path):
    import re

    pdfs = [
        pathlib.Path("starter-datasets/delhivery/02-delhivery-annual-report-fy24-excerpt.pdf"),
        pathlib.Path("starter-datasets/delhivery/03-delhivery-q4-fy24-earnings-presentation.pdf"),
    ]
    if not all(p.exists() for p in pdfs):
        pytest.skip("starter PDFs not present")
    from app.ingest import ingest_pdf

    doc_ids = [ingest_pdf(str(pdf)).document_id for pdf in pdfs]
    e = _entity(conn, "Delhivery")
    for doc in doc_ids:
        page = conn.execute(
            "SELECT text FROM pages WHERE document_id = ? AND length(text) > 200 "
            "ORDER BY page_index LIMIT 1", (doc,),
        ).fetchone()["text"]
        num = re.search(r"\d[\d,]*\.?\d*", page)
        insert_fact(conn, FactIn(
            document_id=doc, page_index=0, subject_raw="Delhivery", predicate="reported a figure",
            predicate_norm="reported a figure", object_raw=(num.group(0) if num else "x"),
            fact_type="numeric", value_raw=(num.group(0) if num else "1"),
            base_value=1.0, numeric_value=1.0, subject_entity_id=e,
            lifecycle_state="ELIGIBLE_FOR_REASONING", evidence_status="VERIFIED",
            reasoning_eligible=True,
        ))
    conn.commit()

    pairs = retrieve_candidates(conn)
    assert len(pairs) == 1
    assert pairs[0].signals.same_document is False          # cross-document candidate
    assert pairs[0].signals.entity_relation == "same"
