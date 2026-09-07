# Implementation Plan (revised)

Phased. **Do not start a phase until the previous phase's gate passes.** Each
phase ends with runnable verification. Tests are `pytest`, plain functions, using
small **synthetic** PDF fixtures — no fixture frameworks, no mocking of our own
code. (The provided starter PDFs are used for manual smoke tests only and are not
committed.) Assertions are **structural properties**, never hard‑coded answers
(see EVALUATION_PLAN).

Revised phase order (per review): evaluation harness now lands **before** the API
so retrieval/threshold tuning is evidence‑driven.

**Status:** Phase 0 ✅ · Phase 1 ✅ · Phase 2 ✅ · Phase 3 not started.

| Phase | Title |
|---|---|
| 0 | Architecture / design — ✅ |
| 1 | Foundation + configuration + SQLite schema + tests — ✅ |
| 2 | PDF ingestion + page‑preserving extraction — ✅ |
| 3 | Fact / evidence model + persistence |
| 4 | Candidate fact extraction |
| 5 | Evidence verification + quarantine |
| 6 | Context + numeric / date / unit normalization |
| 7 | Entity resolution |
| 8 | Candidate retrieval + deterministic signals |
| 9 | Relationship reasoning + explanations |
| 10 | Evaluation harness |
| 11 | API |
| 12 | UI |
| 13 | Full dataset evaluation |
| 14 | Hardening + README + demo |

## Stack (locked)

Python 3.11+, FastAPI + Uvicorn, SQLite (`sqlite3` stdlib), **PyMuPDF**
(imported as `pymupdf`) for PDF, `fastembed` (local ONNX embeddings), `rapidfuzz`, `anthropic`
SDK (`claude-sonnet-5`, `temperature=0`), `pytesseract`+`pdf2image` (OCR fallback
only), `numpy`, `pydantic` + `pydantic-settings`. Dev: `pytest`, `httpx`, `ruff`.
Dependencies are added **in the phase that first imports them**, not up front.

## Repo layout (target)

```
app/
  __init__.py
  config.py        # Settings (pydantic-settings), all tunables, env-backed
  db.py            # connect / init_db / transaction  (no ORM, no repo layer)
  schema.sql       # the whole schema, one readable file
  main.py          # FastAPI app + /health  (routes added Phase 11)
  models.py        # pydantic response/request models (grows per phase)
  ingest.py        # Phase 2
  extract.py       # Phase 4
  ground.py        # Phase 5
  normalize.py     # Phase 6
  entities.py      # Phase 7
  retrieve.py      # Phase 8
  signals.py       # Phase 8  (deterministic comparison)
  reason.py        # Phase 9  (LLM proposes; signals.validate() decides)
  llm.py           # Phase 4+ anthropic wrapper: extract_facts / confirm_entities / classify_relationship
  pipeline.py      # Phase 11 orchestration
static/            # Phase 12
evaluation/
  cases/           # Phase 10: corroboration/ contradiction/ contextual/ failure/
  harness.py       # Phase 10
tests/
  conftest.py      # synthetic-PDF factory + isolated db/uploads fixtures
docs/
.env.example  .gitignore  requirements.txt  pyproject.toml  README.md
```

---

## PHASE 0 — Architecture & design ✅

**Output:** `docs/*.md` (7 files), revised per review.
**Gate:** reviewer approval. *(granted — proceeding to Phase 1)*

---

## PHASE 1 — Foundation, configuration, SQLite schema, tests  ✅ COMPLETE

**Objective:** a clean, boots‑from‑scratch FastAPI app with centralized config,
the full DATA_MODEL schema, robust SQLite handling, a health endpoint, and
foundational tests. **No pipeline logic.**

**Files:**
- `app/__init__.py`, `app/config.py`, `app/db.py`, `app/schema.sql`,
  `app/main.py`, `app/models.py` (health models only)
- `tests/__init__.py`, `tests/conftest.py`, `tests/test_config.py`,
  `tests/test_db.py`, `tests/test_health.py`
- `requirements.txt`, `.env.example`, `.gitignore`, `README.md`

**Tasks:**
1. `config.py` — `Settings(BaseSettings)`, `env_prefix="FKL_"`, `.env` support,
   `@lru_cache get_settings()`. Fields: database path, uploads dir; LLM
   provider/model/api‑key‑env‑name/temperature/max‑tokens/prompt‑version;
   embedding provider/model/dim; retrieval top‑k + candidate/predicate/
   relationship thresholds + the three retrieval weights; numeric equivalence &
   contradiction tolerances; ingestion limits (upload MB, pages, chunks, llm
   calls, ocr min chars, fy convention default); app host/port/log level. Helper
   `llm_api_key()` reads `os.environ[llm_api_key_env]` (the real secret is
   **not** `FKL_`‑prefixed).
2. `schema.sql` — every table, CHECK, index, and the `facts_fts` virtual table
   from DATA_MODEL, all `CREATE ... IF NOT EXISTS`.
3. `db.py` — `connect(path=None)` (mkdir parent; `PRAGMA foreign_keys=ON`,
   `journal_mode=WAL`, `busy_timeout`; `row_factory=Row`); `init_db(path=None)`
   (`executescript(schema)`, idempotent); `@contextmanager transaction(conn)`
   (commit / rollback+reraise). No repository/DAO abstraction.
4. `main.py` — `FastAPI(lifespan=…)` calling `init_db()` on startup;
   `GET /health` → `{status, version, database:{path, ok, tables}, llm:{provider,
   model, api_key_present}}`.
5. `requirements.txt` — Phase‑1 deps only: fastapi, uvicorn[standard], pydantic,
   pydantic‑settings, python‑dotenv, httpx, pytest, ruff.
6. `.env.example` — every `FKL_*` with its default + `ANTHROPIC_API_KEY=` blank.
7. `.gitignore` — `.env`, `data/`, `uploads/`, `__pycache__/`, `*.db*`,
   `.pytest_cache/`, `.ruff_cache/`, `.venv/`.
8. `README.md` — Phase‑1 scope note, prerequisites, install, run, test,
   config table, pointer to `docs/`.

**Dependencies:** none.

**Acceptance criteria:**
- `pip install -r requirements.txt` then `uvicorn app.main:app` boots with no
  error and creates `data/knowledge.db`.
- `GET /health` → 200, `status="ok"`, `database.ok=true`, `database.tables` ==
  the count in `schema.sql` (all `CHECK`ed tables present).
- `init_db()` is idempotent (second call: no error, no duplicate objects).
- A connection from `connect()` reports `PRAGMA foreign_keys` = 1 and (file DB)
  `PRAGMA journal_mode` = `wal`.
- FK violation and every documented `CHECK` (lifecycle vocab, relationship pair
  order, the two cross‑column fact invariants) raise `sqlite3.IntegrityError`.
- Config loads defaults; a `FKL_*` env var overrides; `llm_api_key()` reflects
  `ANTHROPIC_API_KEY`.
- No secret committed; `.env` git‑ignored.

**Tests:**
- `test_config`: defaults (`llm_model=="claude-sonnet-5"`,
  `llm_temperature==0.0`); `monkeypatch.setenv("FKL_RETRIEVAL_TOP_K","3")` +
  `get_settings.cache_clear()` ⇒ `retrieval_top_k==3`; `ANTHROPIC_API_KEY`
  set/unset ⇒ `llm_api_key()` value/None.
- `test_db`: expected table set == `sqlite_master`; `init_db()` twice OK;
  `foreign_keys` & `journal_mode` pragmas; FK violation raises; bad
  `lifecycle_state` raises; `relationships` with `fact_a_id>=fact_b_id` raises;
  fact `QUARANTINED` + `reasoning_eligible=1` raises; fact
  `reasoning_eligible=1` + `evidence_status='UNVERIFIED'` raises; insert+read a
  `documents` row round‑trips; `transaction()` rolls back on exception.
- `test_health`: `TestClient` (context‑managed so lifespan runs) → 200; body
  shape; `database.tables >= 13`.

**Gate:** app boots, schema matches DATA_MODEL incl. all CHECKs, all Phase‑1
tests pass. **Stop. Await explicit instruction for Phase 2.**

---

## PHASE 2 — PDF ingestion + page‑preserving extraction  ✅ COMPLETE

**Objective:** PDF → `documents` (+ file metadata) → `pages` (verbatim text,
offsets, optional label, source‑quality status) → deterministic `chunks`. Opens a
`runs` row (`run_type=ingest`). Corpus‑agnostic. No OCR, no LLM, no fact logic.
**Files shipped:** `app/ingest.py` (`ingest_pdf()` service — the whole public
surface); `app/models.py` (`IngestResult`); `app/config.py` (ingestion knobs);
`app/db.py` (forward‑only migration runner + migration 1); `tests/conftest.py`
(synthetic‑PDF factory); `tests/test_ingest.py` (28 tests). The `POST /documents`
API is **deferred to Phase 11** — Phase 2 exposes a service function only.
**What it does:** validate (magic bytes, size vs `max_upload_mb`, page cap vs
`max_pages`, encryption, corrupt) → SHA‑256 → store at `uploads/<sha256>.pdf`
(original filename sanitised to a basename, display‑only) → SHA‑256 dedup /
idempotent retry into the same row → PyMuPDF per‑page `text` (verbatim, no
normalization), `char_count`, `width/height`, `extraction_meta`
(block/image count, density, printed‑label candidate) → per‑page
`extraction_status` ∈ `TEXT_EXTRACTED | LOW_TEXT | EMPTY | EXTRACTION_ERROR`
(`LOW_TEXT`/`EMPTY` also recorded in `failures` as `ocr_page`; **no OCR run**) →
conservative optional `printed_label` detection → deterministic page‑bounded
chunking (`chunk_target_chars`/`overlap`, `header_prefix_len = 0`). Transactional:
document+run committed first, then pages+chunks+failures atomically; any later
failure marks the document `failed` (never falsely `ingested`).
*Deferred:* semantic provenance (title/publisher/`document_date`/
`publication_date`/`data_vintage`) and FY‑convention detection → Phase 6+.
**Acceptance (met):** synthetic‑PDF unit tests green; `pages` count == PyMuPDF
page count; offset invariant `pages.text[char_offset:char_end] == chunks.text`
holds (verified with 0 violations across the smoke set); no chunk crosses a page
or document; blank/low/error pages classified and flagged; expected errors raise
`IngestError` with a stable `.code`; duplicate → same `document_id`,
`duplicate=True`; unsafe filenames cannot escape the uploads dir. Smoke‑tested on
3 representative starter PDFs (earnings deck, prospectus, RBI report).
**Gate:** page boundaries + character offsets provably preserved. **Met.**

---

## PHASE 3 — Fact / evidence model + persistence

**Objective:** typed write/read layer for `facts`, `evidence`, `raw_extractions`,
`failures`; `facts_fts` kept in sync by explicit upsert; lifecycle transitions
are functions, not free `UPDATE`s.
**Files:** `app/models.py` (full `FactIn/Out`, `EvidenceIn/Out`), `app/db.py`
helpers, `tests/test_facts_store.py`.
**Tasks:** `insert_candidate_fact(raw_extraction_id, fact, evidence_stub)`;
`set_grounded(fact_id, evidence)`, `set_normalized(fact_id, norm)`,
`set_eligible(fact_id)`, `quarantine(fact_id, reason, failure_type)` — each
enforces the invariants and writes `facts_fts` + `failures` as needed. JSON
(de)serialization for `scope`/`qualifiers`/`deterministic_signals`.
**Acceptance:** hand‑built numeric + semantic fact + evidence round‑trip
byte‑identical incl. JSON and all numeric‑representation columns; FTS finds a fact
by a word in `object_raw`; `quarantine()` sets state + `reasoning_eligible=0` +
inserts a `failures` row; invariant violations rejected by the helpers, not just
the DB.
**Gate:** both fact types + full lifecycle representable; failure surface wired.

---

## PHASE 4 — Candidate fact extraction

**Objective:** chunk → **candidate** facts via Claude (structured output),
verbatim response stored in `raw_extractions`, transformed facts persisted at
`lifecycle_state=CANDIDATE`. Extraction only — no grounding, no normalization.
**Files:** `app/llm.py` (`extract_facts(chunk_text, doc_header) -> list[RawFact]`,
`temperature=0`, bounded `max_tokens`/retries, usage → run stats), `app/extract.py`,
`tests/test_extract.py`.
**Tasks:** corpus‑agnostic prompt (`prompt_version` from config) that asks for
subject/predicate/object **as written**, unit, currency, period text, scope
words, qualifiers, **modality**, and the minimal verbatim quote + its offset in
the provided text, plus `context_complete`. Store raw JSON first; parse with
Pydantic; malformed items → `failures(extraction_unparsed)` + counted. Map quote
offset chunk→page (minus `header_prefix_len`). Cost guard `max_llm_calls_per_doc`.
**Acceptance:** the 27‑page deck yields ≥30 candidate facts, each with non‑empty
subject/predicate/object, a quote, an offset within its page, a `modality` in the
controlled set; ≥1 numeric + ≥1 semantic; a `raw_extractions` row per chunk;
boilerplate page → ~0 facts. Offline path: a recorded chunk+response fixture
exercises the parser with no network (`pytest -m llm` gates live calls).
**Gate:** real candidate facts from a real PDF, verbatim LLM output retained.

---

## PHASE 5 — Evidence verification + quarantine

**Objective:** the grounding gate. Set `evidence.verification_method`,
`numeric_rederivation`, `evidence_status`; promote passing facts to `GROUNDED`,
quarantine the rest. Unverified facts **remain inspectable**.
**Files:** `app/ground.py`, wire into `pipeline`/`extract`, `tests/test_ground.py`.
**Tasks:** locate `quote` in `pages.text` → `exact`; whitespace/unicode‑normalized
match → `normalized_exact`; `rapidfuzz.partial_ratio ≥ 92` window → `fuzzy` (store
`fuzzy_score`); else `unverified`. For numeric facts, run
`normalize.parse_number` on the quote and compare to the extracted number →
`numeric_rederivation ∈ {success, failed}`. Derive `evidence_status` per the
DATA_MODEL table; set `facts.evidence_status`. `UNVERIFIED` (or `fuzzy`+`failed`)
⇒ `quarantine(grounding_failed)`. Write human‑readable `evidence.notes`.
**Acceptance:** on the deck, ≥90% of candidate facts reach `VERIFIED`; a planted
fabricated quote → `UNVERIFIED` + `QUARANTINED` + a `failures` row, and is absent
from any `reasoning_eligible` set; a whitespace‑mangled real quote →
`normalized_exact` or `fuzzy`, `PARTIAL`.
**Gate:** no `UNVERIFIED` fact can become `reasoning_eligible`; gate rejects a
planted hallucination; failures inspectable.

---

## PHASE 6 — Context + numeric / date / unit normalization

**Objective:** deterministic parsing fills the numeric‑representation columns, the
period columns, `unit_norm`, `modality` sanity; promote to `NORMALIZED` then
(after entity link in Phase 7) `ELIGIBLE_FOR_REASONING`. Raw always preserved.
**Files:** `app/normalize.py`, `tests/test_normalize.py`.
**Tasks:** `parse_number` (commas, ₹/Rs/US$, `(x)`→neg, magnitude map
crore/lakh/mn/bn/k, `%`→`percentage_ratio`, `bps`); `parse_currency`;
`parse_unit`; `parse_period(raw, fy_convention)` covering every DATA_MODEL row
incl. `FY2025/26` slash form and 9‑month ranges; `apply(fact, document)` fills
`value_raw/numeric_value/magnitude/magnitude_factor/base_value/currency/
is_percentage/percentage_ratio/unit_raw/unit_norm/reporting_period_*`, never
clearing a `*_raw`. Normalization failure → `failures(normalization_failed)`,
fact stays `GROUNDED` (not eligible).
**Acceptance:** the DATA_MODEL numeric table and period table reproduce exactly;
`(452)`→−452, `₹8,142 crore`→base 8.142e10, `5%`→ratio 0.05, `781 Bps`→0.0781,
`FY2025/26`→(2025‑04‑01,2026‑04‑01); property: every set normalized field has a
non‑null `*_raw`.
**Gate:** normalization correct on the full example tables; context not lost.

---

## PHASE 7 — Entity resolution

**Objective:** general‑purpose resolution: deterministic normalization → blocking
→ similarity scoring → LLM confirmation for ambiguous clusters only. No
Delhivery‑specific aliases anywhere in `app/`.
**Files:** `app/entities.py`, `app/embed.py`, `tests/test_entities.py`.
**Tasks:** normalize (generic legal‑suffix + honorific lists from config);
blocking (`token_set_ratio ≥ 88` OR name+context cosine ≥ 0.82); score pairs;
merge unambiguous pairs deterministically; send only borderline clusters to
`llm.confirm_entities` (may split); persist `entities` + `entity_aliases`
(`match_method` recorded). Rename facts (`predicate` ~ "former name"/"incorporated
as") → alias edges `match_method='derived_fact'`, `source_fact_id` set. Backfill
`facts.subject_entity_id`; facts that are `NORMALIZED` + entity‑linked (or entity
unresolved but not required) → `ELIGIBLE_FOR_REASONING`.
**Acceptance:** {"Delhivery","Delhivery Limited","the Company","SSN Logistics
Private Limited"} → one entity, one alias `derived_fact`; a distinct person stays
separate; two clearly different orgs don't merge; the module contains no starter
entity strings.
**Gate:** entities from corpus evidence + generic config only; ambiguous‑only LLM
use.

---

## PHASE 8 — Candidate retrieval + deterministic signals

**Objective:** bounded candidate‑pair generation (config‑driven weights &
thresholds) + `signals.compute(a,b)` producing the full deterministic signal set.
**Files:** `app/retrieve.py`, `app/signals.py`, `tests/test_retrieve.py`,
`tests/test_signals.py`.
**Tasks:** embed eligible facts (`fastembed`); per fact block on
entity/alias/normalization‑key; rank others by
`w_e·cos(fact) + w_p·cos(pred) + w_b·bm25` with `w_*` and `top_k` and
`candidate_threshold` from `Settings`; ordered‑pair dedup; skip already‑scored
pairs. `signals.compute`: `entity_match`, `predicate_sim`, `unit_equivalent`
(after normalization, incl. Cr↔mn, %↔ratio; INR≠USD), `base_value_delta_pct`,
`period_relation` (equal/overlaps/contains/disjoint/unknown),
`scope_conflict[]`, `modality_pair`, `publication_gap_days`, `vintage_differs`.
**Acceptance:** pair count ≤ `top_k · n_eligible`; every pair shares a block;
re‑run adds no pairs; a DATASET_ANALYSIS candidate (C1) is retrieved; signals on
a hand‑built pair match expected values; all weights/thresholds come from config
(changing `FKL_RETRIEVAL_TOP_K` changes output).
**Gate:** retrieval bounded/blocked/idempotent; signals correct & config‑driven.

---

## PHASE 9 — Relationship reasoning + explanations

**Objective:** **LLM proposes** a category + reasoning from a structured context
packet; **`signals.validate()` decides** the final category against deterministic
constraints. Persist both (`llm_proposed_category`, `validation_action`).
**Files:** `app/reason.py`, `app/llm.py` (`classify_relationship`),
`tests/test_reason.py`.
**Tasks:** build the packet (Fact A/B, both quotes, reporting periods,
publication dates/vintage, scope, units, modality, all deterministic signals);
`classify_relationship` returns `{category, context_dimension, reasoning,
confidence}` (structured, `temperature=0`). `signals.validate(proposed, signals)`
rules, e.g.:
- unit‑equivalent after normalization + `base_value_delta_pct ≤ tol` + same
  period + no scope conflict ⇒ force `CORROBORATES` (override any `CONTRADICTS`).
- both `HISTORICAL`/`ASSERTED`, same entity/predicate, **disjoint** periods,
  values differ ⇒ prefer `TEMPORAL_EVOLUTION` over `CONTRADICTS`.
- `scope_conflict ≠ ∅` explaining the gap ⇒ prefer `DIFFERENT_CONTEXT`
  (`context_dimension` = the scope key).
- different modality (e.g. `FORECAST` vs `HISTORICAL`) ⇒ not `CONTRADICTS`;
  `DIFFERENT_CONTEXT`/`UNCERTAIN`.
- `vintage_differs` + same period ⇒ `TEMPORAL_EVOLUTION` (revision) or
  `DIFFERENT_CONTEXT`.
- entity_match < threshold or `period_relation=unknown` or either fact `PARTIAL`
  with weak signals ⇒ `UNCERTAIN`.
Record `validation_action` (`accepted`/`overridden`/`downgraded`) + `notes`; cap
`confidence` when a fact is `PARTIAL`. Only `reasoning_eligible=1` facts are
considered (guard‑tested).
**Acceptance:** synthetic pairs produce each of the five categories with correct
`validation_action`; the "director active vs resigned July 2024" shape, given
bounded historical periods, classifies as `TEMPORAL_EVOLUTION` **or**
`DIFFERENT_CONTEXT`, **not** `CONTRADICTS`, purely from dates/modality; an
LLM `CONTRADICTS` on unit‑equivalent numbers is overridden to `CORROBORATES`.
**Gate:** all five categories; deterministic validation demonstrably overrides a
wrong LLM proposal; no ineligible fact in a relationship.

---

## PHASE 10 — Evaluation harness

**Objective:** first‑class evaluation. `evaluation/cases/{corroboration,
contradiction,contextual,failure}/` hold **expected‑property specs** (YAML/JSON),
not answers. `evaluation/harness.py` runs the pipeline on a corpus and checks
properties; also a threshold sweep to tune `candidate_threshold`, `top_k`,
`predicate_similarity_threshold`, `relationship_confidence_threshold`.
**Files:** `evaluation/harness.py`, `evaluation/cases/**`,
`tests/test_eval_harness.py`.
**Property examples (per EVALUATION_PLAN):** a `CORROBORATES` across two documents
with differing raw wording and both sides `VERIFIED`; ≥1 `CONTRADICTS` meeting
the strict signal profile *or* a recorded reason none exists; ≥1
`DIFFERENT_CONTEXT` **or** `TEMPORAL_EVOLUTION` with a `context_dimension`; ≥1
quarantined extraction present in `failures` and absent from reasoning; every
relationship has evidence on both sides; no `UNVERIFIED` fact participates.
**Tuning:** harness emits a table of (threshold set → property pass count,
precision proxy) so config values are chosen from data, not guessed. A guard test
fails if the evidence gate is disabled.
**Acceptance:** `python -m evaluation.harness --corpus tests/data/delhivery`
prints per‑property PASS/FAIL + the tuning table; `test_eval_harness` asserts the
harness itself is honest (disabling grounding flips a property to FAIL).
**Gate:** evaluation runs, properties are real, thresholds are data‑chosen.

---

## PHASE 11 — API

**Objective:** every endpoint in API_DESIGN wired to the storage/query layer;
`POST /documents/{id}/process` runs `pipeline.run` as a `BackgroundTask` writing
`runs.stage`/stats after each stage.
**Files:** `app/main.py`, `app/pipeline.py`, `app/models.py`, `tests/test_api.py`.
**Endpoints:** documents (upload/list/detail/process/status), `GET /facts`
(+filters incl. `lifecycle_state`, `evidence_status`, `modality`, `reasoning_
eligible`, FTS `q`), `GET /facts/{id}` (+ audit window + relationships),
`GET /relationships` (+`category`, `context_dimension`, `validation_action`),
`GET /relationships/{id}` (both full facts + quotes + signals + proposed vs final
category), `GET /entities`, `GET /entities/{id}`, `GET /failures`.
**Acceptance:** full run over both corpora via HTTP only; bodies match the
models; filter matrix works; unknown id → 404 with the common error body; a
failed run surfaces its error at HTTP 200.
**Gate:** "upload PDFs and inspect results" fully satisfiable through the API.

---

## PHASE 12 — UI

**Objective:** static, framework‑free UI over the API. Views: Documents
(upload/status), Facts (filters, expandable rows with quote + doc/page + full
context + verification detail), Relationships (category tabs; card shows Fact A |
Fact B, the deterministic signals table, **LLM‑proposed vs final category +
validation note**, reasoning, evidence both sides), Failures (all failure rows
with reasons). ~150 lines CSS, system font, no build step.
**Acceptance:** a reviewer with no terminal can upload → watch processing → open
a relationship and see both evidences, the signals, and why the category was
chosen (incl. any override). No console errors.
**Gate:** the four demo cases visible in the UI with no hard‑coded examples.

---

## PHASE 13 — Full dataset evaluation

**Objective:** run Phase‑10 harness across **both** corpora + at least one unseen
PDF; fill EVALUATION_PLAN §"observed examples" from the actual run; record tuned
config values in `.env.example` with a comment citing the tuning table.
**Acceptance:** every EVALUATION_PLAN property passes on both corpora; the four
required cases each have a concrete grounded example; unseen PDF produces facts +
≥1 relationship; `grep -ri '<starter entity/filename>' app/ evaluation/` empty.
**Gate:** the four required cases are demonstrably produced, not scripted.

---

## PHASE 14 — Hardening, README, demo

**Objective:** ship. Full README (Setup and Run Instructions, Video Demo,
Approach, Limitations and Next Steps, Additional Notes; name the AI tools used);
commit `samples/` (exported `GET /facts` + `/relationships` JSON + screenshots
for key‑less review); security pass (no secret in `git log -p`, upload limits,
error paths don't leak paths); resource pass (`max_llm_calls`, `max_chunks`,
timeouts, cost estimate sane); record the ≤3‑min demo; `ruff` clean; non‑`llm`
`pytest` green from a clean clone.
**Acceptance:** fresh clone → follow README → app runs → unseen PDF → facts +
relationships appear; demo ≤3:00 shows all four cases + a failure; submission
checklist (assignment "Before You Submit") all ticked.
**Gate:** submission‑ready.

---

## Changes vs the Phase‑0 plan

- 15 phases (was 13); **evaluation harness moved before the API** (Phase 10) so
  retrieval weights/thresholds are chosen from data (correction H/N).
- Entity resolution is its own phase (7), decoupled from retrieval (8).
- Extraction (4) is strictly candidate‑only; grounding (5), normalization (6),
  and entity linking (7) each own one lifecycle transition (correction J).
- Reasoning phase (9) splits **LLM proposal** from **deterministic validation**
  and persists both (correction I).
- Schema (Phase 1) now carries lifecycle, modality, four provenance concepts,
  full numeric representation, `raw_extractions`, `failures`, and reproducibility
  metadata (corrections A–F, K–M).
