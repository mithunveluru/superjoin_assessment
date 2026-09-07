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

> **Status: Phases 1–7 of 15 complete; Phase 8 not started.** This repo contains
> the project skeleton, centralised configuration, the full SQLite schema (+ a
> forward-only migration mechanism), database utilities, a health check, a
> **corpus-agnostic PDF ingestion layer**, the **canonical fact + evidence +
> relationship persistence model** (`app/facts.py`), **candidate fact
> extraction** (`app/extract.py` + `app/llm.py` — Claude structured output →
> deterministic validation → facts persisted as `CANDIDATE`), and **deterministic
> evidence verification** (`app/verify.py` — no LLM): each candidate fact's
> evidence is checked against the persisted source text via an
> `exact → normalized_exact → recovered_exact → fuzzy` ladder; a match grounds the
> fact (`GROUNDED`), a genuine mismatch (absent quote, ambiguous match, wrong
> number/currency/unit) **quarantines** it (preserved, never reasoning-eligible).
> Claims are never rewritten — only an evidence *span* is corrected, and only when
> the exact quote occurs once. **Phase 5 verifies evidence; it does not perform
> semantic reasoning.** **Deterministic normalization** (`app/normalize.py` — no
> LLM) then parses each grounded fact's number, currency, unit, and reporting
> period into the comparison columns (`base_value`, `percentage_ratio`,
> `unit_norm`, `reporting_period_*`), resolving fiscal-year labels against a
> convention detected from the document's own text (config default otherwise);
> `*_raw` is never overwritten, an unparseable number leaves the fact `GROUNDED`
> with a `normalization_failed` record, an unresolvable period becomes
> `type='unknown'` rather than a guess, and a success promotes `GROUNDED →
> NORMALIZED`. **Entity resolution** (`app/entities.py`) then links each fact's
> subject surface to a row in a global `entities` table using only
> language-generic config (legal-form suffixes, honorifics, anaphora words,
> rename predicates — never a dataset alias list): exact normalized-key match and
> a conservative `rapidfuzz` gate merge deterministically, "formerly known as"
> facts become `derived_fact` aliases, "the Company" resolves to the document's
> dominant entity, and only genuinely borderline pairs go to a single
> `confirm_entities` LLM call (absent a key they are left unresolved, never
> blind-merged). A `NORMALIZED` fact with verified evidence is then promoted to
> `ELIGIBLE_FOR_REASONING`. No retrieval or
> relationship inference exists yet — those are later phases, see
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

An `ANTHROPIC_API_KEY` is **not** needed for Phases 1–3, 5–6, or for the test suite
(candidate extraction is exercised with a fake LLM client). It **is** needed for
a live candidate-extraction run — `scripts/smoke_extract.py <pdf>`.

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
- **Phase 4** covers: the structured-output extraction contract, deterministic
  candidate validation (fields, controlled vocabularies, chunk-relative offsets,
  numeric value present, in-chunk de-duplication), persistence as non-eligible
  `CANDIDATE` facts with the verbatim `raw_payload` and run metadata, the
  unverified candidate-citation `evidence` row (page-relative offsets, full
  FACT→EVIDENCE→CHUNK→PAGE→DOCUMENT trace), failure isolation (malformed JSON,
  truncation, API errors, client exceptions, per-candidate rejections — each
  recorded, siblings unaffected), and the `extraction_summary` observability.
  Uses a deterministic fake LLM — no API calls.
- **Phase 5** covers: migration 3 (climbs from any prior state, idempotent,
  FK-checked); the verification ladder — `exact` at offset, `normalized_exact`
  (whitespace/punctuation folding only, no value changes), `recovered_exact`
  (unique in-chunk quote → span corrected, `>1` → `ambiguous_quote_match`),
  conservative `fuzzy` (numeric/unit token guard + threshold → `PARTIAL`),
  else quarantine; bounded numeric consistency (`raw_value_text` present,
  `parsed_value` == its digits, percentage coherence, currency/magnitude/unit
  present) → `numeric_mismatch` quarantine that **never rewrites the claim**;
  `CANDIDATE → GROUNDED` on success, `→ QUARANTINED` on failure with the
  `raw_payload` preserved; a GROUNDED VERIFIED fact can then become
  reasoning-eligible while a quarantined one cannot (helper + DB CHECK);
  provenance chain preserved; idempotent re-runs; `verification_summary`
  observability; a real starter-PDF deterministic check. **No LLM, no API key.**
- **Phase 6** covers: the pure parsers against the DATA_MODEL numeric and period
  worked tables exactly (`parse_number` — commas, `(x)`/`−` negatives, magnitude
  map incl. `bps`, `%` → ratio; `parse_currency`; `parse_unit`; `parse_period` —
  instant / quarter / half_year / fiscal_year / calendar_year / range / unknown,
  incl. `FY2025/26`, `2024-25`, `Fiscal 2021`, "N months ended <date>";
  `detect_fy_convention` incl. the unrepresentable-month → `None` case);
  `apply_normalization` filling the columns without ever touching a `*_raw`
  (property test), `GROUNDED → NORMALIZED` on success, `normalization_failed` +
  stays `GROUNDED` on an unparseable number, `type='unknown'` (still promoted) on
  an unresolvable period; `normalize_document` batch summary, FY-convention
  detection + storage + config-default fallback, per-fact failure isolation,
  idempotent re-runs (no duplicate rows/failures), only `GROUNDED`/`NORMALIZED`
  processed; provenance chain preserved; a real starter-PDF deterministic check;
  schema unchanged (`user_version` stays 3). **No LLM, no API key.**
- **Phase 7** covers: `normalize_name` / `guess_type` against generic suffix +
  honorific config; the acceptance cluster (`{name, name + "Limited", "the
  Company", former name}` → one entity, one `derived_fact` alias with
  `source_fact_id`); a distinct person kept separate; a bare name vs
  "<name> Robotics" **not** merged without an LLM (`entity_ambiguous`, surface
  left unresolved) and merged/split on demand by a fake `confirm_entities`;
  anaphora with no dominant entity → ambiguous; promotion to
  `ELIGIBLE_FOR_REASONING` only with a verified evidence chain; only
  `NORMALIZED`/`ELIGIBLE` subjects resolved; idempotent re-runs (identical
  entities/aliases/links, no new failures); `resolution_summary`; a grep asserting
  `app/entities.py` carries no corpus entity strings; a real starter-PDF smoke.
  Uses a fake confirmer — no API calls. Schema unchanged (`user_version` 3).

Unit tests use small synthetic PDFs / synthetic source rows and a fake LLM
client; the provided starter PDFs and any live Anthropic call are for manual
smoke tests only. PDFs are kept locally (git-ignored), not committed.

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
| entity resolution | `FKL_ENTITY_LEGAL_SUFFIXES`, `FKL_ENTITY_PERSON_HONORIFICS`, `FKL_ENTITY_ANAPHORA`, `FKL_ENTITY_RENAME_PREDICATES`, `FKL_ENTITY_BLOCK_FUZZY_THRESHOLD`, `FKL_ENTITY_MERGE_FUZZY_THRESHOLD` |
| retrieval (tuned by the eval harness) | `FKL_RETRIEVAL_TOP_K`, `FKL_RETRIEVAL_CANDIDATE_THRESHOLD`, `FKL_RETRIEVAL_WEIGHT_*` |
| numeric comparison | `FKL_NUMERIC_EQUIVALENCE_TOLERANCE`, `FKL_NUMERIC_CONTRADICTION_THRESHOLD` |
| ingestion | `FKL_MAX_UPLOAD_MB`, `FKL_MAX_PAGES`, `FKL_OCR_MIN_CHARS`, `FKL_CHUNK_TARGET_CHARS`, `FKL_CHUNK_OVERLAP_CHARS` |

## Project layout

```
app/
  config.py     # Settings (pydantic-settings), every tunable
  db.py         # connect / init_db / transaction + forward-only migrations
  schema.sql    # genesis schema (user_version 0)
  migrations/   # NNNN_*.sql forward-only migrations (0001 Phase 2, 0002 Phase 3, 0003 Phase 5)
  main.py       # FastAPI app + /health
  models.py     # pydantic models (grows per phase)
  ingest.py     # Phase 2 — corpus-agnostic PDF ingestion service (ingest_pdf)
  facts.py      # Phase 3 — fact/evidence/relationship persistence + validation
  extract.py    # Phase 4 — candidate fact extraction (extract_document, extraction_summary)
  llm.py        # Phase 4/7 — Anthropic clients (AnthropicExtractor, AnthropicEntityConfirmer)
  verify.py     # Phase 5 — deterministic evidence verification (verify_document, verify_fact)
  normalize.py  # Phase 6 — deterministic numeric/date/unit/period normalization (normalize_document)
  entities.py   # Phase 7 — entity resolution (resolve_document, resolution_summary)
  prompts/      # versioned prompts (extraction_v1.md, entity_confirm_v1.md)
scripts/        # smoke_extract.py — live extraction smoke test (needs ANTHROPIC_API_KEY)
tests/          # config, db, health, ingestion, facts, extraction, verification, normalization, entities (+ conftest, fakes)
docs/           # design artifacts
starter-datasets/   # provided dataset READMEs (the PDFs are kept locally, git-ignored)
```

*(The full README sections required for submission — Video Demo, Approach,
Limitations and Next Steps, Additional Notes — are written in Phase 14.)*
