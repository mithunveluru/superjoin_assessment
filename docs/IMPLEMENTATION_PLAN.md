# Implementation Plan (revised)

Phased. **Do not start a phase until the previous phase's gate passes.** Each
phase ends with runnable verification. Tests are `pytest`, plain functions, using
small **synthetic** PDF fixtures — no fixture frameworks, no mocking of our own
code. (The provided starter PDFs are used for manual smoke tests only and are not
committed.) Assertions are **structural properties**, never hard‑coded answers
(see EVALUATION_PLAN).

Revised phase order (per review): evaluation harness now lands **before** the API
so retrieval/threshold tuning is evidence‑driven.

**Status:** Phase 0 ✅ · Phase 1 ✅ · Phase 2 ✅ · Phase 3 ✅ · Phase 4 ✅ · Phase 5 ✅ · Phase 6 ✅ · Phase 7 ✅ · Phase 8 not started.

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
