"""Phase 9 — relationship reasoning.

Deterministic classification first (all five categories reachable without an
LLM); a fake confirmer exercises the semantic step and the deterministic
validation guard. No network, no API key.
"""

from __future__ import annotations

import pathlib

import pytest

from app.config import Settings, get_settings
from app.facts import insert_fact
from app.models import CandidatePair, FactIn, RelationshipProposal, SignalSet
from app.reason import (
    ReasonError,
    _validate_llm,
    deterministic_verdict,
    reason_document,
    reason_pair,
    reasoning_summary,
)
from app.signals import compute as compute_signals
from tests.fakes import FakeRelationshipConfirmer

CFG = get_settings()
NOW = "2026-01-01T00:00:00Z"

FY24 = ("2023-04-01", "2024-04-01")
FY25 = ("2024-04-01", "2025-04-01")


def _entity(conn, label) -> int:
    return conn.execute(
        "INSERT INTO entities (canonical_label, created_at) VALUES (?, ?)", (label, NOW)
    ).lastrowid


def _efact(conn, src, *, subject, predicate, obj, fact_type="semantic", entity_id=None,
           period=None, period_type="fiscal_year", scope=None, modality="HISTORICAL",
           base_value=None, numeric_value=None, is_percentage=False, unit_norm=None,
           currency=None, ev="VERIFIED", state="ELIGIBLE_FOR_REASONING", **over) -> int:
    if fact_type == "numeric":
        over.setdefault("value_raw", obj)
        over["value_text"] = None
        over["base_value"] = base_value
        over["numeric_value"] = numeric_value if numeric_value is not None else base_value
        over["is_percentage"] = is_percentage
        over["unit_norm"] = unit_norm
        over["currency"] = currency
    else:
        over.setdefault("value_text", obj)
    if period:
        over["reporting_period_start"], over["reporting_period_end"] = period
        over["reporting_period_type"] = period_type
    return insert_fact(conn, FactIn(
        document_id=src["document_id"], page_index=0, subject_raw=subject,
        predicate=predicate, predicate_norm=predicate.lower(), object_raw=obj,
        fact_type=fact_type, subject_entity_id=entity_id, modality=modality, scope=scope,
        lifecycle_state=state, evidence_status=ev,
        reasoning_eligible=(state == "ELIGIBLE_FOR_REASONING"), **over,
    ))


def _pair(conn, a, b, *, score=0.9, methods=("entity", "predicate_exact")) -> CandidatePair:
    lo, hi = sorted((a, b))
    return CandidatePair(fact_a_id=lo, fact_b_id=hi, retrieval_score=score,
                         retrieval_methods=list(methods), signals=compute_signals(conn, lo, hi))


def _decide(conn, a, b, *, llm=None, settings=None, **pk):
    return reason_pair(conn, _pair(conn, a, b, **pk), settings=settings or CFG, llm=llm)


def _num_signals(**over) -> SignalSet:
    base = dict(
        entity_relation="same", predicate_exact=True, predicate_similarity=1.0,
        predicate_token_overlap=1.0, fact_type_a="numeric", fact_type_b="numeric",
        fact_type_match=True, numeric_comparable=True, base_value_a=100.0, base_value_b=100.0,
        base_value_abs_diff=0.0, base_value_delta_pct=0.0, sign_match=True,
        percentage_vs_absolute=False, unit_equivalent=True, unit_relation="same",
        currency_relation="same", period_relation="equal", scope_relation="missing",
        modality_a="HISTORICAL", modality_b="HISTORICAL", modality_relation="same",
        modality_comparable=True, same_document=False,
    )
    base.update(over)
    return SignalSet(**base)


# --------------------------------------------------------------------------- #
# the five categories — deterministic, no LLM                                #
# --------------------------------------------------------------------------- #
def test_corroborates(conn, make_source):
    src = make_source(["p"])
    e = _entity(conn, "Company X")
    a = _efact(conn, src, subject="Company X", predicate="revenue", obj="81,415 mn",
               fact_type="numeric", base_value=8.1415e10, currency="INR", entity_id=e, period=FY24)
    b = _efact(conn, src, subject="Company X", predicate="revenue", obj="8,142 cr",
               fact_type="numeric", base_value=8.142e10, currency="INR", entity_id=e, period=FY24)
    d = _decide(conn, a, b)
    assert d.category == "CORROBORATES"
    assert d.method == "deterministic"
    assert d.llm_used is False
    assert d.confidence > 0.8


def test_contradicts(conn, make_source):
    src = make_source(["p"])
    e = _entity(conn, "Company X")
    a = _efact(conn, src, subject="Company X", predicate="revenue", obj="100",
               fact_type="numeric", base_value=100.0, currency="INR", entity_id=e, period=FY24)
    b = _efact(conn, src, subject="Company X", predicate="revenue", obj="140",
               fact_type="numeric", base_value=140.0, currency="INR", entity_id=e, period=FY24)
    d = _decide(conn, a, b)
    assert d.category == "CONTRADICTS"
    assert d.method == "deterministic"
    assert "differ" in d.reasoning


def test_different_context_scope(conn, make_source):
    src = make_source(["p"])
    e = _entity(conn, "Company X")
    a = _efact(conn, src, subject="Company X", predicate="revenue", obj="74,540 mn",
               fact_type="numeric", base_value=7.454e10, currency="INR", entity_id=e, period=FY24,
               scope={"basis": "standalone"})
    b = _efact(conn, src, subject="Company X", predicate="revenue", obj="81,415 mn",
               fact_type="numeric", base_value=8.1415e10, currency="INR", entity_id=e, period=FY24,
               scope={"basis": "consolidated"})
    d = _decide(conn, a, b)
    assert d.category == "DIFFERENT_CONTEXT"
    assert d.context_dimension == "scope:basis"


def test_temporal_evolution_numeric(conn, make_source):
    src = make_source(["p"])
    e = _entity(conn, "Company X")
    a = _efact(conn, src, subject="Company X", predicate="revenue", obj="60,000 mn",
               fact_type="numeric", base_value=6.0e10, currency="INR", entity_id=e, period=FY24)
    b = _efact(conn, src, subject="Company X", predicate="revenue", obj="81,415 mn",
               fact_type="numeric", base_value=8.1415e10, currency="INR", entity_id=e, period=FY25)
    d = _decide(conn, a, b)
    assert d.category == "TEMPORAL_EVOLUTION"
    assert d.context_dimension == "time"


def test_temporal_evolution_semantic_director_shape(conn, make_source):
    src = make_source(["p"])
    e = _entity(conn, "S K Barasia")
    a = _efact(conn, src, subject="S K Barasia", predicate="board status",
               obj="Executive Director and Chief Business Officer", entity_id=e, period=FY24)
    b = _efact(conn, src, subject="S K Barasia", predicate="board status",
               obj="resigned from the directorship with effect from July 01, 2024", entity_id=e,
               period=("2024-07-01", "2024-07-02"), period_type="instant")
    d = _decide(conn, a, b, methods=("entity", "predicate_exact"))
    assert d.category == "TEMPORAL_EVOLUTION"        # from dates + modality, not CONTRADICTS
    assert d.context_dimension == "time"


def test_uncertain_no_llm_semantic(conn, make_source):
    src = make_source(["p"])
    e = _entity(conn, "Company X")
    a = _efact(conn, src, subject="Company X", predicate="described as",
               obj="India's largest integrated logistics platform", entity_id=e)
    b = _efact(conn, src, subject="Company X", predicate="described as",
               obj="a listed company on the NSE", entity_id=e)
    d = _decide(conn, a, b)
    assert d.category == "UNCERTAIN"
    assert d.method == "uncertain_no_llm"


def test_uncertain_unresolved_entity(conn, make_source):
    src = make_source(["p"])
    a = _efact(conn, src, subject="the economy", predicate="gdp growth", obj="6.4%")
    b = _efact(conn, src, subject="the economy", predicate="gdp growth", obj="6.6%")
    d = _decide(conn, a, b, methods=("lexical",))
    assert d.category == "UNCERTAIN"
    assert d.deterministic_signals["entity_relation"] == "unresolved"


def test_uncertain_weak_predicate(conn, make_source):
    src = make_source(["p"])
    e = _entity(conn, "Company X")
    a = _efact(conn, src, subject="Company X", predicate="revenue from operations", obj="x",
               entity_id=e, period=FY24)
    b = _efact(conn, src, subject="Company X", predicate="number of gateways", obj="y",
               entity_id=e, period=FY24)
    d = _decide(conn, a, b, methods=("entity",))
    assert d.category == "UNCERTAIN"


# --------------------------------------------------------------------------- #
# numeric edge cases — via deterministic_verdict                             #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("delta", "period", "expected"),
    [
        (0.0, "equal", "CORROBORATES"),
        (0.01, "equal", "CORROBORATES"),         # within tolerance (0.02)
        (0.05, "equal", "UNCERTAIN"),            # between tolerance and contradiction (0.15)
        (0.30, "equal", "CONTRADICTS"),
        (0.30, "overlaps", "CONTRADICTS"),
        (0.30, "adjacent", "TEMPORAL_EVOLUTION"),
        (0.30, "disjoint", "TEMPORAL_EVOLUTION"),
        (0.30, "missing", "UNCERTAIN"),
    ],
)
def test_numeric_thresholds(delta, period, expected):
    s = _num_signals(base_value_delta_pct=delta, base_value_b=100.0 * (1 + delta),
                     period_relation=period)
    assert deterministic_verdict(s, objects_equal=False, settings=CFG).category == expected


def test_numeric_sign_mismatch_is_uncertain():
    s = _num_signals(base_value_b=-100.0, base_value_delta_pct=2.0, sign_match=False)
    assert deterministic_verdict(s, objects_equal=False, settings=CFG).category == "UNCERTAIN"


def test_percentage_vs_absolute_is_uncertain():
    s = _num_signals(percentage_vs_absolute=True, base_value_delta_pct=0.9)
    assert deterministic_verdict(s, objects_equal=False, settings=CFG).category == "UNCERTAIN"


def test_missing_base_values_is_uncertain():
    s = _num_signals(numeric_comparable=False, base_value_a=None, base_value_b=None,
                     base_value_delta_pct=None, sign_match=None, unit_equivalent=None)
    v = deterministic_verdict(s, objects_equal=False, settings=CFG)
    assert v.category == "UNCERTAIN"
    assert v.code == "numeric_unparsed"


def test_different_currency_is_context_not_contradiction():
    s = _num_signals(currency_relation="different", unit_equivalent=False,
                     base_value_delta_pct=0.9)
    v = deterministic_verdict(s, objects_equal=False, settings=CFG)
    assert v.category == "DIFFERENT_CONTEXT"
    assert v.context_dimension == "currency"


# --------------------------------------------------------------------------- #
# modality                                                                   #
# --------------------------------------------------------------------------- #
def test_actual_vs_projection_not_contradiction(conn, make_source):
    src = make_source(["p"])
    e = _entity(conn, "Economy")
    a = _efact(conn, src, subject="Economy", predicate="gdp growth", obj="6.4%",
               fact_type="numeric", base_value=0.064, is_percentage=True, unit_norm="ratio",
               entity_id=e, period=FY24, modality="HISTORICAL")
    b = _efact(conn, src, subject="Economy", predicate="gdp growth", obj="8.0%",
               fact_type="numeric", base_value=0.08, is_percentage=True, unit_norm="ratio",
               entity_id=e, period=FY24, modality="FORECAST")
    d = _decide(conn, a, b)
    assert d.category == "DIFFERENT_CONTEXT"
    assert d.context_dimension == "modality"


def test_target_vs_actual_is_context():
    s = _num_signals(modality_a="HISTORICAL", modality_b="TARGET", modality_relation="different",
                     modality_comparable=False, base_value_delta_pct=0.5)
    v = deterministic_verdict(s, objects_equal=False, settings=CFG)
    assert v.category == "DIFFERENT_CONTEXT"
    assert v.context_dimension == "modality"


def test_projection_vs_projection_can_corroborate():
    s = _num_signals(modality_a="FORECAST", modality_b="FORECAST", modality_relation="same",
                     modality_comparable=False, base_value_delta_pct=0.005)
    assert deterministic_verdict(s, objects_equal=False, settings=CFG).category == "CORROBORATES"


# --------------------------------------------------------------------------- #
# deterministic validation of an LLM proposal (guard)                        #
# --------------------------------------------------------------------------- #
def test_validate_llm_overrides_contradicts_on_equal_numbers():
    s = _num_signals(base_value_delta_pct=0.0, period_relation="equal")
    final, action, _ = _validate_llm("CONTRADICTS", s, CFG)
    assert (final, action) == ("CORROBORATES", "overridden")


def test_validate_llm_scope_conflict_overrides_to_context():
    s = _num_signals(scope_conflict=["basis"], base_value_delta_pct=0.4)
    final, action, _ = _validate_llm("CONTRADICTS", s, CFG)
    assert final == "DIFFERENT_CONTEXT"
    assert action == "overridden"


def test_validate_llm_disjoint_period_overrides_to_temporal():
    s = _num_signals(period_relation="disjoint", base_value_delta_pct=0.4)
    final, action, _ = _validate_llm("CONTRADICTS", s, CFG)
    assert (final, action) == ("TEMPORAL_EVOLUTION", "overridden")


def test_validate_llm_mixed_modality_downgrades_contradicts():
    s = _num_signals(modality_comparable=False, base_value_delta_pct=0.4)
    final, action, _ = _validate_llm("CONTRADICTS", s, CFG)
    assert final == "DIFFERENT_CONTEXT"
    assert action == "downgraded"


# --------------------------------------------------------------------------- #
# LLM semantic step (fake confirmer)                                         #
# --------------------------------------------------------------------------- #
def _semantic_pair(conn, make_source, *, period_a=None, period_b=None):
    src = make_source(["p"])
    e = _entity(conn, "Company X")
    a = _efact(conn, src, subject="Company X", predicate="market position",
               obj="the clear market leader", entity_id=e, period=period_a)
    b = _efact(conn, src, subject="Company X", predicate="market position",
               obj="a small niche player", entity_id=e, period=period_b)
    return a, b


def test_llm_proposal_accepted(conn, make_source):
    a, b = _semantic_pair(conn, make_source, period_a=FY24, period_b=FY24)
    llm = FakeRelationshipConfirmer([RelationshipProposal(
        relationship="CONTRADICTS", confidence=0.8, reason="leader vs niche player, same year")])
    d = _decide(conn, a, b, llm=llm)
    assert d.category == "CONTRADICTS"
    assert d.method == "llm_confirmed"
    assert d.llm_used is True
    assert d.llm_proposed_category == "CONTRADICTS"
    assert d.validation_action == "accepted"
    assert llm.calls


def test_llm_proposal_downgraded_when_period_missing(conn, make_source):
    a, b = _semantic_pair(conn, make_source)               # no periods
    llm = FakeRelationshipConfirmer([RelationshipProposal(
        relationship="CONTRADICTS", confidence=0.9, reason="opposite claims")])
    d = _decide(conn, a, b, llm=llm)
    assert d.category == "UNCERTAIN"
    assert d.validation_action == "downgraded"
    assert d.llm_proposed_category == "CONTRADICTS"


def test_llm_invalid_category_falls_back_to_uncertain(conn, make_source):
    a, b = _semantic_pair(conn, make_source, period_a=FY24, period_b=FY24)
    llm = FakeRelationshipConfirmer([RelationshipProposal(
        relationship="BANANA", confidence=0.9, reason="nonsense")])
    d = _decide(conn, a, b, llm=llm)
    assert d.category == "UNCERTAIN"
    assert d.llm_used is True
    assert d.llm_proposed_category == "BANANA"


def test_llm_exception_does_not_crash(conn, make_source):
    a, b = _semantic_pair(conn, make_source, period_a=FY24, period_b=FY24)
    llm = FakeRelationshipConfirmer([RuntimeError("boom")])
    d = _decide(conn, a, b, llm=llm)
    assert d.category == "UNCERTAIN"
    assert d.llm_used is True


def test_deterministic_case_never_calls_llm(conn, make_source):
    src = make_source(["p"])
    e = _entity(conn, "Company X")
    a = _efact(conn, src, subject="Company X", predicate="revenue", obj="100",
               fact_type="numeric", base_value=100.0, currency="INR", entity_id=e, period=FY24)
    b = _efact(conn, src, subject="Company X", predicate="revenue", obj="100",
               fact_type="numeric", base_value=100.0, currency="INR", entity_id=e, period=FY24)
    llm = FakeRelationshipConfirmer([RuntimeError("must not be called")])
    d = _decide(conn, a, b, llm=llm)
    assert d.category == "CORROBORATES"
    assert d.llm_used is False
    assert llm.calls == []


# --------------------------------------------------------------------------- #
# persistence via reason_document                                            #
# --------------------------------------------------------------------------- #
def _corpus(conn, make_source):
    src = make_source(["p"])
    e = _entity(conn, "Company X")
    ids = {
        "corr_a": _efact(conn, src, subject="Company X", predicate="revenue", obj="81,415 mn",
                         fact_type="numeric", base_value=8.1415e10, currency="INR",
                         entity_id=e, period=FY24),
        "corr_b": _efact(conn, src, subject="Company X", predicate="revenue", obj="8,142 cr",
                         fact_type="numeric", base_value=8.142e10, currency="INR",
                         entity_id=e, period=FY24),
        "evo": _efact(conn, src, subject="Company X", predicate="revenue", obj="60,000 mn",
                      fact_type="numeric", base_value=6.0e10, currency="INR",
                      entity_id=e, period=FY25),
    }
    conn.commit()
    return src, ids


def test_reason_document_persists_and_is_idempotent(conn, db_path, make_source):
    src, ids = _corpus(conn, make_source)
    s1 = reason_document(src["document_id"], database_path=db_path)
    assert s1.status == "done"
    assert s1.relationships_created >= 1

    rels = conn.execute(
        "SELECT fact_a_id, fact_b_id, category, llm_used, validation_action FROM relationships "
        "ORDER BY fact_a_id, fact_b_id"
    ).fetchall()
    for r in rels:
        assert r["fact_a_id"] < r["fact_b_id"]            # canonical order
        assert r["category"] in {
            "CORROBORATES", "CONTRADICTS", "DIFFERENT_CONTEXT", "TEMPORAL_EVOLUTION", "UNCERTAIN"
        }
    cats = {(r["fact_a_id"], r["fact_b_id"]): r["category"] for r in rels}
    lo, hi = sorted((ids["corr_a"], ids["corr_b"]))
    assert cats[(lo, hi)] == "CORROBORATES"
    lo, hi = sorted((ids["corr_a"], ids["evo"]))
    assert cats[(lo, hi)] == "TEMPORAL_EVOLUTION"

    facts_before = conn.execute(
        "SELECT id, lifecycle_state, evidence_status, reasoning_eligible FROM facts ORDER BY id"
    ).fetchall()

    s2 = reason_document(src["document_id"], database_path=db_path)
    assert s2.relationships_created == 0
    assert s2.relationships_existing == s1.relationships_created
    rels2 = conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]
    assert rels2 == len(rels)                             # no duplicates
    fails2 = conn.execute("SELECT COUNT(*) FROM failures").fetchone()[0]
    reason_document(src["document_id"], database_path=db_path)
    assert conn.execute("SELECT COUNT(*) FROM failures").fetchone()[0] == fails2

    facts_after = conn.execute(
        "SELECT id, lifecycle_state, evidence_status, reasoning_eligible FROM facts ORDER BY id"
    ).fetchall()
    assert [tuple(r) for r in facts_before] == [tuple(r) for r in facts_after]


def test_reason_document_deterministic(conn, db_path, make_source):
    src, _ = _corpus(conn, make_source)
    reason_document(src["document_id"], database_path=db_path)
    snap1 = conn.execute(
        "SELECT fact_a_id, fact_b_id, category, context_dimension, confidence, validation_action "
        "FROM relationships ORDER BY fact_a_id, fact_b_id"
    ).fetchall()
    conn.execute("DELETE FROM relationships")
    conn.execute("DELETE FROM failures")
    conn.commit()
    reason_document(src["document_id"], database_path=db_path)
    snap2 = conn.execute(
        "SELECT fact_a_id, fact_b_id, category, context_dimension, confidence, validation_action "
        "FROM relationships ORDER BY fact_a_id, fact_b_id"
    ).fetchall()
    assert [tuple(r) for r in snap1] == [tuple(r) for r in snap2]


def test_reason_document_uncertain_writes_failure(conn, db_path, make_source):
    src = make_source(["p"])
    e = _entity(conn, "Company X")
    _efact(conn, src, subject="Company X", predicate="market view", obj="bullish outlook",
           entity_id=e)
    _efact(conn, src, subject="Company X", predicate="market view", obj="bearish outlook",
           entity_id=e)
    conn.commit()
    summ = reason_document(src["document_id"], database_path=db_path)   # no llm
    assert summ.uncertain_decisions == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM failures WHERE failure_type = 'relationship_uncertain'"
    ).fetchone()[0] == 1
    s = reasoning_summary(conn, document_id=src["document_id"])
    assert s["by_category"].get("UNCERTAIN") == 1


# --------------------------------------------------------------------------- #
# lifecycle / safety                                                         #
# --------------------------------------------------------------------------- #
def test_quarantined_and_unverified_facts_do_not_participate(conn, db_path, make_source):
    src = make_source(["p"])
    e = _entity(conn, "Company X")
    a = _efact(conn, src, subject="Company X", predicate="revenue", obj="100",
               fact_type="numeric", base_value=100.0, currency="INR", entity_id=e, period=FY24)
    b = _efact(conn, src, subject="Company X", predicate="revenue", obj="101",
               fact_type="numeric", base_value=101.0, currency="INR", entity_id=e, period=FY24)
    q = _efact(conn, src, subject="Company X", predicate="revenue", obj="9999",
               fact_type="numeric", base_value=9999.0, currency="INR", entity_id=e, period=FY24,
               state="QUARANTINED", ev="UNVERIFIED")
    cand = _efact(conn, src, subject="Company X", predicate="revenue", obj="8888",
                  fact_type="numeric", base_value=8888.0, currency="INR", entity_id=e, period=FY24,
                  state="CANDIDATE", ev="UNVERIFIED")
    conn.commit()
    reason_document(src["document_id"], database_path=db_path)
    involved = conn.execute(
        "SELECT fact_a_id, fact_b_id FROM relationships"
    ).fetchall()
    seen = {x for r in involved for x in (r["fact_a_id"], r["fact_b_id"])}
    assert q not in seen and cand not in seen
    assert seen == {a, b}


def test_reason_pair_guard_rejects_ineligible_fact(conn, make_source):
    src = make_source(["p"])
    e = _entity(conn, "Company X")
    good = _efact(conn, src, subject="Company X", predicate="revenue", obj="100",
                  fact_type="numeric", base_value=100.0, currency="INR", entity_id=e, period=FY24)
    bad = _efact(conn, src, subject="Company X", predicate="revenue", obj="140",
                 fact_type="numeric", base_value=140.0, currency="INR", entity_id=e, period=FY24,
                 state="NORMALIZED", ev="VERIFIED")
    conn.commit()
    with pytest.raises(ReasonError) as ei:
        reason_pair(conn, _pair(conn, good, bad), settings=CFG)
    assert ei.value.code == "fact_not_eligible"


def test_reason_document_llm_failure_does_not_crash_run(conn, db_path, make_source):
    src = make_source(["p"])
    e = _entity(conn, "Company X")
    _efact(conn, src, subject="Company X", predicate="market position", obj="the market leader",
           entity_id=e, period=FY24)
    _efact(conn, src, subject="Company X", predicate="market position", obj="a niche player",
           entity_id=e, period=FY24)
    conn.commit()
    llm = FakeRelationshipConfirmer([RuntimeError("boom")])
    summ = reason_document(src["document_id"], database_path=db_path, llm=llm)
    assert summ.status == "done"
    assert summ.errors == 0
    assert conn.execute("SELECT status FROM runs WHERE run_type = 'reason'").fetchone()[0] == "done"


def test_reason_document_unknown_document(db_path):
    from app import db
    db.init_db()
    with pytest.raises(ReasonError) as ei:
        reason_document(999999, database_path=db_path)
    assert ei.value.code == "unknown_document"


# --------------------------------------------------------------------------- #
# config-driven thresholds                                                   #
# --------------------------------------------------------------------------- #
def test_thresholds_come_from_config():
    s = _num_signals(base_value_delta_pct=0.08, period_relation="equal")
    assert deterministic_verdict(s, objects_equal=False,
                                 settings=Settings()).category == "UNCERTAIN"
    loose = Settings(numeric_equivalence_tolerance=0.10)
    assert deterministic_verdict(s, objects_equal=False, settings=loose).category == "CORROBORATES"
    strict = Settings(numeric_contradiction_threshold=0.05)
    assert deterministic_verdict(s, objects_equal=False, settings=strict).category == "CONTRADICTS"


def test_module_has_no_corpus_strings():
    text = pathlib.Path("app/reason.py").read_text(encoding="utf-8").lower()
    for needle in ("delhivery", "rbi", "reserve bank", "economic survey", "imf", "sahil",
                   "barasia", "crore", "india"):
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

    doc_ids = [ingest_pdf(str(p)).document_id for p in pdfs]
    e = _entity(conn, "Delhivery")
    for doc in doc_ids:
        page = conn.execute(
            "SELECT text FROM pages WHERE document_id = ? AND length(text) > 200 "
            "ORDER BY page_index LIMIT 1", (doc,),
        ).fetchone()["text"]
        num = re.search(r"\d[\d,]*\.?\d*", page)
        insert_fact(conn, FactIn(
            document_id=doc, page_index=0, subject_raw="Delhivery", predicate="reported a figure",
            predicate_norm="reported a figure", object_raw=(num.group(0) if num else "1"),
            fact_type="numeric", value_raw=(num.group(0) if num else "1"),
            base_value=1.0, numeric_value=1.0, currency="INR", subject_entity_id=e,
            reporting_period_start=FY24[0], reporting_period_end=FY24[1],
            reporting_period_type="fiscal_year",
            lifecycle_state="ELIGIBLE_FOR_REASONING", evidence_status="VERIFIED",
            reasoning_eligible=True,
        ))
    conn.commit()

    summ = reason_document(doc_ids[0], database_path=db_path)
    assert summ.status == "done"
    assert summ.candidate_pairs >= 1
    rels = conn.execute("SELECT category FROM relationships").fetchall()
    assert len(rels) == summ.relationships_created
