"""Phase 9 — relationship reasoning.

Consumes Phase 8 candidate pairs (``app.retrieve.retrieve_candidates``) and their
deterministic ``SignalSet`` and decides the relationship category:

    CandidatePair -> deterministic verdict -> (optional) LLM proposal ->
    deterministic validation -> final category -> persisted relationship + rationale

Deterministic logic comes first and has the final say. The LLM (if a confirmer is
supplied) only proposes a category for genuinely semantic questions the signals
cannot settle (predicate synonymy, statement polarity); ``_validate_llm`` then
accepts / overrides / downgrades that proposal against the same signals. With no
confirmer, an unsettled pair is ``UNCERTAIN`` — the system still produces every
deterministic category without an API key.

Phase 9 does NOT retrieve, re-resolve entities, re-verify evidence, or
re-normalize. It never changes a fact's lifecycle or evidence. It persists
through the existing ``app.facts.add_relationship`` (canonical (min,max) pair,
idempotent first-write-wins).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app import db
from app.config import Settings, get_settings
from app.facts import add_relationship
from app.models import (
    CandidatePair,
    DocReasoningSummary,
    RelationshipDecision,
    RelationshipIn,
    RelationshipProposal,
    SignalSet,
)
from app.retrieve import retrieve_candidates

log = logging.getLogger("fkl.reason")

CATEGORIES = frozenset({
    "CORROBORATES", "CONTRADICTS", "DIFFERENT_CONTEXT", "TEMPORAL_EVOLUTION", "UNCERTAIN",
})
_DISTINCT_PERIODS = frozenset({"adjacent", "disjoint", "same_year"})
_WS_RE = re.compile(r"\s+")


class ReasonError(RuntimeError):
    def __init__(self, code: str, *, detail: Any = None, run_id: int | None = None):
        self.code = code
        self.detail = detail
        self.run_id = run_id
        super().__init__(code if detail is None else f"{code}: {detail}")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: str | None) -> dict | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


# --------------------------------------------------------------------------- #
# deterministic verdict                                                      #
# --------------------------------------------------------------------------- #
@dataclass
class Verdict:
    category: str | None            # None -> needs a semantic step
    context_dimension: str | None
    code: str                       # stable machine code for observability
    reason: str


def _pred_ok(s: SignalSet, settings: Settings) -> bool:
    return s.predicate_exact or s.predicate_similarity >= settings.predicate_similarity_threshold


def deterministic_verdict(s: SignalSet, *, objects_equal: bool,
                          settings: Settings | None = None) -> Verdict:
    """Everything the signals alone can decide. Conservative: an ambiguous pair
    returns ``category=None`` (semantic step) or ``UNCERTAIN``."""
    settings = settings or get_settings()
    tol = settings.numeric_equivalence_tolerance
    contr = settings.numeric_contradiction_threshold
    delta = s.base_value_delta_pct if s.base_value_delta_pct is not None else 0.0
    pred_ok = _pred_ok(s, settings)

    if s.entity_relation != "same":
        return Verdict("UNCERTAIN", None, "entity_not_same",
                       f"subjects are {s.entity_relation}; not confidently the same entity")
    if s.period_relation == "unknown":
        return Verdict("UNCERTAIN", None, "period_unknown",
                       "a reporting period could not be resolved")
    if not pred_ok and s.predicate_token_overlap < 0.34:
        return Verdict("UNCERTAIN", None, "weak_predicate",
                       "predicates are not clearly the same measure")
    if not s.fact_type_match and not s.numeric_comparable:
        return Verdict("UNCERTAIN", None, "fact_type_mismatch",
                       "facts are different types and not numerically comparable")
    if s.percentage_vs_absolute:
        return Verdict("UNCERTAIN", None, "percentage_vs_absolute",
                       "one value is a percentage and the other is not; not directly comparable")
    if s.scope_conflict:
        return Verdict("DIFFERENT_CONTEXT", f"scope:{s.scope_conflict[0]}", "scope_conflict",
                       f"scope differs on {', '.join(s.scope_conflict)}; the two statements "
                       "describe different slices")
    if not s.modality_comparable:
        if (s.modality_relation == "same" and s.numeric_comparable and s.unit_equivalent
                and delta <= tol and s.period_relation in ("equal", "contains")):
            return Verdict("CORROBORATES", None, "same_modality_agree",
                           f"both {s.modality_a} for the same period and equal within tolerance")
        return Verdict("DIFFERENT_CONTEXT", "modality", "modality_mismatch",
                       f"modality differs ({s.modality_a} vs {s.modality_b}); an actual and a "
                       "projection/target are not a contradiction")

    if s.numeric_comparable:
        if not s.unit_equivalent:
            if s.currency_relation == "different":
                return Verdict("DIFFERENT_CONTEXT", "currency", "currency_differs",
                               "values are in different currencies; not comparable without FX")
            if s.unit_relation == "different":
                return Verdict("DIFFERENT_CONTEXT", "units", "unit_differs",
                               "values are in different, non-equivalent units")
            return Verdict("UNCERTAIN", None, "unit_not_established",
                           "unit/currency equivalence could not be established")
        if s.period_relation in _DISTINCT_PERIODS:
            if delta > tol:
                return Verdict("TEMPORAL_EVOLUTION", "time", "temporal_numeric",
                               f"same measure across distinct periods ({s.period_relation}); "
                               f"value moved {delta:.1%}")
            return Verdict("UNCERTAIN", None, "equal_across_periods",
                           "equal value across distinct periods; cannot confirm it is the "
                           "same measurement")
        if s.period_relation == "contains":
            if delta > tol:
                return Verdict("DIFFERENT_CONTEXT", "period_type", "period_type_differs",
                               "one period contains the other (e.g. a quarter within a year); "
                               "different period type")
            return Verdict("UNCERTAIN", None, "contained_period",
                           "one period contains the other; comparison is ambiguous")
        if s.period_relation in ("equal", "overlaps"):
            if not s.sign_match:
                return Verdict("UNCERTAIN", None, "sign_mismatch",
                               "values have opposite signs under comparable context")
            if delta <= tol:
                return Verdict("CORROBORATES", None, "numeric_equivalent",
                               f"same entity/predicate/period, unit-equivalent, values equal "
                               f"within tolerance ({delta:.2%} <= {tol:.0%})")
            if delta > contr:
                return Verdict("CONTRADICTS", None, "numeric_conflict",
                               f"same entity/predicate/period, unit-equivalent, values differ "
                               f"{delta:.1%} (> {contr:.0%})")
            return Verdict("UNCERTAIN", None, "numeric_borderline",
                           f"value difference {delta:.1%} is between the equivalence and "
                           "contradiction thresholds")
        return Verdict("UNCERTAIN", None, "period_missing",
                       "no reporting period on at least one side; cannot compare numeric values")

    if s.fact_type_a == "numeric" and s.fact_type_b == "numeric":
        return Verdict("UNCERTAIN", None, "numeric_unparsed",
                       "both facts are numeric but base values are not available to compare")

    # semantic (both non-numeric)
    if s.period_relation in _DISTINCT_PERIODS and s.modality_comparable and not objects_equal:
        return Verdict("TEMPORAL_EVOLUTION", "time", "temporal_semantic",
                       "same subject and predicate across distinct periods with different "
                       "statements")
    if (pred_ok and objects_equal and not s.scope_conflict
            and s.period_relation in ("equal", "missing", "same_year", "contains", "overlaps")):
        return Verdict("CORROBORATES", None, "identical_claim",
                       "same entity, equivalent predicate, and the same stated value")
    return Verdict(None, None, "needs_semantic",
                   "deterministic signals do not settle a semantic (predicate / polarity) "
                   "question")


# --------------------------------------------------------------------------- #
# LLM proposal validation                                                    #
# --------------------------------------------------------------------------- #
def _validate_proposal(p: RelationshipProposal) -> str | None:
    if p.error_code:
        return None
    cat = (p.relationship or "").strip().upper()
    if cat not in CATEGORIES:
        return None
    if not (0.0 <= p.confidence <= 1.0):
        return None
    if not p.reason.strip():
        return None
    return cat


def _validate_llm(proposed: str, s: SignalSet, settings: Settings) -> tuple[str, str, str]:
    """Deterministic guard over the LLM's proposal. Returns
    (final_category, validation_action, note)."""
    tol = settings.numeric_equivalence_tolerance
    contr = settings.numeric_contradiction_threshold
    delta = s.base_value_delta_pct if s.base_value_delta_pct is not None else 0.0

    if s.entity_relation != "same":
        return "UNCERTAIN", "downgraded", "subjects are not the same entity"
    if (proposed == "CONTRADICTS" and s.numeric_comparable and s.unit_equivalent
            and delta <= tol and s.period_relation == "equal" and not s.scope_conflict
            and s.sign_match):
        return ("CORROBORATES", "overridden",
                "values equal within tolerance under identical context; CONTRADICTS overridden")
    if proposed in ("CONTRADICTS", "CORROBORATES") and s.scope_conflict:
        return ("DIFFERENT_CONTEXT", "overridden",
                f"scope differs on {s.scope_conflict}; reconciled by context")
    if (proposed == "CONTRADICTS" and s.modality_comparable
            and s.period_relation in _DISTINCT_PERIODS):
        return ("TEMPORAL_EVOLUTION", "overridden",
                "distinct reporting periods with comparable modality; temporal change, not a "
                "contradiction")
    if proposed == "CONTRADICTS" and not s.modality_comparable:
        return ("DIFFERENT_CONTEXT", "downgraded",
                "modality differs (actual vs projection / target); not a contradiction")
    if (proposed == "CORROBORATES" and s.numeric_comparable and delta > contr
            and not s.scope_conflict):
        return ("UNCERTAIN", "downgraded",
                "values differ beyond the contradiction threshold; corroboration not supported")
    if proposed in ("CONTRADICTS", "CORROBORATES") and s.period_relation in ("unknown", "missing"):
        return "UNCERTAIN", "downgraded", "reporting period could not be established"
    return proposed, "accepted", "LLM proposal consistent with deterministic signals"


# --------------------------------------------------------------------------- #
# confidence + context dimension                                             #
# --------------------------------------------------------------------------- #
def _confidence(category: str, s: SignalSet, *, llm_conf: float | None,
                partial: bool, settings: Settings) -> float:
    """Explicit function of the signals — not a calibrated probability. Phase 10
    tunes ``relationship_confidence_threshold`` against this."""
    tol = max(settings.numeric_equivalence_tolerance, 1e-9)
    contr = max(settings.numeric_contradiction_threshold, 1e-9)
    delta = s.base_value_delta_pct if s.base_value_delta_pct is not None else 0.0
    if category == "UNCERTAIN":
        c = 0.25
    elif category in ("CORROBORATES", "CONTRADICTS") and s.numeric_comparable:
        if category == "CORROBORATES":
            c = 0.75 + 0.2 * (1.0 - min(1.0, delta / tol))
        else:
            c = 0.7 + 0.25 * min(1.0, (delta - contr) / contr)
    else:
        c = 0.55 + 0.2 * s.predicate_similarity
    if llm_conf is not None:
        c = min(c, max(0.3, llm_conf))
    if partial:
        c = min(c, 0.6)
    return round(min(c, 0.99), 3)


def _context_dimension(final: str, s: SignalSet, given: str | None) -> str | None:
    if given:
        return given
    if final == "TEMPORAL_EVOLUTION":
        return "time"
    if final == "DIFFERENT_CONTEXT":
        if s.scope_conflict:
            return f"scope:{s.scope_conflict[0]}"
        if not s.modality_comparable:
            return "modality"
        if s.vintage_differs:
            return "vintage"
        if s.currency_relation == "different":
            return "currency"
        if s.unit_relation == "different":
            return "units"
        return "period"
    return None


def _norm(text: str | None) -> str:
    return _WS_RE.sub(" ", (text or "").strip().lower()).rstrip(".,;:")


# --------------------------------------------------------------------------- #
# one pair                                                                   #
# --------------------------------------------------------------------------- #
def _packet(conn: sqlite3.Connection, fa_id: int, fb_id: int, s: SignalSet) -> dict:
    def _f(fid: int) -> dict:
        r = conn.execute(
            "SELECT subject_raw, predicate, object_raw, value_raw, base_value, unit_norm, "
            "currency, reporting_period_raw, reporting_period_type, scope, modality, "
            "evidence_status FROM facts WHERE id = ?", (fid,),
        ).fetchone()
        ev = conn.execute(
            "SELECT quote, verification_method FROM evidence WHERE fact_id = ?", (fid,)
        ).fetchone()
        return {
            "subject": r["subject_raw"], "predicate": r["predicate"], "object": r["object_raw"],
            "value_raw": r["value_raw"], "base_value": r["base_value"], "unit": r["unit_norm"],
            "currency": r["currency"], "reporting_period": r["reporting_period_raw"],
            "reporting_period_type": r["reporting_period_type"], "scope": _json(r["scope"]),
            "modality": r["modality"], "evidence_status": r["evidence_status"],
            "evidence_quote": ev["quote"] if ev else None,
        }
    return {"fact_a": _f(fa_id), "fact_b": _f(fb_id), "signals": s.model_dump()}


def reason_pair(conn: sqlite3.Connection, pair: CandidatePair, *,
                settings: Settings | None = None, llm: Any = None) -> RelationshipDecision:
    """Classify one candidate pair. Deterministic first; LLM only for a genuine
    semantic question, and only as a *proposal* that ``_validate_llm`` checks."""
    settings = settings or get_settings()
    s = pair.signals

    rows = {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, lifecycle_state, reasoning_eligible, evidence_status, object_raw, "
            "value_raw FROM facts WHERE id IN (?, ?)", (pair.fact_a_id, pair.fact_b_id),
        )
    }
    for fid in (pair.fact_a_id, pair.fact_b_id):
        r = rows.get(fid)
        if r is None:
            raise ReasonError("unknown_fact", detail=fid)
        if r["lifecycle_state"] != "ELIGIBLE_FOR_REASONING" or not r["reasoning_eligible"]:
            raise ReasonError("fact_not_eligible", detail=fid)
        if r["evidence_status"] == "UNVERIFIED":
            raise ReasonError("fact_unverified", detail=fid)

    partial = any(rows[f]["evidence_status"] == "PARTIAL" for f in (pair.fact_a_id, pair.fact_b_id))
    objects_equal = (
        _norm(rows[pair.fact_a_id]["object_raw"]) == _norm(rows[pair.fact_b_id]["object_raw"])
        and _norm(rows[pair.fact_a_id]["value_raw"]) == _norm(rows[pair.fact_b_id]["value_raw"])
    )

    verdict = deterministic_verdict(s, objects_equal=objects_equal, settings=settings)
    llm_used = False
    llm_proposed: str | None = None
    action = "not_applicable"
    llm_conf: float | None = None
    given_dim = verdict.context_dimension
    method = "deterministic"
    notes = verdict.reason

    if verdict.category is not None:
        final = verdict.category
    elif llm is None:
        final = "UNCERTAIN"
        method = "uncertain_no_llm"
        given_dim = None
        notes = f"{verdict.reason}; no semantic confirmer available"
    else:
        method = "llm_confirmed"
        llm_used = True
        try:
            proposal = llm.classify_relationship(
                _packet(conn, pair.fact_a_id, pair.fact_b_id, s)
            )
        except Exception as e:  # noqa: BLE001 — a broken client must not crash the run
            proposal = RelationshipProposal(
                error_code="client_exception", error_detail=repr(e)[:200]
            )
        prop_cat = _validate_proposal(proposal)
        if prop_cat is None:
            final = "UNCERTAIN"
            given_dim = None
            llm_proposed = proposal.relationship or None
            notes = (f"LLM proposal unusable ({proposal.error_code or 'invalid'}); "
                     "defaulting to UNCERTAIN")
        else:
            llm_proposed = prop_cat
            llm_conf = proposal.confidence
            final, action, vnote = _validate_llm(prop_cat, s, settings)
            given_dim = None
            notes = f"{proposal.reason.strip()} | validation: {vnote}"

    context_dim = _context_dimension(final, s, given_dim)
    confidence = _confidence(final, s, llm_conf=llm_conf if llm_used else None,
                             partial=partial, settings=settings)
    reasoning = (
        f"{final}. {notes} "
        f"[entity={s.entity_relation}, predicate_sim={s.predicate_similarity}, "
        f"period={s.period_relation}, scope={s.scope_relation}, "
        f"modality={s.modality_a}/{s.modality_b}, numeric_delta_pct={s.base_value_delta_pct}]"
    )
    det = {
        **s.model_dump(),
        "retrieval_score": pair.retrieval_score,
        "retrieval_methods": pair.retrieval_methods,
        "verdict_code": verdict.code,
    }
    return RelationshipDecision(
        fact_a_id=pair.fact_a_id, fact_b_id=pair.fact_b_id, category=final,
        context_dimension=context_dim, confidence=confidence, reasoning=reasoning,
        deterministic_signals=det, method=method, llm_used=llm_used,
        llm_proposed_category=llm_proposed, validation_action=action, validation_notes=notes,
    )


# --------------------------------------------------------------------------- #
# batch                                                                      #
# --------------------------------------------------------------------------- #
def _record_failure(conn, run_id, document_id, failure_type, reason, ref_id, detail) -> None:
    conn.execute(
        "INSERT INTO failures "
        "(run_id, document_id, failure_type, ref_table, ref_id, reason, detail, created_at) "
        "VALUES (?, ?, ?, 'relationships', ?, ?, ?, ?)",
        (run_id, document_id, failure_type, ref_id, reason[:500],
         json.dumps(detail, ensure_ascii=False, default=str), _now()),
    )


def _mark_run_failed(conn, run_id: int, document_id: int, reason: str) -> None:
    try:
        with db.transaction(conn):
            conn.execute(
                "UPDATE runs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                (reason[:500], _now(), run_id),
            )
            _record_failure(conn, run_id, document_id, "run_error", reason[:500], None, {})
    except Exception:  # noqa: BLE001
        log.exception("failed to record reason-run failure run_id=%s", run_id)


def reason_document(document_id: int, *, database_path: str | None = None,
                    settings: Settings | None = None, llm: Any = None) -> DocReasoningSummary:
    """Reason over every Phase-8 candidate pair that touches ``document_id`` and
    persist a relationship + rationale per pair. Per-pair failures are isolated;
    a re-run creates no duplicates (``add_relationship`` first-write-wins)."""
    settings = settings or get_settings()
    conn = db.connect(database_path)
    run_id: int | None = None
    try:
        if conn.execute("SELECT 1 FROM documents WHERE id = ?", (document_id,)).fetchone() is None:
            raise ReasonError("unknown_document", detail=document_id)

        with db.transaction(conn):
            run_id = conn.execute(
                "INSERT INTO runs (document_id, run_type, status, stage, started_at, model_name, "
                "prompt_versions) VALUES (?, 'reason', 'running', 'reason', ?, ?, ?)",
                (document_id, _now(),
                 settings.llm_model if llm is not None else None,
                 json.dumps({"relationship": settings.relationship_prompt_version})
                 if llm is not None else None),
            ).lastrowid

        pairs = retrieve_candidates(conn, document_id=document_id, settings=settings)
        c = dict.fromkeys(
            ("deterministic_decisions", "llm_confirmed_decisions", "uncertain_decisions",
             "relationships_created", "relationships_existing", "llm_calls", "llm_errors",
             "validation_overrides", "skipped", "errors"), 0)
        by_category: dict[str, int] = {}

        for pair in pairs:
            lo, hi = pair.fact_a_id, pair.fact_b_id
            try:
                with db.transaction(conn):
                    decision = reason_pair(conn, pair, settings=settings, llm=llm)
                    pre = conn.execute(
                        "SELECT id FROM relationships WHERE fact_a_id = ? AND fact_b_id = ?",
                        (lo, hi),
                    ).fetchone()
                    rel_id = add_relationship(conn, RelationshipIn(
                        fact_a_id=lo, fact_b_id=hi, category=decision.category,
                        context_dimension=decision.context_dimension,
                        reasoning=decision.reasoning, confidence=decision.confidence,
                        deterministic_signals=decision.deterministic_signals, run_id=run_id,
                        model_name=settings.llm_model if decision.llm_used else None,
                        prompt_version=(settings.relationship_prompt_version
                                        if decision.llm_used else None),
                        llm_used=decision.llm_used,
                        llm_proposed_category=decision.llm_proposed_category,
                        validation_action=decision.validation_action,
                        validation_notes=decision.validation_notes,
                    ))
                    if decision.llm_used:
                        c["llm_calls"] += 1
                        if decision.llm_proposed_category is None:
                            c["llm_errors"] += 1
                    if pre:
                        c["relationships_existing"] += 1
                        continue
                    c["relationships_created"] += 1
                    by_category[decision.category] = by_category.get(decision.category, 0) + 1
                    if decision.method == "llm_confirmed":
                        c["llm_confirmed_decisions"] += 1
                    else:
                        c["deterministic_decisions"] += 1
                    if decision.validation_action in ("overridden", "downgraded"):
                        c["validation_overrides"] += 1
                    if decision.category == "UNCERTAIN":
                        c["uncertain_decisions"] += 1
                        conn.execute(
                            "DELETE FROM failures WHERE ref_table = 'relationships' "
                            "AND ref_id = ? AND failure_type = 'relationship_uncertain'",
                            (rel_id,),
                        )
                        _record_failure(
                            conn, run_id, document_id, "relationship_uncertain",
                            decision.validation_notes or decision.reasoning, rel_id,
                            {"fact_a_id": lo, "fact_b_id": hi, "method": decision.method},
                        )
            except ReasonError as e:
                c["skipped"] += 1
                with db.transaction(conn):
                    _record_failure(conn, run_id, document_id, "run_error",
                                    f"reason_skipped: {e.code}", None,
                                    {"fact_a_id": lo, "fact_b_id": hi})
            except Exception as e:  # noqa: BLE001 — isolate a failing pair
                c["errors"] += 1
                with db.transaction(conn):
                    _record_failure(conn, run_id, document_id, "run_error",
                                    f"reason_exception: {e!r}"[:300], None,
                                    {"fact_a_id": lo, "fact_b_id": hi})

        with db.transaction(conn):
            conn.execute(
                "UPDATE runs SET status = 'done', stage = 'complete', finished_at = ?, "
                "relationships_produced = ?, llm_calls = ? WHERE id = ?",
                (_now(), c["relationships_created"], c["llm_calls"], run_id),
            )
        log.info(
            "reasoned document_id=%s pairs=%s created=%s existing=%s uncertain=%s overrides=%s "
            "llm_calls=%s by_category=%s", document_id, len(pairs), c["relationships_created"],
            c["relationships_existing"], c["uncertain_decisions"], c["validation_overrides"],
            c["llm_calls"], by_category,
        )
        return DocReasoningSummary(
            run_id=run_id, document_id=document_id, status="done",
            candidate_pairs=len(pairs), by_category=by_category, **c,
        )
    except ReasonError as e:
        if run_id is not None:
            _mark_run_failed(conn, run_id, document_id, e.code)
            e.run_id = run_id
        raise
    except Exception as e:  # noqa: BLE001
        if run_id is not None:
            _mark_run_failed(conn, run_id, document_id, f"unexpected: {e!r}")
        raise ReasonError("reasoning_failed", detail=repr(e)[:200], run_id=run_id) from e
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# observability                                                              #
# --------------------------------------------------------------------------- #
def reasoning_summary(conn: sqlite3.Connection, *,
                      document_id: int | None = None) -> dict[str, Any]:
    where, params = "", []
    if document_id is not None:
        where = (" WHERE (fact_a_id IN (SELECT id FROM facts WHERE document_id = ?) "
                 "OR fact_b_id IN (SELECT id FROM facts WHERE document_id = ?))")
        params = [document_id, document_id]

    by_category = {
        r["category"]: r["n"]
        for r in conn.execute(
            f"SELECT category, COUNT(*) AS n FROM relationships{where} GROUP BY category", params,
        )
    }
    by_action = {
        r["validation_action"]: r["n"]
        for r in conn.execute(
            f"SELECT validation_action, COUNT(*) AS n FROM relationships{where} "
            "GROUP BY validation_action", params,
        )
    }
    llm_used = conn.execute(
        f"SELECT COUNT(*) FROM relationships{where}"
        f"{' AND' if where else ' WHERE'} llm_used = 1", params,
    ).fetchone()[0]
    uncertain_failures = conn.execute(
        "SELECT COUNT(*) FROM failures WHERE failure_type = 'relationship_uncertain'"
    ).fetchone()[0]
    total = conn.execute(f"SELECT COUNT(*) FROM relationships{where}", params).fetchone()[0]
    return {
        "relationships_total": total,
        "by_category": by_category,
        "by_validation_action": by_action,
        "llm_confirmed": llm_used,
        "relationship_uncertain_failures": uncertain_failures,
    }
