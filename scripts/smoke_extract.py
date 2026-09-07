"""Phase 4 smoke test: ingest a PDF, run candidate extraction, print the
observability summary. Needs the starter PDFs locally (not committed) and, for a
live run, ANTHROPIC_API_KEY set.

    python scripts/smoke_extract.py starter-datasets/delhivery/03-*.pdf
    FKL_DATABASE_PATH=/tmp/smoke.db python scripts/smoke_extract.py <pdf> [<pdf> ...]

Not imported by the app or the test suite.
"""

from __future__ import annotations

import sys

from app.db import connect, init_db
from app.extract import extract_document, extraction_summary
from app.facts import evidence_chain
from app.ingest import ingest_pdf


def main(pdf_paths: list[str]) -> int:
    init_db()
    conn = connect()
    try:
        for path in pdf_paths:
            ing = ingest_pdf(path)
            res = extract_document(ing.document_id)  # real AnthropicExtractor
            print(f"\n=== {path} (document_id={ing.document_id}) ===")
            print(f"  run={res.run_id} status={res.status} cost≈${res.estimated_cost_usd}")
            print(f"  chunks={res.chunks_processed} generated={res.candidates_generated} "
                  f"persisted={res.candidates_persisted} rejected={res.candidates_rejected} "
                  f"errors={res.extraction_errors}")
            print(f"  by_type={res.facts_by_type}")
            fids = [r["id"] for r in conn.execute(
                "SELECT id FROM facts WHERE document_id = ?", (ing.document_id,))]
            broken = sum(1 for f in fids if evidence_chain(conn, f) is None)
            print(f"  provenance: {len(fids)} facts, {broken} with a broken chain")
            for r in conn.execute(
                "SELECT fact_type, predicate, substr(object_raw,1,60) o, "
                "reporting_period_raw p, modality m "
                "FROM facts WHERE document_id = ? LIMIT 8", (ing.document_id,)
            ):
                print(f"    [{r['fact_type']:>11}] {r['predicate']!r}: {r['o']!r} "
                      f"period={r['p']!r} modality={r['m']}")
        print("\n=== overall ===")
        print(extraction_summary(conn))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1:]))
