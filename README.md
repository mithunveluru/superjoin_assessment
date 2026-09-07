# Fact Knowledge Layer

**Problem.** Facts that matter are scattered across heterogeneous PDFs — annual
reports, prospectuses, earnings decks, institutional research — stated in
different words, units, and reporting conventions. This system extracts those
facts and determines how they relate across documents.

**Core idea.** A fact does not participate in reasoning until it is tied to
**source evidence** (a verbatim quote on a specific page), a **normalized
context** (period, unit, scale, currency, scope), and its **provenance**
(document date, publication date, data vintage). Ungrounded claims are kept and
surfaced, never used.

**Architecture.**
`PDF → extraction → evidence grounding → normalization → entity resolution →
candidate retrieval → relationship reasoning → API / UI`

**Design principle.** LLMs handle **semantic interpretation** (what a claim
says, whether two predicates mean the same thing, a proposed relationship
category). Deterministic code handles **verification, normalization, and
consistency constraints** (number/date/unit parsing, evidence re-derivation,
comparison signals) and has the final say on every relationship.

**Failure handling.** Facts that cannot be grounded, normalized, or confidently
placed are moved to a quarantine/failure state and remain inspectable through the
failure surface — the system prefers "unsupported / uncertain" over a confident
guess.

Relationships are classified as `CORROBORATES`, `CONTRADICTS`,
`DIFFERENT_CONTEXT`, `TEMPORAL_EVOLUTION`, or `UNCERTAIN`.

> **Status: Phases 1–3 of 15 complete; Phase 4 not started.** This repo contains
> the project skeleton, centralised configuration, the full SQLite schema (+ a
> forward-only migration mechanism), database utilities, a health check, a
> **corpus-agnostic PDF ingestion layer** (PDF → document → pages →
> page-preserving text → deterministic chunks, with character-offset
> traceability), and the **canonical fact + evidence + relationship persistence
> model** (`app/facts.py`) — one fact model for numeric/semantic/temporal/
> categorical facts, an explicit `RAW→…→ELIGIBLE_FOR_REASONING` lifecycle, and
> the enforced invariant that a fact reaches the reasoning layer only with a
> traceable FACT→EVIDENCE→(CHUNK→)PAGE→DOCUMENT chain. No LLM, fact-extraction,
> normalization, entity-resolution, retrieval, or relationship-*inference* code
> exists yet — those are later phases, see
> [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md). Deliberate design
> positions and known risks are in [`docs/RISKS.md`](docs/RISKS.md).

## Design docs

| Doc | What |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | pipeline, AI/deterministic boundary, failure handling |
| [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) | the schema this phase implements — fact lifecycle, evidence verification, numeric representation, provenance, modality |
| [`docs/API_DESIGN.md`](docs/API_DESIGN.md) | endpoint contract (Phase 11) |
| [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) | the 15 phases and their gates |
| [`docs/EVALUATION_PLAN.md`](docs/EVALUATION_PLAN.md) | tests + evaluation harness (Phase 10) |
| [`docs/DATASET_ANALYSIS.md`](docs/DATASET_ANALYSIS.md) | starter-dataset observations (design only, never code) |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | ADR-style decisions and their revisions |
| [`docs/RISKS.md`](docs/RISKS.md) | deliberate design considerations & known risks |

## Prerequisites

- Python 3.11+
- No database server, no Docker, no Node. SQLite ships with Python.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                  # optional; every setting has a default
```

An `ANTHROPIC_API_KEY` is **not** needed for Phases 1–2 (nothing calls the LLM yet).

## Run

```bash
uvicorn app.main:app --reload
```

On startup the app creates `data/knowledge.db` from `app/schema.sql` (the genesis
schema) and applies any pending migrations from `app/db.py`. Check it:

```bash
curl -s localhost:8000/health | python -m json.tool
```

```json
{
  "status": "ok",
  "version": "0.1.0",
  "database": { "path": ".../data/knowledge.db", "ok": true, "tables": 12 },
  "llm": { "provider": "anthropic", "model": "claude-sonnet-5", "api_key_present": false }
}
```

## Test

```bash
pytest
```

- **Phase 1** covers: configuration loading + env overrides, schema creation and
  idempotency, `PRAGMA foreign_keys` / WAL enforcement, foreign-key and `CHECK`
  constraints (including the fact-lifecycle invariants), a document round-trip,
  transaction rollback, FTS query, and the health endpoint.
- **Phase 2** covers: migration application, document creation + metadata,
  SHA-256 dedup / idempotent retry, page identity (`page_index`) and verbatim
  per-page text, deterministic page-aware chunking with the offset invariant
  `pages.text[char_offset:char_end] == chunks.text`, chunks never crossing a
  page, optional printed-label detection, `LOW_TEXT` / `EMPTY` /
  `EXTRACTION_ERROR` page detection, expected-error handling, and safe file
  handling (unsafe filenames cannot escape the uploads directory).
- **Phase 3** covers: migration 2 (schema climbs from any prior state,
  idempotent, FK-checked), persistence of all four fact types, raw + normalized
  numeric representation and `raw_payload` round-trips, the FACT→EVIDENCE→
  (CHUNK→)PAGE→DOCUMENT chain validation (bad page/chunk links and out-of-range
  or unpaired offsets rejected), the lifecycle state machine, the
  reasoning-eligibility / evidence invariant (an UNVERIFIED or evidence-less
  fact cannot become eligible — helper *and* DB CHECK), and relationship storage
  (five categories, canonical pair order, self-relationship rejected, duplicates
  a deterministic no-op).

Unit tests use small synthetic PDFs / synthetic source rows; the provided starter
PDFs are used only for manual smoke tests and are kept locally (git-ignored), not
committed.

## Configuration

All settings use the `FKL_` env prefix (see [`.env.example`](.env.example)); the
LLM API key is read from the external variable named by `FKL_LLM_API_KEY_ENV`
(default `ANTHROPIC_API_KEY`) so real secrets never carry a project-specific
name. Key groups:

| Group | Examples |
|---|---|
| storage | `FKL_DATABASE_PATH`, `FKL_UPLOADS_DIR` |
| LLM | `FKL_LLM_MODEL`, `FKL_LLM_TEMPERATURE`, `ANTHROPIC_API_KEY` |
| embeddings | `FKL_EMBEDDING_MODEL`, `FKL_EMBEDDING_DIM` |
| retrieval (tuned by the eval harness) | `FKL_RETRIEVAL_TOP_K`, `FKL_RETRIEVAL_CANDIDATE_THRESHOLD`, `FKL_RETRIEVAL_WEIGHT_*` |
| numeric comparison | `FKL_NUMERIC_EQUIVALENCE_TOLERANCE`, `FKL_NUMERIC_CONTRADICTION_THRESHOLD` |
| ingestion | `FKL_MAX_UPLOAD_MB`, `FKL_MAX_PAGES`, `FKL_OCR_MIN_CHARS`, `FKL_CHUNK_TARGET_CHARS`, `FKL_CHUNK_OVERLAP_CHARS` |

## Project layout

```
app/
  config.py     # Settings (pydantic-settings), every tunable
  db.py         # connect / init_db / transaction + forward-only migrations
  schema.sql    # genesis schema (user_version 0)
  migrations/   # NNNN_*.sql forward-only migrations (0001 Phase 2, 0002 Phase 3)
  main.py       # FastAPI app + /health
  models.py     # pydantic models (grows per phase)
  ingest.py     # Phase 2 — corpus-agnostic PDF ingestion service (ingest_pdf)
  facts.py      # Phase 3 — fact/evidence/relationship persistence + validation
tests/          # config, db/schema, health, ingestion, facts (+ conftest factories)
docs/           # design artifacts
starter-datasets/   # provided dataset READMEs (the PDFs are kept locally, git-ignored)
```

*(The full README sections required for submission — Video Demo, Approach,
Limitations and Next Steps, Additional Notes — are written in Phase 14.)*
