"""Phase 4 — candidate fact extraction.

Uses a deterministic FakeLLM (tests/fakes.py); no Gemini API calls. Covers the
structured-output contract, deterministic candidate validation, persistence as
non-eligible CANDIDATE facts, raw-payload/run-metadata preservation, the
FACT->EVIDENCE->CHUNK->PAGE->DOCUMENT provenance chain, failure isolation, and
the lightweight extraction_summary observability.
"""

from __future__ import annotations

import pytest

from app import db
from app.extract import ExtractError, extract_document, extraction_summary
from app.facts import evidence_chain
from tests.fakes import FakeLLM, api_error, candidate

PAGE = "Acme Corp reported revenue of 42 units in fiscal year 2024 across the group."


def _one_page_doc(make_source):
    return make_source([PAGE])


# --------------------------------------------------------------------------- #
# structured extraction                                                      #
# --------------------------------------------------------------------------- #
def test_zero_facts_chunk(conn, db_path, make_source):
    src = _one_page_doc(make_source)
    res = extract_document(src["document_id"], client=FakeLLM([{"facts": []}]))
    assert res.status == "done"
    assert res.chunks_processed == 1
    assert res.candidates_persisted == 0
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
    # a raw_extractions row is still written for the chunk
    assert conn.execute("SELECT COUNT(*) FROM raw_extractions").fetchone()[0] == 1


def test_valid_semantic_fact_persisted(conn, db_path, make_source):
    src = _one_page_doc(make_source)
    cand = candidate(subject="Acme Corp", predicate="reported revenue",
                     object="42 units", fact_type="semantic",
                     quote="reported revenue", char_start=10, char_end=26)
    res = extract_document(src["document_id"], client=FakeLLM([{"facts": [cand]}]))
    assert res.candidates_persisted == 1
    row = conn.execute("SELECT * FROM facts").fetchone()
    assert row["fact_type"] == "semantic"
    assert row["subject_raw"] == "Acme Corp"
    assert row["value_text"] == "42 units"
    assert row["lifecycle_state"] == "CANDIDATE"
    assert row["reasoning_eligible"] == 0
    assert row["evidence_status"] == "UNVERIFIED"
    assert res.facts_by_type == {"semantic": 1}


def test_valid_numeric_fact_preserves_raw_representation(conn, db_path, make_source):
    src = _one_page_doc(make_source)
    cand = candidate(
        subject="Acme Corp", predicate="revenue", object="42 units",
        fact_type="numeric", raw_value_text="42 units", parsed_value=42.0,
        unit="units", magnitude=None, currency=None, percentage=False,
        quote="42 units", char_start=29, char_end=37,
    )
    extract_document(src["document_id"], client=FakeLLM([{"facts": [cand]}]))
    row = conn.execute("SELECT * FROM facts").fetchone()
    assert row["fact_type"] == "numeric"
    assert row["value_raw"] == "42 units"
    assert row["numeric_value"] == 42.0
    assert row["unit_raw"] == "units"
    assert row["value_text"] is None
    # no normalization in Phase 4
    assert row["magnitude_factor"] is None
    assert row["base_value"] is None
    assert row["unit_norm"] is None
    assert row["reporting_period_start"] is None


@pytest.mark.parametrize("ft", ["temporal", "categorical"])
def test_temporal_and_categorical_facts(conn, db_path, make_source, ft):
    src = _one_page_doc(make_source)
    cand = candidate(fact_type=ft, quote="fiscal year 2024", char_start=41, char_end=57,
                     reporting_period="fiscal year 2024", period_type="fiscal_year")
    extract_document(src["document_id"], client=FakeLLM([{"facts": [cand]}]))
    row = conn.execute("SELECT fact_type, reporting_period_raw, reporting_period_type "
                       "FROM facts").fetchone()
    assert row["fact_type"] == ft
    assert row["reporting_period_raw"] == "fiscal year 2024"
    assert row["reporting_period_type"] == "fiscal_year"


def test_multiple_facts_from_one_chunk(conn, db_path, make_source):
    src = _one_page_doc(make_source)
    cands = [
        candidate(predicate="a", object="x", quote="Acme", char_start=0, char_end=4),
        candidate(predicate="b", object="y", quote="Corp", char_start=5, char_end=9),
        candidate(predicate="c", object="z", quote="group", char_start=68, char_end=73),
    ]
    res = extract_document(src["document_id"], client=FakeLLM([{"facts": cands}]))
    assert res.candidates_generated == 3
    assert res.candidates_persisted == 3


def test_modality_mapped_to_uppercase(conn, db_path, make_source):
    src = _one_page_doc(make_source)
    cand = candidate(modality="forecast", quote="Acme", char_start=0, char_end=4)
    extract_document(src["document_id"], client=FakeLLM([{"facts": [cand]}]))
    assert conn.execute("SELECT modality FROM facts").fetchone()["modality"] == "FORECAST"


# --------------------------------------------------------------------------- #
# deterministic validation (payload preserved, siblings survive)             #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"subject": "  "}, "missing_subject"),
        ({"predicate": ""}, "missing_predicate"),
        ({"object": ""}, "missing_object"),
        ({"quote": ""}, "missing_quote"),
        ({"fact_type": "financial"}, "invalid_fact_type"),
        ({"modality": "definitely"}, "invalid_modality"),
        ({"period_type": "eon"}, "invalid_period_type"),
        ({"char_start": 5, "char_end": 5}, "invalid_offsets"),
        ({"char_start": 9, "char_end": 3}, "invalid_offsets"),
        ({"char_start": 0, "char_end": 9999}, "invalid_offsets"),
        ({"char_start": -1, "char_end": 4}, "invalid_offsets"),
        ({"fact_type": "numeric", "raw_value_text": None, "parsed_value": None},
         "numeric_missing_value"),
    ],
)
def test_candidate_rejected(conn, db_path, make_source, over, reason):
    src = _one_page_doc(make_source)
    good = candidate(predicate="good", object="ok", quote="Acme", char_start=0, char_end=4)
    bad = candidate(**over)
    res = extract_document(src["document_id"], client=FakeLLM([{"facts": [good, bad]}]))

    assert res.candidates_persisted == 1          # the good one survives
    assert res.candidates_rejected == 1
    fail = conn.execute(
        "SELECT failure_type, reason, detail FROM failures "
        "WHERE failure_type = 'extraction_unparsed'"
    ).fetchone()
    assert fail["reason"] == reason
    import json
    detail = json.loads(fail["detail"])
    assert detail["candidate_index"] == 1
    assert detail["candidate"]["predicate"] == bad["predicate"]  # payload preserved


def test_duplicate_candidate_in_same_chunk_rejected(conn, db_path, make_source):
    src = _one_page_doc(make_source)
    dup = candidate(subject="Acme", predicate="p", object="v", quote="Acme",
                    char_start=0, char_end=4)
    res = extract_document(src["document_id"], client=FakeLLM([{"facts": [dup, dict(dup)]}]))
    assert res.candidates_persisted == 1
    assert res.candidates_rejected == 1
    assert conn.execute(
        "SELECT reason FROM failures WHERE failure_type='extraction_unparsed'"
    ).fetchone()["reason"] == "duplicate_in_chunk"


def test_malformed_json_response(conn, db_path, make_source):
    src = _one_page_doc(make_source)
    res = extract_document(src["document_id"], client=FakeLLM(["this is not json at all"]))
    assert res.status == "done"
    assert res.candidates_persisted == 0
    raw = conn.execute(
        "SELECT raw_response, parse_error, item_count FROM raw_extractions"
    ).fetchone()
    assert raw["raw_response"] == "this is not json at all"
    assert raw["parse_error"] is not None
    assert raw["item_count"] is None
    assert conn.execute(
        "SELECT reason FROM failures WHERE failure_type='extraction_unparsed'"
    ).fetchone()["reason"] == "malformed_response"


def test_unknown_key_in_candidate_is_malformed(conn, db_path, make_source):
    src = _one_page_doc(make_source)
    bad = {"facts": [candidate() | {"speculation": True}]}
    res = extract_document(src["document_id"], client=FakeLLM([bad]))
    assert res.candidates_persisted == 0
    assert res.candidates_generated == 0  # whole response failed to parse


# --------------------------------------------------------------------------- #
# persistence: raw payload + run metadata                                    #
# --------------------------------------------------------------------------- #
def test_raw_payload_and_run_metadata_preserved(conn, db_path, make_source):
    src = _one_page_doc(make_source)
    cand = candidate(predicate="net revenue", object="42 units",
                     quote="revenue", char_start=18, char_end=25, magnitude=None)
    res = extract_document(src["document_id"], client=FakeLLM([{"facts": [cand]}]))

    fact = conn.execute("SELECT * FROM facts").fetchone()
    import json
    assert json.loads(fact["raw_payload"])["predicate"] == "net revenue"
    assert fact["run_id"] == res.run_id
    assert fact["extraction_model"] == "fake-sonnet"
    assert fact["prompt_version"] == "test-v1"
    assert fact["extracted_at"]
    assert fact["raw_extraction_id"] is not None

    run = conn.execute("SELECT * FROM runs WHERE id = ?", (res.run_id,)).fetchone()
    assert run["run_type"] == "extract"
    assert run["status"] == "done"
    assert run["model_name"] == "fake-sonnet"
    assert json.loads(run["prompt_versions"]) == {"extract": "test-v1"}
    assert "temperature" in json.loads(run["settings"])
    assert run["facts_extracted"] == 1
    assert run["chunks_processed"] == 1
    assert run["llm_calls"] == 1
    assert run["estimated_cost_usd"] is not None

    raw = conn.execute("SELECT * FROM raw_extractions WHERE id = ?",
                       (fact["raw_extraction_id"],)).fetchone()
    assert raw["item_count"] == 1
    assert raw["temperature"] is None       # Sonnet 5 sends no sampling params
    assert raw["chunk_id"] == src["chunk_ids"][0]


# --------------------------------------------------------------------------- #
# provenance chain                                                           #
# --------------------------------------------------------------------------- #
def test_candidate_evidence_chain_traceable(conn, db_path, make_source):
    src = _one_page_doc(make_source)
    cand = candidate(quote="Acme Corp", char_start=0, char_end=9)
    extract_document(src["document_id"], client=FakeLLM([{"facts": [cand]}]))

    fact_id = conn.execute("SELECT id FROM facts").fetchone()["id"]
    chain = evidence_chain(conn, fact_id)
    assert chain is not None
    assert chain["document_id"] == src["document_id"]
    assert chain["page_id"] == src["page_ids"][0]
    assert chain["chunk_id"] == src["chunk_ids"][0]

    ev = conn.execute("SELECT * FROM evidence WHERE fact_id = ?", (fact_id,)).fetchone()
    assert ev["verification_method"] == "unverified"   # NOT verified in Phase 4
    assert ev["evidence_status"] == "UNVERIFIED"
    assert ev["quote"] == "Acme Corp"
    assert (ev["char_start"], ev["char_end"]) == (0, 9)


def test_evidence_offsets_are_page_relative(conn, db_path, make_source):
    src = make_source(["padding padding padding Acme Corp reported figures here"])
    # replace the single chunk with a page-slice starting at offset 24
    doc_id = src["document_id"]
    conn.execute("DELETE FROM chunks WHERE document_id = ?", (doc_id,))
    page_text = src["page_texts"][0]
    chunk_id = conn.execute(
        "INSERT INTO chunks (document_id, page_index, seq, char_offset, char_end, text) "
        "VALUES (?, 0, 0, 24, ?, ?)",
        (doc_id, len(page_text), page_text[24:]),
    ).lastrowid
    conn.commit()

    cand = candidate(quote="Acme Corp", char_start=0, char_end=9)  # into the chunk
    extract_document(doc_id, client=FakeLLM([{"facts": [cand]}]))

    ev = conn.execute("SELECT char_start, char_end, chunk_id FROM evidence").fetchone()
    assert ev["chunk_id"] == chunk_id
    assert (ev["char_start"], ev["char_end"]) == (24, 33)   # 24 + candidate offsets


def test_doc_header_is_corpus_agnostic(make_source):
    src = _one_page_doc(make_source)
    fake = FakeLLM([{"facts": []}])
    extract_document(src["document_id"], client=fake)
    header = fake.calls[0]["doc_header"]
    assert f"document_id={src['document_id']}" in header
    for corpus_token in ("Acme", "Delhivery", "RBI", "revenue", "42"):
        assert corpus_token not in header


# --------------------------------------------------------------------------- #
# failure isolation                                                          #
# --------------------------------------------------------------------------- #
def test_api_error_on_one_chunk_isolated(conn, db_path, make_source):
    src = make_source(["page zero AAAA text", "page one BBBB text", "page two CCCC text"])
    ok = {"facts": [candidate(quote="page", char_start=0, char_end=4)]}
    res = extract_document(
        src["document_id"],
        client=FakeLLM([ok, api_error("api_error", "5xx"), ok]),
    )
    assert res.status == "done"
    assert res.chunks_processed == 3
    assert res.extraction_errors == 1
    assert res.candidates_persisted == 2          # chunks 0 and 2 survived
    fail = conn.execute(
        "SELECT reason, ref_table FROM failures WHERE failure_type = 'run_error'"
    ).fetchone()
    assert fail["reason"] == "api_error"
    assert fail["ref_table"] == "raw_extractions"


def test_client_exception_on_one_chunk_isolated(conn, db_path, make_source):
    src = make_source(["chunk zero text here", "chunk one text here"])
    ok = {"facts": [candidate(quote="chunk", char_start=0, char_end=5)]}
    res = extract_document(
        src["document_id"], client=FakeLLM([RuntimeError("kaboom"), ok]),
    )
    assert res.status == "done"
    assert res.extraction_errors == 1
    assert res.candidates_persisted == 1
    assert conn.execute(
        "SELECT reason FROM failures WHERE failure_type='run_error'"
    ).fetchone()["reason"] == "llm_client_exception"


def test_auth_error_fails_the_run(conn, db_path, make_source):
    src = _one_page_doc(make_source)
    with pytest.raises(ExtractError) as ei:
        extract_document(src["document_id"], client=FakeLLM([api_error("auth", "401")]))
    assert ei.value.code == "llm_auth_failed"
    run = conn.execute("SELECT status FROM runs WHERE id = ?", (ei.value.run_id,)).fetchone()
    assert run["status"] == "failed"


def test_unknown_document(db_path):
    db.init_db()
    with pytest.raises(ExtractError) as ei:
        extract_document(999999, client=FakeLLM())
    assert ei.value.code == "unknown_document"


def test_document_not_ingested(conn, db_path, make_source):
    src = _one_page_doc(make_source)
    conn.execute("UPDATE documents SET status = 'failed' WHERE id = ?", (src["document_id"],))
    conn.commit()
    with pytest.raises(ExtractError) as ei:
        extract_document(src["document_id"], client=FakeLLM())
    assert ei.value.code == "document_not_ingested"


def test_missing_api_key_fails_fast_naming_the_variable(conn, db_path, make_source,
                                                        monkeypatch):
    """No key must fail at client construction, not once per chunk mid-run."""
    from app.config import get_settings
    from app.llm import LLMConfigError

    # a name nothing sets, so the assertion holds whatever is in the real .env
    monkeypatch.setenv("FKL_LLM_API_KEY_ENV", "FKL_TEST_ABSENT_KEY")
    monkeypatch.delenv("FKL_TEST_ABSENT_KEY", raising=False)
    get_settings.cache_clear()
    src = _one_page_doc(make_source)
    with pytest.raises(LLMConfigError) as ei:
        extract_document(src["document_id"])  # no client injected -> builds a real one
    assert "FKL_TEST_ABSENT_KEY" in str(ei.value)
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# observability                                                              #
# --------------------------------------------------------------------------- #
def test_extraction_summary(conn, db_path, make_source):
    src = make_source(["alpha page text here", "bravo page text here"])
    good = candidate(quote="alpha", char_start=0, char_end=5)
    bad = candidate(char_start=0, char_end=999)  # invalid offsets
    numeric = candidate(fact_type="numeric", raw_value_text="7", parsed_value=7.0,
                        quote="page", char_start=6, char_end=10)
    res = extract_document(
        src["document_id"],
        client=FakeLLM([{"facts": [good, bad]}, {"facts": [numeric]}]),
    )
    summ = extraction_summary(conn, document_id=src["document_id"])
    assert summ["chunks_processed"] == 2
    assert summ["candidates_generated"] == 3
    assert summ["candidates_persisted"] == 2
    assert summ["candidates_rejected"] == 1
    assert summ["extraction_errors"] == 0
    assert summ["facts_by_type"] == {"semantic": 1, "numeric": 1}
    assert summ["facts_by_document"] == {src["document_id"]: 2}
    assert res.candidates_persisted == 2


def test_max_llm_calls_per_doc_caps_extraction(conn, db_path, make_source, monkeypatch):
    """The per-document LLM call budget stops extraction and records why."""
    from app.config import get_settings

    monkeypatch.setenv("FKL_MAX_LLM_CALLS_PER_DOC", "2")
    get_settings.cache_clear()
    src = make_source([f"Acme reported {n} units in FY24." for n in range(5)])
    client = FakeLLM()
    res = extract_document(src["document_id"], client=client)

    assert len(client.calls) == 2, "extraction must stop at the configured budget"
    assert res.chunks_processed == 2
    capped = conn.execute(
        "SELECT reason, detail FROM failures WHERE failure_type = 'run_error' "
        "AND reason = 'max_llm_calls_per_doc'"
    ).fetchall()
    assert len(capped) == 1
    assert '"limit": 2' in capped[0]["detail"]


def test_sustained_provider_failure_aborts_early(conn, db_path, make_source, monkeypatch):
    """A dead/quota-exhausted provider must stop the run, not grind every chunk.

    Regression: a 300-chunk document kept calling for all 300 chunks while every
    call returned 429, each with the SDK's full retry/backoff ladder — ~4 hours
    to produce nothing.
    """
    from app.config import get_settings

    monkeypatch.setenv("FKL_EXTRACT_CONSECUTIVE_ERROR_LIMIT", "3")
    get_settings.cache_clear()
    src = make_source([f"Acme reported {n} units in FY24." for n in range(20)])
    client = FakeLLM([api_error("rate_limit", "429 quota") for _ in range(20)])
    res = extract_document(src["document_id"], client=client)

    assert len(client.calls) == 3, "must stop at the consecutive-error limit"
    assert res.chunks_processed == 3
    row = conn.execute(
        "SELECT detail FROM failures WHERE reason = 'consecutive_chunk_errors'"
    ).fetchone()
    assert row is not None, "the early abort must be recorded"
    assert '"last_reason": "rate_limit"' in row["detail"]


def test_isolated_failures_do_not_abort_the_run(conn, db_path, make_source, monkeypatch):
    """The counter resets on success: scattered bad chunks never trip the abort."""
    from app.config import get_settings

    monkeypatch.setenv("FKL_EXTRACT_CONSECUTIVE_ERROR_LIMIT", "3")
    get_settings.cache_clear()
    src = make_source([f"Acme reported {n} units in FY24." for n in range(6)])
    good = {"facts": []}
    script = [api_error("rate_limit", "429"), good, api_error("rate_limit", "429"), good,
              api_error("rate_limit", "429"), good]
    res = extract_document(src["document_id"], client=FakeLLM(script))

    assert res.chunks_processed == 6, "no abort — failures were never consecutive"
    assert conn.execute(
        "SELECT COUNT(*) FROM failures WHERE reason = 'consecutive_chunk_errors'"
    ).fetchone()[0] == 0
