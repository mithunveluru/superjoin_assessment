"""Seed a reproducible local demo database — no LLM, no network.

    python scripts/seed_demo.py                  # -> FKL_DATABASE_PATH (default data/knowledge.db)
    FKL_DATABASE_PATH=/tmp/demo.db python scripts/seed_demo.py
    python scripts/seed_demo.py --force          # overwrite a non-empty DB

Builds the Phase-10 synthetic corpus (evaluation/corpora/synthetic.py): two
documents, one resolved entity ("Acme"), and facts/relationships that exercise
all five relationship categories plus one quarantined extraction. Nothing here is
corpus-specific — it is a deterministic fixture for eyeballing the UI/API without
an ANTHROPIC_API_KEY.

Then:

    uvicorn app.main:app
    open http://localhost:8000/

Not imported by the app or the test suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.config import get_settings  # noqa: E402
from evaluation.corpora.synthetic import seed_corpus  # noqa: E402


def main(argv: list[str]) -> int:
    force = "--force" in argv
    path = str(get_settings().database_path)
    p = Path(path)

    if p.exists():
        db.init_db(path)
        conn = db.connect(path)
        try:
            n = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        finally:
            conn.close()
        if n and not force:
            print(f"{path} already has {n} document(s). Re-run with --force to overwrite, "
                  f"or set FKL_DATABASE_PATH to a fresh file.")
            return 1
        for f in p.parent.glob(p.name + "*"):
            f.unlink()

    summary = seed_corpus(path)
    conn = db.connect(path)
    try:
        facts = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        by_cat = {r["category"]: r["n"] for r in conn.execute(
            "SELECT category, COUNT(*) AS n FROM relationships GROUP BY category")}
        fails = {r["failure_type"]: r["n"] for r in conn.execute(
            "SELECT failure_type, COUNT(*) AS n FROM failures GROUP BY failure_type")}
    finally:
        conn.close()

    print(f"seeded {path}")
    print(f"  documents={2}  entity=#{summary['entity']}  facts={facts}")
    print(f"  relationships by category = {by_cat}")
    print(f"  failures = {fails}")
    print("\nnext:  uvicorn app.main:app   then open http://localhost:8000/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
