# Implementation Plan (revised)

Phased. **Do not start a phase until the previous phase's gate passes.** Each
phase ends with runnable verification. Tests are `pytest`, plain functions, using
small **synthetic** PDF fixtures — no fixture frameworks, no mocking of our own
code. (The provided starter PDFs are used for manual smoke tests only and are not
committed.) Assertions are **structural properties**, never hard‑coded answers
(see EVALUATION_PLAN).

Revised phase order (per review): evaluation harness now lands **before** the API
so retrieval/threshold tuning is evidence‑driven.

**Status:** Phase 0 ✅ · Phase 1 ✅ · Phase 2 ✅ · Phase 3 ✅ · Phase 4 ✅ · Phase 5 ✅ · Phase 6 ✅ · Phase 7 ✅ · Phase 8 ✅ · Phase 9 ✅ · Phase 10 ✅ · Phase 11 ✅ · Phase 12 ✅ · Phase 13 not started.

| Phase | Title |
|---|---|
| 0 | Architecture / design — ✅ |
| 1 | Foundation + configuration + SQLite schema + tests — ✅ |
| 2 | PDF ingestion + page‑preserving extraction — ✅ |
| 3 | Fact / evidence model + persistence — ✅ |
| 4 | Candidate fact extraction — ✅ |
| 5 | Evidence verification + quarantine — ✅ |
| 6 | Context + numeric / date / unit normalization — ✅ |
| 7 | Entity resolution — ✅ |
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
SDK (`claude-sonnet-5`; no sampling params — Sonnet 5 rejects them), `pytesseract`+`pdf2image` (OCR fallback
only), `numpy`, `pydantic` + `pydantic-settings`. Dev: `pytest`, `httpx`, `ruff`.
Dependencies are added **in the phase that first imports them**, not up front.

## Repo layout (target)

```
app/
  __init__.py
  config.py        # Settings (pydantic-settings), all tunables, env-backed
  db.py            # connect / init_db / transaction  (no ORM, no repo layer)
  schema.sql       # genesis schema (user_version 0)
  migrations/      # NNNN_*.sql forward-only migrations (0001 Phase 2, 0002 Phase 3)
  facts.py         # Phase 3 — fact/evidence/relationship persistence + validation
  main.py          # FastAPI app + /health  (routes added Phase 11)
  models.py        # pydantic response/request models (grows per phase)
  ingest.py        # Phase 2
  extract.py       # Phase 4
  verify.py        # Phase 5 — deterministic evidence verification (verify_document / verify_fact)
  normalize.py     # Phase 6 — deterministic numeric/date/unit/period normalization (normalize_document)
  entities.py      # Phase 7 — deterministic entity resolution + borderline LLM confirm (resolve_document)
  embed.py         # Phase 8 — embeddings (deferred from Phase 7; retrieval needs facts.embedding)
  retrieve.py      # Phase 8
  signals.py       # Phase 8  (deterministic comparison)
  reason.py        # Phase 9  (LLM proposes; signals.validate() decides)
  llm.py           # Phase 4 — Anthropic wrappers (AnthropicExtractor, AnthropicEntityConfirmer)
  prompts/         # versioned prompts (extraction_v1.md, entity_confirm_v1.md)
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

## PHASE 3 — Fact / evidence model + persistence  ✅ COMPLETE

**Objective:** the durable, corpus-agnostic write/validate layer for facts,
evidence, and (future) relationships. No LLM, no extraction, no inference.
**Files shipped:** `app/migrations/0002_phase3_fact_evidence_model.sql`
(migration 2); `app/db.py` (migrations loaded from `app/migrations/*.sql`,
`_apply_migrations` now runs `PRAGMA foreign_key_check`); `app/models.py`
(`FactIn` / `EvidenceIn` / `RelationshipIn` + the `Literal` vocabularies);
`app/facts.py` (`insert_fact`, `attach_evidence`, `evidence_chain`,
`set_lifecycle`, `mark_reasoning_eligible`, `quarantine_fact`, `add_relationship`,
`FactError`, the `FACT_TYPES`/`LIFECYCLE_STATES`/… constants);
`tests/conftest.py` (`make_source` synthetic document/page/chunk factory);
`tests/test_facts.py` (40 tests).
**Schema (migration 2):** one canonical fact model for all types —
`facts.fact_type` widened to `numeric|semantic|temporal|categorical`;
`lifecycle_state` gains `RAW`; `facts.raw_payload` (per-fact pre-normalization
extractor output); `evidence.page_id`/`chunk_id` FKs for the explicit
FACT→EVIDENCE→(CHUNK→)PAGE→DOCUMENT chain; `evidence.verification_method` gains
`unavailable`; `evidence` CHECK `char_start < char_end`. Rebuild via the SQLite
table-redefinition procedure (empty tables), `foreign_key_check` verified.
Context (unit/currency/period/scope/qualifiers/**modality**) and the four
provenance concepts were already columns from Phase 1 — unchanged.
**Evidence invariant:** `mark_reasoning_eligible` promotes a fact to
`ELIGIBLE_FOR_REASONING` **only if** `evidence_chain()` resolves to a document
**and** `evidence_status ∈ (VERIFIED, PARTIAL)`; the two Phase-1 cross-column
CHECKs are the storage backstop. `add_relationship` refuses non-eligible facts.
**Acceptance (met):** all four fact types persist; numeric raw + normalized
representation and `raw_payload` round-trip; `facts_fts` populated on insert; the
full chain is validated (page∈doc, chunk∈page, offsets∈`[0,len(text)]`,
start<end); every lifecycle state accepted, invalid ones rejected; an
UNVERIFIED / evidence-less fact cannot become eligible (helper *and* DB CHECK); a
grounded VERIFIED fact can; all five relationship categories persist, invalid
ones rejected, self-relationship rejected, pair canonicalised, duplicates a
deterministic no-op. Migration idempotent and correct from genesis-only and
Phase-2 states. All synthetic data — no starter PDFs.
**Gate:** canonical fact model + full lifecycle + traceability invariant +
relationship storage, all enforced. **Met.**

---

## PHASE 4 — Candidate fact extraction  ✅ COMPLETE

**Objective:** chunk → **candidate** facts via Claude structured output; the
verbatim LLM response and every rejected candidate preserved; facts persisted at
`lifecycle_state=CANDIDATE`, `reasoning_eligible=0`, `evidence_status=UNVERIFIED`.
Extraction only — no evidence verification, normalization, entity resolution, or
inference.
**Files shipped:** `app/prompts/extraction_v1.md` (versioned, corpus-agnostic
prompt); `app/llm.py` (`AnthropicExtractor` — lazy SDK import,
`output_config.format` json_schema, no sampling params (Sonnet 5), SDK error
chain → typed `LLMExtraction`); `app/extract.py` (`extract_document`, deterministic
`_validate_candidate`, `_candidate_to_fact_in`, `extraction_summary`,
`ExtractError`); `app/models.py` (`RawCandidate` / `RawExtraction` /
`LLMExtraction` / `ExtractionRunResult`); `app/config.py` (llm effort / max_tokens
/ timeout); `tests/fakes.py` (`FakeLLM`); `tests/test_extract.py` (32 tests);
`scripts/smoke_extract.py`. `anthropic>=1.4` added.
**How it works:** open an `extract` `runs` row → for each chunk (ordered by
page_index, seq): call the client → **always** write a `raw_extractions` row (raw
response + `stop_reason` + `parse_error`/`item_count`) → API / timeout / refusal /
client-exception → record a `run_error` failure and continue → malformed or
truncated JSON → `extraction_unparsed` and continue → else deterministically
validate each candidate (non-empty subject/predicate/object/quote; `fact_type`,
`modality`, `period_type` in the controlled sets; integer offsets with
`0 ≤ start < end ≤ len(chunk.text)`; numeric facts carry a value; no duplicate
`(subject,predicate,object,start,end)` within the chunk) → a failing candidate →
`extraction_unparsed` failure with the **full candidate payload** in `detail`,
siblings unaffected → a passing candidate → `insert_fact` (raw beside any parsed
value; `raw_payload` = the candidate as produced; run/model/prompt metadata) then
`attach_evidence` as a **candidate citation** (`verification_method='unverified'`,
`evidence_status='UNVERIFIED'`, `chunk_id`/`page_id` set, offsets translated to
page coordinates `chunk.char_offset + candidate offset`, quote as returned). Per-
chunk DB writes are one transaction; the run row is finalised at the end; an
unexpected error marks the run `failed`. Cost accumulates from `usage`;
`max_llm_calls_per_doc` guard.
**Acceptance (met):** all four fact types persist as `CANDIDATE` / non-eligible /
`UNVERIFIED`; `raw_payload` + run + `raw_extractions` metadata round-trip; every
persisted fact traces FACT→EVIDENCE→CHUNK→PAGE→DOCUMENT (`evidence_chain`);
malformed JSON, truncation, API errors, client exceptions, and per-candidate
validation failures are each recorded and isolated; `extraction_summary` reports
chunks / generated / persisted / rejected / errors / by-type / by-document.
Structural smoke on 2 real starter PDFs (259 chunks): 0 broken chains, 0
mis-stated lifecycle. **Live-LLM extraction quality requires `ANTHROPIC_API_KEY`**
(`scripts/smoke_extract.py`) — not run in this environment.
**Gate:** candidate facts from real chunks with preserved provenance + raw
output; nothing reasoning-eligible. **Met.**

---

## PHASE 5 — Evidence verification + quarantine  ✅ COMPLETE

**Objective:** decide, deterministically and **without an LLM**, whether each
Phase-4 candidate fact's evidence is actually supported by the persisted Phase-2
source text. Verified facts → `GROUNDED`; unverifiable → `QUARANTINED` (preserved,
never reasoning-eligible). Claims are never rewritten — only an evidence *span*
may be corrected when the exact quote occurs once in the chunk.
**Files shipped:** `app/migrations/0003_phase5_verification.sql` (migration 3 —
`evidence` rebuilt: `verification_method` gains `recovered_exact`, `+ verified_at`);
`app/verify.py` (`verify_fact`, `verify_document`, `verification_summary`,
`normalize_for_compare`, `_check_numeric_consistency`, `_run_ladder`,
`VerifyError`); `app/models.py` (`VerificationResult`, `DocVerificationSummary`);
`app/config.py` (`verify_fuzzy_threshold` = 90, `verify_fuzzy_min_quote_chars`,
`verify_numeric_tolerance`); `tests/test_verify.py` (46 tests). `rapidfuzz>=3.6`
added.
**Verification ladder** (first hit wins; source of truth is `pages.text` /
`chunks.text` resolved from the *authoritative* `document_id`/`page_id`/`chunk_id`
— never the LLM's numbers):
  1. `exact` — `source[start:end] == quote` → VERIFIED
  2. `normalized_exact` — equal after collapsing Unicode whitespace + folding
     trivial quote/dash variants (no case, digit, separator, currency, %, or unit
     changes) → VERIFIED
  3. `recovered_exact` — offsets wrong, but the exact quote occurs **exactly
     once** in the chunk → span corrected to the authoritative location (quote +
     `raw_payload` untouched) → VERIFIED; **>1 occurrence → not guessed →
     quarantine `ambiguous_quote_match`**
  4. `fuzzy` — last resort for PDF artifacts: a guard first requires every number,
     currency token and unit/magnitude word from the quote to be present verbatim
     in the source; then `rapidfuzz.partial_ratio ≥ FKL_VERIFY_FUZZY_THRESHOLD`
     → **PARTIAL** (never VERIFIED; `fuzzy_score` recorded)
  5. `unverified` — none of the above → quarantine (`quote_not_found`)
**Numeric consistency (§9, bounded — not Phase-6 normalization):** for numeric
facts that reach VERIFIED/PARTIAL, check that `raw_value_text`'s tokens are in the
verified span, `parsed_value` equals the number in `raw_value_text` (no scale
applied), percentage/ratio is coherent, and `currency`/`magnitude`/`unit_raw`
(alias-aware) appear in the span. Any failure → `numeric_rederivation='failed'` →
**quarantine `numeric_mismatch`**, claim unchanged (candidate `8000` vs source
`8142` is a failure, never a repair).
**Idempotency:** the single `evidence` row is updated in place (never a second
row); stale `grounding_failed` failures for a fact are deleted before re-writing;
a re-run is deterministic; `verify_document` skips already-`QUARANTINED` facts.
**Acceptance (met):** exact / normalized / recovered / ambiguous / absent /
out-of-range-offset / source-unavailable / no-evidence cases each land on the
right rung; numeric mismatch (wrong value, currency, unit, magnitude, percentage)
quarantines without repairing the claim; `CANDIDATE → GROUNDED` on success and a
grounded VERIFIED fact can then `mark_reasoning_eligible`; a failed fact is
`QUARANTINED`, `reasoning_eligible=0`, `evidence_status='UNVERIFIED'`, payload
preserved, and the DB CHECK still blocks a manual `reasoning_eligible=1`; every
result still traces FACT→EVIDENCE→CHUNK→PAGE→DOCUMENT; verification twice is a
no-op. Real starter-PDF smoke over 2 corpora: exact/recovered/normalized →
GROUNDED, fabricated quote + wrong number → QUARANTINED, 0 broken chains.
**Gate:** deterministic evidence verification with quarantine; nothing
reasoning-eligible without VERIFIED/PARTIAL grounding. **Met.**

---

## PHASE 6 — Context + numeric / date / unit normalization  ✅ COMPLETE

**Objective:** deterministic parsing (no LLM) fills the numeric‑representation
columns, the period columns, `currency`, and `unit_norm`; promote `GROUNDED →
NORMALIZED`. `*_raw` fields are never cleared or overwritten.
**Files shipped:** `app/normalize.py`, `tests/test_normalize.py` (72 tests). No
migration — every target column already exists from migration 2. No new
dependency.
**What shipped:**
- Pure parsers, each reproducing its DATA_MODEL worked row exactly:
  `parse_number` (thousands commas, `(x)`/leading `−` → negative, magnitude map
  crore·lakh·thousand·million·billion·trillion + `bps` factor 1e‑4, `%` →
  `percentage_ratio`), `parse_currency` (₹/Rs/INR, US$/USD, €, £),
  `parse_unit` (`%`/bps/`x` → ratio, tonne, days, sqft, count‑nouns, currency),
  `parse_period(raw, fy_convention)` (instant / quarter / half_year /
  fiscal_year / calendar_year / range / `unknown`, incl. `FY2025/26` slash form,
  `2024‑25` Indian form, `Fiscal 2021`, and "N months ended <date>" ranges),
  `detect_fy_convention` (reads "financial/fiscal year ended <Month>" from the
  document's own text → `apr-mar` / `jan-dec` / `jul-jun`; other end‑months not
  representable → `None`).
- `apply_normalization(conn, fact_id, *, fy_convention)` — fills the columns in
  one in‑place `UPDATE`; numeric‑unparseable → `failures(normalization_failed)`
  and the fact **stays `GROUNDED`** (never promoted); an unresolvable period is
  not a failure — `reporting_period_type='unknown'`, fact still `NORMALIZED`.
- `normalize_document(document_id)` — resolves `documents.fy_convention` first
  (detected from page text, else `settings.fy_convention_default`,
  `fy_convention_source` recorded), opens a `run_type='normalize'` run, processes
  `lifecycle_state IN ('GROUNDED','NORMALIZED')` with per‑fact isolation,
  idempotent (re‑run → identical values, stale `normalization_failed` rows
  cleared first, no duplicates). `normalization_summary` for observability.
**Acceptance (met):** the DATA_MODEL numeric table and period table reproduce
exactly — `₹(452) Cr`→−452 / base −4.52e9, `₹8,142 crore`→base 8.142e10,
`5%`→ratio 0.05 / base 0.05, `781 Bps`→ratio 0.0781, `FY2025/26`→
(2025‑04‑01,2026‑04‑01), `nine months ended December 31, 2021`→
(2021‑04‑01,2022‑01‑01,range). Property test: every populated normalized field
has a non‑null `*_raw`. Real starter‑PDF deterministic smoke: FY convention
detected from the annual‑report text, numeric + period columns filled,
`value_raw`/`reporting_period_raw` untouched, `GROUNDED → NORMALIZED`.
**Gate:** normalization correct on the full example tables; context not lost;
schema unchanged (`user_version` stays 3). **Met.**

---

## PHASE 7 — Entity resolution  ✅ COMPLETE

**Objective:** general‑purpose resolution of `facts.subject_raw` surfaces to the
global `entities` table: deterministic normalization → blocking → auto‑merge or
borderline‑only LLM confirmation. No dataset‑specific aliases anywhere in `app/`.
**Files shipped:** `app/entities.py`, `app/prompts/entity_confirm_v1.md`,
`AnthropicEntityConfirmer` in `app/llm.py`, `tests/test_entities.py` (26 tests).
No migration — `entities` / `entity_aliases` / `facts.subject_entity_id` are all
in the genesis schema (`user_version` stays 3). No new dependency (`rapidfuzz`
already present; **no embeddings** — see deviation).
**What shipped:**
- `normalize_name` — lowercase, strip punctuation, strip trailing legal‑form
  suffixes and leading honorifics (generic `Settings` lists) → `normalization_key`.
  `guess_type` — honorific prefix → `person`, else `org`.
- `resolve_document(document_id, *, llm=None)` — opens a `run_type='resolve'`
  run, then per subject surface:
  1. exact `normalization_key` match to an existing entity → link, alias
     `deterministic`;
  2. fuzzy block `token_set_ratio ≥ entity_block_fuzzy_threshold` (88): if
     `token_sort_ratio ≥ entity_merge_fuzzy_threshold` (94) → auto‑merge
     (`similarity`); else if an `llm` confirmer is supplied → `confirm_entities`
     (merge on `same` + confidence ≥ 0.5, alias `llm`); else → `entity_ambiguous`
     failure, **surface left unresolved** (never a blind merge);
  3. no match → new entity (`deterministic`).
- Rename facts (`predicate` matches a generic `entity_rename_predicates` phrase)
  → the object becomes an alias `match_method='derived_fact'`, `source_fact_id`
  set; if the former name already had its own entity the two are merged.
- Anaphora surfaces (`entity_anaphora` list: "the Company", "the Group", …) →
  linked to the document's **dominant** entity (most facts; a tie → unresolved).
- Backfill `facts.subject_entity_id`; every `NORMALIZED` fact with a verified/
  partial evidence chain → `ELIGIBLE_FOR_REASONING` (entity link recorded, not
  required). Per‑surface failure isolation; `resolution_summary` observability.
**Deviation from the original plan:** the "name+context cosine ≥ 0.82" blocking
rung and `app/embed.py` are **deferred to Phase 8**, which needs
`facts.embedding` for retrieval regardless. The acceptance cluster resolves on
deterministic + fuzzy + rename + anaphora alone, and `fastembed` is not
installed here. Auto‑merge uses `token_sort_ratio` (order/length‑sensitive)
rather than `token_set_ratio`, which scores a bare name vs "<name> Robotics" at
100 — the exact false‑merge D8 rejects; `token_set_ratio` stays the recall gate.
**Acceptance (met):** `{"Delhivery","Delhivery Limited","the Company","SSN
Logistics Private Limited"}` → one entity, `SSN Logistics…` alias
`derived_fact` with `source_fact_id`; `{"Mr. Sahil Barua","Sahil Barua"}` stays
separate from the org; a bare name vs "<name> Robotics" does **not** merge
without an LLM (`entity_ambiguous`, surface unresolved); a fake confirmer merges
or splits it on demand; `grep -niE 'delhivery|rbi|…' app/` is clean; cross‑
document smoke: one entity spans two ingested PDFs, "the Company" in the second
resolves to it. Idempotent re‑run → identical entities/aliases/links, no new
failures.
**Gate:** entities from corpus evidence + generic config only; ambiguous‑only LLM
use; schema unchanged. **Met.**

---

## PHASE 8 — Candidate retrieval + deterministic signals  ✅ COMPLETE

**Objective:** bounded candidate‑pair generation (config‑driven weights &
thresholds) + `signals.compute(a,b)` producing the full deterministic signal set.
Phase 8 answers *"which other facts are plausible candidates for comparison?"* —
**not** *"are these facts related / contradictory?"* (that is Phase 9).
**Files shipped:** `app/retrieve.py` (`retrieve_candidates`, `retrieval_summary`,
`_score`), `app/signals.py` (`compute`, `predicate_signals`, `period_relation`,
`scope_relation`, `content_tokens`), `app/models.py` (`SignalSet`,
`CandidatePair`, `PeriodRelation`), `app/config.py` (`retrieval_weight_entity`,
`retrieval_bucket_max`, `retrieval_min_shared_tokens`), `tests/test_retrieve.py`
(16 tests), `tests/test_signals.py` (26 tests), `scripts/smoke_retrieve.py`. **No
migration** (`user_version` stays 3 — every column needed already exists). **No
new dependency** (`rapidfuzz` already present).

**Retrieval strategy (Stage A).** Deterministic, high‑recall, read‑only.
Embeddings are **deferred** — `fastembed`/`numpy` are not installed here, Phase 7
already deferred the cosine rung, and the brief says not to force embeddings when
the environment can't support them. `facts.embedding` stays NULL;
`app/embed.py` is **not** created; `retrieval_weight_embedding` is reserved.
1. Load every `ELIGIBLE_FOR_REASONING` fact (cross‑document).
2. Inverted‑index blocking (near‑linear in output size, not a full O(n²) DB scan):
   `entity` (same `subject_entity_id`), `predicate_exact` (identical
   `predicate_norm`), `lexical` (≥ `FKL_RETRIEVAL_MIN_SHARED_TOKENS` shared
   content tokens; a token covering > `FKL_RETRIEVAL_BUCKET_MAX` facts is dropped
   as non‑discriminative).
3. Score: `retrieval_score = (w_entity·same_entity + w_predicate·predicate_sim +
   w_bm25·lexical_overlap) / (w_entity+w_predicate+w_bm25)`, all weights from
   `Settings`. `predicate_sim` is deterministic `rapidfuzz.token_set_ratio`.
4. Keep entity / exact‑predicate pairs regardless of score (strong structural
   candidates); keep the rest at `score ≥ FKL_RETRIEVAL_CANDIDATE_THRESHOLD`.
5. Keep top `FKL_RETRIEVAL_TOP_K` per fact (union), canonicalize to `(min,max)`
   fact id, dedupe, sort → `≤ top_k · n_eligible` pairs.
Candidate pairs are **transient** (returned, not persisted) — no candidate‑pair
table, so "re‑run adds no pairs" holds by deterministic recomputation and there
is zero lifecycle risk. Context differences never exclude a pair; they surface as
signals in step 6.

**Deterministic signals (Stage B) — `signals.compute(conn, a_id, b_id) →
SignalSet`.** Every field derived only from Phases 1–7 data; no LLM, no unit
conversion beyond the Phase‑6 normalized representation. `entity_relation`
(same/different/unresolved); `predicate_exact` / `predicate_similarity` /
`predicate_token_overlap`; `fact_type_*` + `fact_type_match`; numeric (both
numeric w/ `base_value`): `base_value_abs_diff`, `base_value_delta_pct`,
`sign_match`, `percentage_vs_absolute`, `ratio_a_to_b`, `unit_equivalent`;
`unit_relation` / `currency_relation` (same/different/missing — INR≠USD, never
FX‑converted); `period_relation`
(equal/same_year/contains/overlaps/adjacent/disjoint/unknown/missing — reporting
period only, never the document date); `scope_relation` + `scope_conflict[]`
(generic dict‑key comparison, no hard‑coded scope names); `modality_a/b`,
`modality_relation`, `modality_comparable`; `same_document`,
`publication_gap_days`, `vintage_differs`; `reasons[]`.

**Acceptance (met):** pair count `≤ top_k · n_eligible`; every pair shares a
block; re‑run is byte‑identical (pairs, order, scores, signal values);
`FKL_RETRIEVAL_TOP_K` change changes output; a C1‑shape pair (₹8,142 Cr FY24 ≡
₹81,415.38 mn FY24, cross‑document, same entity, related predicate) is retrieved
with `numeric_comparable`, `base_value_delta_pct < 0.01`, `unit_equivalent` and
**no category**; only `ELIGIBLE_FOR_REASONING` facts participate
(CANDIDATE/GROUNDED/NORMALIZED/QUARANTINED excluded); Phase 8 never changes a
fact's lifecycle/evidence/eligibility and writes no relationship; signal helpers
reproduce their worked cases exactly; `grep -niE` for corpus strings in
`app/retrieve.py` + `app/signals.py` is clean. Deterministic real‑corpus smoke
(3 Delhivery PDFs, LLM‑free fact harvest): 36 eligible facts → 322 candidate
pairs (≤ 540), 197 cross‑document, identical on re‑run.
**Gate:** retrieval bounded/blocked/idempotent; signals correct & config‑driven.
**Met.**

**Limitations (carried to RISKS):** lexical‑only retrieval misses true synonyms
with zero shared tokens *unless* the two facts share a resolved entity (the
primary path); broad entity blocking is high‑recall by design and yields many
non‑meaningful pairs for Phase 9 to filter; embedding retrieval remains the next
enhancement.

---

## PHASE 9 — Relationship reasoning + explanations  ✅ COMPLETE

**Objective:** turn a Phase‑8 `CandidatePair` + its `SignalSet` into one of the
five relationship categories, persisted with a full rationale. **Deterministic
logic first and final**; the LLM is an *optional proposer* for genuinely semantic
questions and its proposal is always re‑checked deterministically.
**Files shipped:** `app/reason.py` (`reason_pair`, `reason_document`,
`deterministic_verdict`, `_validate_llm`, `_confidence`, `reasoning_summary`,
`ReasonError`), `app/llm.py` (`AnthropicRelationshipConfirmer`,
`classify_relationship`), `app/prompts/relationship_v1.md`, `app/models.py`
(`RelationshipProposal`, `RelationshipDecision`, `DocReasoningSummary`,
`ValidationAction`), `app/config.py` (`relationship_prompt_version`),
`app/facts.py` + `app/models.py` (`RelationshipIn` / `add_relationship` now carry
`llm_used` / `llm_proposed_category` / `validation_action` / `validation_notes`,
the columns Phase‑3's schema already reserved), `tests/test_reason.py` (42 tests),
`scripts/smoke_reason.py`. **No migration** (`user_version` stays 3). **No new
dependency.**

**Pipeline** `CandidatePair → deterministic verdict → (optional) LLM proposal →
deterministic validation → final category → persist`. Phase 9 does **not**
retrieve, re‑resolve entities, re‑verify evidence, or re‑normalize — it consumes
Phase 5–8 output. Read‑only w.r.t. facts and evidence; the only writes are
`relationships` rows (via the existing `add_relationship`) and
`relationship_uncertain` `failures` rows.

**`deterministic_verdict(signals)`** — first matching rule wins; all thresholds
from `Settings` (`numeric_equivalence_tolerance`, `numeric_contradiction_threshold`,
`predicate_similarity_threshold`). Precedence: entity not same → `UNCERTAIN`;
`period_relation=unknown` → `UNCERTAIN`; weak predicate → `UNCERTAIN`; fact‑type
mismatch w/o numeric comparability → `UNCERTAIN`; `percentage_vs_absolute` →
`UNCERTAIN`; `scope_conflict` → `DIFFERENT_CONTEXT` (`context_dimension="scope:"+key`);
`modality` not comparable → `DIFFERENT_CONTEXT`(`modality`) unless both the *same*
non‑actual modality and equivalent → `CORROBORATES`; then the numeric branch —
different currency/unit → `DIFFERENT_CONTEXT`(`currency`/`units`); distinct
periods (`adjacent`/`disjoint`/`same_year`) + `Δ% > tol` → `TEMPORAL_EVOLUTION`;
`contains` + `Δ% > tol` → `DIFFERENT_CONTEXT`(`period_type`); `equal`/`overlaps` →
`Δ% ≤ tol` & sign match → `CORROBORATES`, `Δ% > contradiction_threshold` →
`CONTRADICTS`, in between → `UNCERTAIN`; `period missing` → `UNCERTAIN`. Semantic
pairs: distinct periods + differing statements + comparable modality →
`TEMPORAL_EVOLUTION`; identical claim → `CORROBORATES`; otherwise `None` → the
semantic step.

**Semantic step.** No confirmer → `UNCERTAIN` (the system produces every
deterministic category with no API key). With a confirmer:
`classify_relationship` sends a **minimal structured packet** (both facts'
subject/predicate/object/value/period/scope/modality/evidence‑quote + the full
signal set — never the corpus) and returns
`{relationship, confidence, reason, context_differences, uncertainties}` via
`output_config.format` json_schema, no sampling params. The proposal is validated
(`_validate_proposal`: category ∈ the 5, confidence ∈ [0,1], non‑empty reason —
else `UNCERTAIN`) then run through **`_validate_llm`** which can `accept` /
`override` / `downgrade`: `CONTRADICTS` on tol‑equal numbers under identical
context → `CORROBORATES` (overridden); any `CONTRADICTS`/`CORROBORATES` with a
`scope_conflict` → `DIFFERENT_CONTEXT` (overridden); `CONTRADICTS` on distinct
periods → `TEMPORAL_EVOLUTION` (overridden); `CONTRADICTS` on mixed modality →
`DIFFERENT_CONTEXT` (downgraded); `CORROBORATES` on `Δ% > contradiction_threshold`
→ `UNCERTAIN` (downgraded); `period` unknown/missing → `UNCERTAIN` (downgraded).
An LLM exception is caught → `UNCERTAIN`; one bad pair never fails the run.

**Confidence** is an explicit function of the signals (documented in `_confidence`,
not a calibrated probability): forced `CORROBORATES`/`CONTRADICTS` scale with the
value delta vs the two tolerances; context/temporal ≈ `0.55 + 0.2·predicate_sim`;
`UNCERTAIN` = 0.25; capped at the LLM's own confidence when it was used; capped at
0.6 when either side's evidence is `PARTIAL`.

**Persistence.** `reason_document(document_id)` opens a `run_type='reason'` run,
takes `retrieve_candidates(document_id=…)`, classifies each pair, and writes one
`relationships` row per pair through `add_relationship` (canonical `(min,max)`,
first‑write‑wins → **idempotent, no duplicates**), storing `category`,
`context_dimension`, `confidence`, `reasoning`, the full `deterministic_signals`
JSON, `llm_used`, `llm_proposed_category`, `validation_action`, `validation_notes`,
`run_id`, and model/prompt metadata when the LLM was used. Every `UNCERTAIN` pair
also gets a `failures(relationship_uncertain)` row. Only
`ELIGIBLE_FOR_REASONING` + `reasoning_eligible=1` facts participate (guard in
`reason_pair` **and** the `add_relationship` gate); `QUARANTINED` / `CANDIDATE` /
`NORMALIZED` / `UNVERIFIED` facts are excluded upstream and rejected if reached.

**Acceptance (met):** synthetic pairs produce each of the five categories
deterministically with no LLM; the "director active vs resigned July 2024" shape
→ `TEMPORAL_EVOLUTION` from dates + modality, not `CONTRADICTS`;
`_validate_llm` demonstrably overrides an LLM `CONTRADICTS` on tol‑equal numbers
to `CORROBORATES` and downgrades/overrides the scope/period/modality cases;
numeric edge cases (exact, within tol, between the thresholds, sign flip,
`%`‑vs‑absolute, missing base value) each land on the right rung;
`reason_document` is byte‑identical on a wipe‑and‑re‑run and creates no
duplicates; quarantined/unverified facts appear in no relationship;
`reason_pair` never mutates a fact's lifecycle or evidence; an LLM exception
leaves the run `done`. Deterministic real‑corpus smoke (3 Delhivery PDFs,
LLM‑free fact harvest): 42 eligible facts → 383 candidate pairs → 383
relationships (CORROBORATES 22, TEMPORAL_EVOLUTION 9, CONTRADICTS 4, UNCERTAIN
348), 348 `relationship_uncertain` failures, 0 errors, **identical on re‑run**.
The harvested facts are crude (real extraction needs the LLM); the smoke
exercises the *mechanism*, and the 4 `CONTRADICTS` are **not** a claim that the
corpus contains a contradiction.
**Gate:** all five categories; deterministic validation overrides a wrong LLM
proposal; no ineligible fact in a relationship; runs without an API key. **Met.**

**Limitations (carried to RISKS):** semantic pairs with no distinct period and no
identical claim are `UNCERTAIN` without a confirmer (by design); the confidence
formula is heuristic and untuned (Phase 10); broad Phase‑8 recall means most
pairs are `UNCERTAIN`; `add_relationship` is first‑write‑wins, so a re‑run after a
rule change needs a fresh `relationships` table.

---

## PHASE 10 — Evaluation harness  ✅ COMPLETE

**Objective:** first‑class evaluation — run the pipeline (or evaluate an existing
DB), check **expected properties** (never hard‑coded answers), and sweep the
config.
**Files shipped:** `evaluation/__init__.py`, `evaluation/harness.py`
(`evaluate`, `run_synthetic` / `run_db` / `run_corpus`, `sweep_thresholds`,
`load_cases`, CLI `main`), `evaluation/properties.py` (four case predicates +
four global invariants, all structural — no entity/figure/filename), `evaluation/
corpora/synthetic.py` (`seed_corpus` — a deterministic LLM‑free fixture with two
honesty knobs), `evaluation/cases/{corroboration,contradiction,contextual,
failure}/*.json` (property specs: `{property, corpus, must_hold, description}`,
JSON not YAML — no new dependency), `tests/test_eval_harness.py` (11 tests). No
migration, no new dependency.

**How it runs.**
- `python -m evaluation.harness` — builds the **synthetic offline corpus** (7
  seeded facts + evidence + one resolved entity, engineered so retrieval +
  reasoning yield CORROBORATES 1 / CONTRADICTS 1 / DIFFERENT_CONTEXT 3 /
  TEMPORAL_EVOLUTION 2 / UNCERTAIN 8 and one `grounding_failed` quarantine),
  evaluates every property + invariant, prints per‑property PASS/FAIL, exits 0
  iff all `must_hold` case‑properties and all invariants pass.
- `--sweep` — re‑runs reasoning under a curated config grid (`baseline`,
  `retrieval_top_k=5`, `candidate_threshold=0.80`,
  `predicate_similarity_threshold=0.85`, `relationship_confidence_threshold=0.90`,
  `numeric_equivalence_tolerance=0.001`, `numeric_contradiction_threshold=0.50`)
  and prints a `(config → per‑property pass, #relationships, #UNCERTAIN,
  #quarantined, must‑hold)` table + a `recommended` config, then restores the
  baseline DB state. The grid has real teeth: `predicate_similarity_threshold=0.85`
  drops the cross‑document CORROBORATES; `numeric_contradiction_threshold=0.50`
  drops the strict CONTRADICTS **and** the apparent‑conflict context case.
- `--db <path>` — evaluate an already‑processed database, no pipeline run.
- `--corpus <dir>` — run the real ingest→extract→verify→normalize→resolve→reason
  pipeline over a directory of PDFs; exits with a clear message if
  `ANTHROPIC_API_KEY` is unset (extraction needs it). Not exercised in this
  environment — same key constraint as Phases 4–9.

**Properties (structural, generalise to any corpus).**
1. `corroboration_cross_document` — ∃ CORROBORATES, `fact_a.document_id ≠
   fact_b.document_id`, differing `object_raw` and `predicate`, both
   `evidence_status ∈ {VERIFIED, PARTIAL}` (≥1 VERIFIED),
   `base_value_delta_pct ≤ numeric_equivalence_tolerance`.
2. `contradiction_strict_profile` — ∃ CONTRADICTS with same entity,
   `predicate_similarity ≥ threshold` (or exact), `unit_equivalent`,
   `period_relation ∈ {equal, overlaps}`, no `scope_conflict`, both modality
   HISTORICAL/ASSERTED, `base_value_delta_pct > numeric_contradiction_threshold`,
   `validation_action ∈ {not_applicable, accepted}`, reasoning present. (A corpus
   with no face‑value contradiction sets `must_hold=false` and records it.)
3. `context_reconciled_with_dimension` — ∃ DIFFERENT_CONTEXT / TEMPORAL_EVOLUTION
   with `base_value_delta_pct > numeric_contradiction_threshold` (*looks* like a
   conflict) but `context_dimension` set, reasoning present, both sides grounded.
4. `failure_surface_populated` — `failures` non‑empty with ≥1 machine type, every
   row has a `reason`, every fact a failure points at is `QUARANTINED` and absent
   from every relationship.
**Global invariants** (every run): every relationship has an evidence chain on
both sides; no `UNVERIFIED` / non‑`reasoning_eligible` fact participates; every
TEMPORAL_EVOLUTION / DIFFERENT_CONTEXT has a `context_dimension`; every UNCERTAIN
relationship and every `failures` row has a reason.

**Honesty guards (tests).** `seed_corpus(break_grounding=True)` leaves the
corroboration pair ineligible → `corroboration_cross_document` FAILs while the
harness still runs and the other invariants hold.
`seed_corpus(fabricate_corroboration=True)` sets the deck value far from the
report value → the pair classifies CONTRADICTS not CORROBORATES → the property
FAILs (unsupported facts cannot satisfy it). Demoting a relationship fact's
`reasoning_eligible` → `no_unverified_or_ineligible_participates` FAILs.

**Acceptance (met):** `python -m evaluation.harness --sweep` prints per‑property
PASS/FAIL + the sweep table and exits 0 on the synthetic corpus; the four case
files map 1:1 to registered properties; the honesty knobs flip the corroboration
property to FAIL; the sweep detects configs that break a must‑hold property; no
`evaluation/*.py` contains a starter‑corpus string. 360 tests pass, ruff clean.
**Gate:** evaluation runs, properties are real and structural, the sweep is
config‑driven and has teeth. **Met.**

**Limitations (carried to RISKS):** the synthetic corpus is deliberately tiny, so
`retrieval_top_k` / `candidate_threshold` / `relationship_confidence_threshold`
don't move its properties — meaningful threshold tuning needs the real corpus +
an API key (Phase 13); the property predicates read config tolerances, so a
report is only meaningful against the settings that produced the DB.

---

## PHASE 11 — API  ✅ COMPLETE

**Objective:** every endpoint in API_DESIGN wired to the storage/query layer;
`POST /documents/{id}/process` runs `pipeline.run` as a `BackgroundTask` that
writes `runs.stage`/stats after each stage.
**Files shipped:** `app/main.py` (14 routes over the Phase 2-10 tables +
`APIError` → common error body + `get_conn` dependency + guarded static mount),
`app/queries.py` (read/shaping layer — `fact_out` / `relationship_out` /
`document_out` / `entity_out` / `list_*` / `list_failures`; no writes, no LLM),
`app/pipeline.py` (`start` opens the `run_type='full'` row synchronously;
`run` executes extract→ground→normalize→resolve→reason, updating stage + aggregate
stats, isolating a failing stage → `full` run `failed` + `documents.status`),
`app/models.py` (Phase-11 response models — `FactOut`, `RelationshipOut`,
`DocumentOut`, `RunOut`, `EntityOut`, `FailureOut`, the `*Page` envelopes,
`ErrorOut`, and the sub-objects `NumericRepr` / `ReportingPeriod` / `EvidenceOut`
/ `ReproOut` / `EntityRef`), `tests/test_api.py` (20 tests). One new dependency:
`python-multipart` (the documented `multipart/form-data` upload). No migration.

**Endpoints.** `POST /documents` (multipart; duplicate SHA → 200 `"duplicate":
true`; ingest-error → 400 `invalid_pdf` / 400 `encrypted_pdf` / 413
`file_too_large` / 422 `too_many_pages`), `GET /documents` (`?status=`,
pagination, per-doc `counts`), `GET /documents/{id}` (+`counts` + `latest_run`),
`POST /documents/{id}/process` (202; a `running` full run → that run; unknown →
404), `GET /documents/{id}/status` (mirrors `documents.status`; a failed run
shows `run.status="failed"` + `run.error` at HTTP 200). `GET /facts` (filters:
`document_id`, `entity_id`, `entity` substring, `predicate` substring, `type`,
`lifecycle_state`, `evidence_status`, `modality`, `reasoning_eligible`, FTS `q`,
`sort`, `limit`/`offset`), `GET /facts/{id}` (+`context_window` ±200 chars,
`raw_extraction`, `relationships`). `GET /relationships` (filters: `category`,
`context_dimension`, `validation_action`, `document_id`/`entity_id` either side,
`min_confidence`, `llm_used`, `sort`), `GET /relationships/{id}` (both full
`FactOut` objects incl. `context_window`, the persisted `deterministic_signals`,
`llm_proposed_category` vs `category`, `validation_action`/`_notes`).
`GET /entities`, `GET /entities/{id}` (+`aliases` with `source_fact_id`,
`sample_facts`), `GET /failures` (`?document_id=`, `?failure_type=`;
`counts_by_type`; each row carries its `fact` (QUARANTINED) or `relationship`).
All lists paginate `?limit=` (default 50, max 200) `?offset=`; every error is
`{"error": {"code", "message"}}`.

**Deviations from the frozen API_DESIGN (Phase-0 artifact).** `deterministic_
signals` is exposed as the **actual** persisted `SignalSet` dump (richer:
`entity_relation`, `predicate_similarity`, `modality_a`/`modality_b`, …) rather
than the Phase-0 renamed subset (`entity_match`, `predicate_sim`, …) — the real
signal names are what Phase 9 stores and what a reviewer should see. The
`period_overlaps` fact filter is not implemented (needs an FY-convention
resolution; not in the acceptance) — noted in RISKS.

**Pipeline.** `POST /process` creates the `full` run and returns immediately; a
`BackgroundTask` runs `pipeline.run`. Each stage's own phase function still opens
its sub-run; the `full` run is the umbrella `GET /status` reports. The extract
stage is treated as failed when `extract_document` reports `status='failed'` **or**
every chunk errored with nothing persisted (a broken/absent client) — an honest
0-fact extraction with no errors is not a failure. `documents.status`:
`processing` → `done` | `failed` (with `status_detail`). Concurrency is a single
process; a second `POST /process` while one is `running` returns the running run.

**Acceptance (met):** `uvicorn app.main:app` boots, `/health` and `/docs`
(OpenAPI) work, all 14 routes register; the filter matrix returns the right
counts against the seeded corpus (7 facts, 15 relationships, 1 quarantine, 9
failures); unknown id → 404 with the common error body on documents / facts /
relationships / entities; a keyless `POST /documents/{id}/process` → 202 then
`GET /status` shows `run.status="failed"` + `run.error` at HTTP 200;
`pipeline.run` with an empty fake extractor completes `done`, with a raising
extractor stops `failed` at `extract`. A **full run over the real corpora
through HTTP** needs `ANTHROPIC_API_KEY` for extraction — same constraint as
Phases 4-10; the pipeline path is wired and unit-tested with fakes.
**Gate:** "upload PDFs and inspect results" is satisfiable through the API.
**Met.** 380 tests pass, ruff clean.

---

## PHASE 12 — UI  ✅ COMPLETE

**Objective:** static, framework‑free UI over the Phase‑11 API. No build step,
no framework, no dependency.
**Files shipped:** `app/static/index.html` (nav + `<main>` + footer, ~20 lines),
`app/static/style.css` (~160 lines, system font, CSS variables, tables + cards +
badges), `app/static/app.js` (~400 lines vanilla — `el()` DOM helper, `api()`
fetch wrapper that raises the `{error:{code,message}}` body, a hash router over
five views), `app/main.py` (`GET /` now serves the page via `FileResponse`;
`/static/*` mounted), `app/db.py` (`connect(check_same_thread=…)` — see fix),
`tests/test_ui.py` (5 tests).
**Views.**
- *Documents* — `<input type=file>` → `POST /documents`; a table row per document
  with status badge + `counts`; a **Process** button → `POST /documents/{id}/
  process` then polls `GET /documents/{id}/status` every 2 s, showing the stage
  and, on failure, the error, then refreshing.
- *Facts* — a filter bar (search/FTS `q`, doc id, `lifecycle_state`,
  `evidence_status`, `modality`, `type`, `reasoning_eligible`) + `?limit/offset`
  pager; each row expands (`GET /facts/{id}`) to the verbatim quote, the numeric
  representation, reporting period, scope, modality, the **verification detail**
  (`verification_method` / `numeric_rederivation` / notes / char range), the
  ±200‑char context window, and links to the fact's relationships.
- *Relationships* — category tabs (All + the five categories) over
  `GET /relationships`; each card shows the `category_label` badge, confidence,
  and **Fact A | Fact B** (claim + quote + doc/page); expanding
  (`GET /relationships/{id}`) shows the full deterministic‑signals table, then
  **LLM‑proposed → final category (validation_action)** + `validation_notes`,
  the reasoning sentence, and both evidences with their context windows.
- *Failures* — `GET /failures` as a table: `failure_type` badge, `reason`, ref,
  document, and the linked QUARANTINED fact or the linked relationship.
- *Entities* (secondary) — `GET /entities` list; each row expands to aliases
  (with `match_method` and any `source_fact_id`) and sample facts.
**Server fix (surfaced by the UI smoke).** Under real `uvicorn` (a threadpool),
Starlette can run a sync dependency's teardown on a different thread than its
setup, so `get_conn`'s `conn.close()` hit sqlite's thread check. `db.connect`
gained an opt‑in `check_same_thread` kwarg (default `True` — every existing call
unchanged); the API's `get_conn` passes `False` (one connection per request, used
sequentially). Not a schema change.
**Acceptance (met):** `uvicorn app.main:app` → open `http://localhost:8000/`;
all five views render with **no console errors and no server exceptions**
(verified headless with jsdom driving every route + a fact expansion + a
relationship expansion against a live seeded API); a reviewer can upload a PDF,
watch `processing → done|failed`, open a relationship and see both quotes, the
signals table, and the proposed‑vs‑final category with the override note. The
page names no entity, figure, or filename — every view is a live API read.
`tests/test_ui.py`: `GET /` serves `index.html`; the two assets have the right
content types; every path `app.js` calls exists in the OpenAPI schema; `node
--check app/static/app.js` parses; `app/static/` holds exactly the three files
(no build artifacts). 385 tests pass, ruff clean.
**Gate:** the four demo cases are visible in the UI with no hard‑coded examples —
the Relationships view + the Failures view render whatever the pipeline produced.
**Met** (fully populated cases need a real processed corpus — Phase 13).

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
