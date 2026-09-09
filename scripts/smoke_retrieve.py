"""Phase 8 smoke test: run candidate retrieval + deterministic signals against
the persisted facts in the configured database and print the observability
summary (§16 of the Phase-8 brief).

    FKL_DATABASE_PATH=/tmp/smoke.db python scripts/smoke_retrieve.py

Needs a database that already holds ELIGIBLE_FOR_REASONING facts — i.e. the
extract -> verify -> normalize -> resolve pipeline has run (which needs
GEMINI_API_KEY for extraction). With an empty database it reports that and
exits cleanly. Not imported by the app or the test suite.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import connect, init_db  # noqa: E402
from app.retrieve import retrieval_summary, retrieve_candidates  # noqa: E402


def main() -> int:
    init_db()
    conn = connect()
    try:
        eligible = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE lifecycle_state = 'ELIGIBLE_FOR_REASONING'"
        ).fetchone()[0]
        print(f"eligible facts: {eligible}")
        if eligible < 2:
            print("nothing to retrieve — run the extract/verify/normalize/resolve pipeline first")
            return 0

        pairs = retrieve_candidates(conn)
        summary = retrieval_summary(conn, pairs)
        print("\n=== retrieval summary ===")
        print(json.dumps(summary, indent=2, default=str))

        per_fact: dict[int, int] = {}
        for p in pairs:
            per_fact[p.fact_a_id] = per_fact.get(p.fact_a_id, 0) + 1
            per_fact[p.fact_b_id] = per_fact.get(p.fact_b_id, 0) + 1
        if per_fact:
            counts = sorted(per_fact.values())
            print(f"\ncandidates per fact: min={counts[0]} "
                  f"median={counts[len(counts) // 2]} max={counts[-1]}")

        print("\n=== top 5 candidate pairs by retrieval_score ===")
        for p in sorted(pairs, key=lambda x: -x.retrieval_score)[:5]:
            a = conn.execute(
                "SELECT subject_raw, predicate, object_raw, document_id FROM facts WHERE id = ?",
                (p.fact_a_id,),
            ).fetchone()
            b = conn.execute(
                "SELECT subject_raw, predicate, object_raw, document_id FROM facts WHERE id = ?",
                (p.fact_b_id,),
            ).fetchone()
            s = p.signals
            print(f"\n  score={p.retrieval_score} methods={p.retrieval_methods}")
            print(f"  A[{a['document_id']}] {a['subject_raw']!r} · {a['predicate']!r} · "
                  f"{a['object_raw'][:50]!r}")
            print(f"  B[{b['document_id']}] {b['subject_raw']!r} · {b['predicate']!r} · "
                  f"{b['object_raw'][:50]!r}")
            print(f"  signals: entity={s.entity_relation} predicate_sim={s.predicate_similarity} "
                  f"period={s.period_relation} scope={s.scope_relation} "
                  f"unit_equiv={s.unit_equivalent} delta_pct={s.base_value_delta_pct} "
                  f"modality={s.modality_a}/{s.modality_b}")
            print("  NOTE: candidate for downstream relationship reasoning — "
                  "not a corroboration, not a contradiction.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
