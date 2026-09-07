"""Phase 6 — deterministic context / numeric / date / unit normalization.

Pure-function tests reproduce the two DATA_MODEL worked tables exactly; the
integration tests exercise apply_normalization / normalize_document against
synthetic GROUNDED facts (+ one real starter PDF). No LLM.
"""

from __future__ import annotations

import pytest

from app import db
from app.facts import attach_evidence, evidence_chain, insert_fact
from app.models import EvidenceIn, FactIn
from app.normalize import (
    NormalizeError,
    apply_normalization,
    detect_fy_convention,
    normalization_summary,
    normalize_document,
    parse_currency,
    parse_number,
    parse_period,
    parse_unit,
)

NOW = "2026-01-01T00:00:00Z"


def _grounded(conn, src, page=0, **over) -> int:
    over.setdefault("object_raw", over.get("value_raw") or "x")
    if over.get("fact_type") == "numeric":
        over["value_text"] = None
    else:
        over.setdefault("value_text", over["object_raw"])
    return insert_fact(conn, FactIn(
        document_id=src["document_id"], page_index=page, subject_raw="S", predicate="p",
        fact_type=over.pop("fact_type", "semantic"), lifecycle_state="GROUNDED",
        evidence_status="VERIFIED", **over,
    ))


# --------------------------------------------------------------------------- #
# parse_number — DATA_MODEL "Numeric representation — worked examples"        #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "value", "magnitude", "factor", "base", "pct", "ratio"),
    [
        ("₹8,142 crore", 8142.0, "crore", 1e7, 8.142e10, False, None),
        ("Rs. 578 Cr", 578.0, "crore", 1e7, 5.78e9, False, None),
        ("5%", 5.0, None, None, 0.05, True, 0.05),
        ("781 Bps", 781.0, "bps", 1e-4, 0.0781, True, 0.0781),
        ("₹(452) Cr", -452.0, "crore", 1e7, -4.52e9, False, None),
        ("US$216 billion", 216.0, "billion", 1e9, 2.16e11, False, None),
        ("1.4 Mn Tons", 1.4, "million", 1e6, 1.4e6, False, None),
        ("1,234", 1234.0, None, None, 1234.0, False, None),
        ("18%", 18.0, None, None, 0.18, True, 0.18),
    ],
)
def test_parse_number(raw, value, magnitude, factor, base, pct, ratio):
    p = parse_number(raw)
    assert p is not None
    assert p.value == value
    assert p.magnitude == magnitude
    assert p.magnitude_factor == factor
    assert p.is_percentage is pct
    if ratio is None:
        assert p.percentage_ratio is None
    else:
        assert abs(p.percentage_ratio - ratio) < 1e-9
    got_base = p.percentage_ratio if p.is_percentage else p.value * (p.magnitude_factor or 1.0)
    assert abs(got_base - base) < abs(base) * 1e-9 + 1e-9


@pytest.mark.parametrize("raw", ["", "two 1 and 2", "no digits here", None])
def test_parse_number_ambiguous_returns_none(raw):
    assert parse_number(raw) is None


# --------------------------------------------------------------------------- #
# parse_currency / parse_unit                                                #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "code"),
    [("₹8,142 crore", "INR"), ("Rs. 578 Cr", "INR"), ("US$216 billion", "USD"),
     ("$5.00", "USD"), ("€10m", "EUR"), ("£3bn", "GBP"), ("42 units", None), ("", None)],
)
def test_parse_currency(text, code):
    assert parse_currency(text) == code


@pytest.mark.parametrize(
    ("unit_raw", "norm"),
    [("₹ crore", "INR"), ("Rs Cr", "INR"), ("US$ billion", "USD"), ("%", "ratio"),
     ("per cent", "ratio"), ("bps", "ratio"), ("Mn Tons", "tonne"), ("tonnes", "tonne"),
     ("days", "days"), ("million sq ft", "sqft"), ("shipments", "count"),
     ("pin codes", "count"), ("0.01x", "ratio"), ("widgets", None), ("", None)],
)
def test_parse_unit(unit_raw, norm):
    assert parse_unit(unit_raw) == norm


# --------------------------------------------------------------------------- #
# parse_period — DATA_MODEL "Period representation examples"                  #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "start", "end", "ptype"),
    [
        ("FY24", "2023-04-01", "2024-04-01", "fiscal_year"),
        ("Q3 FY24", "2023-10-01", "2024-01-01", "quarter"),
        ("nine months ended December 31, 2021", "2021-04-01", "2022-01-01", "range"),
        ("as on March 31, 2024", "2024-03-31", "2024-04-01", "instant"),
        ("FY2025/26", "2025-04-01", "2026-04-01", "fiscal_year"),
        ("Fiscal 2021", "2020-04-01", "2021-04-01", "fiscal_year"),
        ("2024", "2024-01-01", "2025-01-01", "calendar_year"),
        ("FY 2023-24", "2023-04-01", "2024-04-01", "fiscal_year"),
        ("2024-25", "2024-04-01", "2025-04-01", "fiscal_year"),
        ("Q2 FY25", "2024-07-01", "2024-10-01", "quarter"),
        ("H1 FY24", "2023-04-01", "2023-10-01", "half_year"),
        ("year ended March 31, 2024", "2023-04-01", "2024-04-01", "fiscal_year"),
        ("March 31, 2024", "2024-03-31", "2024-04-01", "instant"),
        ("2020-2021", "2020-01-01", "2022-01-01", "range"),
        ("some vague phrase", None, None, "unknown"),
    ],
)
def test_parse_period_apr_mar(raw, start, end, ptype):
    p = parse_period(raw, "apr-mar")
    assert (p.start, p.end, p.type) == (start, end, ptype)


def test_parse_period_none_and_convention():
    assert parse_period(None, "apr-mar") == parse_period("", "apr-mar")
    assert parse_period(None, "apr-mar").type is None
    # a different resolved convention gives different FY bounds
    jan = parse_period("FY24", "jan-dec")
    assert (jan.start, jan.end) == ("2024-01-01", "2025-01-01")


@pytest.mark.parametrize(
    ("text", "conv"),
    [
        ("... for the financial year ended March 31, 2024 ...", "apr-mar"),
        ("The fiscal year ending December 31, 2024", "jan-dec"),
        ("financial year that ends June 30", "jul-jun"),
        ("year ended September 30, 2024", None),           # unrepresentable month
        ("nothing relevant in this passage", None),
    ],
)
def test_detect_fy_convention(text, conv):
    assert detect_fy_convention(text) == conv


# --------------------------------------------------------------------------- #
# apply_normalization                                                        #
# --------------------------------------------------------------------------- #
def test_numeric_fact_normalized(conn, db_path, make_source):
    src = make_source(["irrelevant page text"])
    fid = _grounded(conn, src, fact_type="numeric", value_raw="₹8,142 crore",
                    numeric_value=8142.0, unit_raw="₹ crore", currency="INR",
                    reporting_period_raw="FY24")
    conn.commit()
    r = apply_normalization(conn, fid, fy_convention="apr-mar")
    assert r.lifecycle_state == "NORMALIZED"
    row = conn.execute("SELECT * FROM facts WHERE id = ?", (fid,)).fetchone()
    assert row["magnitude"] == "crore"
    assert row["magnitude_factor"] == 1e7
    assert row["base_value"] == 8.142e10
    assert row["currency"] == "INR"
    assert row["unit_norm"] == "INR"
    assert row["reporting_period_start"] == "2023-04-01"
    assert row["reporting_period_end"] == "2024-04-01"
    assert row["reporting_period_type"] == "fiscal_year"
    # raw preserved
    assert row["value_raw"] == "₹8,142 crore"
    assert row["reporting_period_raw"] == "FY24"
    assert row["unit_raw"] == "₹ crore"


def test_percentage_fact_normalized(conn, db_path, make_source):
    src = make_source(["p"])
    fid = _grounded(conn, src, fact_type="numeric", value_raw="5%", numeric_value=5.0,
                    is_percentage=True, unit_raw="%")
    conn.commit()
    apply_normalization(conn, fid, fy_convention="apr-mar")
    row = conn.execute("SELECT * FROM facts WHERE id = ?", (fid,)).fetchone()
    assert row["is_percentage"] == 1
    assert row["percentage_ratio"] == 0.05
    assert row["base_value"] == 0.05
    assert row["unit_norm"] == "ratio"
    assert row["magnitude_factor"] is None


def test_unparseable_numeric_is_normalization_failed(conn, db_path, make_source):
    src = make_source(["p"])
    fid = _grounded(conn, src, fact_type="numeric", value_raw="roughly a handful",
                    reporting_period_raw="FY24")
    conn.commit()
    r = apply_normalization(conn, fid, fy_convention="apr-mar")
    assert r.failed is True
    assert r.reason == "numeric_unparseable"
    assert r.lifecycle_state == "GROUNDED"     # NOT promoted
    assert conn.execute(
        "SELECT COUNT(*) FROM failures WHERE ref_id = ? AND failure_type = 'normalization_failed'",
        (fid,),
    ).fetchone()[0] == 1


def test_unknown_period_still_normalized(conn, db_path, make_source):
    src = make_source(["p"])
    fid = _grounded(conn, src, reporting_period_raw="at some recent point")
    conn.commit()
    r = apply_normalization(conn, fid, fy_convention="apr-mar")
    assert r.period_type == "unknown"
    assert r.lifecycle_state == "NORMALIZED"    # ambiguity recorded, not blocking
    row = conn.execute(
        "SELECT reporting_period_type, reporting_period_start FROM facts WHERE id = ?", (fid,)
    ).fetchone()
    assert row["reporting_period_type"] == "unknown"
    assert row["reporting_period_start"] is None


def test_no_period_normalized(conn, db_path, make_source):
    src = make_source(["p"])
    fid = _grounded(conn, src)
    conn.commit()
    r = apply_normalization(conn, fid, fy_convention="apr-mar")
    assert r.period_type is None
    assert r.lifecycle_state == "NORMALIZED"


def test_apply_normalization_requires_grounded(conn, db_path, make_source):
    src = make_source(["p"])
    fid = insert_fact(conn, FactIn(
        document_id=src["document_id"], page_index=0, subject_raw="s", predicate="p",
        object_raw="o", fact_type="semantic", value_text="o", lifecycle_state="CANDIDATE",
    ))
    conn.commit()
    with pytest.raises(NormalizeError) as e:
        apply_normalization(conn, fid, fy_convention="apr-mar")
    assert e.value.code == "fact_not_grounded"


def test_provenance_chain_preserved(conn, db_path, make_source):
    src = make_source(["The revenue line reads eight thousand one hundred crore here."])
    fid = _grounded(conn, src, fact_type="numeric", value_raw="8100 crore",
                    numeric_value=8100.0, reporting_period_raw="FY24")
    attach_evidence(conn, fid, EvidenceIn(
        document_id=src["document_id"], page_index=0, chunk_id=src["chunk_ids"][0],
        quote="eight thousand one hundred crore", char_start=16, char_end=48,
        verification_method="exact", evidence_status="VERIFIED"))
    conn.commit()
    apply_normalization(conn, fid, fy_convention="apr-mar")
    chain = evidence_chain(conn, fid)
    assert chain is not None
    assert chain["document_id"] == src["document_id"]
    assert chain["chunk_id"] == src["chunk_ids"][0]


# --------------------------------------------------------------------------- #
# normalize_document                                                         #
# --------------------------------------------------------------------------- #
def test_normalize_document_summary_and_fy_detection(conn, db_path, make_source):
    src = make_source([
        "Cover. The financial year ended March 31, 2024. Other text.",
        "Body text on page two with no fiscal statement.",
    ])
    _grounded(conn, src, fact_type="numeric", value_raw="₹8,142 crore", numeric_value=8142.0,
              reporting_period_raw="FY24")
    _grounded(conn, src, page=1, reporting_period_raw="as on March 31, 2024")
    _grounded(conn, src, page=1, reporting_period_raw="a fuzzy time")           # -> unknown
    _grounded(conn, src, page=1)                                                # no period
    conn.commit()

    summ = normalize_document(src["document_id"], database_path=db_path)
    assert summ.status == "done"
    assert summ.examined == 4
    assert summ.normalized == 4
    assert summ.numeric_normalized == 1
    assert summ.period_resolved == 2
    assert summ.period_unknown == 1
    assert summ.period_absent == 1
    assert summ.fy_convention == "apr-mar"
    assert summ.fy_convention_source == "detected"
    # stored on the document
    row = conn.execute(
        "SELECT fy_convention, fy_convention_source FROM documents WHERE id = ?",
        (src["document_id"],),
    ).fetchone()
    assert (row["fy_convention"], row["fy_convention_source"]) == ("apr-mar", "detected")


def test_normalize_document_fy_falls_back_to_config_default(conn, db_path, make_source):
    src = make_source(["Body with no fiscal-year statement anywhere on it."])
    _grounded(conn, src, reporting_period_raw="FY24")
    conn.commit()
    summ = normalize_document(src["document_id"], database_path=db_path)
    assert summ.fy_convention_source == "config_default"
    assert summ.fy_convention == "apr-mar"


def test_normalize_document_isolates_failures(conn, db_path, make_source, monkeypatch):
    src = make_source(["p p p"])
    a = _grounded(conn, src, reporting_period_raw="FY24")
    b = _grounded(conn, src, reporting_period_raw="FY23")
    conn.commit()

    import app.normalize as nmod
    real = nmod.apply_normalization

    def flaky(conn_, fid, **kw):
        if fid == a:
            raise RuntimeError("kaboom")
        return real(conn_, fid, **kw)

    monkeypatch.setattr(nmod, "apply_normalization", flaky)
    summ = normalize_document(src["document_id"], database_path=db_path)
    assert summ.errors == 1
    assert summ.normalized == 1
    state = conn.execute("SELECT lifecycle_state FROM facts WHERE id = ?", (b,)).fetchone()[0]
    assert state == "NORMALIZED"


def test_normalize_document_is_idempotent(conn, db_path, make_source):
    src = make_source(["financial year ended March 31, 2024"])
    fid = _grounded(conn, src, fact_type="numeric", value_raw="₹8,142 crore",
                    numeric_value=8142.0, reporting_period_raw="FY24")
    _grounded(conn, src, fact_type="numeric", value_raw="unparseable blob")   # stays GROUNDED
    conn.commit()
    s1 = normalize_document(src["document_id"], database_path=db_path)
    row1 = conn.execute("SELECT base_value, reporting_period_start, lifecycle_state FROM facts "
                        "WHERE id = ?", (fid,)).fetchone()
    s2 = normalize_document(src["document_id"], database_path=db_path)
    row2 = conn.execute("SELECT base_value, reporting_period_start, lifecycle_state FROM facts "
                        "WHERE id = ?", (fid,)).fetchone()
    assert tuple(row1) == tuple(row2)
    assert s2.normalized == s1.normalized
    # normalization_failed failure not duplicated across runs
    assert conn.execute(
        "SELECT COUNT(*) FROM failures WHERE document_id = ? "
        "AND failure_type = 'normalization_failed'",
        (src["document_id"],),
    ).fetchone()[0] == 1


def test_normalize_document_skips_candidate_facts(conn, db_path, make_source):
    src = make_source(["p"])
    cand = insert_fact(conn, FactIn(
        document_id=src["document_id"], page_index=0, subject_raw="s", predicate="p",
        object_raw="o", fact_type="semantic", value_text="o", lifecycle_state="CANDIDATE",
        reporting_period_raw="FY24"))
    conn.commit()
    summ = normalize_document(src["document_id"], database_path=db_path)
    assert summ.examined == 0
    assert conn.execute(
        "SELECT reporting_period_start, lifecycle_state FROM facts WHERE id = ?", (cand,)
    ).fetchone()["reporting_period_start"] is None


def test_normalize_document_unknown(db_path):
    db.init_db()
    with pytest.raises(NormalizeError) as e:
        normalize_document(999999, database_path=db_path)
    assert e.value.code == "unknown_document"


def test_normalization_summary(conn, db_path, make_source):
    src = make_source(["financial year ended March 31, 2024"])
    _grounded(conn, src, fact_type="numeric", value_raw="₹8,142 crore", numeric_value=8142.0,
              reporting_period_raw="FY24")
    _grounded(conn, src, fact_type="numeric", value_raw="5%", numeric_value=5.0,
              is_percentage=True, reporting_period_raw="Q3 FY24")
    _grounded(conn, src, fact_type="numeric", value_raw="garbled")
    conn.commit()
    normalize_document(src["document_id"], database_path=db_path)
    s = normalization_summary(conn, document_id=src["document_id"])
    assert s["facts_by_lifecycle"].get("NORMALIZED", 0) == 2
    assert s["facts_by_lifecycle"].get("GROUNDED", 0) == 1
    assert s["numeric_with_base_value"] == 2
    assert "fiscal_year" in s["facts_by_period_type"]
    assert s["normalization_failed_reasons"].get("numeric_unparseable", 0) == 1


# --------------------------------------------------------------------------- #
# real starter PDF (deterministic; no LLM)                                   #
# --------------------------------------------------------------------------- #
def test_real_pdf_normalization(conn, db_path):
    import pathlib
    import re
    pdf = pathlib.Path("starter-datasets/delhivery/03-delhivery-q4-fy24-earnings-presentation.pdf")
    if not pdf.exists():
        pytest.skip("starter PDF not present")
    from app.ingest import ingest_pdf
    doc = ingest_pdf(str(pdf)).document_id
    row = conn.execute(
        "SELECT text FROM pages WHERE document_id = ? AND length(text) > 300 "
        "ORDER BY page_index LIMIT 1", (doc,),
    ).fetchone()
    m = re.search(r"\d[\d,]{2,}", row["text"])
    assert m, "expected a multi-digit number in the page"
    raw = m.group(0)
    fid = insert_fact(conn, FactIn(
        document_id=doc, page_index=0, subject_raw="d", predicate="figure", object_raw=raw,
        fact_type="numeric", value_raw=f"{raw} crore", numeric_value=float(raw.replace(",", "")),
        lifecycle_state="GROUNDED", evidence_status="VERIFIED", reporting_period_raw="FY24",
    ))
    conn.commit()
    summ = normalize_document(doc, database_path=db_path)
    assert summ.numeric_normalized >= 1
    got = conn.execute(
        "SELECT base_value, magnitude, reporting_period_start, reporting_period_type, "
        "value_raw, lifecycle_state FROM facts WHERE id = ?", (fid,),
    ).fetchone()
    assert got["magnitude"] == "crore"
    assert got["base_value"] == float(raw.replace(",", "")) * 1e7
    assert got["reporting_period_start"] == "2023-04-01"
    assert got["reporting_period_type"] == "fiscal_year"
    assert got["value_raw"] == f"{raw} crore"     # raw preserved
    assert got["lifecycle_state"] == "NORMALIZED"
