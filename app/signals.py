"""Phase 8 (Stage B) — deterministic comparison signals for a candidate pair.

Given two facts, compute an explicit, inspectable set of signals: how their
entities, predicates, fact types, numeric values, units, currencies, reporting
periods, scopes, modalities, and document provenance relate. Every value is
derived only from what Phases 1-7 already persisted — no LLM, and no unit
conversion beyond the Phase-6 normalized representation (``base_value`` already
folds magnitude; ``percentage_ratio`` is stored as ``base_value`` for %).

A signal *describes a difference*. It never decides a relationship — that is
Phase 9. In particular a period / scope / currency / modality difference is
reported here, not used to exclude the pair.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime
from typing import Any

from rapidfuzz import fuzz

from app.models import SignalSet

# generic English function words — not corpus-specific. Used for predicate
# token-overlap and for the retrieval token blocks.
STOPWORDS = frozenset({
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or",
    "per", "the", "to", "with", "s",
})
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_COMPARABLE_MODALITY = frozenset({"ASSERTED", "HISTORICAL"})


def content_tokens(text: str | None) -> set[str]:
    """Lowercase alphanumeric tokens with generic stopwords removed."""
    if not text:
        return set()
    return {t for t in _TOKEN_RE.findall(text.lower()) if t not in STOPWORDS and len(t) > 1}


def _predicate_text(predicate: str | None, predicate_norm: str | None) -> str:
    return (predicate_norm or "").strip() or (predicate or "").strip()


def predicate_signals(
    predicate_a: str | None, predicate_norm_a: str | None,
    predicate_b: str | None, predicate_norm_b: str | None,
) -> tuple[bool, float, float]:
    """(exact, token_set_similarity 0-1, token-overlap Jaccard 0-1)."""
    pa = _predicate_text(predicate_a, predicate_norm_a).lower()
    pb = _predicate_text(predicate_b, predicate_norm_b).lower()
    na = (predicate_norm_a or "").strip().lower()
    nb = (predicate_norm_b or "").strip().lower()
    exact = bool(na and nb and na == nb) or bool(pa and pa == pb)
    similarity = fuzz.token_set_ratio(pa, pb) / 100.0 if pa and pb else 0.0
    ta, tb = content_tokens(pa), content_tokens(pb)
    overlap = len(ta & tb) / len(ta | tb) if (ta or tb) else 0.0
    return exact, round(similarity, 4), round(overlap, 4)


def _rel(x: Any, y: Any) -> str:
    if x is None or x == "" or y is None or y == "":
        return "missing"
    return "same" if str(x).strip().lower() == str(y).strip().lower() else "different"


def period_relation(
    a_start: str | None, a_end: str | None, b_start: str | None, b_end: str | None,
    a_type: str | None = None, b_type: str | None = None,
) -> str:
    """Deterministic relation between two half-open [start, end) ISO-date periods."""
    if a_type == "unknown" or b_type == "unknown":
        return "unknown"
    if not (a_start and a_end and b_start and b_end):
        return "missing"
    if a_start == b_start and a_end == b_end:
        return "equal"
    a_in_b = b_start <= a_start and a_end <= b_end
    b_in_a = a_start <= b_start and b_end <= a_end
    if a_in_b or b_in_a:
        return "contains"
    if max(a_start, b_start) < min(a_end, b_end):
        return "overlaps"
    if a_end == b_start or b_end == a_start:
        return "adjacent"
    if a_start[:4] == b_start[:4]:
        return "same_year"
    return "disjoint"


def _scope_val(v: Any) -> str:
    return str(v).strip().lower()


def scope_relation(a: dict | None, b: dict | None) -> tuple[str, list[str]]:
    """(relation, conflicting-keys). ``different`` means at least one shared key
    carries a different value — those keys are exactly what Phase 9 may use to
    explain an apparent contradiction."""
    if not a or not b:
        return "missing", []
    common = set(a) & set(b)
    conflicts = sorted(k for k in common if _scope_val(a[k]) != _scope_val(b[k]))
    if conflicts:
        return "different", conflicts
    if not common:
        return "different", []
    if set(a) == set(b):
        return "same", []
    return "overlap", []


def _numeric_signals(fa: sqlite3.Row, fb: sqlite3.Row) -> dict[str, Any]:
    if not (
        fa["fact_type"] == "numeric" and fb["fact_type"] == "numeric"
        and fa["base_value"] is not None and fb["base_value"] is not None
    ):
        return {"numeric_comparable": False}
    a, b = float(fa["base_value"]), float(fb["base_value"])
    abs_diff = abs(a - b)
    denom = max(abs(a), abs(b))
    pct_vs_abs = bool(fa["is_percentage"]) != bool(fb["is_percentage"])
    unit_rel = _rel(fa["unit_norm"], fb["unit_norm"])
    cur_rel = _rel(fa["currency"], fb["currency"])
    return {
        "numeric_comparable": True,
        "base_value_a": a,
        "base_value_b": b,
        "base_value_abs_diff": abs_diff,
        "base_value_delta_pct": round(abs_diff / denom, 6) if denom else 0.0,
        "sign_match": (a >= 0) == (b >= 0),
        "percentage_vs_absolute": pct_vs_abs,
        "ratio_a_to_b": round(a / b, 6) if b != 0 else None,
        "unit_equivalent": (not pct_vs_abs) and unit_rel != "different" and cur_rel != "different",
    }


def _parse_day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None


def _fact_row(conn: sqlite3.Connection, fact_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, document_id, subject_entity_id, predicate, predicate_norm, fact_type, "
        "base_value, numeric_value, is_percentage, unit_norm, currency, "
        "reporting_period_start, reporting_period_end, reporting_period_type, scope, modality "
        "FROM facts WHERE id = ?",
        (fact_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown fact {fact_id}")
    return row


def compute(conn: sqlite3.Connection, fact_a_id: int, fact_b_id: int) -> SignalSet:
    """The full deterministic signal set for one candidate pair. Order-independent
    in meaning; callers pass the canonical ``a < b`` order."""
    fa, fb = _fact_row(conn, fact_a_id), _fact_row(conn, fact_b_id)
    reasons: list[str] = []

    ea, eb = fa["subject_entity_id"], fb["subject_entity_id"]
    if ea is not None and eb is not None:
        entity_relation = "same" if ea == eb else "different"
    else:
        entity_relation = "unresolved"
        reasons.append("one or both subjects are not resolved to an entity")

    exact, sim, overlap = predicate_signals(
        fa["predicate"], fa["predicate_norm"], fb["predicate"], fb["predicate_norm"]
    )

    num = _numeric_signals(fa, fb)
    if not num["numeric_comparable"] and "numeric" in (fa["fact_type"], fb["fact_type"]):
        reasons.append("numeric comparison skipped (missing base_value or mixed fact types)")

    prel = period_relation(
        fa["reporting_period_start"], fa["reporting_period_end"],
        fb["reporting_period_start"], fb["reporting_period_end"],
        fa["reporting_period_type"], fb["reporting_period_type"],
    )
    if prel == "missing":
        reasons.append("reporting period absent on at least one side")

    srel, conflicts = scope_relation(_json(fa["scope"]), _json(fb["scope"]))
    if srel == "missing":
        reasons.append("scope not described on both sides")

    da = conn.execute(
        "SELECT publication_date, document_date, data_vintage FROM documents WHERE id = ?",
        (fa["document_id"],),
    ).fetchone()
    db_ = conn.execute(
        "SELECT publication_date, document_date, data_vintage FROM documents WHERE id = ?",
        (fb["document_id"],),
    ).fetchone()
    pub_a = _parse_day(da["publication_date"] or da["document_date"])
    pub_b = _parse_day(db_["publication_date"] or db_["document_date"])
    gap = abs((pub_a - pub_b).days) if (pub_a and pub_b) else None
    vintage_differs = bool(
        da["data_vintage"] and db_["data_vintage"] and da["data_vintage"] != db_["data_vintage"]
    )

    mod_a, mod_b = fa["modality"], fb["modality"]

    return SignalSet(
        entity_relation=entity_relation,
        subject_entity_id_a=ea,
        subject_entity_id_b=eb,
        predicate_exact=exact,
        predicate_similarity=sim,
        predicate_token_overlap=overlap,
        fact_type_a=fa["fact_type"],
        fact_type_b=fb["fact_type"],
        fact_type_match=fa["fact_type"] == fb["fact_type"],
        unit_relation=_rel(fa["unit_norm"], fb["unit_norm"]),
        currency_relation=_rel(fa["currency"], fb["currency"]),
        period_relation=prel,
        scope_relation=srel,
        scope_conflict=conflicts,
        modality_a=mod_a,
        modality_b=mod_b,
        modality_relation="same" if mod_a == mod_b else "different",
        modality_comparable=mod_a in _COMPARABLE_MODALITY and mod_b in _COMPARABLE_MODALITY,
        same_document=fa["document_id"] == fb["document_id"],
        publication_gap_days=gap,
        vintage_differs=vintage_differs,
        reasons=reasons,
        **num,
    )


def _json(value: str | None) -> dict | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None
