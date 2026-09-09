"""Phase 13 — deterministic end-to-end pipeline integration.

Runs the *whole* pipeline on a synthetic multi-page PDF with **no live LLM**: a
substring-matching fake extractor emits candidates whose quote/offsets are real
spans of the ingested chunk text, so Phase-5 verification, Phase-6 normalization,
Phase-7 resolution, Phase-8 retrieval and Phase-9 reasoning all run for real.

Asserts the full chain holds (FACT -> EVIDENCE -> CHUNK -> PAGE -> DOCUMENT), a
fabricated quote is quarantined and never enters a relationship, and every
relationship category is reachable deterministically.
"""

from __future__ import annotations

import pytest

from app import db
from app.config import Settings
from app.models import LLMExtraction, RawCandidate, RawExtraction
from app.pipeline import run as pipeline_run
from app.pipeline import start as pipeline_start

S = Settings()

# (marker substring that must appear verbatim in a chunk, candidate field overrides)
MARKERS: list[tuple[str, dict]] = [
    ("consolidated basis for FY24 was INR 81,415.38 million", {
        "subject": "Acme", "predicate": "revenue from operations",
        "object": "INR 81,415.38 million", "fact_type": "numeric",
        "raw_value_text": "81,415.38 million", "parsed_value": 81415.38,
        "magnitude": "million", "currency": "INR", "reporting_period": "FY24",
        "period_type": "fiscal_year", "scope": "consolidated", "modality": "historical"}),
    ("standalone basis for FY24 was INR 74,540.82 million", {
        "subject": "Acme", "predicate": "revenue from operations",
        "object": "INR 74,540.82 million", "fact_type": "numeric",
        "raw_value_text": "74,540.82 million", "parsed_value": 74540.82,
        "magnitude": "million", "currency": "INR", "reporting_period": "FY24",
        "period_type": "fiscal_year", "scope": "standalone", "modality": "historical"}),
    ("consolidated basis for FY23 was INR 60,000.00 million", {
        "subject": "Acme", "predicate": "revenue from operations",
        "object": "INR 60,000.00 million", "fact_type": "numeric",
        "raw_value_text": "60,000.00 million", "parsed_value": 60000.00,
        "magnitude": "million", "currency": "INR", "reporting_period": "FY23",
        "period_type": "fiscal_year", "scope": "consolidated", "modality": "historical"}),
    ("profit after tax for FY24 was INR 5,000.00 million", {
        "subject": "Acme", "predicate": "profit after tax",
        "object": "INR 5,000.00 million", "fact_type": "numeric",
        "raw_value_text": "5,000.00 million", "parsed_value": 5000.00,
        "magnitude": "million", "currency": "INR", "reporting_period": "FY24",
        "period_type": "fiscal_year", "modality": "historical"}),
    ("profit after tax for FY24 was INR 8,000.00 million", {
        "subject": "Acme", "predicate": "profit after tax",
        "object": "INR 8,000.00 million", "fact_type": "numeric",
        "raw_value_text": "8,000.00 million", "parsed_value": 8000.00,
        "magnitude": "million", "currency": "INR", "reporting_period": "FY24",
        "period_type": "fiscal_year", "modality": "historical"}),
    ("FY24 revenue from services was INR 8142 Cr", {
        "subject": "Acme", "predicate": "revenue from services",
        "object": "INR 8142 Cr", "fact_type": "numeric",
        "raw_value_text": "8142 Cr", "parsed_value": 8142.0,
        "magnitude": "crore", "currency": "INR", "reporting_period": "FY24",
        "period_type": "fiscal_year", "scope": "consolidated", "modality": "historical"}),
]

DOC_A_PAGES = [
    "Acme revenue from operations on a consolidated basis for FY24 was INR 81,415.38 million.",
    "Acme revenue from operations on a standalone basis for FY24 was INR 74,540.82 million.",
    "Acme revenue from operations on a consolidated basis for FY23 was INR 60,000.00 million.",
    "Acme profit after tax for FY24 was INR 5,000.00 million as reported.",
    "Acme profit after tax for FY24 was INR 8,000.00 million on an adjusted view.",
    "Acme permanent employees on the rolls numbered many thousands as on March 31 2024.",
]
DOC_B_PAGES = ["Acme FY24 revenue from services was INR 8142 Cr for the full year."]


class SubstringExtractor:
    """Same surface as app.llm.Extractor. For each chunk, emit a
    candidate per MARKER found verbatim in the chunk, plus one fabricated-quote
    candidate on the 'permanent employees' page (to exercise the quarantine)."""

    model = "fake-e2e"
    prompt_version = "test-v1"

    def __init__(self):
        self.calls = 0

    def extract(self, chunk_text: str, doc_header: str) -> LLMExtraction:
        self.calls += 1
        facts: list[RawCandidate] = []
        for marker, over in MARKERS:
            i = chunk_text.find(marker)
            if i < 0:
                continue
            facts.append(RawCandidate(
                quote=marker, char_start=i, char_end=i + len(marker),
                percentage=False, qualifiers=None, **over))
        if "permanent employees on the rolls" in chunk_text:
            anchor = "permanent employees on the rolls"
            i = chunk_text.find(anchor)
            facts.append(RawCandidate(
                subject="Acme", predicate="permanent employees", object="23,381",
                fact_type="numeric", raw_value_text="23,381", parsed_value=23381.0,
                reporting_period="FY24", period_type="fiscal_year", modality="historical",
                quote="permanent employees on the rolls were 23,381",  # NOT in the page
                char_start=i, char_end=i + len(anchor), percentage=False, qualifiers=None))
        raw = RawExtraction(facts=facts)
        return LLMExtraction(raw_text=raw.model_dump_json(), parsed=raw, model=self.model,
                             prompt_version=self.prompt_version, stop_reason="end_turn",
                             input_tokens=10, output_tokens=20)


@pytest.fixture
def e2e(db_path, make_pdf):
    from app.ingest import ingest_pdf

    db.init_db(db_path)
    doc_a = ingest_pdf(str(make_pdf(DOC_A_PAGES, name="e2e_a.pdf")),
                       database_path=db_path).document_id
    doc_b = ingest_pdf(str(make_pdf(DOC_B_PAGES, name="e2e_b.pdf")),
                       database_path=db_path).document_id
    conn = db.connect(db_path)
    with db.transaction(conn):
        conn.execute("UPDATE documents SET publication_date = '2024-08-08' WHERE id = ?", (doc_a,))
        conn.execute("UPDATE documents SET publication_date = '2024-05-17' WHERE id = ?", (doc_b,))
    conn.close()
    ext = SubstringExtractor()
    for doc in (doc_a, doc_b):
        conn = db.connect(db_path)
        with db.transaction(conn):
            run_id = pipeline_start(conn, doc, settings=S)
        conn.close()
        res = pipeline_run(doc, run_id, database_path=db_path, settings=S, extractor=ext)
        assert res["status"] == "done", res
    return {"db": db_path, "doc_a": doc_a, "doc_b": doc_b, "extractor": ext}


def _counts(conn):
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "facts": q("SELECT COUNT(*) FROM facts"),
        "grounded": q("SELECT COUNT(*) FROM facts WHERE lifecycle_state IN "
                      "('GROUNDED','NORMALIZED','ELIGIBLE_FOR_REASONING')"),
        "quarantined": q("SELECT COUNT(*) FROM facts WHERE lifecycle_state='QUARANTINED'"),
        "normalized": q("SELECT COUNT(*) FROM facts WHERE base_value IS NOT NULL"),
        "eligible": q("SELECT COUNT(*) FROM facts WHERE lifecycle_state='ELIGIBLE_FOR_REASONING'"),
        "entities": q("SELECT COUNT(*) FROM entities"),
        "relationships": q("SELECT COUNT(*) FROM relationships"),
    }


def test_full_pipeline_flows_facts_to_relationships(e2e):
    conn = db.connect(e2e["db"])
    c = _counts(conn)
    # 6 real facts (2 docs) + 1 fabricated -> 7 candidates; 6 verified, 1 quarantined
    assert c["facts"] == 7
    assert c["grounded"] == 6
    assert c["quarantined"] == 1
    assert c["normalized"] == 6
    assert c["entities"] == 1                     # all "Acme" -> one deterministic entity
    assert c["eligible"] == 6
    assert c["relationships"] >= 4

    # every persisted fact traces the full chain
    for (fid,) in conn.execute("SELECT id FROM facts"):
        from app.facts import evidence_chain
        chain = evidence_chain(conn, fid)
        row = conn.execute("SELECT lifecycle_state FROM facts WHERE id=?", (fid,)).fetchone()
        if row["lifecycle_state"] != "QUARANTINED":
            assert chain and chain["document_id"] and chain["page_id"], fid
    conn.close()


def test_all_five_categories_reachable_end_to_end(e2e):
    conn = db.connect(e2e["db"])
    cats = {r["category"] for r in conn.execute("SELECT DISTINCT category FROM relationships")}
    for expect in ("CORROBORATES", "CONTRADICTS", "DIFFERENT_CONTEXT",
                   "TEMPORAL_EVOLUTION", "UNCERTAIN"):
        assert expect in cats, (expect, cats)

    # the cross-document CORROBORATES (crore == million, FY24)
    corr = conn.execute(
        "SELECT r.*, fa.document_id a_doc, fb.document_id b_doc FROM relationships r "
        "JOIN facts fa ON fa.id=r.fact_a_id JOIN facts fb ON fb.id=r.fact_b_id "
        "WHERE r.category='CORROBORATES'").fetchall()
    assert any(r["a_doc"] != r["b_doc"] for r in corr)

    # the CONTRADICTS is same entity / same period / no scope conflict
    contra = conn.execute("SELECT deterministic_signals FROM relationships "
                          "WHERE category='CONTRADICTS'").fetchone()
    import json
    sig = json.loads(contra["deterministic_signals"])
    assert sig["entity_relation"] == "same"
    assert sig["period_relation"] == "equal"
    assert not sig["scope_conflict"]
    assert sig["base_value_delta_pct"] > S.numeric_contradiction_threshold
    conn.close()


def test_quarantined_fact_is_isolated(e2e):
    conn = db.connect(e2e["db"])
    q = conn.execute(
        "SELECT id FROM facts WHERE lifecycle_state='QUARANTINED'").fetchone()["id"]
    assert conn.execute(
        "SELECT COUNT(*) FROM relationships WHERE fact_a_id=? OR fact_b_id=?", (q, q)
    ).fetchone()[0] == 0
    fail = conn.execute(
        "SELECT failure_type, reason FROM failures WHERE ref_table='facts' AND ref_id=?", (q,)
    ).fetchone()
    assert fail and fail["failure_type"] == "grounding_failed" and fail["reason"]
    conn.close()


def test_full_run_stage_progression_and_status(e2e):
    conn = db.connect(e2e["db"])
    for r in conn.execute("SELECT status, stage FROM runs WHERE run_type='full'"):
        assert (r["status"], r["stage"]) == ("done", "complete")
    for r in conn.execute("SELECT status FROM documents"):
        assert r["status"] == "done"
    # aggregate stats were written onto the full run
    row = conn.execute("SELECT facts_extracted, relationships_produced FROM runs "
                       "WHERE run_type='full' ORDER BY id DESC LIMIT 1").fetchone()
    assert row["facts_extracted"] >= 1
    conn.close()


def test_evidence_offsets_match_the_quote(e2e):
    """Regression guard: a verified fact's page span must equal its quote."""
    conn = db.connect(e2e["db"])
    rows = conn.execute(
        "SELECT e.quote, e.char_start, e.char_end, p.text FROM evidence e "
        "JOIN facts f ON f.id=e.fact_id "
        "JOIN pages p ON p.document_id=e.document_id AND p.page_index=e.page_index "
        "WHERE f.evidence_status='VERIFIED' AND e.char_start IS NOT NULL").fetchall()
    assert rows
    for r in rows:
        assert r["text"][r["char_start"]:r["char_end"]] == r["quote"]
    conn.close()


def test_chunk_error_reasons_names_the_dominant_cause(db_path):
    """A failed extract stage must say *why*, not just how many chunks broke."""
    from app.pipeline import _chunk_error_reasons

    db.init_db(db_path)
    conn = db.connect(db_path)
    with db.transaction(conn):
        conn.execute("INSERT INTO documents (sha256, stored_path, status, uploaded_at) "
                     "VALUES ('x', 'p', 'ingested', '2026-01-01T00:00:00Z')")
        conn.execute("INSERT INTO runs (document_id, run_type, status, started_at) "
                     "VALUES (1, 'full', 'running', '2026-01-01T00:00:00Z')")
        for reason in ["rate_limit"] * 3 + ["api_error"]:
            conn.execute(
                "INSERT INTO failures (run_id, document_id, failure_type, reason, created_at) "
                "VALUES (1, 1, 'run_error', ?, '2026-01-01T00:00:00Z')", (reason,),
            )
    assert _chunk_error_reasons(conn, 1) == "rate_limit x3, api_error x1"
    assert _chunk_error_reasons(conn, 999) == "no reason recorded"
    conn.close()


def test_pipeline_wires_the_llm_confirmers_when_a_key_is_present(db_path, make_pdf, monkeypatch):
    """Phase 7/9 semantic steps must actually reach the LLM.

    Regression guard: the pipeline used to construct only the Extractor, so
    resolve/reason always ran with ``llm=None`` — every pair the deterministic
    layer deferred became UNCERTAIN 'no semantic confirmer available'.
    """
    from app.config import get_settings
    from app.ingest import ingest_pdf

    monkeypatch.setenv("FKL_LLM_API_KEY_ENV", "FKL_TEST_KEY")
    monkeypatch.setenv("FKL_TEST_KEY", "not-a-real-key")
    get_settings.cache_clear()

    monkeypatch.setattr("app.llm.EntityConfirmer", lambda s: "ENTITY_LLM")
    monkeypatch.setattr("app.llm.RelationshipConfirmer", lambda s: "RELATIONSHIP_LLM")
    seen: dict[str, object] = {}
    monkeypatch.setattr("app.entities.resolve_document",
                        lambda doc, **kw: seen.update(entity=kw.get("llm")))
    monkeypatch.setattr("app.reason.reason_document",
                        lambda doc, **kw: seen.update(relationship=kw.get("llm")))

    db.init_db(db_path)
    doc_id = ingest_pdf(str(make_pdf(DOC_B_PAGES, name="wiring.pdf")),
                        database_path=db_path).document_id
    conn = db.connect(db_path)
    with db.transaction(conn):
        run_id = pipeline_start(conn, doc_id, settings=get_settings())
    conn.close()
    pipeline_run(doc_id, run_id, database_path=db_path, settings=get_settings(),
                 extractor=SubstringExtractor())

    assert seen.get("entity") == "ENTITY_LLM", "resolve stage ran without the LLM confirmer"
    assert seen.get("relationship") == "RELATIONSHIP_LLM", \
        "reason stage ran without the LLM confirmer"


def test_failed_extraction_message_names_the_real_reason(db_path, make_pdf):
    """End-to-end: the stage error must carry the chunk failure reason, which
    lives on the *extract* run, not the enclosing full run."""
    from app.config import get_settings
    from app.ingest import ingest_pdf
    from tests.fakes import FakeLLM, api_error

    db.init_db(db_path)
    doc = ingest_pdf(str(make_pdf(["Acme reported revenue for FY24."], name="boom.pdf")),
                     database_path=db_path).document_id
    conn = db.connect(db_path)
    with db.transaction(conn):
        rid = pipeline_start(conn, doc, settings=get_settings())
    conn.close()

    pipeline_run(doc, rid, database_path=db_path, settings=get_settings(),
                 extractor=FakeLLM([api_error("rate_limit", "429 quota")]))

    conn = db.connect(db_path)
    detail = conn.execute("SELECT status_detail FROM documents WHERE id = ?", (doc,)).fetchone()[0]
    conn.close()
    assert "chunk error(s)" in detail
    assert "no reason recorded" not in detail, detail
    assert "rate_limit" in detail, detail
