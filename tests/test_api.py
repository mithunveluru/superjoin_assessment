"""Phase 11 — HTTP API over the Phase 2-10 storage.

Read endpoints run against a deterministic seeded database (the Phase-10
synthetic corpus); upload/process run against an empty one. No live LLM — a
keyless ``POST /documents/{id}/process`` surfaces a failed run at HTTP 200.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import Settings, get_settings
from app.main import app
from app.pipeline import run as pipeline_run
from app.pipeline import start as pipeline_start
from evaluation.corpora.synthetic import seed_corpus
from tests.fakes import FakeLLM

S = Settings()


@pytest.fixture
def api(db_path):
    get_settings.cache_clear()
    with TestClient(app) as client:
        yield client


@pytest.fixture
def seeded_api(db_path):
    seed_corpus(db_path, settings=S)
    get_settings.cache_clear()
    with TestClient(app) as client:
        yield client


# --------------------------------------------------------------------------- #
# documents (read)                                                           #
# --------------------------------------------------------------------------- #
def test_list_documents(seeded_api):
    body = seeded_api.get("/documents").json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    assert all("counts" in d and d["counts"]["facts"] >= 0 for d in body["items"])


def test_document_detail_has_counts_and_latest_run(seeded_api):
    body = seeded_api.get("/documents/1").json()
    assert body["id"] == 1
    assert body["counts"]["relationships"] >= 1
    assert body["latest_run"] is not None
    assert body["latest_run"]["run_type"] in {"reason", "resolve", "normalize", "ground", "full"}


def test_unknown_document_404_common_error_body(seeded_api):
    resp = seeded_api.get("/documents/999")
    assert resp.status_code == 404
    assert resp.json() == {"error": {"code": "document_not_found", "message": "no document 999"}}


# --------------------------------------------------------------------------- #
# facts                                                                      #
# --------------------------------------------------------------------------- #
def test_facts_filter_matrix(seeded_api):
    g = seeded_api.get
    assert g("/facts").json()["total"] == 7
    assert g("/facts?type=numeric").json()["total"] == 7
    assert g("/facts?lifecycle_state=QUARANTINED").json()["total"] == 1
    assert g("/facts?reasoning_eligible=true").json()["total"] == 6
    assert g("/facts?evidence_status=VERIFIED").json()["total"] == 6
    assert g("/facts?document_id=2").json()["total"] == 1
    assert g("/facts?q=revenue").json()["total"] == 4
    assert g("/facts?predicate=profit").json()["total"] == 2


def test_facts_pagination(seeded_api):
    body = seeded_api.get("/facts?limit=2&offset=0").json()
    assert len(body["items"]) == 2
    assert body["total"] == 7
    page2 = seeded_api.get("/facts?limit=2&offset=6").json()
    assert len(page2["items"]) == 1


def test_fact_detail_shape(seeded_api):
    f = seeded_api.get("/facts/1").json()
    assert f["fact_type"] == "numeric"
    assert f["numeric"]["base_value"] is not None
    assert f["evidence"]["quote"]
    assert f["reporting_period"]["start"]
    assert f["repro"]["extraction_temperature"] is None or isinstance(
        f["repro"]["extraction_temperature"], float)
    assert isinstance(f["context_window"], str) and f["context_window"]
    assert isinstance(f["relationships"], list) and f["relationships"]
    assert f["entity"]["canonical_label"] == "Acme"


def test_unknown_fact_404(seeded_api):
    assert seeded_api.get("/facts/999").status_code == 404


# --------------------------------------------------------------------------- #
# relationships                                                              #
# --------------------------------------------------------------------------- #
def test_relationships_list_and_filters(seeded_api):
    g = seeded_api.get
    assert g("/relationships").json()["total"] == 15
    assert g("/relationships?category=CORROBORATES").json()["total"] == 1
    dc = g("/relationships?category=DIFFERENT_CONTEXT").json()
    assert dc["total"] == 3
    assert all(r["context_dimension"] for r in dc["items"])
    assert g("/relationships?category=UNCERTAIN").json()["total"] == 8
    assert g("/relationships?min_confidence=0.9").json()["total"] <= 15


def test_relationship_item_shape(seeded_api):
    r = seeded_api.get("/relationships?category=DIFFERENT_CONTEXT").json()["items"][0]
    assert r["category_label"] == "Reconciled by context"
    assert r["fact_a"]["id"] and r["fact_b"]["id"]
    assert isinstance(r["deterministic_signals"], dict)
    assert "period_relation" in r["deterministic_signals"]


def test_relationship_detail_has_full_facts(seeded_api):
    rid = seeded_api.get("/relationships").json()["items"][0]["id"]
    r = seeded_api.get(f"/relationships/{rid}").json()
    assert r["fact_a"]["context_window"] is not None
    assert r["fact_b"]["context_window"] is not None


def test_unknown_relationship_404(seeded_api):
    assert seeded_api.get("/relationships/999").status_code == 404


# --------------------------------------------------------------------------- #
# entities                                                                   #
# --------------------------------------------------------------------------- #
def test_entities_list_and_detail(seeded_api):
    lst = seeded_api.get("/entities").json()
    assert lst["total"] == 1
    e = lst["items"][0]
    assert e["fact_count"] == 6 and e["alias_count"] >= 0
    detail = seeded_api.get(f"/entities/{e['id']}").json()
    assert isinstance(detail["aliases"], list)
    assert detail["sample_facts"] and detail["sample_facts"][0]["fact_type"] == "numeric"
    assert seeded_api.get("/entities/999").status_code == 404


# --------------------------------------------------------------------------- #
# failures                                                                   #
# --------------------------------------------------------------------------- #
def test_failures_surface(seeded_api):
    body = seeded_api.get("/failures").json()
    assert body["total"] == 9
    assert body["counts_by_type"]["grounding_failed"] == 1
    assert body["counts_by_type"]["relationship_uncertain"] == 8
    grounding = seeded_api.get("/failures?failure_type=grounding_failed").json()
    assert grounding["total"] == 1
    row = grounding["items"][0]
    assert row["reason"]
    assert row["fact"]["lifecycle_state"] == "QUARANTINED"
    uncertain = next(i for i in body["items"] if i["failure_type"] == "relationship_uncertain")
    assert uncertain["relationship"]["category"] == "UNCERTAIN"


# --------------------------------------------------------------------------- #
# upload + process                                                           #
# --------------------------------------------------------------------------- #
def _pdf_bytes(make_pdf) -> bytes:
    return make_pdf(["Acme reported revenue of INR 100 million for FY24."]).read_bytes()


def test_upload_pdf_then_duplicate(api, make_pdf):
    data = _pdf_bytes(make_pdf)
    r1 = api.post("/documents", files={"file": ("doc.pdf", data, "application/pdf")})
    assert r1.status_code == 201
    body = r1.json()
    assert body["id"] and body["sha256"] and body["status"] in {"ingested", "uploaded"}
    assert body["duplicate"] is False

    r2 = api.post("/documents", files={"file": ("doc.pdf", data, "application/pdf")})
    assert r2.status_code == 200
    assert r2.json()["duplicate"] is True
    assert r2.json()["id"] == body["id"]


def test_upload_non_pdf_400(api):
    r = api.post("/documents", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_pdf"


def test_upload_pdf_with_bad_magic_bytes_400(api):
    r = api.post("/documents", files={"file": ("fake.pdf", b"not a pdf at all", "application/pdf")})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_pdf"


def test_process_without_key_surfaces_failed_run_at_200(api, make_pdf, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    get_settings.cache_clear()
    up = api.post("/documents", files={"file": ("d.pdf", _pdf_bytes(make_pdf), "application/pdf")})
    doc_id = up.json()["id"]

    started = api.post(f"/documents/{doc_id}/process")
    assert started.status_code == 202
    body = started.json()
    assert body["run_id"] and body["document_id"] == doc_id and body["status"] == "running"

    # TestClient runs the BackgroundTask after the response
    status = api.get(f"/documents/{doc_id}/status")
    assert status.status_code == 200
    run = status.json()["run"]
    assert run["status"] == "failed"
    assert run["error"]
    assert status.json()["status"] == "failed"


def test_process_unknown_document_404(api):
    assert api.post("/documents/999/process").status_code == 404


# --------------------------------------------------------------------------- #
# pipeline orchestration (direct, with fakes)                                #
# --------------------------------------------------------------------------- #
def test_pipeline_run_completes_done_with_empty_extraction(db_path, make_pdf):
    from app.ingest import ingest_pdf

    db.init_db(db_path)
    pdf = make_pdf(["Acme is a logistics company. It filed its FY24 report."])
    doc_id = ingest_pdf(str(pdf), database_path=db_path).document_id
    conn = db.connect(db_path)
    with db.transaction(conn):
        run_id = pipeline_start(conn, doc_id, settings=S)
    conn.close()

    result = pipeline_run(doc_id, run_id, database_path=db_path, settings=S,
                          extractor=FakeLLM([]))
    assert result["status"] == "done"

    conn = db.connect(db_path)
    run = conn.execute(
        "SELECT run_type, status, stage FROM runs WHERE id = ?", (run_id,)).fetchone()
    doc = conn.execute("SELECT status FROM documents WHERE id = ?", (doc_id,)).fetchone()
    conn.close()
    assert (run["run_type"], run["status"], run["stage"]) == ("full", "done", "complete")
    assert doc["status"] == "done"


def test_pipeline_run_marks_failed_when_a_stage_raises(db_path, make_pdf):
    from app.ingest import ingest_pdf

    class Boom:
        model = "boom"
        prompt_version = "v1"

        def extract(self, *a, **k):
            raise RuntimeError("kaboom")

    db.init_db(db_path)
    pdf = make_pdf(["Acme text for the failing pipeline test."])
    doc_id = ingest_pdf(str(pdf), database_path=db_path).document_id
    conn = db.connect(db_path)
    with db.transaction(conn):
        run_id = pipeline_start(conn, doc_id, settings=S)
    conn.close()

    result = pipeline_run(doc_id, run_id, database_path=db_path, settings=S, extractor=Boom())
    assert result["status"] == "failed"
    assert result["failed_stage"] == "extract"

    conn = db.connect(db_path)
    run = conn.execute("SELECT status, stage, error FROM runs WHERE id = ?", (run_id,)).fetchone()
    doc = conn.execute("SELECT status, status_detail FROM documents WHERE id = ?",
                       (doc_id,)).fetchone()
    conn.close()
    assert run["status"] == "failed" and run["stage"] == "extract" and run["error"]
    assert doc["status"] == "failed" and "extract" in (doc["status_detail"] or "")
