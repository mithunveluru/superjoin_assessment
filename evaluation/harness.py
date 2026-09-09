"""Phase 10 — evaluation harness.

    python -m evaluation.harness                 # build the synthetic offline corpus, evaluate
    python -m evaluation.harness --sweep         # also run the curated config sweep
    python -m evaluation.harness --db <path>     # evaluate an existing database (no pipeline)
    python -m evaluation.harness --corpus <dir>  # run the real pipeline on a dir of PDFs
                                                 #   (needs GEMINI_API_KEY for extraction)

Checks **expected properties** (evaluation/cases/**/*.json -> evaluation.properties),
never hard-coded answers, plus global invariants and a config sweep. Exit code 0
iff every ``must_hold`` case-property and every invariant pass.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from app import db
from app.config import Settings, get_settings
from app.reason import reason_document
from evaluation import properties as P

CASES_DIR = Path(__file__).with_name("cases")
REPORTS_DIR = Path(__file__).with_name("reports")

# curated grid — a handful of named configs, more legible than a full cartesian
# product and enough to show which knobs move the properties (EVALUATION_PLAN §3.3).
SWEEP_CONFIGS: list[tuple[str, dict]] = [
    ("baseline", {}),
    ("retrieval_top_k=5", {"retrieval_top_k": 5}),
    ("candidate_threshold=0.80", {"retrieval_candidate_threshold": 0.80}),
    ("predicate_similarity_threshold=0.85", {"predicate_similarity_threshold": 0.85}),
    ("relationship_confidence_threshold=0.90", {"relationship_confidence_threshold": 0.90}),
    ("numeric_equivalence_tolerance=0.001", {"numeric_equivalence_tolerance": 0.001}),
    ("numeric_contradiction_threshold=0.50", {"numeric_contradiction_threshold": 0.50}),
]


def _now() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------- #
# evaluation                                                                 #
# --------------------------------------------------------------------------- #
def evaluate(conn: sqlite3.Connection, settings: Settings) -> dict:
    return {
        "properties": {n: fn(conn, settings) for n, fn in P.CASE_PROPERTIES.items()},
        "invariants": {n: fn(conn, settings) for n, fn in P.INVARIANTS.items()},
    }


def load_cases() -> list[dict]:
    cases = []
    for path in sorted(CASES_DIR.rglob("*.json")):
        spec = json.loads(path.read_text(encoding="utf-8"))
        spec["file"] = str(path.relative_to(CASES_DIR.parent))
        if spec["property"] not in P.CASE_PROPERTIES:
            raise ValueError(f"{spec['file']}: unknown property {spec['property']!r}")
        cases.append(spec)
    return cases


def _pr(r: P.PropertyResult) -> dict:
    return {"passed": r.passed, "detail": r.detail, "rows": r.rows}


def _counts(conn: sqlite3.Connection) -> dict:
    by_cat = {r["category"]: r["n"] for r in conn.execute(
        "SELECT category, COUNT(*) AS n FROM relationships GROUP BY category")}
    by_fail = {r["failure_type"]: r["n"] for r in conn.execute(
        "SELECT failure_type, COUNT(*) AS n FROM failures GROUP BY failure_type")}
    return {
        "relationships_total": sum(by_cat.values()),
        "by_category": by_cat,
        "uncertain": by_cat.get("UNCERTAIN", 0),
        "quarantined_facts": conn.execute(
            "SELECT COUNT(*) FROM facts WHERE lifecycle_state = 'QUARANTINED'").fetchone()[0],
        "eligible_facts": conn.execute(
            "SELECT COUNT(*) FROM facts WHERE lifecycle_state = 'ELIGIBLE_FOR_REASONING'"
        ).fetchone()[0],
        "failures_by_type": by_fail,
    }


def _finish(db_path: str, settings: Settings, *, sweep: bool, source: str) -> dict:
    conn = db.connect(db_path)
    try:
        ev = evaluate(conn, settings)
        by_name = {**ev["properties"], **ev["invariants"]}
        cases = load_cases()
        for c in cases:
            c["passed"] = by_name[c["property"]].passed
            c["detail"] = by_name[c["property"]].detail
        report = {
            "source": source,
            "generated_at": _now(),
            "settings": {
                "numeric_equivalence_tolerance": settings.numeric_equivalence_tolerance,
                "numeric_contradiction_threshold": settings.numeric_contradiction_threshold,
                "predicate_similarity_threshold": settings.predicate_similarity_threshold,
                "retrieval_top_k": settings.retrieval_top_k,
                "retrieval_candidate_threshold": settings.retrieval_candidate_threshold,
                "relationship_confidence_threshold": settings.relationship_confidence_threshold,
            },
            "properties": {n: _pr(r) for n, r in ev["properties"].items()},
            "invariants": {n: _pr(r) for n, r in ev["invariants"].items()},
            "cases": cases,
            "counts": _counts(conn),
        }
        report["invariants_pass"] = all(r.passed for r in ev["invariants"].values())
        report["must_hold_pass"] = all(c["passed"] for c in cases if c["must_hold"])
        report["all_pass"] = report["invariants_pass"] and report["must_hold_pass"]
    finally:
        conn.close()
    if sweep:
        report["sweep"] = sweep_thresholds(db_path)
    return report


# --------------------------------------------------------------------------- #
# config sweep                                                               #
# --------------------------------------------------------------------------- #
def _doc_ids(db_path: str) -> list[int]:
    conn = db.connect(db_path)
    try:
        return [r["id"] for r in conn.execute("SELECT id FROM documents ORDER BY id")]
    finally:
        conn.close()


def sweep_thresholds(db_path: str) -> dict:
    """Re-run reasoning under each curated config and report the property table.
    Restores the baseline config at the end so the on-disk DB matches the report."""
    docs = _doc_ids(db_path)
    rows = []
    for label, overrides in [*SWEEP_CONFIGS, ("baseline", {})]:
        settings = Settings(**overrides)
        reset = db.connect(db_path)
        try:
            with db.transaction(reset):
                reset.execute("DELETE FROM relationships")
                reset.execute("DELETE FROM failures WHERE failure_type = 'relationship_uncertain'")
        finally:
            reset.close()
        for doc in docs:
            reason_document(doc, database_path=db_path, settings=settings)

        conn = db.connect(db_path)
        try:
            ev = evaluate(conn, settings)
            counts = _counts(conn)
            above = conn.execute(
                "SELECT COUNT(*) FROM relationships WHERE confidence >= ?",
                (settings.relationship_confidence_threshold,),
            ).fetchone()[0]
        finally:
            conn.close()
        cases = load_cases()
        by_name = {**ev["properties"], **ev["invariants"]}
        rows.append({
            "label": label,
            "overrides": overrides,
            "properties": {n: by_name[n].passed for n in P.CASE_PROPERTIES},
            "invariants_pass": all(r.passed for r in ev["invariants"].values()),
            "must_hold_pass": all(by_name[c["property"]].passed for c in cases if c["must_hold"]),
            "relationships": counts["relationships_total"],
            "by_category": counts["by_category"],
            "uncertain": counts["uncertain"],
            "quarantined": counts["quarantined_facts"],
            "above_confidence": above,
        })
    # the trailing baseline re-run left the DB in the baseline state; drop it from the table
    table = rows[:-1]
    ok = [r for r in table if r["must_hold_pass"] and r["invariants_pass"]]
    ok.sort(key=lambda r: (-(r["relationships"] - r["uncertain"]), r["uncertain"]))
    return {"rows": table, "recommended": ok[0]["label"] if ok else None}


# --------------------------------------------------------------------------- #
# entry points                                                               #
# --------------------------------------------------------------------------- #
def run_synthetic(*, settings: Settings | None = None, sweep: bool = False) -> dict:
    import shutil

    from evaluation.corpora.synthetic import seed_corpus

    settings = settings or get_settings()
    tmp = Path(tempfile.mkdtemp(prefix="fkl-eval-"))
    try:
        db_path = str(tmp / "eval.db")
        seed_corpus(db_path, settings=settings)
        return _finish(db_path, settings, sweep=sweep, source="synthetic offline corpus")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_db(db_path: str, *, settings: Settings | None = None, sweep: bool = False) -> dict:
    settings = settings or get_settings()
    return _finish(db_path, settings, sweep=sweep, source=f"database {db_path}")


def run_corpus(corpus_dir: str, *, settings: Settings | None = None,
               sweep: bool = False) -> dict:
    from app.entities import resolve_document
    from app.extract import extract_document
    from app.ingest import ingest_pdf
    from app.normalize import normalize_document
    from app.verify import verify_document

    settings = settings or get_settings()
    if not settings.llm_api_key():
        raise SystemExit(
            "--corpus runs Phase-4 extraction, which needs GEMINI_API_KEY. "
            "Use the default synthetic corpus, or --db <path> on an already-processed database."
        )
    pdfs = sorted(Path(corpus_dir).glob("*.pdf"))
    if not pdfs:
        raise SystemExit(f"no *.pdf files in {corpus_dir}")
    db_path = str(settings.database_path)
    for pdf in pdfs:
        doc = ingest_pdf(str(pdf)).document_id
        extract_document(doc)
        verify_document(doc)
        normalize_document(doc)
        resolve_document(doc)
        reason_document(doc, settings=settings)
    return _finish(db_path, settings, sweep=sweep, source=f"corpus {corpus_dir}")


# --------------------------------------------------------------------------- #
# rendering                                                                  #
# --------------------------------------------------------------------------- #
def _print(report: dict) -> None:
    w = sys.stdout.write
    w(f"\n=== evaluation: {report['source']} ===\n")
    w(f"settings: {json.dumps(report['settings'])}\n\n")

    w("PROPERTIES (four required cases)\n")
    for c in report["cases"]:
        mark = "PASS" if c["passed"] else "FAIL"
        req = "must-hold" if c["must_hold"] else "optional "
        w(f"  [{mark}] {req}  {c['property']:<34} {c['detail']}\n")
    w("\nGLOBAL INVARIANTS\n")
    for n, r in report["invariants"].items():
        w(f"  [{'PASS' if r['passed'] else 'FAIL'}] {n:<44} {r['detail']}\n")

    c = report["counts"]
    w(f"\nrelationships: {c['relationships_total']}  by_category={c['by_category']}\n")
    w(f"eligible_facts={c['eligible_facts']}  quarantined={c['quarantined_facts']}  "
      f"failures={c['failures_by_type']}\n")

    if "sweep" in report:
        w("\nCONFIG SWEEP  (property pass per config)\n")
        cols = list(P.CASE_PROPERTIES)
        w("  " + "config".ljust(40) + "  ".join(x[:10].rjust(10) for x in cols)
          + "   rel  unc  q  hold\n")
        for row in report["sweep"]["rows"]:
            cells = "  ".join(("Y" if row["properties"][x] else "·").rjust(10) for x in cols)
            hold = "Y" if (row["must_hold_pass"] and row["invariants_pass"]) else "·"
            w(f"  {row['label']:<40}{cells}  {row['relationships']:>4} "
              f"{row['uncertain']:>4} {row['quarantined']:>2}   {hold}\n")
        w(f"  recommended config: {report['sweep']['recommended']}\n")

    w(f"\nRESULT: {'PASS' if report['all_pass'] else 'FAIL'} "
      f"(must-hold properties {'ok' if report['must_hold_pass'] else 'FAILED'}, "
      f"invariants {'ok' if report['invariants_pass'] else 'FAILED'})\n")


def _write(report: dict, path: str | None) -> Path:
    if path:
        out = Path(path)
    else:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        out = REPORTS_DIR / f"eval-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="evaluation.harness", description=__doc__)
    ap.add_argument("--corpus", help="directory of PDFs; runs the full pipeline (needs a key)")
    ap.add_argument("--db", help="evaluate an existing database (no pipeline run)")
    ap.add_argument("--sweep", action="store_true", help="also run the curated config sweep")
    ap.add_argument("--report", help="write the JSON report to this path")
    ap.add_argument("--no-report", action="store_true", help="do not write a JSON report")
    args = ap.parse_args(argv)

    if args.corpus:
        report = run_corpus(args.corpus, sweep=args.sweep)
    elif args.db:
        report = run_db(args.db, sweep=args.sweep)
    else:
        report = run_synthetic(sweep=args.sweep)

    _print(report)
    if not args.no_report:
        sys.stdout.write(f"\nreport: {_write(report, args.report)}\n")
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
