"""Phase 9 smoke test: run relationship reasoning against the persisted facts in
the configured database and print the observability summary (§24 of the Phase-9
brief).

    FKL_DATABASE_PATH=/tmp/smoke.db python scripts/smoke_reason.py

Needs a database that already holds ELIGIBLE_FOR_REASONING facts (the
extract -> verify -> normalize -> resolve pipeline has run — extraction needs
GEMINI_API_KEY). With an empty database it reports that and exits. Runs
deterministically with NO LLM unless GEMINI_API_KEY is set and --llm is
passed. Not imported by the app or the test suite.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import connect, init_db  # noqa: E402
from app.reason import reason_document, reasoning_summary  # noqa: E402
from app.retrieve import retrieve_candidates  # noqa: E402


def main(argv: list[str]) -> int:
    use_llm = "--llm" in argv
    init_db()
    conn = connect()
    llm = None
    try:
        if use_llm:
            from app.config import get_settings
            from app.llm import RelationshipConfirmer

            if get_settings().llm_api_key():
                llm = RelationshipConfirmer()
            else:
                print("--llm requested but no API key; running deterministically")

        eligible = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE lifecycle_state = 'ELIGIBLE_FOR_REASONING'"
        ).fetchone()[0]
        print(f"eligible facts: {eligible}")
        if eligible < 2:
            print("nothing to reason over — run extract/verify/normalize/resolve first")
            return 0

        doc_ids = [r["id"] for r in conn.execute("SELECT id FROM documents ORDER BY id")]
        total_pairs = 0
        for doc in doc_ids:
            pairs = retrieve_candidates(conn, document_id=doc)
            total_pairs += len(pairs)
            summ = reason_document(doc, llm=llm)
            print(f"\n=== document_id={doc} ===")
            print(f"  candidate_pairs={summ.candidate_pairs} "
                  f"created={summ.relationships_created} existing={summ.relationships_existing}")
            print(f"  deterministic={summ.deterministic_decisions} "
                  f"llm_confirmed={summ.llm_confirmed_decisions} "
                  f"uncertain={summ.uncertain_decisions} overrides={summ.validation_overrides}")
            print(f"  llm_calls={summ.llm_calls} llm_errors={summ.llm_errors} "
                  f"skipped={summ.skipped} errors={summ.errors}")
            print(f"  by_category={summ.by_category}")

        print("\n=== corpus reasoning summary ===")
        print(json.dumps(reasoning_summary(conn), indent=2, default=str))

        print("\n=== example relationships (with evidence refs) ===")
        rows = conn.execute(
            "SELECT r.fact_a_id, r.fact_b_id, r.category, r.context_dimension, r.confidence, "
            "r.llm_used, r.validation_action FROM relationships r "
            "ORDER BY r.confidence DESC LIMIT 5"
        ).fetchall()
        for r in rows:
            a = conn.execute(
                "SELECT f.subject_raw, f.predicate, f.object_raw, e.quote, e.page_index, "
                "f.document_id FROM facts f LEFT JOIN evidence e ON e.fact_id = f.id "
                "WHERE f.id = ?", (r["fact_a_id"],),
            ).fetchone()
            b = conn.execute(
                "SELECT f.subject_raw, f.predicate, f.object_raw, e.quote, e.page_index, "
                "f.document_id FROM facts f LEFT JOIN evidence e ON e.fact_id = f.id "
                "WHERE f.id = ?", (r["fact_b_id"],),
            ).fetchone()
            print(f"\n  {r['category']} (ctx={r['context_dimension']} conf={r['confidence']} "
                  f"llm={bool(r['llm_used'])} action={r['validation_action']})")
            print(f"  A[doc{a['document_id']} p{a['page_index']}] {a['subject_raw']!r} · "
                  f"{a['predicate']!r} · {a['object_raw'][:50]!r}")
            print(f"     evidence: {str(a['quote'])[:90]!r}")
            print(f"  B[doc{b['document_id']} p{b['page_index']}] {b['subject_raw']!r} · "
                  f"{b['predicate']!r} · {b['object_raw'][:50]!r}")
            print(f"     evidence: {str(b['quote'])[:90]!r}")
            print("  NOTE: category is the system's classification of these two extracted facts.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
