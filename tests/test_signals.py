"""Phase 8 (Stage B) — deterministic comparison signals.

Pure-function tests for the relation helpers; integration tests for
``signals.compute`` over synthetic eligible facts. No LLM, no network.
"""

from __future__ import annotations

import pytest

from app.facts import insert_fact
from app.models import FactIn
from app.signals import (
    compute,
    content_tokens,
    period_relation,
    predicate_signals,
    scope_relation,
)

NOW = "2026-01-01T00:00:00Z"


def _entity(conn, label="India") -> int:
    return conn.execute(
        "INSERT INTO entities (canonical_label, created_at) VALUES (?, ?)", (label, NOW)
    ).lastrowid


def _efact(conn, src, *, subject="India", predicate="real gdp growth", obj="6.4%",
           fact_type="semantic", entity_id=None, **over) -> int:
    if fact_type == "numeric":
        over.setdefault("value_raw", obj)
        over["value_text"] = None
    else:
        over.setdefault("value_text", obj)
    return insert_fact(conn, FactIn(
        document_id=src["document_id"], page_index=0, subject_raw=subject,
        predicate=predicate, predicate_norm=predicate.lower(), object_raw=obj,
        fact_type=fact_type, subject_entity_id=entity_id,
        lifecycle_state="ELIGIBLE_FOR_REASONING", evidence_status="VERIFIED",
        reasoning_eligible=True, **over,
    ))


# --------------------------------------------------------------------------- #
# pure helpers                                                               #
# --------------------------------------------------------------------------- #
def test_content_tokens_drops_stopwords():
    assert content_tokens("Revenue from Operations of the Company") == {
        "revenue", "operations", "company"
    }


@pytest.mark.parametrize(
    ("a", "b", "exact", "sim_min", "sim_max"),
    [
        ("revenue", "revenue", True, 1.0, 1.0),
        ("revenue from operations", "revenue from services", False, 0.55, 0.95),
        ("total revenue", "revenue", False, 0.60, 1.0),
        ("employees", "headcount", False, 0.0, 0.40),
    ],
)
def test_predicate_signals(a, b, exact, sim_min, sim_max):
    got_exact, sim, _overlap = predicate_signals(a, a, b, b)
    assert got_exact is exact
    assert sim_min <= sim <= sim_max


@pytest.mark.parametrize(
    ("a_start", "a_end", "b_start", "b_end", "expected"),
    [
        ("2023-04-01", "2024-04-01", "2023-04-01", "2024-04-01", "equal"),
        ("2023-04-01", "2024-04-01", "2024-01-01", "2024-04-01", "contains"),
        ("2023-04-01", "2024-04-01", "2024-04-01", "2025-04-01", "adjacent"),
        ("2023-04-01", "2024-04-01", "2023-10-01", "2024-10-01", "overlaps"),
        ("2024-01-01", "2024-04-01", "2024-07-01", "2024-10-01", "same_year"),
        ("2021-04-01", "2022-04-01", "2024-04-01", "2025-04-01", "disjoint"),
        ("2023-04-01", "2024-04-01", None, None, "missing"),
    ],
)
def test_period_relation(a_start, a_end, b_start, b_end, expected):
    assert period_relation(a_start, a_end, b_start, b_end) == expected


def test_period_relation_unknown_wins():
    assert period_relation("2023-04-01", "2024-04-01", "2023-04-01", "2024-04-01",
                           "fiscal_year", "unknown") == "unknown"


@pytest.mark.parametrize(
    ("a", "b", "relation", "conflict"),
    [
        ({"basis": "consolidated"}, {"basis": "consolidated"}, "same", []),
        ({"basis": "standalone"}, {"basis": "consolidated"}, "different", ["basis"]),
        ({"basis": "consolidated", "segment": "total"}, {"basis": "consolidated"}, "overlap", []),
        ({"geo": "india"}, {"segment": "express"}, "different", []),
        (None, {"basis": "x"}, "missing", []),
        ({}, {}, "missing", []),
    ],
)
def test_scope_relation(a, b, relation, conflict):
    assert scope_relation(a, b) == (relation, conflict)


# --------------------------------------------------------------------------- #
# compute() integration                                                      #
# --------------------------------------------------------------------------- #
def test_compute_numeric_equivalent(conn, make_source):
    src = make_source(["p"])
    eid = _entity(conn)
    a = _efact(conn, src, fact_type="numeric", predicate="revenue from operations",
               obj="81,415.38 million", base_value=8.141538e10, numeric_value=81415.38,
               currency="INR", entity_id=eid, reporting_period_start="2023-04-01",
               reporting_period_end="2024-04-01", reporting_period_type="fiscal_year")
    b = _efact(conn, src, fact_type="numeric", predicate="revenue from services",
               obj="8,142 Cr", base_value=8.142e10, numeric_value=8142.0,
               currency="INR", entity_id=eid, reporting_period_start="2023-04-01",
               reporting_period_end="2024-04-01", reporting_period_type="fiscal_year")
    conn.commit()
    s = compute(conn, a, b)
    assert s.entity_relation == "same"
    assert s.numeric_comparable
    assert s.base_value_delta_pct < 0.01
    assert s.sign_match is True
    assert s.currency_relation == "same"
    assert s.unit_equivalent is True
    assert s.period_relation == "equal"
    assert s.predicate_similarity >= 0.5


def test_compute_currency_mismatch_blocks_unit_equivalent(conn, make_source):
    src = make_source(["p"])
    a = _efact(conn, src, fact_type="numeric", obj="100", base_value=100.0,
               numeric_value=100.0, currency="INR")
    b = _efact(conn, src, fact_type="numeric", obj="100", base_value=100.0,
               numeric_value=100.0, currency="USD")
    conn.commit()
    s = compute(conn, a, b)
    assert s.currency_relation == "different"
    assert s.unit_equivalent is False


def test_compute_percentage_vs_absolute(conn, make_source):
    src = make_source(["p"])
    a = _efact(conn, src, fact_type="numeric", obj="5%", base_value=0.05,
               numeric_value=5.0, is_percentage=True)
    b = _efact(conn, src, fact_type="numeric", obj="5", base_value=5.0, numeric_value=5.0)
    conn.commit()
    s = compute(conn, a, b)
    assert s.percentage_vs_absolute is True
    assert s.unit_equivalent is False


def test_compute_scope_conflict_and_modality(conn, make_source):
    src = make_source(["p"])
    a = _efact(conn, src, scope={"basis": "standalone"}, modality="HISTORICAL")
    b = _efact(conn, src, scope={"basis": "consolidated"}, modality="FORECAST")
    conn.commit()
    s = compute(conn, a, b)
    assert s.scope_conflict == ["basis"]
    assert s.scope_relation == "different"
    assert s.modality_relation == "different"
    assert s.modality_comparable is False


def test_compute_adjacent_period_and_unresolved_entity(conn, make_source):
    src = make_source(["p"])
    a = _efact(conn, src, reporting_period_start="2023-04-01",
               reporting_period_end="2024-04-01", reporting_period_type="fiscal_year")
    b = _efact(conn, src, reporting_period_start="2024-04-01",
               reporting_period_end="2025-04-01", reporting_period_type="fiscal_year")
    conn.commit()
    s = compute(conn, a, b)
    assert s.period_relation == "adjacent"
    assert s.entity_relation == "unresolved"
    assert any("not resolved" in r for r in s.reasons)


def test_compute_missing_context_is_recorded(conn, make_source):
    src = make_source(["p"])
    a = _efact(conn, src)
    b = _efact(conn, src)
    conn.commit()
    s = compute(conn, a, b)
    assert s.period_relation == "missing"
    assert s.scope_relation == "missing"
    assert s.unit_relation == "missing"
    assert s.currency_relation == "missing"


def test_compute_publication_gap_and_vintage(conn, make_source):
    src_a = make_source(["p"])
    src_b = make_source(["p"])
    conn.execute("UPDATE documents SET publication_date = ?, data_vintage = ? WHERE id = ?",
                 ("2025-01-30", "advance estimate", src_a["document_id"]))
    conn.execute("UPDATE documents SET publication_date = ?, data_vintage = ? WHERE id = ?",
                 ("2025-11-22", "projection", src_b["document_id"]))
    a = _efact(conn, src_a)
    b = _efact(conn, src_b)
    conn.commit()
    s = compute(conn, a, b)
    assert s.same_document is False
    assert s.publication_gap_days == 296
    assert s.vintage_differs is True
