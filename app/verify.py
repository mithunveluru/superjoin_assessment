"""Phase 5 — evidence verification + quarantine.

Determines whether each Phase-4 candidate fact's evidence is actually supported by
the persisted Phase-2 source text (`pages.text` / `chunks.text`). Fully
deterministic — **no LLM**. It never rewrites a claim; it may only correct an
evidence *span* when the exact quote occurs once in the chunk.

Verification ladder (first hit wins):
  1. exact           — source[start:end] == quote
  2. normalized_exact — equal after conservative whitespace / punctuation folding
  3. recovered_exact  — offsets wrong, but the exact quote occurs exactly once in
                        the chunk → span corrected (quote + payload untouched)
  4. fuzzy            — last resort for PDF formatting artifacts; a numeric / unit
                        token guard runs first, so changed numbers/currencies/
                        units can never pass → PARTIAL
  5. unverified       — none of the above, or numeric consistency failed → the
                        fact is quarantined (preserved, never reasoning-eligible)
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from rapidfuzz import fuzz

from app import db
from app.config import Settings, get_settings
from app.facts import quarantine_fact, set_lifecycle
from app.models import DocVerificationSummary, VerificationResult

log = logging.getLogger("fkl.verify")

# conservative, formatting-only folding — no case, no digits, no separators
_WS_RE = re.compile(
    "[\\s\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+"
)
_PUNCT_FOLD = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2015": "-", "\u2212": "-",
})

_CURRENCY_ALIASES: dict[str, list[str]] = {
    "inr": ["₹", "rs", "rs.", "inr"], "₹": ["₹", "rs", "rs.", "inr"],
    "rs": ["₹", "rs", "rs.", "inr"], "usd": ["$", "us$", "usd"],
    "$": ["$", "us$", "usd"], "eur": ["€", "eur"], "€": ["€", "eur"],
    "gbp": ["£", "gbp"], "£": ["£", "gbp"],
}
_MAGNITUDE_ALIASES: dict[str, list[str]] = {
    "crore": ["crore", "crores", "cr"], "cr": ["cr", "crore", "crores"],
    "lakh": ["lakh", "lakhs", "lac"], "million": ["million", "mn", "mln"],
    "mn": ["mn", "million"], "billion": ["billion", "bn"], "bn": ["bn", "billion"],
    "thousand": ["thousand", "'000", "000s"], "bps": ["bps", "basis points", "basis point"],
}
_CURRENCY_SYMBOLS = "₹$€£"
_MAGNITUDE_WORDS = re.compile(
    r"\b(crores?|cr|lakhs?|lac|millions?|mn|mln|billions?|bn|thousand|bps|"
    r"basis\s+points?|tonnes?|tons?|units?|days?|sq\.?\s?ft)\b",
    re.IGNORECASE,
)
_PERCENT_RE = re.compile(r"%|per\s?cent|percent", re.IGNORECASE)
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


class VerifyError(RuntimeError):
    def __init__(self, code: str, *, detail: Any = None, run_id: int | None = None):
        self.code = code
        self.detail = detail
        self.run_id = run_id
        super().__init__(code if detail is None else f"{code}: {detail}")


def _now() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------- #
# text helpers (conservative)                                                #
# --------------------------------------------------------------------------- #
def normalize_for_compare(text: str) -> str:
    """Collapse Unicode whitespace and fold trivial quote/dash variants. Does
    NOT touch case, digits, separators, currency symbols, %, units."""
    return _WS_RE.sub(" ", text.translate(_PUNCT_FOLD)).strip()


def _has_digit(s: str) -> bool:
    return any(c.isdigit() for c in s)


def _token_in_text(token: str, text: str) -> bool:
    t = token.strip()
    if not t:
        return True
    if t == "%":
        return _PERCENT_RE.search(text) is not None
    if _has_digit(t):
        tn, txn = t.replace(",", ""), text.replace(",", "")
        return re.search(r"(?<![\d.])" + re.escape(tn) + r"(?!\d)", txn) is not None
    return t.lower() in text.lower()


def _token_or_alias_in_text(value: str, text: str, aliases: dict[str, list[str]] | None) -> bool:
    v = value.strip().lower()
    if aliases and v in aliases:
        low = text.lower()
        return any(a in low for a in aliases[v])
    return _token_in_text(value, text)


def _salient_tokens(quote: str) -> list[str]:
    """Numbers, currency indicators, %, and magnitude/unit words actually present
    in the quote — the things a fuzzy match must NOT be allowed to change."""
    toks: list[str] = [m.group(0) for m in _NUM_RE.finditer(quote) if _has_digit(m.group(0))]
    toks += [c for c in _CURRENCY_SYMBOLS if c in quote]
    for code in ("rs", "inr", "usd", "eur", "gbp"):
        if re.search(rf"\b{code}\b", quote, re.IGNORECASE):
            toks.append(code)
    if _PERCENT_RE.search(quote):
        toks.append("%")
    toks += [m.group(0).lower() for m in _MAGNITUDE_WORDS.finditer(quote)]
    # de-dup, preserve order
    seen: set[str] = set()
    return [t for t in toks if not (t.lower() in seen or seen.add(t.lower()))]


def _missing_salient_tokens(quote: str, source_text: str) -> list[str]:
    norm = normalize_for_compare(source_text)
    return [t for t in _salient_tokens(quote) if not _token_in_text(t, norm)]


def _digits_of(raw: str | None) -> float | None:
    """The single bare number in a raw value expression, or None if ambiguous.
    No scale multiplier is applied — '₹8,142 crore' -> 8142.0."""
    if not raw:
        return None
    s = raw.strip()
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("−", "-")
    if s.lstrip().startswith("-"):
        negative = True
    nums = _NUM_RE.findall(s)
    if len(nums) != 1:
        return None
    try:
        v = float(nums[0].replace(",", ""))
    except ValueError:
        return None
    return -v if negative else v


# --------------------------------------------------------------------------- #
# numeric consistency (§9) — bounded; NOT Phase-6 normalization               #
# --------------------------------------------------------------------------- #
def _check_numeric_consistency(fact: sqlite3.Row, span_text: str,
                               settings: Settings) -> tuple[bool, str | None]:
    norm = normalize_for_compare(span_text)
    tol = settings.verify_numeric_tolerance
    raw = fact["value_raw"]
    if raw:
        for tok in _salient_tokens(raw):
            if not _token_in_text(tok, norm):
                return False, f"raw_value_text token {tok!r} is not in the verified source span"
        n = _digits_of(raw)
        nv = fact["numeric_value"]
        if n is not None and nv is not None and abs(nv - n) > tol:
            return False, f"parsed_value {nv} != the number in raw_value_text ({n})"
    if (fact["is_percentage"] and fact["percentage_ratio"] is not None
            and fact["numeric_value"] is not None):
        pr, nv = fact["percentage_ratio"], fact["numeric_value"]
        if abs(pr - nv) > tol and abs(pr - nv / 100.0) > tol:
            return False, f"percentage_ratio {pr} is incoherent with parsed_value {nv}"
    for field, aliases in (("currency", _CURRENCY_ALIASES),
                           ("magnitude", _MAGNITUDE_ALIASES),
                           ("unit_raw", None)):
        val = fact[field]
        if val and not _token_or_alias_in_text(str(val), norm, aliases):
            return False, f"{field} {val!r} is not in the verified source span"
    return True, None


# --------------------------------------------------------------------------- #
# the ladder                                                                 #
# --------------------------------------------------------------------------- #
@dataclass
class _Outcome:
    evidence_status: str          # VERIFIED | PARTIAL | UNVERIFIED
    verification_method: str      # exact | normalized_exact | recovered_exact | fuzzy | unverified
    numeric_rederivation: str     # not_applicable | success | failed
    fuzzy_score: float | None
    recovered_span: tuple[int, int] | None
    note: str
    reason: str                   # deterministic outcome code


def _resolve_page_text(conn: sqlite3.Connection, ev: sqlite3.Row) -> str | None:
    if ev["page_id"] is not None:
        row = conn.execute("SELECT text FROM pages WHERE id = ?", (ev["page_id"],)).fetchone()
        if row is not None:
            return row["text"]
    row = conn.execute(
        "SELECT text FROM pages WHERE document_id = ? AND page_index = ?",
        (ev["document_id"], ev["page_index"]),
    ).fetchone()
    return row["text"] if row is not None else None


def _resolve_chunk_span(conn: sqlite3.Connection, ev: sqlite3.Row,
                        page_text: str) -> tuple[int, str]:
    if ev["chunk_id"] is not None:
        c = conn.execute(
            "SELECT char_offset, char_end FROM chunks WHERE id = ?", (ev["chunk_id"],)
        ).fetchone()
        if c is not None:
            b, e = c["char_offset"], c["char_end"]
            if isinstance(b, int) and isinstance(e, int) and 0 <= b < e <= len(page_text):
                return b, page_text[b:e]
    return 0, page_text


def _find_all(haystack: str, needle: str) -> list[int]:
    out, i = [], haystack.find(needle)
    while i != -1:
        out.append(i)
        i = haystack.find(needle, i + 1)
    return out


def _run_ladder(fact: sqlite3.Row, ev: sqlite3.Row, page_text: str | None,
                chunk_base: int, chunk_span: str, settings: Settings) -> _Outcome:
    quote = ev["quote"]
    if not page_text:
        return _Outcome("UNVERIFIED", "unverified", "not_applicable", None, None,
                        "source page text is unavailable", "source_unavailable")

    n, s, e = len(page_text), ev["char_start"], ev["char_end"]
    offsets_valid = isinstance(s, int) and isinstance(e, int) and 0 <= s < e <= n

    recovered: tuple[int, int] | None = None
    fuzzy_score: float | None = None

    if offsets_valid and page_text[s:e] == quote:
        status, vm = "VERIFIED", "exact"
        note = "quote matches the source verbatim at the stated offsets"
    elif offsets_valid and normalize_for_compare(page_text[s:e]) == normalize_for_compare(quote):
        status, vm = "VERIFIED", "normalized_exact"
        note = "quote matches the source after conservative whitespace/punctuation folding"
    else:
        occ = _find_all(chunk_span, quote)
        if len(occ) == 1:
            new_s = chunk_base + occ[0]
            recovered = (new_s, new_s + len(quote))
            status, vm = "VERIFIED", "recovered_exact"
            note = (f"stated offsets ({s},{e}) did not match; the exact quote occurs once "
                    f"in the chunk — span corrected to {recovered}")
        elif len(occ) > 1:
            return _Outcome("UNVERIFIED", "unverified", "not_applicable", None, None,
                            f"the exact quote occurs {len(occ)} times in the chunk; not guessing",
                            "ambiguous_quote_match")
        elif len(quote.strip()) < settings.verify_fuzzy_min_quote_chars:
            return _Outcome("UNVERIFIED", "unverified", "not_applicable", None, None,
                            "quote not found in the chunk and too short for fuzzy matching",
                            "quote_not_found")
        else:
            missing = _missing_salient_tokens(quote, chunk_span)
            if missing:
                numeric = any(_has_digit(t) or t in _CURRENCY_SYMBOLS or t == "%" for t in missing)
                return _Outcome("UNVERIFIED", "unverified", "not_applicable", None, None,
                                f"quote not found; salient token(s) absent from source: {missing}",
                                "numeric_token_mismatch" if numeric else "quote_not_found")
            score = float(fuzz.partial_ratio(
                normalize_for_compare(quote), normalize_for_compare(chunk_span)
            ))
            if score < settings.verify_fuzzy_threshold:
                return _Outcome(
                    "UNVERIFIED", "unverified", "not_applicable", score, None,
                    f"fuzzy score {score:.1f} < threshold {settings.verify_fuzzy_threshold}",
                    "quote_not_found",
                )
            status, vm, fuzzy_score = "PARTIAL", "fuzzy", score
            note = (f"fuzzy match score {score:.1f} >= threshold "
                    f"{settings.verify_fuzzy_threshold}; formatting differences only, all "
                    f"salient tokens present")

    numeric_rederivation = "not_applicable"
    if fact["fact_type"] == "numeric":
        if recovered:
            span_text = page_text[recovered[0]:recovered[1]]
        elif offsets_valid and vm in ("exact", "normalized_exact"):
            span_text = page_text[s:e]
        else:
            span_text = chunk_span
        ok, why = _check_numeric_consistency(fact, span_text, settings)
        if not ok:
            return _Outcome("UNVERIFIED", vm, "failed", fuzzy_score, None,
                            f"{note}; numeric consistency failed: {why}", "numeric_mismatch")
        numeric_rederivation = "success"

    reason = {
        "exact": "verified_exact", "normalized_exact": "verified_normalized_exact",
        "recovered_exact": "verified_recovered_exact", "fuzzy": "partial_fuzzy",
    }[vm]
    return _Outcome(status, vm, numeric_rederivation, fuzzy_score, recovered, note, reason)


# --------------------------------------------------------------------------- #
# persistence                                                                #
# --------------------------------------------------------------------------- #
def _record_failure(conn, run_id, document_id, failure_type, reason, ref_table, ref_id, detail):
    import json
    conn.execute(
        "INSERT INTO failures "
        "(run_id, document_id, failure_type, ref_table, ref_id, reason, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, document_id, failure_type, ref_table, ref_id, reason,
         json.dumps(detail, ensure_ascii=False, default=str), _now()),
    )


def _apply(conn: sqlite3.Connection, fact_id: int, ev: sqlite3.Row, outcome: _Outcome,
           *, verified_at: str, run_id: int | None) -> str:
    cs, ce = ev["char_start"], ev["char_end"]
    if outcome.recovered_span:
        cs, ce = outcome.recovered_span
    conn.execute(
        "UPDATE evidence SET verification_method = ?, evidence_status = ?, "
        "numeric_rederivation = ?, fuzzy_score = ?, notes = ?, verified_at = ?, "
        "char_start = ?, char_end = ? WHERE fact_id = ?",
        (outcome.verification_method, outcome.evidence_status, outcome.numeric_rederivation,
         outcome.fuzzy_score, outcome.note, verified_at, cs, ce, fact_id),
    )
    conn.execute("UPDATE facts SET evidence_status = ? WHERE id = ?",
                 (outcome.evidence_status, fact_id))
    # idempotent re-run: drop any stale grounding failure for this fact
    conn.execute(
        "DELETE FROM failures WHERE ref_table = 'facts' AND ref_id = ? "
        "AND failure_type = 'grounding_failed'",
        (fact_id,),
    )
    if outcome.evidence_status in ("VERIFIED", "PARTIAL"):
        cur = conn.execute(
            "SELECT lifecycle_state FROM facts WHERE id = ?", (fact_id,)
        ).fetchone()["lifecycle_state"]
        if cur in ("RAW", "CANDIDATE", "QUARANTINED"):
            set_lifecycle(conn, fact_id, "GROUNDED")
        return "GROUNDED" if cur in ("RAW", "CANDIDATE", "QUARANTINED") else cur
    quarantine_fact(conn, fact_id, outcome.reason)
    _record_failure(conn, run_id, ev["document_id"], "grounding_failed", outcome.reason,
                    "facts", fact_id, {"evidence_id": ev["id"], "note": outcome.note})
    return "QUARANTINED"


# --------------------------------------------------------------------------- #
# public entry points                                                        #
# --------------------------------------------------------------------------- #
def verify_fact(conn: sqlite3.Connection, fact_id: int, *,
                settings: Settings | None = None, run_id: int | None = None,
                verified_at: str | None = None) -> VerificationResult:
    """Verify one candidate fact's evidence against persisted source text.
    Idempotent — updates the single evidence row in place; deterministic."""
    settings = settings or get_settings()
    verified_at = verified_at or _now()

    fact = conn.execute(
        "SELECT id, document_id, fact_type, lifecycle_state, value_raw, numeric_value, "
        "magnitude, currency, is_percentage, percentage_ratio, unit_raw "
        "FROM facts WHERE id = ?", (fact_id,),
    ).fetchone()
    if fact is None:
        raise VerifyError("unknown_fact", detail=fact_id)

    ev = conn.execute("SELECT * FROM evidence WHERE fact_id = ?", (fact_id,)).fetchone()
    if ev is None:
        with db.transaction(conn):
            quarantine_fact(conn, fact_id, "unsupported_evidence")
            _record_failure(conn, run_id, fact["document_id"], "grounding_failed",
                            "unsupported_evidence", "facts", fact_id,
                            {"note": "no candidate evidence row exists for this fact"})
        return VerificationResult(
            fact_id=fact_id, evidence_id=None, evidence_status="UNVERIFIED",
            verification_method="unverified", numeric_rederivation="not_applicable",
            lifecycle_state="QUARANTINED", reason="unsupported_evidence",
            detail="no candidate evidence row",
        )

    page_text = _resolve_page_text(conn, ev)
    chunk_base, chunk_span = _resolve_chunk_span(conn, ev, page_text or "")
    outcome = _run_ladder(fact, ev, page_text, chunk_base, chunk_span, settings)

    with db.transaction(conn):
        final_state = _apply(conn, fact_id, ev, outcome, verified_at=verified_at, run_id=run_id)

    return VerificationResult(
        fact_id=fact_id, evidence_id=ev["id"], evidence_status=outcome.evidence_status,
        verification_method=outcome.verification_method,
        numeric_rederivation=outcome.numeric_rederivation, lifecycle_state=final_state,
        recovered=outcome.recovered_span is not None, fuzzy_score=outcome.fuzzy_score,
        reason=outcome.reason, detail=outcome.note,
    )


def verify_document(document_id: int, *, database_path: str | None = None,
                    settings: Settings | None = None) -> DocVerificationSummary:
    """Verify every CANDIDATE/GROUNDED fact of one document. Failures are
    isolated per fact; already-QUARANTINED facts are left untouched."""
    settings = settings or get_settings()
    conn = db.connect(database_path)
    run_id: int | None = None
    try:
        if conn.execute("SELECT 1 FROM documents WHERE id = ?", (document_id,)).fetchone() is None:
            raise VerifyError("unknown_document", detail=document_id)
        fact_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM facts WHERE document_id = ? "
                "AND lifecycle_state IN ('CANDIDATE','GROUNDED') ORDER BY id",
                (document_id,),
            )
        ]
        with db.transaction(conn):
            run_id = conn.execute(
                "INSERT INTO runs (document_id, run_type, status, stage, started_at) "
                "VALUES (?, 'ground', 'running', 'verify', ?)",
                (document_id, _now()),
            ).lastrowid

        c = {k: 0 for k in ("examined", "verified", "partial", "unverified", "quarantined",
                            "recovered", "numeric_mismatches", "ambiguous_matches", "errors")}
        by_method: dict[str, int] = {}
        verified_at = _now()
        for fid in fact_ids:
            c["examined"] += 1
            try:
                r = verify_fact(conn, fid, settings=settings, run_id=run_id,
                                verified_at=verified_at)
            except Exception as e:  # noqa: BLE001 - isolate a failing fact
                c["errors"] += 1
                with db.transaction(conn):
                    _record_failure(conn, run_id, document_id, "run_error", "verify_exception",
                                    "facts", fid, {"error": repr(e)[:300]})
                continue
            bucket = {"VERIFIED": "verified", "PARTIAL": "partial",
                      "UNVERIFIED": "unverified"}[r.evidence_status]
            c[bucket] += 1
            if r.evidence_status == "UNVERIFIED":
                c["quarantined"] += 1
            if r.recovered:
                c["recovered"] += 1
            if r.reason == "numeric_mismatch":
                c["numeric_mismatches"] += 1
            if r.reason == "ambiguous_quote_match":
                c["ambiguous_matches"] += 1
            by_method[r.verification_method] = by_method.get(r.verification_method, 0) + 1

        with db.transaction(conn):
            conn.execute(
                "UPDATE runs SET status = 'done', stage = 'complete', finished_at = ?, "
                "facts_grounded = ?, facts_quarantined = ? WHERE id = ?",
                (_now(), c["verified"] + c["partial"], c["quarantined"], run_id),
            )
        log.info("verified document_id=%s examined=%s verified=%s partial=%s quarantined=%s "
                 "recovered=%s numeric_mismatch=%s", document_id, c["examined"], c["verified"],
                 c["partial"], c["quarantined"], c["recovered"], c["numeric_mismatches"])
        return DocVerificationSummary(run_id=run_id, document_id=document_id, status="done",
                                      by_method=by_method, **c)
    except VerifyError as e:
        if run_id is not None:
            _mark_run_failed(conn, run_id, document_id, e.code)
            e.run_id = run_id
        raise
    except Exception as e:  # noqa: BLE001
        if run_id is not None:
            _mark_run_failed(conn, run_id, document_id, f"unexpected: {e!r}")
        raise VerifyError("verification_failed", detail=repr(e)[:200], run_id=run_id) from e
    finally:
        conn.close()


def _mark_run_failed(conn, run_id: int, document_id: int, reason: str) -> None:
    reason = reason[:500]
    try:
        with db.transaction(conn):
            conn.execute(
                "UPDATE runs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                (reason, _now(), run_id),
            )
            _record_failure(conn, run_id, document_id, "run_error", reason,
                            "documents", document_id, {"reason": reason})
    except Exception:  # noqa: BLE001
        log.exception("failed to record verification-run failure run_id=%s", run_id)


# --------------------------------------------------------------------------- #
# observability                                                              #
# --------------------------------------------------------------------------- #
def verification_summary(conn: sqlite3.Connection, *, document_id: int | None = None,
                         run_id: int | None = None) -> dict[str, Any]:
    where, params = [], []
    if document_id is not None:
        where.append("document_id = ?")
        params.append(document_id)
    ev_clause = (" WHERE " + " AND ".join(where)) if where else ""

    by_method = {
        r["verification_method"]: r["n"]
        for r in conn.execute(
            f"SELECT verification_method, COUNT(*) AS n FROM evidence{ev_clause} "
            f"GROUP BY verification_method", params,
        )
    }
    by_status = {
        r["evidence_status"]: r["n"]
        for r in conn.execute(
            f"SELECT evidence_status, COUNT(*) AS n FROM evidence{ev_clause} "
            f"GROUP BY evidence_status", params,
        )
    }
    by_lifecycle = {
        r["lifecycle_state"]: r["n"]
        for r in conn.execute(
            f"SELECT lifecycle_state, COUNT(*) AS n FROM facts{ev_clause} "
            f"GROUP BY lifecycle_state", params,
        )
    }
    fail_params = list(params)
    fail_where = list(where)
    fail_where.append("failure_type = 'grounding_failed'")
    if run_id is not None:
        fail_where.append("run_id = ?")
        fail_params.append(run_id)
    quarantine_reasons = {
        r["reason"]: r["n"]
        for r in conn.execute(
            "SELECT reason, COUNT(*) AS n FROM failures WHERE "
            + " AND ".join(fail_where) + " GROUP BY reason",
            fail_params,
        )
    }
    return {
        "evidence_by_method": by_method,
        "evidence_by_status": by_status,
        "facts_by_lifecycle": by_lifecycle,
        "quarantine_reasons": quarantine_reasons,
        "recovered": by_method.get("recovered_exact", 0),
    }
