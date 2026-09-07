"""Phase 6 — deterministic context / numeric / date / unit normalization.

Takes a GROUNDED fact (verified evidence, Phase 5) and fills the *normalized*
representation columns that Phase 4 deliberately left NULL:

  numeric  : magnitude, magnitude_factor, base_value, currency (code),
             is_percentage, percentage_ratio, unit_norm
  context  : reporting_period_start / _end / _type  (parsed from
             reporting_period_raw using the document's fy_convention)

then promotes GROUNDED -> NORMALIZED. **Raw is never overwritten.** No LLM.
Not Phase-7+: no cross-fact comparison, entity resolution, retrieval, or
relationship inference.

An unresolvable period yields reporting_period_type='unknown' (the fact is still
NORMALIZED — the ambiguity is recorded, not hidden). A numeric fact whose value
cannot be parsed is a `normalization_failed` failure and stays GROUNDED.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app import db
from app.config import Settings, get_settings
from app.facts import set_lifecycle
from app.models import DocNormalizationSummary, NormalizationResult

log = logging.getLogger("fkl.normalize")

# --- magnitude / currency / unit vocab (generic language config, not dataset) ---
_MAGNITUDES: list[tuple[str, str, float]] = [
    (r"crores?", "crore", 1e7),
    (r"cr\b", "crore", 1e7),
    (r"lakhs?|lac\b", "lakh", 1e5),
    (r"millions?|mn\b|mln\b", "million", 1e6),
    (r"billions?|bn\b", "billion", 1e9),
    (r"trillions?|tn\b", "trillion", 1e12),
    (r"thousand", "thousand", 1e3),
]
_PERCENT_RE = re.compile(r"%|per\s?cent|percent", re.IGNORECASE)
_BPS_RE = re.compile(r"\bbps\b|basis\s+points?", re.IGNORECASE)
_PAREN_NEG_RE = re.compile(r"\(\s*[\d.,]+\s*\)")
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

_MONTHS = {
    m: i + 1
    for i, names in enumerate([
        ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
        ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
        ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
        ("dec", "december"),
    ])
    for m in names
}
_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
_FY_START_MONTH = {"apr-mar": 4, "jan-dec": 1, "jul-jun": 7}


class NormalizeError(RuntimeError):
    def __init__(self, code: str, *, detail: Any = None, run_id: int | None = None):
        self.code = code
        self.detail = detail
        self.run_id = run_id
        super().__init__(code if detail is None else f"{code}: {detail}")


def _now() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------- #
# numeric / currency / unit                                                  #
# --------------------------------------------------------------------------- #
@dataclass
class NumberParse:
    value: float
    magnitude: str | None
    magnitude_factor: float | None
    is_percentage: bool
    percentage_ratio: float | None


def parse_number(raw: str | None) -> NumberParse | None:
    """The single numeric expression in ``raw`` → value + magnitude + percentage
    info. No scale multiplier is applied to ``value``. ``None`` if ambiguous."""
    if not raw:
        return None
    s = raw.strip().replace("−", "-")
    negative = bool(_PAREN_NEG_RE.search(s)) or bool(re.match(r"^\s*-", s))
    nums = _NUM_RE.findall(s)
    if len(nums) != 1:
        return None
    try:
        value = float(nums[0].replace(",", ""))
    except ValueError:
        return None
    if negative and value >= 0:
        value = -value

    low = s.lower()
    is_bps = bool(_BPS_RE.search(low))
    is_pct = is_bps or bool(_PERCENT_RE.search(low))
    magnitude: str | None = None
    factor: float | None = None
    ratio: float | None = None
    if is_bps:
        magnitude, factor, ratio = "bps", 1e-4, value / 10000.0
    else:
        for pat, canon, f in _MAGNITUDES:
            if re.search(rf"\b{pat}", low):
                magnitude, factor = canon, f
                break
        if is_pct:
            ratio = value / 100.0
    return NumberParse(value, magnitude, factor, is_pct, ratio)


def parse_currency(text: str | None) -> str | None:
    if not text:
        return None
    low = text.lower()
    if "₹" in text or re.search(r"\brs\.?\b|\binr\b", low):
        return "INR"
    if "$" in text or re.search(r"\busd\b|us\$", low):
        return "USD"
    if "€" in text or re.search(r"\beur\b", low):
        return "EUR"
    if "£" in text or re.search(r"\bgbp\b", low):
        return "GBP"
    return None


def parse_unit(unit_raw: str | None, *, currency: str | None = None) -> str | None:
    """A coarse unit code, or None when unknown (never guessed)."""
    if not unit_raw:
        return None
    u = unit_raw.lower().strip()
    if re.search(r"%|per\s?cent|percent|\bbps\b|basis\s+point|(?:^|\d)\s*x$", u):
        return "ratio"
    if re.search(r"tonnes?|\btons?\b|\bmt\b", u):
        return "tonne"
    if re.search(r"\bdays?\b", u):
        return "days"
    if re.search(r"sq\.?\s?(?:ft|feet|foot)|square\s+(?:feet|foot)", u):
        return "sqft"
    cur = parse_currency(unit_raw)
    if cur:
        return cur
    if currency:
        return currency
    if re.search(r"shipments?|units?|pin[\s-]?codes?|customers?|employees?|gateways?|"
                 r"centres?|centers?|orders?|\bcount\b|number\s+of", u):
        return "count"
    return None


# --------------------------------------------------------------------------- #
# dates / periods                                                            #
# --------------------------------------------------------------------------- #
def _parse_date(s: str) -> date | None:
    s = s.strip().strip(".").strip().rstrip(",").strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        return _safe_date(int(m[1]), int(m[2]), int(m[3]))
    m = re.match(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})$", s)  # DD-MM-YYYY (Indian)
    if m:
        return _safe_date(_yy(m[3]), int(m[2]), int(m[1]))
    m = re.match(r"^(\d{1,2})(?:st|nd|rd|th)?[-\s]+([A-Za-z]{3,})[-\s]+['’]?(\d{2,4})$", s)
    if m and m[2].lower() in _MONTHS:
        return _safe_date(_yy(m[3]), _MONTHS[m[2].lower()], int(m[1]))
    m = re.match(r"^([A-Za-z]{3,})[-\s]+(\d{1,2})(?:st|nd|rd|th)?,?\s+['’]?(\d{2,4})$", s)
    if m and m[1].lower() in _MONTHS:
        return _safe_date(_yy(m[3]), _MONTHS[m[1].lower()], int(m[2]))
    m = re.match(r"^([A-Za-z]{3,})\s+['’]?(\d{2,4})$", s)  # "March 2024" / "Mar '24"
    if m and m[1].lower() in _MONTHS:
        return _safe_date(_yy(m[2]), _MONTHS[m[1].lower()], 1)
    return None


def _yy(y: str) -> int:
    n = int(y)
    return n if n >= 1000 else 2000 + n


def _safe_date(y: int, mo: int, d: int) -> date | None:
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _add_months(d: date, n: int) -> date:
    total = d.year * 12 + (d.month - 1) + n
    y, mo = divmod(total, 12)
    return date(y, mo + 1, 1) if d.day == 1 else date(y, mo + 1, min(d.day, 28))


def _fy_bounds(end_year: int, conv: str) -> tuple[date, date]:
    sm = _FY_START_MONTH.get(conv, 4)
    if sm == 1:
        return date(end_year, 1, 1), date(end_year + 1, 1, 1)
    return date(end_year - 1, sm, 1), date(end_year, sm, 1)


@dataclass
class PeriodParse:
    start: str | None
    end: str | None
    type: str | None      # instant|quarter|half_year|fiscal_year|calendar_year|range|unknown|None


_UNKNOWN = "unknown"


def parse_period(raw: str | None, fy_convention: str) -> PeriodParse:
    """Resolve ``raw`` to a half-open [start, end) + type, using ``fy_convention``
    for FY/quarter boundaries. Unresolvable → type ``'unknown'``. Never assumes
    two different labels denote the same period without resolving each."""
    if not raw or not raw.strip():
        return PeriodParse(None, None, None)
    s = re.sub(r"\s+", " ", raw.strip())
    low = s.lower()
    conv = fy_convention if fy_convention in _FY_START_MONTH else "apr-mar"

    # N months ended <date>  -> range
    m = re.search(r"(?:^|\b)((?:\d+|" + "|".join(_WORD_NUM) + r"))[- ]months?\s+"
                  r"(?:period\s+)?end(?:ed|ing)\s+(?:on\s+)?(.+)$", low)
    if m:
        n = int(m[1]) if m[1].isdigit() else _WORD_NUM[m[1]]
        d = _parse_date(_orig_span(s, m, 2))
        if d:
            end = d + timedelta(days=1)
            return PeriodParse(_add_months(end, -n).isoformat(), end.isoformat(), "range")
        return PeriodParse(None, None, _UNKNOWN)

    # year ended <date>  -> fiscal_year (12 months back)
    m = re.search(r"(?:^|\bfor the\b|\bthe\b)?\s*(?:fiscal\s+|financial\s+)?year\s+"
                  r"end(?:ed|ing)\s+(?:on\s+)?(.+)$", low)
    if m:
        d = _parse_date(_orig_span(s, m, 1))
        if d:
            end = d + timedelta(days=1)
            return PeriodParse(_add_months(end, -12).isoformat(), end.isoformat(), "fiscal_year")
        return PeriodParse(None, None, _UNKNOWN)

    # as on/at/of <date>  -> instant
    m = re.search(r"\bas\s+(?:on|at|of)\s+(.+)$", low)
    if m:
        d = _parse_date(_orig_span(s, m, 1))
        if d:
            return PeriodParse(d.isoformat(), (d + timedelta(days=1)).isoformat(), "instant")
        return PeriodParse(None, None, _UNKNOWN)

    # Quarter: Q3 FY24 / Q3 2024 / 3Q FY24
    m = re.match(r"^q\s*([1-4])\s*(?:fy|f\.?y\.?)?\s*['’]?(\d{2}|\d{4})(?:\s*[-/]\s*(\d{2,4}))?$",
                 low) or re.match(r"^([1-4])\s*q\s*(?:fy)?\s*['’]?(\d{2}|\d{4})$", low)
    if m:
        end_year = _yy(m[3]) if m.lastindex and m.lastindex >= 3 and m[3] else _yy(m[2])
        qs, _ = _fy_bounds(end_year, conv)
        start = _add_months(qs, 3 * (int(m[1]) - 1))
        return PeriodParse(start.isoformat(), _add_months(qs, 3 * int(m[1])).isoformat(), "quarter")

    # Half: H1 FY24 / H2 FY24
    m = re.match(r"^h\s*([12])\s*(?:fy|f\.?y\.?)?\s*['’]?(\d{2}|\d{4})$", low)
    if m:
        fs, _ = _fy_bounds(_yy(m[2]), conv)
        start = _add_months(fs, 6 * (int(m[1]) - 1))
        end = _add_months(fs, 6 * int(m[1]))
        return PeriodParse(start.isoformat(), end.isoformat(), "half_year")

    # FY forms: FY24 / FY 2023-24 / FY2023-24 / FY2024/25 / Fiscal 2021
    m = re.match(r"^(?:fy|f\.?y\.?|fiscal(?:\s+year)?|financial\s+year)\s*['’]?"
                 r"(\d{2}|\d{4})(?:\s*[-/]\s*(\d{2,4}))?$", low)
    if m:
        end_year = _yy(m[2]) if m[2] else _yy(m[1])
        a, b = _fy_bounds(end_year, conv)
        return PeriodParse(a.isoformat(), b.isoformat(), "fiscal_year")

    # bare Indian FY: 2023-24 / 2024-25
    m = re.match(r"^(\d{4})\s*[-/]\s*(\d{2})$", low)
    if m:
        a, b = _fy_bounds(_yy(m[2]), conv)
        return PeriodParse(a.isoformat(), b.isoformat(), "fiscal_year")

    # multi-year calendar range: 2020-2021
    m = re.match(r"^(\d{4})\s*[-/]\s*(\d{4})$", low)
    if m and int(m[2]) > int(m[1]):
        return PeriodParse(date(int(m[1]), 1, 1).isoformat(),
                           date(int(m[2]) + 1, 1, 1).isoformat(), "range")

    # bare calendar year
    if re.match(r"^\d{4}$", low):
        y = int(low)
        return PeriodParse(date(y, 1, 1).isoformat(),
                           date(y + 1, 1, 1).isoformat(), "calendar_year")

    # bare date -> instant
    d = _parse_date(s)
    if d:
        return PeriodParse(d.isoformat(), (d + timedelta(days=1)).isoformat(), "instant")

    return PeriodParse(None, None, _UNKNOWN)


def _orig_span(original: str, m: re.Match[str], group: int) -> str:
    """The matched group, taken from the ORIGINAL-case string (dates are
    matched case-insensitively but parsed from the real text)."""
    return original[m.start(group):m.end(group)]


def detect_fy_convention(document_text: str) -> str | None:
    """Scan a document for an explicit fiscal-year-end statement. Returns a
    schema convention (`apr-mar` / `jan-dec` / `jul-jun`) or None. Months other
    than March / December / June cannot be represented and return None."""
    m = re.search(
        r"(?i)(?:financial|fiscal)\s+year\s+(?:that\s+)?end(?:ed|ing|s)?\s+"
        r"(?:on\s+)?(?:the\s+)?(?:\d{1,2}(?:st|nd|rd|th)?\s+)?([A-Za-z]{3,})",
        document_text,
    )
    if not m:
        m = re.search(
            r"(?i)year\s+end(?:ed|ing)\s+(?:on\s+)?([A-Za-z]{3,})\s+\d{1,2}", document_text
        )
    if not m:
        return None
    month = _MONTHS.get(m[1].lower())
    return {3: "apr-mar", 12: "jan-dec", 6: "jul-jun"}.get(month)


# --------------------------------------------------------------------------- #
# per-fact normalization                                                     #
# --------------------------------------------------------------------------- #
def _record_failure(conn, run_id, document_id, failure_type, reason, ref_table, ref_id, detail):
    conn.execute(
        "INSERT INTO failures "
        "(run_id, document_id, failure_type, ref_table, ref_id, reason, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, document_id, failure_type, ref_table, ref_id, reason,
         json.dumps(detail, ensure_ascii=False, default=str), _now()),
    )


def apply_normalization(conn: sqlite3.Connection, fact_id: int, *, fy_convention: str,
                        settings: Settings | None = None,
                        run_id: int | None = None) -> NormalizationResult:
    """Normalize one GROUNDED (or NORMALIZED, for idempotent re-runs) fact.
    Never clears a ``*_raw`` column."""
    settings = settings or get_settings()
    fact = conn.execute(
        "SELECT id, document_id, fact_type, lifecycle_state, value_raw, numeric_value, "
        "is_percentage, unit_raw, currency, reporting_period_raw FROM facts WHERE id = ?",
        (fact_id,),
    ).fetchone()
    if fact is None:
        raise NormalizeError("unknown_fact", detail=fact_id)
    if fact["lifecycle_state"] not in ("GROUNDED", "NORMALIZED"):
        raise NormalizeError("fact_not_grounded", detail=fact["lifecycle_state"])

    updates: dict[str, Any] = {}
    failed_reason: str | None = None
    numeric_normalized = False

    if fact["fact_type"] == "numeric":
        np = parse_number(fact["value_raw"])
        if np is None and fact["numeric_value"] is None:
            failed_reason = "numeric_unparseable"
        else:
            nv = fact["numeric_value"] if fact["numeric_value"] is not None else np.value  # type: ignore[union-attr]
            updates["numeric_value"] = nv
            if np is not None:
                updates["magnitude"] = np.magnitude
                updates["magnitude_factor"] = np.magnitude_factor
                updates["is_percentage"] = int(np.is_percentage)
                if np.is_percentage:
                    updates["percentage_ratio"] = np.percentage_ratio
                    updates["base_value"] = np.percentage_ratio
                else:
                    updates["base_value"] = nv * (np.magnitude_factor or 1.0)
            cur = parse_currency(
                " ".join(filter(None, (fact["value_raw"], fact["unit_raw"], fact["currency"])))
            )
            if cur:
                updates["currency"] = cur
            un = parse_unit(fact["unit_raw"], currency=cur or fact["currency"])
            if un:
                updates["unit_norm"] = un
            numeric_normalized = True

    period_type: str | None = None
    if fact["reporting_period_raw"]:
        pp = parse_period(fact["reporting_period_raw"], fy_convention)
        updates["reporting_period_start"] = pp.start
        updates["reporting_period_end"] = pp.end
        updates["reporting_period_type"] = pp.type
        period_type = pp.type

    with db.transaction(conn):
        if updates:
            sets = ", ".join(f"{k} = :{k}" for k in updates)
            conn.execute(f"UPDATE facts SET {sets} WHERE id = :id", {**updates, "id": fact_id})
        conn.execute(
            "DELETE FROM failures WHERE ref_table = 'facts' AND ref_id = ? "
            "AND failure_type = 'normalization_failed'",
            (fact_id,),
        )
        if failed_reason:
            _record_failure(conn, run_id, fact["document_id"], "normalization_failed",
                            failed_reason, "facts", fact_id, {"value_raw": fact["value_raw"]})
            final_state = fact["lifecycle_state"]
        else:
            if fact["lifecycle_state"] == "GROUNDED":
                set_lifecycle(conn, fact_id, "NORMALIZED")
            final_state = "NORMALIZED"

    return NormalizationResult(
        fact_id=fact_id, lifecycle_state=final_state,
        numeric_normalized=numeric_normalized, period_type=period_type,
        base_value=updates.get("base_value"), magnitude_factor=updates.get("magnitude_factor"),
        currency=updates.get("currency"), unit_norm=updates.get("unit_norm"),
        failed=failed_reason is not None, reason=failed_reason,
    )


# --------------------------------------------------------------------------- #
# batch                                                                      #
# --------------------------------------------------------------------------- #
def normalize_document(document_id: int, *, database_path: str | None = None,
                       settings: Settings | None = None) -> DocNormalizationSummary:
    """Normalize every GROUNDED/NORMALIZED fact of one document. Resolves the
    document's fy_convention first (detect → config default; an `override` is
    left as-is). Per-fact failures are isolated."""
    settings = settings or get_settings()
    conn = db.connect(database_path)
    run_id: int | None = None
    try:
        doc = conn.execute(
            "SELECT id, fy_convention, fy_convention_source FROM documents WHERE id = ?",
            (document_id,),
        ).fetchone()
        if doc is None:
            raise NormalizeError("unknown_document", detail=document_id)

        conv, src = doc["fy_convention"], doc["fy_convention_source"]
        if src != "override" and conv == "unknown":
            text = "\n".join(
                r["text"] for r in conn.execute(
                    "SELECT text FROM pages WHERE document_id = ? ORDER BY page_index",
                    (document_id,),
                )
            )
            detected = detect_fy_convention(text)
            conv, src = (detected, "detected") if detected else \
                (settings.fy_convention_default, "config_default")
            with db.transaction(conn):
                conn.execute(
                    "UPDATE documents SET fy_convention = ?, fy_convention_source = ? WHERE id = ?",
                    (conv, src, document_id),
                )
        elif conv == "unknown":
            conv = settings.fy_convention_default

        with db.transaction(conn):
            run_id = conn.execute(
                "INSERT INTO runs (document_id, run_type, status, stage, started_at, settings) "
                "VALUES (?, 'normalize', 'running', 'normalize', ?, ?)",
                (document_id, _now(),
                 json.dumps({"fy_convention": conv, "fy_convention_source": src})),
            ).lastrowid

        fact_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM facts WHERE document_id = ? "
                "AND lifecycle_state IN ('GROUNDED','NORMALIZED') ORDER BY id",
                (document_id,),
            )
        ]
        c = {k: 0 for k in ("examined", "normalized", "numeric_normalized", "period_resolved",
                            "period_unknown", "period_absent", "normalization_failed", "errors")}
        by_period_type: dict[str, int] = {}
        for fid in fact_ids:
            c["examined"] += 1
            try:
                r = apply_normalization(conn, fid, fy_convention=conv, settings=settings,
                                        run_id=run_id)
            except Exception as e:  # noqa: BLE001 - isolate a failing fact
                c["errors"] += 1
                with db.transaction(conn):
                    _record_failure(conn, run_id, document_id, "run_error", "normalize_exception",
                                    "facts", fid, {"error": repr(e)[:300]})
                continue
            if r.failed:
                c["normalization_failed"] += 1
            else:
                c["normalized"] += 1
                if r.numeric_normalized:
                    c["numeric_normalized"] += 1
            if r.period_type is None:
                c["period_absent"] += 1
            elif r.period_type == "unknown":
                c["period_unknown"] += 1
            else:
                c["period_resolved"] += 1
                by_period_type[r.period_type] = by_period_type.get(r.period_type, 0) + 1

        with db.transaction(conn):
            conn.execute(
                "UPDATE runs SET status = 'done', stage = 'complete', finished_at = ?, "
                "facts_grounded = ? WHERE id = ?",
                (_now(), c["normalized"], run_id),
            )
        log.info("normalized document_id=%s examined=%s normalized=%s failed=%s "
                 "period_resolved=%s period_unknown=%s fy=%s", document_id, c["examined"],
                 c["normalized"], c["normalization_failed"], c["period_resolved"],
                 c["period_unknown"], conv)
        return DocNormalizationSummary(
            run_id=run_id, document_id=document_id, status="done",
            fy_convention=conv, fy_convention_source=src, by_period_type=by_period_type, **c,
        )
    except NormalizeError as e:
        if run_id is not None:
            _mark_run_failed(conn, run_id, document_id, e.code)
            e.run_id = run_id
        raise
    except Exception as e:  # noqa: BLE001
        if run_id is not None:
            _mark_run_failed(conn, run_id, document_id, f"unexpected: {e!r}")
        raise NormalizeError("normalization_failed", detail=repr(e)[:200], run_id=run_id) from e
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
        log.exception("failed to record normalization-run failure run_id=%s", run_id)


# --------------------------------------------------------------------------- #
# observability                                                              #
# --------------------------------------------------------------------------- #
def normalization_summary(conn: sqlite3.Connection, *, document_id: int | None = None,
                          run_id: int | None = None) -> dict[str, Any]:
    where, params = [], []
    if document_id is not None:
        where.append("document_id = ?")
        params.append(document_id)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    by_lifecycle = {
        r["lifecycle_state"]: r["n"]
        for r in conn.execute(
            f"SELECT lifecycle_state, COUNT(*) AS n FROM facts{clause} GROUP BY lifecycle_state",
            params,
        )
    }
    by_period_type = {
        (r["reporting_period_type"] or "«none»"): r["n"]
        for r in conn.execute(
            f"SELECT reporting_period_type, COUNT(*) AS n FROM facts{clause} "
            f"GROUP BY reporting_period_type", params,
        )
    }
    by_magnitude = {
        (r["magnitude"] or "«plain»"): r["n"]
        for r in conn.execute(
            f"SELECT magnitude, COUNT(*) AS n FROM facts{clause} "
            f"{'AND' if where else 'WHERE'} fact_type = 'numeric' GROUP BY magnitude", params,
        )
    }
    with_base_value = conn.execute(
        f"SELECT COUNT(*) FROM facts{clause} "
        f"{'AND' if where else 'WHERE'} fact_type = 'numeric' AND base_value IS NOT NULL", params,
    ).fetchone()[0]
    fail_where = [*where, "failure_type = 'normalization_failed'"]
    fail_params = list(params)
    if run_id is not None:
        fail_where.append("run_id = ?")
        fail_params.append(run_id)
    failed_reasons = {
        r["reason"]: r["n"]
        for r in conn.execute(
            "SELECT reason, COUNT(*) AS n FROM failures WHERE "
            + " AND ".join(fail_where) + " GROUP BY reason", fail_params,
        )
    }
    return {
        "facts_by_lifecycle": by_lifecycle,
        "facts_by_period_type": by_period_type,
        "numeric_by_magnitude": by_magnitude,
        "numeric_with_base_value": with_base_value,
        "normalization_failed_reasons": failed_reasons,
    }
