"""Phase 10 — the evaluation harness must have teeth.

The synthetic offline corpus (no LLM) exercises every property; the honesty
knobs prove the corroboration property is not satisfiable by unsupported facts;
the sweep detects configs that break a must-hold property.
"""

from __future__ import annotations

import pathlib

from app import db
from app.config import Settings
from evaluation import harness, properties
from evaluation.corpora.synthetic import seed_corpus

S = Settings()


def _seed(tmp_path, **kw) -> str:
    path = str(tmp_path / "eval.db")
    seed_corpus(path, settings=S, **kw)
    return path


# --------------------------------------------------------------------------- #
# the synthetic corpus satisfies every property + invariant                  #
# --------------------------------------------------------------------------- #
def test_synthetic_corpus_passes_all(tmp_path):
    rep = harness.run_db(_seed(tmp_path), settings=S)
    assert rep["all_pass"] is True
    assert rep["must_hold_pass"] is True
    assert rep["invariants_pass"] is True
    assert all(c["passed"] for c in rep["cases"])
    assert all(r["passed"] for r in rep["invariants"].values())
    by_cat = rep["counts"]["by_category"]
    assert by_cat["CORROBORATES"] >= 1
    assert by_cat["CONTRADICTS"] >= 1
    assert by_cat["DIFFERENT_CONTEXT"] >= 1
    assert by_cat["TEMPORAL_EVOLUTION"] >= 1
    assert by_cat["UNCERTAIN"] >= 1


def test_case_files_map_to_registered_properties():
    cases = harness.load_cases()
    assert len(cases) == 4
    assert {c["property"] for c in cases} == set(properties.CASE_PROPERTIES)
    assert all(c["must_hold"] is True for c in cases)


def test_every_property_is_structural_no_corpus_strings():
    for f in pathlib.Path("evaluation").rglob("*.py"):
        text = f.read_text(encoding="utf-8").lower()
        for needle in ("delhivery", "rbi", "reserve bank", "economic survey", " imf",
                       "sahil", "barasia", "spoton"):
            assert needle not in text, f"{f}: {needle}"


# --------------------------------------------------------------------------- #
# honesty guards — the corroboration property must fail on unsupported facts  #
# --------------------------------------------------------------------------- #
def test_break_grounding_fails_corroboration_only(tmp_path):
    rep = harness.run_db(_seed(tmp_path, break_grounding=True), settings=S)
    assert rep["properties"]["corroboration_cross_document"]["passed"] is False
    assert rep["must_hold_pass"] is False
    assert rep["all_pass"] is False
    # the harness still ran; the other invariants still hold (no ineligible fact leaked in)
    assert rep["invariants"]["no_unverified_or_ineligible_participates"]["passed"] is True


def test_fabricated_values_fail_corroboration(tmp_path):
    rep = harness.run_db(_seed(tmp_path, fabricate_corroboration=True), settings=S)
    assert rep["properties"]["corroboration_cross_document"]["passed"] is False


def test_invariant_detects_an_ineligible_fact_in_a_relationship(tmp_path):
    # the schema CHECK blocks flipping an eligible fact to UNVERIFIED, so demote
    # reasoning_eligible instead — the invariant must still catch it.
    path = _seed(tmp_path)
    conn = db.connect(path)
    fid = conn.execute("SELECT fact_a_id FROM relationships LIMIT 1").fetchone()[0]
    with db.transaction(conn):
        conn.execute("UPDATE facts SET reasoning_eligible = 0 WHERE id = ?", (fid,))
    conn.close()
    rep = harness.run_db(path, settings=S)
    assert rep["invariants"]["no_unverified_or_ineligible_participates"]["passed"] is False
    assert rep["all_pass"] is False


# --------------------------------------------------------------------------- #
# config sweep                                                               #
# --------------------------------------------------------------------------- #
def test_sweep_table_and_teeth(tmp_path):
    rep = harness.run_db(_seed(tmp_path), settings=S, sweep=True)
    rows = rep["sweep"]["rows"]
    assert len(rows) == len(harness.SWEEP_CONFIGS)
    labels = {r["label"]: r for r in rows}

    assert labels["baseline"]["must_hold_pass"] is True
    assert labels["baseline"]["invariants_pass"] is True

    # a too-high predicate similarity threshold loses the cross-document corroboration
    assert labels["predicate_similarity_threshold=0.85"]["properties"][
        "corroboration_cross_document"] is False
    # a too-loose contradiction threshold loses the strict contradiction
    assert labels["numeric_contradiction_threshold=0.50"]["properties"][
        "contradiction_strict_profile"] is False

    for r in rows:
        assert set(r["properties"]) == set(properties.CASE_PROPERTIES)
        assert isinstance(r["relationships"], int)
        assert "uncertain" in r and "quarantined" in r
    assert rep["sweep"]["recommended"] == "baseline"


def test_sweep_restores_baseline_state(tmp_path):
    path = _seed(tmp_path)
    harness.run_db(path, settings=S, sweep=True)
    conn = db.connect(path)
    n = conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]
    conn.close()
    assert n == 15  # baseline relationship count, not a swept config's


# --------------------------------------------------------------------------- #
# CLI                                                                        #
# --------------------------------------------------------------------------- #
def test_main_synthetic_exit_zero(capsys):
    rc = harness.main(["--no-report"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "corroboration_cross_document" in out
    assert "RESULT: PASS" in out


def test_main_writes_report(tmp_path, capsys):
    report_path = tmp_path / "r.json"
    rc = harness.main(["--report", str(report_path)])
    assert rc == 0
    assert report_path.exists()
    import json
    data = json.loads(report_path.read_text())
    assert data["all_pass"] is True
    assert set(data["properties"]) == set(properties.CASE_PROPERTIES)


def test_run_db_on_unprocessed_database_fails_gracefully(tmp_path):
    path = str(tmp_path / "empty.db")
    db.init_db(path)
    rep = harness.run_db(path, settings=S)
    # no relationships, no failures -> case properties fail, but the harness does not crash
    assert rep["all_pass"] is False
    assert rep["properties"]["failure_surface_populated"]["passed"] is False
