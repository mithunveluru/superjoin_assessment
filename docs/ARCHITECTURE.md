# Architecture

Companion docs: [DATA_MODEL.md](DATA_MODEL.md), [API_DESIGN.md](API_DESIGN.md),
[DECISIONS.md](DECISIONS.md).

---

## 1. Core abstraction — what is a "fact"

Starting hypothesis **CLAIM + CONTEXT + EVIDENCE** holds for both corpora. Refined:

```
FACT = CLAIM      (subject, predicate, object)
     + CONTEXT    (numeric representation, unit, currency, reporting period,
                   scope, qualifiers, modality)
     + PROVENANCE (document, publisher, disclosure type,
                   document date, publication date, data vintage)
     + EVIDENCE    (page, verbatim quote, char span, verification method)
     + LIFECYCLE   (state, evidence_status, reasoning_eligible)
     + REPRO       (model, prompt version, temperature, run id, raw extraction)
```

- **subject** — entity surface string + resolved `entity_id` (nullable).
- **predicate** — short noun phrase as written ("revenue from operations", "board role", "former name"), plus a lowercased normalized form for blocking. No controlled vocabulary — the corpus defines predicates.
- **object** — for `fact_type='numeric'`, the full representation is kept: `value_raw` string, `numeric_value`, `magnitude` + `magnitude_factor`, `base_value`, `currency`, `is_percentage` + `percentage_ratio`, `unit_raw` + `unit_norm` (DATA_MODEL §"Numeric representation"). For `semantic`, `value_text`. The original text is always recoverable; comparison uses `base_value` + unit/currency compatibility.
- **context** is first-class and never collapsed into the claim:
  - reporting period: `reporting_period_start` / `_end` (half-open ISO) + `_raw` + `_type` (`instant|quarter|half_year|fiscal_year|calendar_year|range|unknown`)
  - `scope` — JSON key/values, open set: `{basis: consolidated|standalone, segment, geography, counting_basis, measure_adjustment: reported|adjusted|pro_forma|restated, ...}`
  - `qualifiers` — JSON string array ("restated", "advance estimate", "pro forma")
  - `modality` — controlled set `ASSERTED | HISTORICAL | ESTIMATED | FORECAST | TARGET | UNCERTAIN`. Facts of different modality are **not** compared as contradictory by default (a forecast that differs from a later actual is not a contradiction).
- **provenance** is four separate concepts on the document + fact: the fact's **reporting period**, the document's **document date**, its **publication date**, and its **data vintage / revision** ("first advance estimate", "provisional", "second revised estimate"). Two reports can describe the same reporting period with different vintages — see §4.7.
- **lifecycle** — `CANDIDATE → GROUNDED → NORMALIZED → ELIGIBLE_FOR_REASONING`, or `QUARANTINED` (terminal, inspectable). Only `reasoning_eligible=1` facts enter relationship reasoning. Failed facts are kept, never deleted (§7).
- **evidence** is a separate row (§4.3), 1:1 with the fact from `GROUNDED` on; it records *how* it was verified, not a made-up score (§4.8).
- **repro** — every LLM-derived fact links to its verbatim `raw_extractions` row and carries model / prompt version / temperature / run id.

This models numeric facts (₹8,142 crore revenue, FY24, consolidated, HISTORICAL)
and semantic facts (a director's board role and later resignation, each with its
own effective period and modality; "Delhivery" formerly "SSN Logistics Private
Limited") with the same schema.

---

## 2. Architecture options

| | A. FastAPI + Postgres + pgvector + Next.js | B. FastAPI + Postgres + light retrieval + Next.js | **C. FastAPI monolith + SQLite + in-process retrieval + static UI** |
|---|---|---|---|
| Complexity | High — 3 services, Docker Compose, pgvector build | Medium-high | **Low — one Python process, one file DB** |
| Dev time | Slowest | Medium | **Fastest** |
| Explainability | Same reasoning core | Same | **Same** |
| Scalability | Best (100s of docs, concurrent) | Good | Fine to ~10³ docs / ~10⁵ facts, then migrate |
| Local setup | `docker compose up`, DB migrations, `npm i && npm build` | Similar minus pgvector | **`pip install -r requirements.txt` + `uvicorn` + one API key** |
| Retrieval | pgvector ANN + SQL | SQLite/`sqlite-vec` or numpy | **numpy brute-force cosine + SQLite FTS5** |
| Evidence storage | Postgres rows | Postgres rows | **SQLite rows (+ page text kept)** |
| Suitability | Over-scoped for a 6-PDF prototype scored on clarity | Middle | **Matches "small, understandable prototype > large unclear system"** |
| Main trade-off | Infra tax, more moving parts to explain | Still two runtimes (Node) | Single-process concurrency; brute-force retrieval has a ceiling — both acceptable now, both have clean upgrade paths |

**Chosen: C.** Rationale in [DECISIONS.md](DECISIONS.md) D1–D4. The reasoning
engine (the interesting part) is identical across all three; C removes everything
that isn't the reasoning engine. Postgres/pgvector and a JS frontend are the
documented upgrade path, not a rewrite — the data model and API contract are
storage-agnostic.

Not used, and why: microservices / queues (one process handles ingest→reason fine
at this scale; a background task covers async processing), graph DB (relationships
are a table with two FKs; "a graph DB or visualization alone is not the solution"),
Kubernetes/infra (local prototype).

---

## 3. System architecture

```
             ┌────────────────────────── FastAPI process ──────────────────────────┐
 upload  ──▶ │ /documents ─▶ file store (uploads/)  ─▶ documents row               │
 (PDF)       │                                                                     │
 process ──▶ │ BackgroundTask pipeline (each stage owns ONE lifecycle move):         │
             │   ingest    ─▶ pages(+text,+offsets) ─▶ chunks                        │
             │   extract   ─▶ raw_extractions (verbatim) ─▶ facts @CANDIDATE         │
             │   ground    ─▶ evidence(verification_method) ─▶ @GROUNDED | QUARANTINE│
             │   normalize ─▶ numeric repr / period / unit  ─▶ @NORMALIZED  [determ.]│
             │   resolve   ─▶ entities (block+score, LLM only if ambiguous)          │
             │              backfill subject_entity_id       ─▶ @ELIGIBLE_FOR_REASON │
             │   retrieve  ─▶ candidate pairs (config weights) + signals.compute()   │
             │   reason    ─▶ LLM proposes ▶ signals.validate() decides ▶ relationship│
             │   all writes ▶ SQLite; every failure ▶ failures table                 │
             │                                                                     │
 inspect ──▶ │ GET /facts /relationships /entities /failures  ─▶ static HTML UI    │
             └─────────────────────────────────────────────────────────────────────┘
```

### 4.1 Document ingestion — PDF → document → pages → text → chunks

- **Parser:** **PyMuPDF** (`fitz`) for both metadata and per-page text/geometry.
  One `pages` row per PDF page: `page_index` (0-based sequence), `printed_label`
  (running-header/margin heuristic, may be null), `text`, `width`, `height`,
  `extraction_method` (`text_layer` | `ocr` | `mixed` | `empty`), `char_count`.
- **Provenance capture:** `document_date`, `publication_date`, `data_vintage`,
  `publisher`, `disclosure_type` extracted best-effort (cover + running header +
  common phrasings), all nullable; `fy_convention` + `fy_convention_source`
  detected from "financial year ended <Month> <day>" style phrasing, else config
  default.
- **Page boundaries & positions:** the page's `text` is the unit of truth. Every
  evidence span is `(pdf_page_index, char_start, char_end)` into *that page's*
  `text`. Because we never concatenate pages, offsets are stable and a quote is
  always `page.text[char_start:char_end]`.
- **OCR fallback:** if `char_count < OCR_MIN_CHARS` (config, default 100) for a
  page, render it (`pdf2image`) and run `pytesseract`; store the OCR text, set
  `extraction_method=ocr`. Not run otherwise. `ponytail: page-level trigger, add
  layout-aware table extraction if table facts prove weak.`
- **Chunking (for the LLM only):** sliding window over a page's text,
  `CHUNK_CHARS` ≈ 3–4k with `CHUNK_OVERLAP` ≈ 400, **never crossing a page
  boundary**. Each chunk carries `(document_id, pdf_page_index, char_offset)` so
  the LLM's quote offsets can be mapped back to absolute page offsets. Table-dense
  pages: include the first ~15 lines of the page (header rows) as a prefix on
  every chunk from that page so column headers stay attached.

### 4.2 Candidate extraction (interpretation is a *separate* later step)

Extraction produces **candidate facts only** — "what claims appear to exist?".
Grounding (§4.3), normalization (§4.4), and entity linking (§4.5) are distinct
stages; no single mega-prompt does everything (correction J).

- **Input to LLM:** one page-bounded chunk + a fixed instruction + the structured
  schema. A short doc header (publisher, disclosure type, document date,
  `fy_convention`) lets the model resolve "FY24"/"the Company" — generic metadata.
- **Prompt asks for, per claim:** subject / predicate / object **exactly as
  written**; the numeric token as written; unit; currency; the period phrase as
  written; scope words (consolidated, standalone, segment, adjusted, per-quarter…);
  qualifiers; **modality** (`ASSERTED|HISTORICAL|ESTIMATED|FORECAST|TARGET|
  UNCERTAIN`); the **minimal verbatim quote** + its start offset in the provided
  text; `context_complete` (was subject+period unambiguous here). Corpus-agnostic:
  it describes *how* to read a fact, never *which* facts.
- **Store raw first:** the model's structured response is written verbatim to
  `raw_extractions` before any transformation (repro / debugging / prompt
  iteration — correction K). Then parsed with Pydantic into `facts` at
  `lifecycle_state=CANDIDATE`; malformed items → `failures(extraction_unparsed)`,
  counted, not dropped silently.
- **Determinism:** `temperature=0`, bounded `max_tokens`, `prompt_version` and
  model recorded on every fact and run.
- **Semantic vs numeric:** same call. A number+unit ⇒ `numeric` (numeric-repr
  columns filled in §4.4); otherwise `value_text`. Modality/qualifiers apply to
  both.

### 4.3 Evidence grounding (the hard gate)

Grounding attaches exactly one `evidence` row and moves the fact to `GROUNDED`
or `QUARANTINED`. It records **how** verification went, not an invented score
(correction B).

| field | meaning |
|---|---|
| `document_id`, `page_index`, `printed_label` | where |
| `char_start`, `char_end` | span into `pages.text`; equals `quote` when method is `exact`/`normalized_exact` |
| `quote` | verbatim supporting span |
| `verification_method` | `exact` \| `normalized_exact` (whitespace/unicode-folded match) \| `fuzzy` (`rapidfuzz.partial_ratio ≥ 92`, `fuzzy_score` stored) \| `unverified` |
| `numeric_rederivation` | `not_applicable` \| `success` \| `failed` — did `normalize.parse_number(quote)` reproduce the extracted number |
| `evidence_status` | `VERIFIED` \| `PARTIAL` \| `UNVERIFIED`, derived per the DATA_MODEL table |
| `notes` | human-readable "why", shown in the UI |
| `evidence_score` | **optional**, only if computed from explicit signals; may be null |

`evidence_status=UNVERIFIED` (or `fuzzy`+`failed`) ⇒ `quarantine(grounding_failed)`
— the fact stays fully inspectable via `failures` / `GET /failures`, and can
never become `reasoning_eligible` (schema CHECK). The UI shows the quote, the
page, and ±200 chars of surrounding `pages.text` for audit.

### 4.4 Normalization — full numeric representation, nothing collapsed

Deterministic. **Raw is never overwritten**; every normalized field has a
`*_raw` sibling (correction E). Moves the fact to `NORMALIZED`.

- **Numbers → all pieces kept:** `value_raw` (`"₹8,142 crore"`), `numeric_value`
  (`8142`), `magnitude`/`magnitude_factor` (`"crore"`/`1e7`), `base_value`
  (`numeric_value·factor`), `currency`, `is_percentage`+`percentage_ratio`
  (`"5%"` → `0.05`), `unit_raw`/`unit_norm`. `(x)`→ negative; `bps`→ ratio.
  Worked table in DATA_MODEL. Comparison later uses `base_value` + unit/currency
  compatibility; **display/audit always uses `value_raw`**.
- **Currencies:** ₹/Rs/INR/US$/USD → code. No FX conversion — cross-currency
  facts are simply not unit-equivalent.
- **Periods:** `parse_period(raw, document.fy_convention)` → `reporting_period_
  start`/`_end` (half-open ISO) + `_type`. Handles `FY24`, `FY 2023-24`,
  `Fiscal 2021`, `FY2025/26` (slash form ⇒ year *ending* in the later calendar
  year, **only** under a resolved convention), `Q3 FY24`, `nine months ended
  December 31, 2021`, `as on March 31, 2024`, calendar years. `FY25` and
  `FY2025/26` are **not assumed equal** — each is resolved through its own
  document's convention first (correction F). Unparseable ⇒ `unknown`,
  `failures(normalization_failed)`, fact not promoted.
- **Units:** tonnage→tonne, area→sqft, counts→integer; `unit_raw` kept.
- **Never normalized away:** consolidation basis, adjusted/reported, segment,
  counting basis, geography, modality, vintage — these live in
  `scope`/`qualifiers`/`modality`/`data_vintage` and are inputs to §4.7, never
  dropped to force a match.

### 4.5 Entity resolution (no hard-coded aliases)

1. Gather every distinct `subject` string with one short context snippet each.
2. **Blocking:** normalize (lowercase, strip legal suffixes from a *generic* list
   — Ltd/Limited/Inc/LLP/Pvt/PLC/Corporation…, strip honorifics/initials for
   person-like strings). Candidate pairs where `rapidfuzz.token_set_ratio ≥ 88`
   **or** cosine(embedding of "name — snippet") ≥ 0.82.
3. **Cluster:** union-find over surviving pairs.
4. **Confirm:** one LLM call per cluster — "are these the same real-world entity?
   give a canonical label + type (org|person|place|metric|other) + confidence;
   split if not." The LLM only *names/validates* clusters the deterministic step
   proposed; it does not free-scan the corpus.
5. Persist `entities` + `entity_aliases(surface, entity_id, confidence, source)`.
6. **Rename chains** ("incorporated as 'SSN Logistics Private Limited' … name
   changed to 'Delhivery Limited'") are themselves extracted facts
   (`predicate=former name`); a post-step turns each into an alias edge with
   `source=derived_fact`. The corpus, not the code, supplies the alias.

The legal-suffix and honorific lists are **generic language config**, not
dataset-specific mappings (DECISIONS D8).

### 4.6 Candidate retrieval — config-driven, not hard-coded weights

Only `reasoning_eligible=1` facts are retrieved against each other. Per fact,
without O(n²):

1. **Block** on entity: same `entity_id`, alias-linked, or same
   `normalization_key`.
2. Rank others in the block by
   `w_e·cos(fact) + w_p·cos(predicate) + w_b·bm25(subject,predicate,object_raw)`.
   `w_e, w_p, w_b, top_k, candidate_threshold, predicate_similarity_threshold`
   all come from `Settings` (`FKL_RETRIEVAL_*`). The Phase-0 values
   (0.5/0.3/0.2, K=15, 0.55, 0.6) are **starting defaults**; the Phase-10
   evaluation harness sweeps them and the chosen values are recorded in
   `.env.example` with a comment citing the tuning table (correction H).
3. Keep top-`K` ≥ `candidate_threshold`. Skip pairs that are neither
   unit-equivalent nor predicate-similar (nothing to reason about).
4. Ordered `(min(id),max(id))` dedup; skip pairs already scored (idempotent).

Embeddings: `fastembed` (`BAAI/bge-small-en-v1.5`, ONNX, offline). float32 `BLOB`
on `facts`; in-memory numpy matrix per pass. `ponytail: brute-force cosine; swap
sqlite-vec/pgvector past ~1e5 facts.`

### 4.7 Relationship reasoning — LLM proposes, deterministic logic decides

**Categories (internal):** `CORROBORATES · CONTRADICTS · DIFFERENT_CONTEXT ·
TEMPORAL_EVOLUTION · UNCERTAIN`. The UI renders `DIFFERENT_CONTEXT` as
"Reconciled by context". **Different dates do not imply a category** — they may
mean genuine temporal evolution (correction C).

**Step 1 — deterministic signals** (`signals.compute(a,b)`, always):

| signal | how |
|---|---|
| `entity_match` | 1.0 same `entity_id` · 0.7 alias-linked · fuzzy score otherwise |
| `predicate_sim` | cosine of predicate embeddings |
| `unit_equivalent` | comparable after normalization (crore↔million yes; %↔ratio yes; INR≠USD; %≠count) |
| `base_value_delta_pct` | `|a−b| / max(|a|,|b|)` on `base_value`, only when `unit_equivalent` |
| `period_relation` | equal · overlaps · contains · disjoint · unknown (on `reporting_period_*`) |
| `scope_conflict` | scope keys present in both with differing values |
| `modality_pair` | e.g. `(HISTORICAL,HISTORICAL)`, `(HISTORICAL,FORECAST)` |
| `publication_gap_days` | `|publication_date_a − publication_date_b|` |
| `vintage_differs` | `data_vintage_a != data_vintage_b` |

**Step 2 — LLM proposal.** For pairs worth interpreting, `classify_relationship`
gets a **structured context packet**: Fact A, Fact B, both verbatim quotes, both
reporting periods, both publication dates + vintages, both scopes, both units,
both modalities, and all signals. It returns structured
`{category, context_dimension, reasoning, confidence}`. `temperature=0`,
`prompt_version` recorded. **It never does arithmetic or date math** — those are
in the packet.

**Step 3 — deterministic validation** (`signals.validate(proposed, signals)`) —
the LLM does not have the final say (correction I):

- `unit_equivalent` + `base_value_delta_pct ≤ tol` + `period_relation=equal` + no
  `scope_conflict` ⇒ force **CORROBORATES** (override a proposed CONTRADICTS on
  numbers that are equal after normalization).
- both `HISTORICAL`/`ASSERTED`, same entity+predicate, **disjoint** periods,
  values differ ⇒ prefer **TEMPORAL_EVOLUTION** over CONTRADICTS.
- `scope_conflict ≠ ∅` that plausibly explains the gap ⇒ prefer
  **DIFFERENT_CONTEXT**, `context_dimension` = the scope key.
- `modality_pair` mismatched (e.g. FORECAST vs HISTORICAL) ⇒ never CONTRADICTS;
  DIFFERENT_CONTEXT or UNCERTAIN.
- `vintage_differs` + same reporting period ⇒ TEMPORAL_EVOLUTION (revision) or
  DIFFERENT_CONTEXT (`context_dimension=vintage`).
- `entity_match` < threshold, or `period_relation=unknown`, or either fact
  `PARTIAL` with weak signals ⇒ **UNCERTAIN**.

Persist one `relationships` row with `llm_proposed_category`, final `category`,
`validation_action` (`accepted`/`overridden`/`downgraded`), `validation_notes`,
`deterministic_signals` (JSON), `context_dimension`, `reasoning`, `confidence`
(capped when a side is `PARTIAL`), `model_name`, `prompt_version`.

### 4.8 Verification & confidence — describe, don't fake

No single blended score is stored (correction B, DECISIONS D9). What is stored:

- **evidence:** `verification_method` (`exact | normalized_exact | fuzzy |
  unverified`), `numeric_rederivation` (`not_applicable | success | failed`),
  derived `evidence_status` (`VERIFIED | PARTIAL | UNVERIFIED`), human-readable
  `notes`, and an **optional** `evidence_score` only when it is a function of
  those explicit signals.
- **entity match:** `entities.resolution_score` / `llm_confidence`; per fact,
  whether `subject_entity_id` is set and confirmed.
- **extraction quality:** not a number — `lifecycle_state`, `context_complete`,
  and any `failures` rows describe it.
- **relationship:** `confidence` in `[0,1]` computed from **explicit signal
  agreement** × (LLM confidence if `llm_used` else a fixed prior) × grounding
  factor (`PARTIAL` side caps it); the formula and its constants live in config
  so the eval harness can tune them. `deterministic_signals` + `validation_action`
  are shown alongside so a reviewer sees *why*.

The UI states, in words, why evidence counts as VERIFIED/PARTIAL and shows the
signal table — not just a bar.

### 4.9 Persistence

SQLite (`data/knowledge.db`), WAL. Full DDL, keys, indexes, CHECKs in
[DATA_MODEL.md](DATA_MODEL.md). Tables: `documents`, `pages`, `chunks`, `runs`,
`raw_extractions`, `entities`, `entity_aliases`, `facts`, `evidence`,
`relationships`, `failures`. Vectors: `facts.embedding` / `predicate_embedding`
`BLOB`. Full-text: `facts_fts` (FTS5, plain, populated by the persistence layer —
no triggers).

### 4.10 API

FastAPI. Contract in [API_DESIGN.md](API_DESIGN.md). Summary:
`POST /documents`, `GET /documents`, `GET /documents/{id}`,
`POST /documents/{id}/process`, `GET /documents/{id}/status`,
`GET /facts`, `GET /facts/{id}`, `GET /relationships`, `GET /relationships/{id}`,
`GET /entities`, `GET /entities/{id}`, `GET /failures`.

### 4.11 UI

Static HTML + vanilla JS + one CSS file, served by FastAPI at `/`. No build step,
no framework (DECISIONS D3). Pages:

1. **Documents** — drag/drop upload, list with status badge
   (`uploaded → processing → done/failed`), per-doc counts, "Process" button,
   status polled every 2 s.
2. **Facts** — filterable table (document, entity, predicate, type,
   `lifecycle_state`, `evidence_status`, `modality`, `reasoning_eligible`,
   free-text `q`); row expands to: verbatim quote, document + page (printed label
   + PDF index), full numeric representation (`value_raw` → `base_value`),
   reporting period, scope, qualifiers, modality, and the verification detail
   (`verification_method`, `numeric_rederivation`, `notes`).
3. **Relationships** — cards grouped by category (`DIFFERENT_CONTEXT` labelled
   "Reconciled by context"). Each card: **Fact A** and **Fact B** side by side
   (subject · predicate · `value_raw` · reporting period · scope · publication
   date/vintage · source doc/page · verbatim quote), the category badge, the
   deterministic signals table, **LLM-proposed category vs final category +
   `validation_notes`**, the `reasoning` text, the relationship confidence with
   its signal breakdown. Satisfies "source evidence and system reasoning must be
   visible".
4. **Failures** — every `failures` row (quarantined extractions, incomplete
   context, OCR pages, normalization failures, ambiguous entities, uncertain
   relationships, run errors), each with its `failure_type` and `reason`.

Clarity over polish: system font, no JS framework, ~150 lines of CSS.

---

## 5. AI / deterministic boundary

| Concern | Owner | Note |
|---|---|---|
| PDF parse, page/offset capture | deterministic | PyMuPDF |
| OCR trigger & run | deterministic | threshold on char count |
| Chunking | deterministic | page-bounded window |
| **Candidate extraction (claim + context words + quote)** | **LLM** | structured output; "what claims exist"; raw response stored |
| Numeric representation (value/magnitude/base/currency/ratio) parsing | deterministic | regex + magnitude map |
| Period / FY / quarter / range parsing | deterministic | rule-based; per-document `fy_convention` |
| Evidence verification (`verification_method`) | deterministic | substring / fold / fuzzy locate — the hard gate |
| Numeric re-derivation check | deterministic | `parse_number(quote)` must reproduce the number |
| Lifecycle transitions & quarantine | deterministic | one stage owns each move |
| Embeddings | model (local ONNX) | not an LLM call; no API key |
| Entity blocking + similarity scoring + merge of unambiguous pairs | deterministic | fuzzy + cosine |
| **Entity confirmation for ambiguous clusters only** | **LLM** | may split; generic prompt |
| Candidate retrieval (block, rank, top-K) | deterministic | cosine + BM25, **config weights** |
| Signal computation (delta, period relation, unit equivalence, scope conflict, vintage, modality pair) | deterministic | the "math" |
| **Relationship interpretation — proposed category + reasoning** | **LLM** | structured packet in; semantic judgement only |
| **Relationship final category — validation of the LLM proposal** | deterministic | `signals.validate()` can override/downgrade |
| Confidence value | deterministic | explicit-signal formula; constants in config |

Principle: **the LLM proposes a semantic interpretation; deterministic logic
validates factual consistency and has the final say.** Every LLM output —
extraction, entity confirmation, relationship proposal — is checked by
deterministic code (schema validation, evidence gate, `signals.validate()`)
before it is trusted or persisted as final.

---

## 6. Generalization

- No code path references a dataset name, filename, publisher, or expected value.
  `grep -ri 'delhivery\|macroeconom\|economic survey\|<any starter entity>'
  app/ evaluation/` must return nothing outside `tests/` / `evaluation/cases/`.
- All tunables (`fy_convention_default`, legal-suffix list, honorific list, unit
  map, retrieval weights + thresholds, numeric tolerances, `ocr_min_chars`,
  models, prompt versions) live in `app/config.py` + `.env` as **generic**
  configuration. Retrieval weights/thresholds are *chosen by the evaluation
  harness*, not asserted (correction H).
- Prompts describe the *shape* of a fact / a relationship packet, never specific
  entities or figures.
- New PDF → upload → process runs the identical pipeline. New predicates, scope
  keys, and `data_vintage` values are just strings/JSON — no migration.
  Incremental: a new document's run only scores new-fact × existing-eligible
  pairs (`runs` + pair-dedup make it idempotent; no global rebuild).
- Tests assert **structural properties** (EVALUATION_PLAN), not hard-coded
  answers. The "director active vs resigned" shape is **not** pre-labelled a
  contradiction — the system classifies it from dates + modality (correction 3).

---

## 7. Failure handling

Nothing is discarded. Every failure writes a `failures` row (`failure_type`,
`ref_table`/`ref_id`, machine-readable `reason`) and stays inspectable via
`GET /failures`.

| Failure | Representation | Behaviour |
|---|---|---|
| OCR / garbled page | `pages.extraction_method=ocr`; `failures(ocr_page)` | evidence `method=ocr`; a `fuzzy`/`normalized_exact` match yields at best `PARTIAL` |
| Table cell w/o header | fact `qualifiers+=["table_cell_unlabeled"]`, `context_complete=0` | may still ground; flagged; reduced relationship confidence |
| Quote not in page | `evidence.verification_method=unverified`, `evidence_status=UNVERIFIED` | fact → `QUARANTINED`, `failures(grounding_failed)`; never `reasoning_eligible` |
| Number not re-derivable from quote | `numeric_rederivation=failed` | `PARTIAL` at best; if method also `fuzzy` → `QUARANTINED` |
| Ambiguous period/subject | `context_complete=0`; `failures(context_incomplete)` | not promoted to `ELIGIBLE_FOR_REASONING` unless disambiguated |
| Malformed LLM output | `raw_extractions.parse_error`; `failures(extraction_unparsed)` | verbatim response retained for debugging |
| Normalization failure | period/number `unknown`; `failures(normalization_failed)` | fact stays `GROUNDED`, not eligible |
| Uncertain entity match | `subject_entity_id=NULL`; `failures(entity_ambiguous)` | cross-entity pair → `UNCERTAIN` |
| Genuine conflict | `relationships.category=CONTRADICTS` | surfaced, not hidden — a required output |
| Weak/insufficient signals | `relationships.category=UNCERTAIN` + `failures(relationship_uncertain)` | shown with its reason |

Global rule: when entity, reporting period, scope, or modality cannot be
established, the system emits **UNCERTAIN / quarantined**, never a confident
guess.

---

## 8. Must-have now vs future extension

**Must have now**

- Upload + async process via API/UI, incremental (no rebuild)
- Page-preserving ingest with char offsets; four provenance concepts captured
- Candidate extraction (structured output) with verbatim `raw_extractions`
- Evidence grounding gate → lifecycle + quarantine, all failures inspectable
- Deterministic full numeric representation + period/unit normalization
- Entity resolution (deterministic + LLM only for ambiguous)
- Config-driven candidate retrieval + deterministic signals
- Relationship reasoning: LLM proposes → `signals.validate()` decides →
  5 categories incl. `TEMPORAL_EVOLUTION`
- Evaluation harness with expected-property specs (before the API)
- UI showing facts, evidence + verification detail, relationships + proposed-vs-
  final category, failures
- The four required cases demonstrable from the starter data

**Future extension (designed for, not built)**

- Large PDFs: pipeline is already page-parallel; move `BackgroundTask` → a worker
  (RQ/Celery) and batch LLM calls.
- Many documents: swap SQLite→Postgres (schema unchanged), numpy cosine→pgvector
  or `sqlite-vec` ANN.
- Evolving schema: `scope`/`qualifiers` are JSON; new predicates need no DDL. A
  `predicate_catalog` view can be added for UI grouping without touching writes.
- Incremental ingestion: processing doc N only computes relationships for
  new-fact × existing-candidate pairs; `runs` + pair-dedup already make
  re-processing idempotent. No global rebuild.
- Better tables: plug a layout/table extractor before chunking; evidence model
  already supports bbox.

---

## 9. Security & configuration

- **Secrets:** `GEMINI_API_KEY` only, read from env. `.env` git-ignored;
  `.env.example` committed with placeholders. No key in code, logs, or the DB.
- **Upload validation:** MIME + magic-byte check (`%PDF-`), extension check,
  `MAX_UPLOAD_MB` (default 25), page-count cap (`MAX_PAGES`, default 300),
  reject encrypted PDFs with a clear 400. Store under `uploads/<uuid>.pdf`;
  original filename kept only as a display string (never used for logic).
- **Resource limits:** per-document `max_chunks_per_doc` and
  `max_llm_calls_per_doc` guard cost; pipeline aborts with a recorded `run.error`
  if exceeded. LLM client `temperature=0`, timeout + bounded retries.
- **Reproducibility:** every LLM-derived row carries model, `prompt_version`,
  temperature, `run_id`; `runs.settings` snapshots thresholds; `raw_extractions`
  keeps the verbatim response.
- **Isolation:** PDF handling in-process (PyMuPDF); only shell-out is `tesseract`
  with fixed args on a validated path. SQLite parameterized queries only.
- **PII:** documents are public filings; still, no external calls except the LLM
  API and no telemetry.
