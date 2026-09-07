"""Phase 7 — entity resolution.

Deterministic clustering (generic suffix/honorific/anaphora config only) + a
borderline-only LLM confirm step exercised with a fake. No corpus alias lists,
no network, no embeddings.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.entities import (
    EntityError,
    guess_type,
    normalize_name,
    resolution_summary,
    resolve_document,
)
from app.facts import attach_evidence, insert_fact
from app.models import EntityConfirmation, EvidenceIn, FactIn
from tests.fakes import FakeEntityConfirmer

CFG = get_settings()


def _fact(conn, src, subject, *, predicate="reported", obj="a value", page=0,
          state="NORMALIZED", ev="VERIFIED") -> int:
    return insert_fact(conn, FactIn(
        document_id=src["document_id"], page_index=page, subject_raw=subject,
        predicate=predicate, object_raw=obj, fact_type="semantic", value_text=obj,
        lifecycle_state=state, evidence_status=ev,
    ))


def _entities(conn):
    return conn.execute(
        "SELECT id, canonical_label, entity_type, normalization_key, resolution_method "
        "FROM entities ORDER BY id"
    ).fetchall()


def _aliases(conn):
    return conn.execute(
        "SELECT entity_id, surface, match_method, score, source_fact_id "
        "FROM entity_aliases ORDER BY id"
    ).fetchall()


# --------------------------------------------------------------------------- #
# pure helpers                                                               #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("Delhivery", "delhivery"),
        ("Delhivery Limited", "delhivery"),
        ("DELHIVERY LTD.", "delhivery"),
        ("SSN Logistics Private Limited", "ssn logistics"),
        ("Mr. Sahil Barua", "sahil barua"),
        ("Reserve Bank of India", "reserve bank of india"),
        ("Acme Corp.", "acme"),
        ("  International  Business   Machines  ", "international business machines"),
    ],
)
def test_normalize_name(raw, key):
    assert normalize_name(raw, CFG) == key


@pytest.mark.parametrize(
    ("raw", "etype"),
    [("Dr. A. Bhattacharya", "person"), ("Shri R Kumar", "person"),
     ("Acme Ltd", "org"), ("Reserve Bank of India", "org")],
)
def test_guess_type(raw, etype):
    assert guess_type(raw, CFG) == etype


# --------------------------------------------------------------------------- #
# the acceptance cluster — deterministic, no LLM                             #
# --------------------------------------------------------------------------- #
def test_acceptance_cluster(conn, db_path, make_source):
    src = make_source([
        "This annual report is issued by Delhivery Limited. Delhivery operates a "
        "logistics network. The Company was formerly known as SSN Logistics "
        "Private Limited.",
    ])
    _fact(conn, src, "Delhivery", obj="a logistics network")
    _fact(conn, src, "Delhivery", predicate="operates in", obj="India")
    _fact(conn, src, "Delhivery Limited", predicate="is", obj="a listed company")
    _fact(conn, src, "the Company", predicate="reported revenue growth", obj="yes")
    rename_id = _fact(conn, src, "Delhivery Limited",
                      predicate="was formerly known as", obj="SSN Logistics Private Limited")
    conn.commit()

    summ = resolve_document(src["document_id"], database_path=db_path)
    assert summ.status == "done"

    ents = _entities(conn)
    assert len(ents) == 1
    eid = ents[0]["id"]

    surfaces = {a["surface"]: a for a in _aliases(conn) if a["entity_id"] == eid}
    assert {"Delhivery", "Delhivery Limited", "the Company",
            "SSN Logistics Private Limited"} <= set(surfaces)
    assert surfaces["SSN Logistics Private Limited"]["match_method"] == "derived_fact"
    assert surfaces["SSN Logistics Private Limited"]["source_fact_id"] == rename_id

    linked = conn.execute(
        "SELECT DISTINCT subject_entity_id FROM facts WHERE document_id = ?",
        (src["document_id"],),
    ).fetchall()
    assert [r["subject_entity_id"] for r in linked] == [eid]
    assert summ.derived_aliases == 1
    assert summ.anaphora_resolved == 1


def test_distinct_person_stays_separate(conn, db_path, make_source):
    src = make_source(["Delhivery is led by Mr. Sahil Barua, its CEO. Sahil Barua joined in 2012."])
    _fact(conn, src, "Delhivery")
    _fact(conn, src, "Mr. Sahil Barua", predicate="is", obj="CEO")
    _fact(conn, src, "Sahil Barua", predicate="joined in", obj="2012")
    conn.commit()

    resolve_document(src["document_id"], database_path=db_path)
    ents = _entities(conn)
    assert len(ents) == 2
    by_key = {e["normalization_key"]: e["id"] for e in ents}
    assert set(by_key) == {"delhivery", "sahil barua"}
    person_facts = conn.execute(
        "SELECT DISTINCT subject_entity_id FROM facts WHERE subject_raw LIKE '%Sahil%'"
    ).fetchall()
    assert [r["subject_entity_id"] for r in person_facts] == [by_key["sahil barua"]]


def test_similar_orgs_do_not_merge_without_llm(conn, db_path, make_source):
    src = make_source(["Delhivery and Delhivery Robotics are mentioned together here."])
    _fact(conn, src, "Delhivery")
    rob = _fact(conn, src, "Delhivery Robotics", predicate="builds", obj="automation")
    conn.commit()

    summ = resolve_document(src["document_id"], database_path=db_path)
    assert len(_entities(conn)) == 1
    assert _entities(conn)[0]["normalization_key"] == "delhivery"
    assert summ.ambiguous == 1
    assert conn.execute(
        "SELECT subject_entity_id FROM facts WHERE id = ?", (rob,)
    ).fetchone()["subject_entity_id"] is None
    assert conn.execute(
        "SELECT COUNT(*) FROM failures WHERE failure_type = 'entity_ambiguous'"
    ).fetchone()[0] == 1


# --------------------------------------------------------------------------- #
# borderline -> LLM confirm / split (fake client)                           #
# --------------------------------------------------------------------------- #
def test_llm_confirms_merge(conn, db_path, make_source):
    src = make_source(["Delhivery and Delhivery Robotics."])
    _fact(conn, src, "Delhivery")
    rob = _fact(conn, src, "Delhivery Robotics", predicate="builds", obj="automation")
    conn.commit()
    llm = FakeEntityConfirmer([EntityConfirmation(same=True, confidence=0.92,
                                                  canonical_label="Delhivery")])
    summ = resolve_document(src["document_id"], database_path=db_path, llm=llm)

    assert len(_entities(conn)) == 1
    eid = _entities(conn)[0]["id"]
    assert conn.execute(
        "SELECT subject_entity_id FROM facts WHERE id = ?", (rob,)
    ).fetchone()["subject_entity_id"] == eid
    assert conn.execute(
        "SELECT match_method FROM entity_aliases WHERE surface = 'Delhivery Robotics'"
    ).fetchone()["match_method"] == "llm"
    assert conn.execute(
        "SELECT llm_confirmed FROM entities WHERE id = ?", (eid,)
    ).fetchone()[0] == 1
    assert summ.by_method.get("llm") == 1
    assert llm.calls and set(llm.calls[0]["surfaces"]) == {"Delhivery", "Delhivery Robotics"}


def test_llm_splits_cluster(conn, db_path, make_source):
    src = make_source(["Delhivery and Delhivery Robotics."])
    _fact(conn, src, "Delhivery")
    rob = _fact(conn, src, "Delhivery Robotics", predicate="builds", obj="automation")
    conn.commit()
    llm = FakeEntityConfirmer([EntityConfirmation(
        same=False, confidence=0.8, groups=[["Delhivery"], ["Delhivery Robotics"]])])
    summ = resolve_document(src["document_id"], database_path=db_path, llm=llm)

    assert len(_entities(conn)) == 1
    assert summ.ambiguous == 1
    assert conn.execute(
        "SELECT subject_entity_id FROM facts WHERE id = ?", (rob,)
    ).fetchone()["subject_entity_id"] is None


def test_llm_error_is_conservative(conn, db_path, make_source):
    src = make_source(["Delhivery and Delhivery Robotics."])
    _fact(conn, src, "Delhivery")
    _fact(conn, src, "Delhivery Robotics", predicate="builds", obj="automation")
    conn.commit()
    llm = FakeEntityConfirmer([EntityConfirmation(same=False, error_code="api_error",
                                                  error_detail="boom")])
    summ = resolve_document(src["document_id"], database_path=db_path, llm=llm)
    assert len(_entities(conn)) == 1
    assert summ.ambiguous == 1
    assert summ.errors == 0


# --------------------------------------------------------------------------- #
# anaphora / promotion / lifecycle scope                                    #
# --------------------------------------------------------------------------- #
def test_anaphora_without_dominant_entity_is_ambiguous(conn, db_path, make_source):
    src = make_source(["The Company did well."])
    fid = _fact(conn, src, "the Company", predicate="did", obj="well")
    conn.commit()
    summ = resolve_document(src["document_id"], database_path=db_path)
    assert summ.anaphora_resolved == 0
    assert summ.ambiguous == 1
    assert conn.execute(
        "SELECT subject_entity_id FROM facts WHERE id = ?", (fid,)
    ).fetchone()["subject_entity_id"] is None


def test_promotion_requires_evidence(conn, db_path, make_source):
    src = make_source(["AAAA Delhivery grew revenue this year BBBB and more text here."])
    with_ev = _fact(conn, src, "Delhivery", predicate="grew", obj="revenue")
    _fact(conn, src, "Delhivery", predicate="also", obj="unbacked")  # no evidence chain
    attach_evidence(conn, with_ev, EvidenceIn(
        document_id=src["document_id"], page_index=0, chunk_id=src["chunk_ids"][0],
        quote="Delhivery grew revenue this year", char_start=5, char_end=36,
        verification_method="exact", evidence_status="VERIFIED"))
    conn.commit()

    summ = resolve_document(src["document_id"], database_path=db_path)
    assert summ.promoted_eligible == 1
    states = {
        r["id"]: r["lifecycle_state"] for r in conn.execute(
            "SELECT id, lifecycle_state FROM facts WHERE document_id = ?", (src["document_id"],))
    }
    assert states[with_ev] == "ELIGIBLE_FOR_REASONING"
    assert list(states.values()).count("NORMALIZED") == 1


def test_only_normalized_and_eligible_surfaces_resolved(conn, db_path, make_source):
    src = make_source(["Delhivery and Acme text."])
    _fact(conn, src, "Delhivery", state="NORMALIZED")
    cand = _fact(conn, src, "Acme Corp", state="CANDIDATE", ev="UNVERIFIED")
    grounded = _fact(conn, src, "Beta LLC", state="GROUNDED")
    conn.commit()
    resolve_document(src["document_id"], database_path=db_path)
    keys = {e["normalization_key"] for e in _entities(conn)}
    assert keys == {"delhivery"}
    for fid in (cand, grounded):
        assert conn.execute(
            "SELECT subject_entity_id FROM facts WHERE id = ?", (fid,)
        ).fetchone()["subject_entity_id"] is None


def test_idempotent(conn, db_path, make_source):
    src = make_source([
        "Issued by Delhivery Limited. Delhivery. The Company was formerly known as "
        "SSN Logistics Private Limited.",
    ])
    _fact(conn, src, "Delhivery")
    _fact(conn, src, "Delhivery Limited", predicate="is", obj="listed")
    _fact(conn, src, "the Company", predicate="did", obj="well")
    _fact(conn, src, "Delhivery Limited", predicate="was formerly known as",
          obj="SSN Logistics Private Limited")
    conn.commit()

    resolve_document(src["document_id"], database_path=db_path)
    ent1 = [dict(e) for e in _entities(conn)]
    ali1 = sorted((a["entity_id"], a["surface"], a["match_method"]) for a in _aliases(conn))
    link1 = conn.execute(
        "SELECT id, subject_entity_id FROM facts WHERE document_id = ? ORDER BY id",
        (src["document_id"],),
    ).fetchall()

    resolve_document(src["document_id"], database_path=db_path)
    ent2 = [dict(e) for e in _entities(conn)]
    ali2 = sorted((a["entity_id"], a["surface"], a["match_method"]) for a in _aliases(conn))
    link2 = conn.execute(
        "SELECT id, subject_entity_id FROM facts WHERE document_id = ? ORDER BY id",
        (src["document_id"],),
    ).fetchall()

    assert ent1 == ent2
    assert ali1 == ali2
    assert [tuple(r) for r in link1] == [tuple(r) for r in link2]
    assert conn.execute("SELECT COUNT(*) FROM failures").fetchone()[0] == 0


def test_unknown_document(db_path):
    from app import db
    db.init_db()
    with pytest.raises(EntityError) as e:
        resolve_document(999999, database_path=db_path)
    assert e.value.code == "unknown_document"


def test_resolution_summary(conn, db_path, make_source):
    src = make_source(["Delhivery Limited. Delhivery."])
    _fact(conn, src, "Delhivery")
    _fact(conn, src, "Delhivery Limited", predicate="is", obj="listed")
    conn.commit()
    resolve_document(src["document_id"], database_path=db_path)
    s = resolution_summary(conn, document_id=src["document_id"])
    assert s["entities_total"] == 1
    assert s["facts_linked"] == 2
    assert s["aliases_by_match_method"].get("deterministic", 0) >= 1


def test_module_has_no_corpus_strings():
    import pathlib
    text = pathlib.Path("app/entities.py").read_text(encoding="utf-8").lower()
    for needle in ("delhivery", "ssn logistics", "sahil", "reserve bank", "rbi",
                   "economic survey", " imf"):
        assert needle not in text


# --------------------------------------------------------------------------- #
# real starter PDF (deterministic; no LLM)                                   #
# --------------------------------------------------------------------------- #
def test_real_pdf_smoke(conn, db_path):
    import pathlib
    import re
    pdf = pathlib.Path("starter-datasets/delhivery/02-delhivery-annual-report-fy24-excerpt.pdf")
    if not pdf.exists():
        pytest.skip("starter PDF not present")
    from app.ingest import ingest_pdf
    doc = ingest_pdf(str(pdf)).document_id
    page = conn.execute(
        "SELECT text FROM pages WHERE document_id = ? AND length(text) > 200 "
        "ORDER BY page_index LIMIT 1", (doc,),
    ).fetchone()["text"]
    name = re.search(r"[A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+){1,3}", page)
    assert name, "expected a capitalised name on the page"
    subj = name.group(0)
    for pred in ("reported", "operates"):
        insert_fact(conn, FactIn(
            document_id=doc, page_index=0, subject_raw=subj, predicate=pred,
            object_raw="x", fact_type="semantic", value_text="x",
            lifecycle_state="NORMALIZED", evidence_status="VERIFIED"))
    conn.commit()

    summ = resolve_document(doc, database_path=db_path)
    assert summ.status == "done"
    assert summ.surfaces == 1
    ent = _entities(conn)
    assert len(ent) == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM facts WHERE document_id = ? AND subject_entity_id = ?",
        (doc, ent[0]["id"]),
    ).fetchone()[0] == 2
