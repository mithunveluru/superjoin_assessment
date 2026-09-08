# Fact Knowledge Layer

A system that extracts **grounded facts** from heterogeneous PDFs and determines
how those facts **relate** across documents.

## Problem

Facts that matter — a company's revenue, a headcount, a macro indicator — are
scattered across annual reports, prospectuses, earnings decks and institutional
research, stated in different words, units, scopes and reporting conventions. Two
documents can describe the *same* thing with different numbers because they use a
different basis (consolidated vs standalone), a different period (FY23 vs FY24),
a different modality (a forecast vs an actual) or a different data vintage. A
naive comparison turns every such difference into a false contradiction. This
system pins each fact to its source evidence and normalized context first, then
compares facts through explicit, inspectable signals so that a *contextual*
difference is never reported as a *contradiction*.

## What it does

- **Ingests** a PDF into page-preserving text with exact character offsets.
- **Extracts** candidate numeric and semantic facts (LLM, structured output).
- **Verifies** every fact's verbatim quote against the source page; a fact that
  cannot be re-derived is **quarantined**, never used, always inspectable.
- **Normalizes** numbers, currencies, units and reporting periods into
  comparison columns without ever overwriting the raw text.
- **Resolves** subject surfaces to entities using only language-generic rules
  (no dataset alias list).
- **Retrieves** bounded candidate fact pairs and computes deterministic
  comparison **signals** for each.
- **Reasons** about each pair, classifying it as `CORROBORATES`, `CONTRADICTS`,
  `DIFFERENT_CONTEXT`, `TEMPORAL_EVOLUTION` or `UNCERTAIN`, with a rationale.
- Exposes everything through a **REST API** and a small **web UI**.

## Architecture / pipeline

```
PDF ─► ingest ─► extract ─► verify ─► normalize ─► resolve ─► retrieve ─► reason ─► API / UI
      (Ph 2)    (Ph 4)     (Ph 5)     (Ph 6)       (Ph 7)     (Ph 8)      (Ph 9)    (Ph 11/12)
```

Storage is a single **SQLite** file (WAL, `sqlite3` stdlib — no ORM, no server).
The API is **FastAPI**; the UI is three static files (vanilla JS, no build step).
`app/pipeline.py` runs the stages for one document as a FastAPI `BackgroundTask`,
writing progress to a `runs` row.

### Fact representation

`FACT = CLAIM (subject, predicate, object) + CONTEXT + PROVENANCE + EVIDENCE +
LIFECYCLE + REPRO`. Predicates are free text as written (no controlled
vocabulary — the corpus defines them) plus a lowercased normalized form for
blocking. Numeric facts keep the full representation: `value_raw`,
`numeric_value`, `magnitude` + `magnitude_factor`, `base_value`, `currency`,
`is_percentage` + `percentage_ratio`, `unit_raw` + `unit_norm`. Context
(`reporting_period_*`, `scope` JSON, `qualifiers`, `modality`) is first-class and
queryable, never folded into the claim. See [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md).

### Evidence / provenance model

Every fact from `GROUNDED` onward has exactly one `evidence` row that resolves a
full chain: **FACT → EVIDENCE → CHUNK → PAGE → DOCUMENT**, with the verbatim
`quote`, the `char_start`/`char_end` span into `pages.text`, the printed page
label + the PDF page index, and the `verification_method`
(`exact | normalized_exact | recovered_exact | fuzzy`). Four provenance concepts
are kept separate: the fact's **reporting period**, the document's **document
date**, its **publication date** and its **data vintage**.

### Fact lifecycle

```
CANDIDATE ─► GROUNDED ─► NORMALIZED ─► ELIGIBLE_FOR_REASONING
    │
    └────────────────────────────────► QUARANTINED   (terminal, inspectable)
```

Each stage owns exactly one transition. A fact becomes `ELIGIBLE_FOR_REASONING`
only with a traceable, non-`UNVERIFIED` evidence chain (helper **and** a DB
`CHECK`). Quarantined facts are kept, surfaced in `GET /failures`, and can never
enter a relationship.

### Relationship reasoning

For each candidate pair, `deterministic_verdict(signals)` runs a config-thresholded
rule ladder and settles every case it can:

| Situation | Result |
|---|---|
| same entity/predicate/period, unit-equivalent, `Δ% ≤ FKL_NUMERIC_EQUIVALENCE_TOLERANCE`, sign match | `CORROBORATES` |
| same context, `Δ% > FKL_NUMERIC_CONTRADICTION_THRESHOLD` | `CONTRADICTS` |
| `Δ%` between the two thresholds | `UNCERTAIN` |
| `scope_conflict` / different currency / different unit / period-type / modality | `DIFFERENT_CONTEXT` (dimension named) |
| distinct reporting periods, comparable modality, value changed | `TEMPORAL_EVOLUTION` |
| unresolved entity / unknown-or-missing period / weak predicate / % vs absolute | `UNCERTAIN` |

Only a genuinely semantic residue (predicate synonymy, statement polarity) is
sent to an **optional** LLM confirmer; its proposal is re-checked by
`_validate_llm`, which can override or downgrade it (e.g. an LLM `CONTRADICTS`
on unit-equivalent equal numbers is forced to `CORROBORATES`). **The LLM never
has the last word.** Each relationship persists its `reasoning`, the full
deterministic signal set, `confidence`, `llm_used`, `llm_proposed_category` and
`validation_action`. Persistence is idempotent (canonical `(min, max)` fact id).

### Deterministic vs LLM responsibilities

| Deterministic (always) | LLM (when a key is set) |
|---|---|
| ingestion, chunking, offsets | candidate fact extraction (structured output) |
| evidence verification / grounding / quarantine | borderline entity-cluster confirmation |
| number / currency / unit / period normalization | a relationship-category *proposal* for the semantic residue |
| entity blocking + fuzzy/rename/anaphora merge | |
| candidate retrieval + all comparison signals | |
| **every relationship category decision** (`_validate_llm` has the final say) | |

With no API key the system still produces every deterministic relationship
category; the semantic residue becomes `UNCERTAIN`.

## API

FastAPI, JSON, common error body `{"error": {"code", "message"}}`, list
endpoints paginate with `?limit=` (default 50, max 200) `?offset=`. `uvicorn
app.main:app` serves the UI at `/`, OpenAPI docs at `/docs`.

| Endpoint | Purpose |
|---|---|
| `POST /documents` | multipart PDF upload → ingest (SHA-256 dedup) |
| `GET /documents` · `GET /documents/{id}` | list / detail (+ counts, latest run) |
| `POST /documents/{id}/process` | run the pipeline as a background task (idempotent — a completed run is returned as-is) |
| `GET /documents/{id}/status` | poll; a failed stage surfaces its error at HTTP 200 |
| `GET /facts` · `GET /facts/{id}` | filter matrix (`type`, `lifecycle_state`, `evidence_status`, `modality`, `reasoning_eligible`, `q` FTS, …) + detail with quote, context window, relationships |
| `GET /relationships` · `GET /relationships/{id}` | category / `context_dimension` / `validation_action` filters; detail with both full facts, the signals table, proposed-vs-final category |
| `GET /entities` · `GET /entities/{id}` | list / detail (aliases, sample facts) |
| `GET /failures` | the quarantine / failure surface, joined to its referents |
| `GET /health` | status, schema table count, `api_key_present` |

## UI

Served at `/` — one `index.html` + `style.css` + `app.js` (~400 lines vanilla,
no framework, no build). Five views:

- **Documents** — upload, then **Process** with 2-second status polling.
- **Facts** — the filter matrix + pagination; a row expands to the verbatim
  quote, the numeric representation, reporting period, scope, modality, the
  verification detail and the ±200-char context window.
- **Relationships** — category tabs; each card shows Fact A | Fact B with quotes;
  expanding shows the deterministic-signals table, the LLM-proposed-vs-final
  category with the validation note, and the reasoning.
- **Failures** — every failure row with its reason and the linked quarantined
  fact or relationship.
- **Entities** — canonical label, aliases (with `source_fact_id` for renames),
  sample facts.

No auth — local prototype.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                  # optional — every setting has a default
```

Python **3.11+**. No database server, no Docker, no Node.

### Environment variables

All app settings use the `FKL_` prefix and have safe defaults in
[`.env.example`](.env.example). The only real secret is the LLM API key, read
from the variable named by `FKL_LLM_API_KEY_ENV` (default `ANTHROPIC_API_KEY`),
so it never carries a project-specific name and is never committed (`.env` is
git-ignored). Groups: `FKL_LLM_*`, `FKL_DATABASE_PATH` / `FKL_UPLOADS_DIR`,
`FKL_ENTITY_*` (two fuzzy thresholds), `FKL_RETRIEVAL_*` (top-k, thresholds,
weights), `FKL_NUMERIC_EQUIVALENCE_TOLERANCE` / `FKL_NUMERIC_CONTRADICTION_THRESHOLD`,
`FKL_MAX_*` ingestion limits, `FKL_VERIFY_*`, chunking knobs. `FKL_EMBEDDING_*`
are reserved (embedding retrieval deferred).

## Run

```bash
uvicorn app.main:app                  # http://localhost:8000/  ·  docs at /docs
curl -s localhost:8000/health | python -m json.tool
```

On first start the app creates `data/knowledge.db` from `app/schema.sql` and
applies the forward-only migrations in `app/db.py`.

### Process a PDF (needs `ANTHROPIC_API_KEY` for the extract stage)

```bash
export ANTHROPIC_API_KEY=...
DOC=$(curl -s -F file=@starter-datasets/delhivery/03-delhivery-q4-fy24-earnings-presentation.pdf \
      localhost:8000/documents | python -c 'import sys,json;print(json.load(sys.stdin)["id"])')
curl -s -X POST localhost:8000/documents/$DOC/process        # 202, runs in the background
curl -s localhost:8000/documents/$DOC/status                 # poll until done | failed
curl -s "localhost:8000/facts?document_id=$DOC&lifecycle_state=ELIGIBLE_FOR_REASONING"
curl -s "localhost:8000/relationships?category=CORROBORATES"
curl -s localhost:8000/failures
```

Without a key, `POST /process` still returns `202`; the background run then fails
at the `extract` stage and `GET /status` reports `status: "failed"` with the
error (HTTP 200). Ingestion, verification, normalization, entity resolution,
retrieval and reasoning are all key-free — only Phase-4 extraction calls the LLM
(one call per chunk; the 3 Delhivery PDFs are ~1006 chunks, roughly $1–3 at
Sonnet-5 rates).

## Test & lint

```bash
pytest                # 391 tests, ~25 s, no network, no API key
ruff check .
```

Tests use small synthetic PDFs and a fake LLM client; `pytest.ini_options`
carries an `llm` marker for any future live test (none exist today). Coverage per
phase is summarized in [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md).

## Demo

```bash
python scripts/seed_demo.py           # deterministic, offline, no key
uvicorn app.main:app                  # open http://localhost:8000/
```

`seed_demo.py` builds a **synthetic** two-document corpus (one resolved entity,
`Acme`) with relationships across **all five categories** plus one quarantined
extraction — enough to walk every UI view. Nothing in it is corpus-specific.
Re-seed with `--force`. A 3-minute walkthrough is in [`docs/DEMO.md`](docs/DEMO.md).

### Assignment demo scenarios

The four required scenarios were exercised end-to-end through the real pipeline
on a **clearly-labelled synthetic fixture** (`tests/test_pipeline_e2e.py`,
`scripts/seed_demo.py`), because a live extraction run on the real corpus was not
performed (see below).

(values as produced by `scripts/seed_demo.py`; entity `Acme`, all synthetic)

| Scenario | Where it shows | Signal that drives it |
|---|---|---|
| **Corroboration** | `revenue from operations · 81,415.38 million` (doc A) ≡ `revenue from services · 8,142 Cr` (doc B), FY24 | `unit_equivalent` after crore↔million normalization, `base_value_delta_pct ≈ 6e-5` |
| **Contradiction** | `profit after tax · 5,000.00 million` vs `8,000.00 million`, same entity/period FY24, no scope conflict | `base_value_delta_pct ≈ 0.375` > `FKL_NUMERIC_CONTRADICTION_THRESHOLD` |
| **Contextual reconciliation** | `revenue from operations` `81,415.38 million` (consolidated) vs `74,540.82 million` (standalone), FY24 | `scope_conflict` non-empty → `DIFFERENT_CONTEXT`, **not** `CONTRADICTS` |
| **Temporal evolution** | `revenue from operations` `81,415.38 million` FY24 vs `60,000.00 million` FY23, adjacent periods, both HISTORICAL | `period_relation = adjacent` → `TEMPORAL_EVOLUTION`, **not** `CONTRADICTS` |
| **Extraction failure** | `permanent employees · 99,999` — a fabricated quote fails Phase-5 verification | `QUARANTINED` + `failures(grounding_failed, "quote_not_found …")`; absent from every relationship; shown in the Failures view |

## Validation status — real corpus vs synthetic

- **Real corpus:** all 3 Delhivery PDFs were **ingested** for real — 3 documents,
  227 pages, 1006 chunks; the offset invariant
  `pages.text[char_offset:char_end] == chunks.text` held with **0 violations**;
  6 low-text pages were flagged as `ocr_page` failures.
- **Synthetic pipeline:** the full `ingest → extract → verify → normalize →
  resolve → retrieve → reason` pipeline was validated deterministically on a
  synthetic multi-page PDF with a substring-matching fake extractor
  (`tests/test_pipeline_e2e.py`), so Phase 5–9 code runs for real. The API and UI
  were validated against a live `uvicorn` server (a real Delhivery PDF uploaded;
  every read endpoint, every 404, the multipart upload, the failed-run path — no
  500s, no browser console errors, all five UI views).
- **Not performed:** a **live LLM extraction run on the real Delhivery corpus**,
  because `ANTHROPIC_API_KEY` was unavailable during development. **No
  corpus-derived contradiction or corroboration is claimed** — the four scenarios
  above are synthetic validation fixtures, labelled as such. Whether the Delhivery
  documents contain a *naturally occurring* contradiction is an open question that
  needs a keyed run.

## Known limitations

- **No live extraction on the real corpus** (above). Every other stage is
  deterministic and was run for real.
- **`scope` is a free string** from extraction, stored as `{"raw": <string>}`, so
  a `DIFFERENT_CONTEXT` `context_dimension` reads `scope:raw` rather than naming
  the dimension (`basis` / `segment` / `geography`). It still distinguishes
  standalone from consolidated and picks context over contradiction correctly.
- **Lexical retrieval only.** Predicate similarity is `rapidfuzz` token overlap;
  the embedding rung was designed and deferred. Cross-entity synonyms with no
  shared tokens are missed *unless* the two facts share a resolved entity.
- **Thresholds are Phase-0 starting values.** The evaluation harness has a config
  sweep with teeth, but real tuning needs both corpora processed with a key.
- **Confidence is a documented heuristic**, not a calibrated probability.
- **Single process.** `BackgroundTask` processing, no queue; fine for one
  reviewer, not for concurrent load. No auth.

## Future improvements

- Live extraction + threshold tuning on both real corpora (harness is ready).
- Structured scope extraction (`{"basis": …, "segment": …}`) so `context_dimension`
  names the dimension.
- Local ONNX embedding retrieval (`app/embed.py`) to recall reworded predicates
  across entities.
- Layout/table-aware extraction for wide statistical appendices.
- A worker/queue if processing needs to scale past one document at a time.

## Reproducibility notes

- Deterministic components produce byte-identical output on re-run over an
  unchanged database (retrieval pairs, reasoning verdicts, signal values).
- `python scripts/seed_demo.py` rebuilds the demo state from scratch, no network.
- Every LLM-derived row records `model_name`, `prompt_version`, `temperature` and
  its `run_id`; prompts are versioned under `app/prompts/`.
- LLM-assisted relationship steps are **not** claimed to be deterministic; the
  `relationships.llm_used` column records which rows involved a model.

## LLM / API-key requirements

- **Not required** for: the full test suite, ruff, ingestion, verification,
  normalization, entity resolution, retrieval, all deterministic relationship
  categories, the API, the UI, and `scripts/seed_demo.py`.
- **Required** for: Phase-4 candidate extraction (`POST /process` on a real PDF),
  borderline entity-cluster confirmation, and the optional semantic
  relationship-proposal step.
- Provider: Anthropic, model `claude-sonnet-5` (`FKL_LLM_MODEL`). The key is read
  at call time from `os.environ[FKL_LLM_API_KEY_ENV]`; it is never logged, never
  written to the database, and `.env` is git-ignored.

## Design docs

| Doc | What |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | pipeline, the AI/deterministic boundary, failure handling |
| [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) | schema — fact lifecycle, evidence, numeric representation, provenance, modality |
| [`docs/API_DESIGN.md`](docs/API_DESIGN.md) | endpoint contract |
| [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) | the 15 phases, their gates, and what each phase's tests cover |
| [`docs/EVALUATION_PLAN.md`](docs/EVALUATION_PLAN.md) | test strategy + the evaluation harness |
| [`docs/DATASET_ANALYSIS.md`](docs/DATASET_ANALYSIS.md) | starter-dataset observations (design only — never in code) |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | ADR-style decisions and revisions |
| [`docs/RISKS.md`](docs/RISKS.md) | deliberate design positions and known risks |
| [`docs/DEMO.md`](docs/DEMO.md) | the ≤ 3-minute demo walkthrough |

## Project layout

```
app/
  config.py     Settings (pydantic-settings), every tunable
  db.py         connect / init_db / transaction + forward-only migrations
  schema.sql    genesis schema (user_version 0)
  migrations/   0001 Phase 2 · 0002 Phase 3 · 0003 Phase 5
  models.py     pydantic request/response models
  ingest.py     Phase 2 — PDF → pages → chunks (offset-preserving)
  facts.py      Phase 3 — fact / evidence / relationship persistence + invariants
  extract.py    Phase 4 — candidate extraction (structured output → validation)
  llm.py        Phase 4/7/9 — Anthropic clients (Extractor, EntityConfirmer, RelationshipConfirmer)
  verify.py     Phase 5 — deterministic evidence verification + quarantine
  normalize.py  Phase 6 — number / currency / unit / period normalization
  entities.py   Phase 7 — entity resolution (generic rules + borderline LLM confirm)
  retrieve.py   Phase 8 — bounded candidate-pair retrieval
  signals.py    Phase 8 — deterministic comparison signals
  reason.py     Phase 9 — relationship reasoning (deterministic verdict; LLM proposes)
  pipeline.py   Phase 11 — full-document orchestration (BackgroundTask)
  queries.py    Phase 11 — read/shaping layer for the API
  main.py       Phase 1 /health + Phase 11 REST API + Phase 12 UI mount
  static/       Phase 12 — index.html, style.css, app.js (no build step)
  prompts/      versioned prompts
evaluation/     Phase 10 — harness.py, properties.py, corpora/synthetic.py, cases/**
scripts/        smoke_*.py (offline slices) · seed_demo.py (reproducible demo DB)
tests/          19 test modules + conftest + fakes
docs/           design artifacts
starter-datasets/  provided dataset READMEs (the PDFs are kept locally, git-ignored)
```
