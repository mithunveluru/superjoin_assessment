"""Phase 10 — expected-property predicates over a processed database.

Each predicate is a **structural** check: it names no entity, figure, or
filename, and generalises to any corpus. ``CASE_PROPERTIES`` are the four
required cases (EVALUATION_PLAN §4); ``INVARIANTS`` are the global checks run on
every evaluation (EVALUATION_PLAN §3.4). Signal values come from the
``relationships.deterministic_signals`` JSON that Phase 9 persisted.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from app.config import Settings


@dataclass
class PropertyResult:
    name: str
    passed: bool
    detail: str
    rows: list[dict] = field(default_factory=list)


def _relationships(conn: sqlite3.Connection) -> list[dict]:
    out: list[dict] = []
    for r in conn.execute(
        "SELECT r.id, r.category, r.context_dimension, r.reasoning, r.confidence, "
        "r.validation_action, r.llm_used, r.deterministic_signals, "
        "fa.document_id AS a_doc, fa.object_raw AS a_obj, fa.predicate AS a_pred, "
        "fa.evidence_status AS a_ev, fb.document_id AS b_doc, fb.object_raw AS b_obj, "
        "fb.predicate AS b_pred, fb.evidence_status AS b_ev "
        "FROM relationships r JOIN facts fa ON fa.id = r.fact_a_id "
        "JOIN facts fb ON fb.id = r.fact_b_id ORDER BY r.id"
    ):
        d = dict(r)
        try:
            d["signals"] = json.loads(r["deterministic_signals"] or "{}")
        except (json.JSONDecodeError, TypeError):
            d["signals"] = {}
        out.append(d)
    return out


def _brief(d: dict) -> dict:
    return {
        "id": d["id"], "category": d["category"], "context_dimension": d["context_dimension"],
        "a": f"doc{d['a_doc']}:{d['a_pred']}={d['a_obj']}",
        "b": f"doc{d['b_doc']}:{d['b_pred']}={d['b_obj']}",
        "delta_pct": d["signals"].get("base_value_delta_pct"),
    }


# --------------------------------------------------------------------------- #
# the four required cases                                                     #
# --------------------------------------------------------------------------- #
def corroboration_cross_document(conn: sqlite3.Connection, settings: Settings) -> PropertyResult:
    """Case 1 — ∃ CORROBORATES across two documents, differing object_raw and
    predicate, both sides VERIFIED/PARTIAL (≥1 VERIFIED),
    base_value_delta_pct ≤ numeric_equivalence_tolerance."""
    tol = settings.numeric_equivalence_tolerance
    hits = []
    for d in _relationships(conn):
        s = d["signals"]
        if d["category"] != "CORROBORATES":
            continue
        if d["a_doc"] == d["b_doc"] or d["a_obj"] == d["b_obj"] or d["a_pred"] == d["b_pred"]:
            continue
        ev = {d["a_ev"], d["b_ev"]}
        if not ev <= {"VERIFIED", "PARTIAL"} or "VERIFIED" not in ev:
            continue
        delta = s.get("base_value_delta_pct")
        if delta is not None and delta <= tol:
            hits.append(_brief(d))
    return PropertyResult(
        "corroboration_cross_document", bool(hits),
        f"{len(hits)} cross-document CORROBORATES within the equivalence tolerance"
        if hits else "no cross-document CORROBORATES with delta ≤ numeric_equivalence_tolerance",
        hits,
    )


def contradiction_strict_profile(conn: sqlite3.Connection, settings: Settings) -> PropertyResult:
    """Case 2 — ∃ CONTRADICTS with the strict signal profile: same entity,
    predicate_sim ≥ threshold, unit_equivalent, period equal/overlaps, no scope
    conflict, both modality HISTORICAL/ASSERTED, delta > contradiction threshold,
    not demoted, reasoning present. If absent, the run records that (§4)."""
    pst = settings.predicate_similarity_threshold
    contr = settings.numeric_contradiction_threshold
    hits = []
    for d in _relationships(conn):
        s = d["signals"]
        if d["category"] != "CONTRADICTS":
            continue
        if s.get("entity_relation") != "same":
            continue
        if not (s.get("predicate_exact") or (s.get("predicate_similarity") or 0.0) >= pst):
            continue
        if not s.get("unit_equivalent"):
            continue
        if s.get("period_relation") not in ("equal", "overlaps"):
            continue
        if s.get("scope_conflict"):
            continue
        if s.get("modality_a") not in ("HISTORICAL", "ASSERTED"):
            continue
        if s.get("modality_b") not in ("HISTORICAL", "ASSERTED"):
            continue
        if (s.get("base_value_delta_pct") or 0.0) <= contr:
            continue
        if d["validation_action"] not in ("not_applicable", "accepted"):
            continue
        if not (d["reasoning"] or "").strip():
            continue
        hits.append(_brief(d))
    return PropertyResult(
        "contradiction_strict_profile", bool(hits),
        f"{len(hits)} CONTRADICTS meeting the strict signal profile" if hits else
        "no face-value contradiction meeting the strict profile in this corpus — "
        "Case 2 must be shown from constructed facts (EVALUATION_PLAN §4)",
        hits,
    )


def context_reconciled_with_dimension(conn: sqlite3.Connection,
                                      settings: Settings) -> PropertyResult:
    """Case 3 — ∃ DIFFERENT_CONTEXT or TEMPORAL_EVOLUTION where the value gap
    *looks* like a conflict (delta > contradiction threshold) but a
    context_dimension is set, reasoning is present, and both sides are grounded."""
    contr = settings.numeric_contradiction_threshold
    hits = []
    for d in _relationships(conn):
        s = d["signals"]
        if d["category"] not in ("DIFFERENT_CONTEXT", "TEMPORAL_EVOLUTION"):
            continue
        if (s.get("base_value_delta_pct") or 0.0) <= contr:
            continue
        if not (d["context_dimension"] or "").strip():
            continue
        if not (d["reasoning"] or "").strip():
            continue
        if "UNVERIFIED" in (d["a_ev"], d["b_ev"]):
            continue
        hits.append(_brief(d))
    return PropertyResult(
        "context_reconciled_with_dimension", bool(hits),
        f"{len(hits)} apparent-conflict pair(s) reconciled by a named context dimension"
        if hits else "no DIFFERENT_CONTEXT / TEMPORAL_EVOLUTION with a value gap past the "
        "contradiction threshold and a context_dimension",
        hits,
    )


_MACHINE_FAILURES = {
    "extraction_unparsed", "grounding_failed", "context_incomplete", "ocr_page",
    "normalization_failed", "entity_ambiguous", "relationship_uncertain",
}


def failure_surface_populated(conn: sqlite3.Connection, settings: Settings) -> PropertyResult:
    """Case 4 — the failures table is non-empty with ≥1 machine-readable type,
    every row has a reason, and every fact a failure points at is QUARANTINED and
    absent from every relationship."""
    fails = [dict(r) for r in conn.execute(
        "SELECT id, failure_type, reason, ref_table, ref_id FROM failures"
    )]
    if not fails:
        return PropertyResult("failure_surface_populated", False, "failures table is empty", [])
    typed = [f for f in fails if f["failure_type"] in _MACHINE_FAILURES]
    no_reason = [f["id"] for f in fails if not (f["reason"] or "").strip()]
    leaked = []
    for f in fails:
        if f["ref_table"] != "facts" or not f["ref_id"]:
            continue
        row = conn.execute(
            "SELECT lifecycle_state FROM facts WHERE id = ?", (f["ref_id"],)
        ).fetchone()
        if row and row["lifecycle_state"] != "QUARANTINED":
            leaked.append(f["ref_id"])
        if conn.execute(
            "SELECT 1 FROM relationships WHERE fact_a_id = ? OR fact_b_id = ?",
            (f["ref_id"], f["ref_id"]),
        ).fetchone():
            leaked.append(f["ref_id"])
    passed = bool(typed) and not no_reason and not leaked
    detail = (f"{len(fails)} failure row(s), {len(typed)} machine-typed"
              if passed else
              f"empty_reason={no_reason} leaked_facts={leaked} typed={len(typed)}")
    return PropertyResult("failure_surface_populated", passed, detail,
                          [{"type": f["failure_type"], "reason": f["reason"]} for f in fails[:10]])


# --------------------------------------------------------------------------- #
# global invariants                                                          #
# --------------------------------------------------------------------------- #
def _inv(name: str, bad: list, singular: str) -> PropertyResult:
    return PropertyResult(name, not bad,
                          "holds" if not bad else f"{len(bad)} {singular}: {bad[:8]}",
                          [{"id": x} for x in bad[:20]])


def relationship_evidence_both_sides(conn: sqlite3.Connection,
                                     settings: Settings) -> PropertyResult:
    bad = [r["id"] for r in conn.execute(
        "SELECT r.id FROM relationships r WHERE "
        "NOT EXISTS (SELECT 1 FROM evidence e JOIN pages p "
        "  ON p.document_id = e.document_id AND p.page_index = e.page_index "
        "  WHERE e.fact_id = r.fact_a_id) OR "
        "NOT EXISTS (SELECT 1 FROM evidence e JOIN pages p "
        "  ON p.document_id = e.document_id AND p.page_index = e.page_index "
        "  WHERE e.fact_id = r.fact_b_id)"
    )]
    return _inv("relationship_evidence_both_sides", bad,
                "relationship(s) missing an evidence chain")


def no_unverified_or_ineligible_participates(conn: sqlite3.Connection,
                                             settings: Settings) -> PropertyResult:
    bad = [r["id"] for r in conn.execute(
        "SELECT DISTINCT r.id FROM relationships r JOIN facts f "
        "ON f.id IN (r.fact_a_id, r.fact_b_id) "
        "WHERE f.evidence_status = 'UNVERIFIED' OR f.reasoning_eligible = 0"
    )]
    return _inv("no_unverified_or_ineligible_participates", bad,
                "relationship(s) with an unverified / ineligible fact")


def context_dimension_present(conn: sqlite3.Connection, settings: Settings) -> PropertyResult:
    bad = [r["id"] for r in conn.execute(
        "SELECT id FROM relationships "
        "WHERE category IN ('TEMPORAL_EVOLUTION', 'DIFFERENT_CONTEXT') "
        "AND (context_dimension IS NULL OR trim(context_dimension) = '')"
    )]
    return _inv("context_dimension_present", bad,
                "context relationship(s) without a context_dimension")


def uncertain_and_failure_have_reason(conn: sqlite3.Connection,
                                      settings: Settings) -> PropertyResult:
    bad = [f"rel:{r['id']}" for r in conn.execute(
        "SELECT id FROM relationships WHERE category = 'UNCERTAIN' "
        "AND (reasoning IS NULL OR trim(reasoning) = '')"
    )]
    bad += [f"fail:{r['id']}" for r in conn.execute(
        "SELECT id FROM failures WHERE reason IS NULL OR trim(reason) = ''"
    )]
    return _inv("uncertain_and_failure_have_reason", bad, "row(s) missing a reason")


CASE_PROPERTIES = {
    "corroboration_cross_document": corroboration_cross_document,
    "contradiction_strict_profile": contradiction_strict_profile,
    "context_reconciled_with_dimension": context_reconciled_with_dimension,
    "failure_surface_populated": failure_surface_populated,
}

INVARIANTS = {
    "relationship_evidence_both_sides": relationship_evidence_both_sides,
    "no_unverified_or_ineligible_participates": no_unverified_or_ineligible_participates,
    "context_dimension_present": context_dimension_present,
    "uncertain_and_failure_have_reason": uncertain_and_failure_have_reason,
}
