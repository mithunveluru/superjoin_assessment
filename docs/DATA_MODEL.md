# Data Model

SQLite (`data/knowledge.db`), WAL, `foreign_keys=ON`. Timestamps ISO‑8601 UTC
text. JSON columns are `TEXT` holding a JSON value. `CHECK` constraints encode the
controlled vocabularies so bad states fail at the storage layer.

Phase 4 (candidate extraction) adds **no schema change** — it fills existing
`facts` / `evidence` / `raw_extractions` / `runs` / `failures` columns; see
*Phase 4 persistence semantics* below.

This document is **authoritative for the current schema** — the Phase 1 genesis
schema (`app/schema.sql`, `user_version 0`) plus migrations in
`app/migrations/` (Phase 2 migration 1, Phase 3 migration 2); see the *Schema
migrations* section at the end. Column names may be adjusted in implementation
but the shape, constraints, and invariants are fixed. The write/validate layer is
`app/facts.py` (`insert_fact`, `attach_evidence`, `mark_reasoning_eligible`,
`quarantine_fact`, `set_lifecycle`, `add_relationship`, `evidence_chain`), with
input DTOs `FactIn` / `EvidenceIn` / `RelationshipIn` in `app/models.py`.

## Design principles (post‑review)

- **One canonical fact model** for numeric, semantic, temporal, and categorical
  facts (`fact_type`). The numeric-representation columns are simply unused for
  non-numeric facts — there are not two fact systems.
- **Explicit fact lifecycle**, not a boolean. `lifecycle_state` ∈
  `RAW → CANDIDATE → GROUNDED → NORMALIZED → ELIGIBLE_FOR_REASONING`, with
  `QUARANTINED` as a terminal side state. Failed facts are **kept and
  inspectable**, never deleted.
- **Verification is described, not scored.** `evidence.verification_method`
  (`exact | normalized_exact | fuzzy | unavailable | unverified`) + `evidence_status`
  (`VERIFIED | PARTIAL | UNVERIFIED`) + `numeric_rederivation`
  (`not_applicable | success | failed`) are the primary record. `evidence_score`
  is optional and only set when derived from explicit measurable signals.
- **Numeric facts keep every representation** (raw string, parsed number,
  magnitude word + factor, base value, currency, percentage ratio, raw + norm
  unit). The original text is always recoverable.
- **Provenance is four separate concepts:** reporting period, document date,
  publication date, data vintage/revision.
- **Modality is a controlled set:** `ASSERTED | HISTORICAL | ESTIMATED |
  FORECAST | TARGET | UNCERTAIN`. Facts of different modality are not compared as
  contradictory by default.
- **Raw LLM output is preserved** (`raw_extractions`) before transformation.
- **Reproducibility metadata** (model, prompt version, temperature, settings,
  run id) is stored on every LLM‑derived row.
- **Retrieval weights/thresholds are config, not schema** — see
  [ARCHITECTURE.md](ARCHITECTURE.md) §4.8 and [DECISIONS.md](DECISIONS.md) D19.
- Raw is always kept beside normalized; context (period, unit, scope, modality)
  is queryable columns, not prose; new predicates/scope keys need no migration.

## Invariants (enforced by `CHECK` where cheap, by tests otherwise)

1. `lifecycle_state='QUARANTINED'` ⇒ `reasoning_eligible=0`. *(CHECK)*
2. `reasoning_eligible=1` ⇒ `evidence_status != 'UNVERIFIED'`. *(CHECK)*
3. `relationships.fact_a_id < fact_b_id`. *(CHECK)* — canonical (undirected) pair
   order; also forbids self-relationships.
4. Only facts with `reasoning_eligible=1` may appear in `relationships`.
   *(enforced by `add_relationship` — raises `not_reasoning_eligible` — + the eval
   guard test; not a CHECK)*
5. **Evidence traceability:** a fact reaches `ELIGIBLE_FOR_REASONING` only if
   `evidence_chain()` resolves FACT → EVIDENCE → (CHUNK →) PAGE → DOCUMENT **and**
   `evidence_status ∈ (VERIFIED, PARTIAL)`. *(enforced by
   `mark_reasoning_eligible`; the two CHECKs above are the storage-level backstop)*
6. `evidence`: `char_start IS NULL OR char_end IS NULL OR char_start < char_end`.
   *(CHECK)* — `attach_evidence` additionally rejects offsets outside
   `[0, len(pages.text)]` and unpaired start/end.
7. Every `fact` has at most one `evidence` row (`evidence.fact_id UNIQUE`);
   `attach_evidence` is insert-only in Phase 3 (updates arrive in Phase 5).

---

## documents

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| sha256 | TEXT UNIQUE NOT NULL | dedup identical uploads |
| stored_path | TEXT NOT NULL | `uploads/<uuid>.pdf` |
| original_filename | TEXT | **display only — never used in logic** |
| title | TEXT | PDF metadata / cover heuristic (Phase 2) |
| publisher | TEXT | extracted (Phase 2) |
| disclosure_type | TEXT | free text: "prospectus", "annual report", "staff report"… |
| document_date | TEXT | the date the document itself bears ("Dated May 14, 2022") |
| publication_date | TEXT | when released/filed (may differ from `document_date`) |
| data_vintage | TEXT | revision/series label if stated ("first advance estimate", "provisional", "second revised estimate") — nullable |
| fy_convention | TEXT | `apr-mar` \| `jan-dec` \| `jul-jun` \| `unknown` |
| fy_convention_source | TEXT | `detected` \| `config_default` \| `override` |
| file_size | INTEGER | bytes on disk *(migration 1, Phase 2)* |
| mime_type | TEXT | sniffed, e.g. `application/pdf` *(migration 1, Phase 2)* |
| page_count | INTEGER | |
| status | TEXT NOT NULL DEFAULT `uploaded` | CHECK ∈ (`uploaded`,`ingesting`,`ingested`,`processing`,`done`,`failed`) |
| status_detail | TEXT | last stage / error |
| uploaded_at | TEXT NOT NULL | |
| processed_at | TEXT | set by the full pipeline (Phase 11), not by ingestion |

Index: `documents(status)`. Ingestion sets `status`
`ingesting → ingested` on success, `→ failed` (with `status_detail`) otherwise.

## pages

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| document_id | INTEGER NOT NULL → documents(id) ON DELETE CASCADE | |
| page_index | INTEGER NOT NULL | 0‑based position in the file |
| printed_label | TEXT | page number as printed (may be null / roman / non‑monotonic) |
| text | TEXT NOT NULL DEFAULT '' | full extracted page text — the offset reference (Phase 2) |
| char_count | INTEGER NOT NULL DEFAULT 0 | |
| width | REAL | pts |
| height | REAL | pts |
| extraction_method | TEXT DEFAULT `text_layer` | CHECK ∈ (`text_layer`,`ocr`,`mixed`,`empty`) — how text was obtained (Phase 2 always `text_layer`) |
| extraction_status | TEXT NOT NULL DEFAULT `TEXT_EXTRACTED` | CHECK ∈ (`TEXT_EXTRACTED`,`LOW_TEXT`,`EMPTY`,`EXTRACTION_ERROR`) — **source-quality signal for later phases** *(migration 1)* |
| extraction_error | TEXT | per-page exception detail, if any *(migration 1)* |
| extraction_meta | TEXT (JSON) | `{block_count, image_count, text_density_per_kchar2, printed_label_candidate}` *(migration 1)* |

Unique: `pages(document_id, page_index)`.

`text` is stored **verbatim** from PyMuPDF `get_text("text")` — no lowercasing,
punctuation stripping, number/date/unit interpretation, or whitespace rewriting.
It is the single reference string that every evidence offset points into.
`LOW_TEXT`/`EMPTY` pages are also recorded in `failures` as `ocr_page` (a signal;
Phase 2 never runs OCR); `EXTRACTION_ERROR` pages as `run_error`.

## chunks

Deterministic, page-aware slices of `pages.text`. Never cross a page (or
document). Kept for audit + as future LLM input units.

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| document_id | INTEGER NOT NULL → documents(id) ON DELETE CASCADE | |
| page_index | INTEGER NOT NULL | |
| seq | INTEGER NOT NULL | chunk order within the page (0-based) |
| char_offset | INTEGER NOT NULL | **start** offset within `pages.text` |
| char_end | INTEGER | **end** offset within `pages.text` *(migration 1, Phase 2)* |
| header_prefix_len | INTEGER NOT NULL DEFAULT 0 | reserved for Phase 4 table-header prefixing; **0 in Phase 2** |
| text | TEXT NOT NULL | |

Unique: `chunks(document_id, page_index, seq)`. Index: `chunks(document_id, page_index)`.

**Offset invariant (Phase 2):** `header_prefix_len = 0` for every chunk, and
`pages.text[char_offset:char_end] == chunks.text` exactly. Consecutive chunks on
a page overlap by ≈ `FKL_CHUNK_OVERLAP_CHARS`; `char_offset` is strictly
increasing; the first chunk starts at 0 and the last ends at `len(pages.text)`.
When Phase 4 introduces a header prefix, the mapping becomes
`pages.text[char_offset:char_end] == chunks.text[header_prefix_len:]`.

## Schema migrations

`app/schema.sql` is the genesis schema (`PRAGMA user_version = 0`). Every change
after Phase 1 is a forward-only `app/migrations/NNNN_*.sql` file, listed in
`app/db._MIGRATION_FILES` and keyed by `user_version`; `init_db()` applies pending
ones in order and runs `PRAGMA foreign_key_check` after each. Fresh databases,
Phase-1, Phase-2, and fully-migrated databases all converge; re-running is a
no-op.

- **Migration 1** (Phase 2, `0001_phase2_ingestion.sql`) — `ADD COLUMN` only:
  `documents.file_size/mime_type`, `pages.extraction_status/error/meta`,
  `chunks.char_end`. Nullable-or-defaulted, no rewrite, no data loss.
- **Migration 2** (Phase 3, `0002_phase3_fact_evidence_model.sql`) — a CHECK
  change, so `facts` and `evidence` are rebuilt with the SQLite
  table-redefinition procedure (create new / `INSERT … SELECT` / drop / rename)
  under `PRAGMA foreign_keys=OFF` inside a transaction, `foreign_key_check`
  verified after. Widens `facts.fact_type` (2→4) and `facts.lifecycle_state`
  (+`RAW`), adds `facts.raw_payload`; adds `evidence.page_id/chunk_id`, the
  `char_start < char_end` CHECK, and `verification_method='unavailable'`. All
  `facts` / `evidence` indexes are recreated. Safe: both tables are empty until
  Phase 4.

## runs  — processing observability

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| document_id | INTEGER → documents(id) ON DELETE CASCADE | nullable (reasoning runs may span docs) |
| run_type | TEXT NOT NULL | CHECK ∈ (`ingest`,`extract`,`ground`,`normalize`,`resolve`,`retrieve`,`reason`,`full`) |
| status | TEXT NOT NULL DEFAULT `running` | CHECK ∈ (`running`,`done`,`failed`) |
| stage | TEXT | last completed stage |
| started_at | TEXT NOT NULL | |
| finished_at | TEXT | |
| pages_processed | INTEGER DEFAULT 0 | |
| chunks_processed | INTEGER DEFAULT 0 | |
| facts_extracted | INTEGER DEFAULT 0 | |
| facts_grounded | INTEGER DEFAULT 0 | |
| facts_quarantined | INTEGER DEFAULT 0 | |
| relationships_produced | INTEGER DEFAULT 0 | |
| llm_calls | INTEGER DEFAULT 0 | |
| estimated_cost_usd | REAL | best‑effort |
| model_name | TEXT | |
| prompt_versions | TEXT (JSON) | `{"extract":"v1","relationship":"v1","entity":"v1"}` |
| settings | TEXT (JSON) | snapshot of temperature, top_k, thresholds for reproducibility |
| error | TEXT | |

Index: `runs(document_id)`, `runs(status)`, `runs(run_type)`.

## raw_extractions  — verbatim LLM output, pre‑transformation

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| run_id | INTEGER NOT NULL → runs(id) ON DELETE CASCADE | |
| document_id | INTEGER NOT NULL → documents(id) ON DELETE CASCADE | |
| page_index | INTEGER NOT NULL | |
| chunk_id | INTEGER → chunks(id) ON DELETE SET NULL | |
| model_name | TEXT NOT NULL | |
| prompt_version | TEXT NOT NULL | |
| temperature | REAL | |
| request_settings | TEXT (JSON) | |
| raw_response | TEXT NOT NULL | the untransformed structured JSON string from the model |
| item_count | INTEGER | candidate facts parsed from it |
| parse_error | TEXT | non‑null if the response failed to parse |
| created_at | TEXT NOT NULL | |

Index: `raw_extractions(run_id)`, `raw_extractions(document_id, page_index)`.

Phase 4 writes **one row per chunk regardless of outcome** (a `run_error` chunk
still gets a row with `raw_response=''` and `parse_error` set). `temperature` is
`NULL` — Sonnet 5 rejects sampling params. `request_settings` carries
`{"stop_reason": …}`. `item_count` is `NULL` when the response did not parse.

## entities

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| canonical_label | TEXT NOT NULL | |
| entity_type | TEXT | CHECK ∈ (`org`,`person`,`place`,`metric`,`product`,`other`) |
| normalization_key | TEXT | deterministic normalized form used for blocking |
| resolution_method | TEXT | CHECK ∈ (`deterministic`,`similarity`,`llm_confirmed`) |
| resolution_score | REAL | similarity score at merge time |
| llm_confirmed | INTEGER NOT NULL DEFAULT 0 | |
| llm_confidence | REAL | |
| created_run_id | INTEGER → runs(id) ON DELETE SET NULL | |
| created_at | TEXT NOT NULL | |

Index: `entities(normalization_key)`, `entities(entity_type)`.

## entity_aliases

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| entity_id | INTEGER NOT NULL → entities(id) ON DELETE CASCADE | |
| surface | TEXT NOT NULL | as seen in a document |
| normalized | TEXT | blocking key |
| match_method | TEXT | CHECK ∈ (`deterministic`,`similarity`,`llm`,`derived_fact`) |
| score | REAL | |
| source_fact_id | INTEGER → facts(id) ON DELETE SET NULL | set when derived from a "former name"/"incorporated as" fact |
| created_at | TEXT NOT NULL | |

Unique: `entity_aliases(entity_id, surface)`. Index: `entity_aliases(normalized)`.

No dataset‑specific alias rows are ever seeded (DECISIONS D8/G).

## facts

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| document_id | INTEGER NOT NULL → documents(id) ON DELETE CASCADE | |
| page_index | INTEGER NOT NULL | |
| run_id | INTEGER → runs(id) ON DELETE SET NULL | |
| raw_extraction_id | INTEGER → raw_extractions(id) ON DELETE SET NULL | link to the chunk-level verbatim LLM output |
| **lifecycle_state** | TEXT NOT NULL DEFAULT `CANDIDATE` | CHECK ∈ (`RAW`,`CANDIDATE`,`GROUNDED`,`NORMALIZED`,`ELIGIBLE_FOR_REASONING`,`QUARANTINED`) *(migration 2 adds `RAW`)* |
| quarantine_reason | TEXT | required when `QUARANTINED` (layer‑enforced) |
| subject_raw | TEXT NOT NULL | entity string as written |
| subject_entity_id | INTEGER → entities(id) ON DELETE SET NULL | nullable until resolved |
| predicate | TEXT NOT NULL | short phrase as written |
| predicate_norm | TEXT NOT NULL DEFAULT '' | lowercased/lemmatized, for blocking |
| object_raw | TEXT NOT NULL | value/object as written |
| fact_type | TEXT NOT NULL | CHECK ∈ (`numeric`,`semantic`,`temporal`,`categorical`) *(migration 2 widens from 2 → 4)* |
| — numeric representation — | | *(all nullable; populated for `numeric`)* |
| value_raw | TEXT | e.g. `"₹8,142 crore"` |
| numeric_value | REAL | e.g. `8142` |
| magnitude | TEXT | e.g. `"crore"`, `"million"`, `"bps"` |
| magnitude_factor | REAL | e.g. `1e7` for crore |
| base_value | REAL | `numeric_value * magnitude_factor` in base unit of `currency`/`unit_norm` |
| currency | TEXT | `INR` \| `USD` \| null |
| is_percentage | INTEGER NOT NULL DEFAULT 0 | |
| percentage_ratio | REAL | `0.05` for `"5%"` |
| unit_raw | TEXT | `"₹ crore"`, `"%"`, `"tonnes"`, `"days"` |
| unit_norm | TEXT | `INR` \| `ratio` \| `tonne` \| `count` \| `days` \| `sqft` \| `date` |
| value_text | TEXT | semantic object ("resigned", "Executive Director", "benign") |
| — context — | | |
| reporting_period_raw | TEXT | "FY24", "nine months ended December 31, 2021" |
| reporting_period_start | TEXT | ISO date, inclusive |
| reporting_period_end | TEXT | ISO date, exclusive |
| reporting_period_type | TEXT | CHECK ∈ (`instant`,`quarter`,`half_year`,`fiscal_year`,`calendar_year`,`range`,`unknown`) |
| scope | TEXT (JSON) | open set: `{basis, segment, geography, counting_basis, measure_adjustment, ...}` |
| qualifiers | TEXT (JSON array) | `["restated","advance estimate",...]` |
| **modality** | TEXT NOT NULL DEFAULT `ASSERTED` | CHECK ∈ (`ASSERTED`,`HISTORICAL`,`ESTIMATED`,`FORECAST`,`TARGET`,`UNCERTAIN`) |
| context_complete | INTEGER NOT NULL DEFAULT 0 | LLM flag: was subject+period unambiguous in the chunk |
| — grounding / eligibility — | | |
| **evidence_status** | TEXT NOT NULL DEFAULT `UNVERIFIED` | CHECK ∈ (`VERIFIED`,`PARTIAL`,`UNVERIFIED`) — mirrors `evidence.evidence_status` |
| **reasoning_eligible** | INTEGER NOT NULL DEFAULT 0 | only these enter relationship reasoning |
| — reproducibility — | | |
| extraction_model | TEXT | |
| prompt_version | TEXT | |
| extraction_temperature | REAL | |
| extracted_at | TEXT | |
| raw_payload | TEXT (JSON) | the extractor's original structured output **for this one fact**, pre-normalization — answers "what did the extractor produce?" *(migration 2)* |
| — vectors (nullable until Phase 8) — | | |
| embedding | BLOB | float32 vector of "subject predicate object + context" |
| predicate_embedding | BLOB | float32 vector of `predicate` |
| created_at | TEXT NOT NULL | |

Cross‑column CHECKs:
`CHECK (lifecycle_state != 'QUARANTINED' OR reasoning_eligible = 0)`,
`CHECK (reasoning_eligible = 0 OR evidence_status != 'UNVERIFIED')`.

**`fact_type` extensibility:** the four values cover numeric / semantic /
temporal / categorical. A CHECK keeps them consistent with `app/models.py` and
`app/facts.FACT_TYPES`; adding a fifth type is a one-file migration
(`facts` currently rebuilds in milliseconds — it is empty until Phase 4).

Indexes: `facts(document_id)`, `facts(subject_entity_id)`,
`facts(predicate_norm)`, `facts(fact_type)`, `facts(lifecycle_state)`,
`facts(reasoning_eligible)`, `facts(evidence_status)`, `facts(run_id)`,
`facts(reporting_period_start, reporting_period_end)`.

### facts_fts (FTS5)

`CREATE VIRTUAL TABLE facts_fts USING fts5(fact_id UNINDEXED, subject_raw,
predicate, object_raw, value_text);` — **plain FTS5, populated explicitly by the
persistence layer** (Phase 3), no triggers. Used for the BM25 term in candidate
retrieval and the UI `q` filter. `ponytail: explicit upserts beat trigger‑magic
for a table written in exactly one place.`

## evidence

At most one row per fact (`fact_id UNIQUE`). Created by `attach_evidence`, which
resolves `page_id` and validates the chain (page ∈ document, chunk ∈ page,
offsets ∈ `[0, len(pages.text)]`).

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| fact_id | INTEGER NOT NULL UNIQUE → facts(id) ON DELETE CASCADE | |
| document_id | INTEGER NOT NULL → documents(id) ON DELETE CASCADE | |
| page_index | INTEGER NOT NULL | |
| page_id | INTEGER → pages(id) ON DELETE SET NULL | explicit FK for the FACT→…→PAGE chain *(migration 2)* |
| chunk_id | INTEGER → chunks(id) ON DELETE SET NULL | explicit FK for the FACT→…→CHUNK chain *(migration 2)* |
| printed_label | TEXT | denormalized from `pages` for display |
| char_start | INTEGER | into `pages.text` |
| char_end | INTEGER | |
| quote | TEXT NOT NULL | verbatim; equals `page.text[char_start:char_end]` when `verification_method ∈ (exact, normalized_exact)` |
| method | TEXT DEFAULT `text_layer` | CHECK ∈ (`text_layer`,`ocr`) |
| **verification_method** | TEXT NOT NULL DEFAULT `unverified` | CHECK ∈ (`exact`,`normalized_exact`,`fuzzy`,`unavailable`,`unverified`) *(migration 2 adds `unavailable` = could not attempt, vs `unverified` = attempted, not found)* |
| fuzzy_score | REAL | set only when `verification_method='fuzzy'` |
| **numeric_rederivation** | TEXT NOT NULL DEFAULT `not_applicable` | CHECK ∈ (`not_applicable`,`success`,`failed`) |
| **evidence_status** | TEXT NOT NULL DEFAULT `UNVERIFIED` | CHECK ∈ (`VERIFIED`,`PARTIAL`,`UNVERIFIED`) |
| evidence_score | REAL | OPTIONAL — only if computed from explicit signals; may be NULL |
| notes | TEXT | human‑readable "why verified/partial/failed" for the UI |
| created_at | TEXT NOT NULL | |

Table CHECK: `char_start IS NULL OR char_end IS NULL OR char_start < char_end`
*(migration 2)*.

Status derivation (implemented Phase 5, documented here):

| verification_method | numeric_rederivation | ⇒ evidence_status |
|---|---|---|
| `exact` / `normalized_exact` | `not_applicable` / `success` | **VERIFIED** |
| `exact` / `normalized_exact` | `failed` | **PARTIAL** (quote real, number not re‑derivable) |
| `fuzzy` | `not_applicable` / `success` | **PARTIAL** |
| `fuzzy` | `failed` | **UNVERIFIED** |
| `unavailable` / `unverified` | any | **UNVERIFIED** |

Index: `evidence(document_id, page_index)`, `evidence(page_id)`,
`evidence(chunk_id)`.

Phase 3 (this phase) persists evidence and validates the chain. It does **not**
determine `verification_method` by comparing `quote` to `pages.text` — that is
Phase 5 (evidence verification).

## relationships

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| fact_a_id | INTEGER NOT NULL → facts(id) ON DELETE CASCADE | |
| fact_b_id | INTEGER NOT NULL → facts(id) ON DELETE CASCADE | CHECK `fact_a_id < fact_b_id` |
| **category** | TEXT NOT NULL | CHECK ∈ (`CORROBORATES`,`CONTRADICTS`,`DIFFERENT_CONTEXT`,`TEMPORAL_EVOLUTION`,`UNCERTAIN`) |
| context_dimension | TEXT | `time` \| `scope:basis` \| `scope:counting_basis` \| `scope:geography` \| `scope:segment` \| `units` \| `modality` \| `vintage` \| null |
| deterministic_signals | TEXT (JSON) NOT NULL | entity_match, predicate_sim, unit_equivalent, base_value_delta_pct, period_relation, scope_conflict[], modality_pair, publication_gap_days, vintage_differs |
| llm_used | INTEGER NOT NULL DEFAULT 0 | |
| llm_proposed_category | TEXT | what the model returned, **before** deterministic validation |
| validation_action | TEXT DEFAULT `not_applicable` | CHECK ∈ (`accepted`,`overridden`,`downgraded`,`not_applicable`) |
| validation_notes | TEXT | why deterministic logic kept/changed the category |
| reasoning | TEXT | concise final explanation citing the signals |
| confidence | REAL | |
| model_name | TEXT | |
| prompt_version | TEXT | |
| run_id | INTEGER → runs(id) ON DELETE SET NULL | |
| created_at | TEXT NOT NULL | |

Unique: `relationships(fact_a_id, fact_b_id)`. Index: `relationships(category)`,
`relationships(fact_a_id)`, `relationships(fact_b_id)`, `relationships(run_id)`.

Stored **canonically as `(min, max)` fact id** (undirected) — `add_relationship`
sorts the pair, rejects `fact_a_id == fact_b_id` (`self_relationship`), and on a
repeated pair is a deterministic no-op returning the existing row's id (first
write wins). Direction, where it matters (e.g. old→new for `TEMPORAL_EVOLUTION`),
is derivable from the two facts' `reporting_period_*`. **Phase 3 only persists —
nothing is inferred; no relationship is created from the starter PDFs.**

### Relationship semantics (intended meaning — the reasoning phase applies these)

| category | meaning |
|---|---|
| `CORROBORATES` | two facts describe materially the same claim after normalization / context alignment |
| `CONTRADICTS` | two facts are materially incompatible under sufficiently comparable context |
| `DIFFERENT_CONTEXT` | the apparent mismatch is explained by a legitimate contextual dimension — scope, unit, reporting basis, definition (UI: "Reconciled by context") |
| `TEMPORAL_EVOLUTION` | the facts differ because the underlying state changed over time (different dates ≠ contradiction) |
| `UNCERTAIN` | available evidence / context is insufficient to classify confidently |

## failures  — unified inspectable failure surface

Points at whatever failed; nothing is discarded.

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| run_id | INTEGER → runs(id) ON DELETE CASCADE | |
| document_id | INTEGER → documents(id) ON DELETE CASCADE | nullable |
| failure_type | TEXT NOT NULL | CHECK ∈ (`extraction_unparsed`,`grounding_failed`,`context_incomplete`,`ocr_page`,`normalization_failed`,`entity_ambiguous`,`relationship_uncertain`,`run_error`) |
| ref_table | TEXT | `facts` \| `relationships` \| `pages` \| `raw_extractions` \| null |
| ref_id | INTEGER | row id in `ref_table` |
| reason | TEXT NOT NULL | machine‑readable code + short text |
| detail | TEXT (JSON) | |
| created_at | TEXT NOT NULL | |

Index: `failures(run_id)`, `failures(failure_type)`.

---

## Lifecycle ↔ column state

| lifecycle_state | set by phase | evidence_status | reasoning_eligible | in `relationships`? |
|---|---|---|---|---|
| `RAW` | reserved (transition not yet used) | `UNVERIFIED` | 0 | no |
| `CANDIDATE` | **4 (extraction)** — every extracted fact enters here | `UNVERIFIED` | 0 | no |
| `GROUNDED` | 5 (verification) | `VERIFIED` / `PARTIAL` | 0 | no |
| `NORMALIZED` | 6 (normalization) | `VERIFIED` / `PARTIAL` | 0 | no |
| `ELIGIBLE_FOR_REASONING` | 6→7 (after normalize + entity link) | `VERIFIED` / `PARTIAL` | 1 | yes |
| `QUARANTINED` | any | any | 0 (CHECK) | no — appears in `failures` + `/failures` UI |

`PARTIAL` facts *may* be `reasoning_eligible` (invariant 2 only bars
`UNVERIFIED`); the relationship layer records the reduced grounding in
`deterministic_signals` and caps `confidence`.

### Phase 4 persistence semantics — candidate facts

`app.extract.extract_document` (LLM structured output → deterministic validation
→ persist) writes each extracted claim as a fact with `lifecycle_state='CANDIDATE'`,
`reasoning_eligible=0`, `evidence_status='UNVERIFIED'`, and `raw_payload` = the
candidate exactly as the model produced it. It **does not** establish truth or
evidence validity, and never promotes a fact past `CANDIDATE`. Alongside each
fact it writes **one `evidence` row as a candidate citation** —
`verification_method='unverified'`, `evidence_status='UNVERIFIED'`, `chunk_id` and
`page_id` set, `char_start`/`char_end` translated from chunk-relative to
page-relative (`chunks.char_offset + candidate offset`), `quote` as the model
returned it. Phase 5 (verification) will compare that quote to `pages.text`,
set the real `verification_method` / `numeric_rederivation`, and move passing
facts to `GROUNDED`. A candidate that fails the structural contract
(missing field, bad `fact_type`/`modality`/`period_type`, offsets outside the
chunk, `start ≥ end`, numeric with no value, duplicate within the chunk) is not
persisted — it becomes a `failures` row (`failure_type='extraction_unparsed'`)
with the full candidate payload in `detail`; sibling candidates are unaffected.

## Numeric representation — worked examples

| input | value_raw | numeric_value | magnitude | magnitude_factor | base_value | currency | is_percentage | percentage_ratio | unit_raw | unit_norm |
|---|---|---|---|---|---|---|---|---|---|---|
| `₹8,142 crore` | `₹8,142 crore` | 8142 | `crore` | 1e7 | 8.142e10 | INR | 0 | – | `₹ crore` | `INR` |
| `Rs. 578 Cr` | `Rs. 578 Cr` | 578 | `crore` | 1e7 | 5.78e9 | INR | 0 | – | `Rs Cr` | `INR` |
| `5%` | `5%` | 5 | – | – | – | – | 1 | 0.05 | `%` | `ratio` |
| `781 Bps` | `781 Bps` | 781 | `bps` | 1e-4 | 0.0781 | – | 1 | 0.0781 | `bps` | `ratio` |
| `(452)` (₹ Cr ctx) | `₹(452) Cr` | -452 | `crore` | 1e7 | -4.52e9 | INR | 0 | – | `₹ Cr` | `INR` |
| `US$216 billion` | `US$216 billion` | 216 | `billion` | 1e9 | 2.16e11 | USD | 0 | – | `US$ billion` | `USD` |
| `1.4 Mn Tons` | `1.4 Mn Tons` | 1.4 | `million` | 1e6 | 1.4e6 | – | 0 | – | `Mn Tons` | `tonne` |

Rule: never store only `base_value`. Comparison uses `base_value` (+ `unit_norm`
/ `currency` compatibility); display and audit use `value_raw`.

## Provenance — four concepts, worked

| doc | reporting period (on a fact) | document_date | publication_date | data_vintage |
|---|---|---|---|---|
| Economic Survey 2024‑25 | FY25 (2024‑04‑01 … 2025‑04‑01) | 2025‑01‑30 | 2025‑01‑31 | "first advance estimates" |
| RBI Annual Report 2024‑25 | FY25 | 2025‑05‑25 | 2025‑05‑29 | "provisional estimates" |
| IMF Article IV 2025 | FY2024/25 **and** FY2025/26 | 2025‑11‑22 | 2025‑11‑25 | "IMF staff projections" |

Two facts about "India real GDP growth, FY25" from the Survey and the RBI have the
**same reporting period** but different `data_vintage` and `publication_date` →
candidate for `TEMPORAL_EVOLUTION` (revision) or `DIFFERENT_CONTEXT` (vintage),
**not** `CONTRADICTS`. `FY25` vs `FY2025/26` must be resolved via each document's
`fy_convention` before any comparison.

## Period representation examples

| `reporting_period_raw` (fy_convention) | start | end | type |
|---|---|---|---|
| `FY24` (apr-mar) | 2023-04-01 | 2024-04-01 | fiscal_year |
| `Q3 FY24` | 2023-10-01 | 2024-01-01 | quarter |
| `nine months ended December 31, 2021` | 2021-04-01 | 2022-01-01 | range |
| `as on March 31, 2024` | 2024-03-31 | 2024-04-01 | instant |
| `FY2025/26` (imf slash form) | 2025-04-01 | 2026-04-01 | fiscal_year |
| `Fiscal 2021` | 2020-04-01 | 2021-04-01 | fiscal_year |
| `2024` (calendar, in a global‑growth context) | 2024-01-01 | 2025-01-01 | calendar_year |
| unparseable | NULL | NULL | unknown |

## Vector storage

`facts.embedding` / `facts.predicate_embedding`: `BLOB` = `np.float32` bytes, dim
`FKL_EMBEDDING_DIM` (384 for `bge-small-en-v1.5`). Loaded into an in‑memory
matrix per retrieval pass; cosine via normalized dot product. No extension.
Upgrade path: `sqlite-vec` / pgvector behind an unchanged
`top_k(fact_id) -> [(id, score)]` interface.

## Confidence fields — where they live (post‑review)

| concept | storage |
|---|---|
| extraction quality | not a single score — `context_complete`, `lifecycle_state`, and `failures` rows describe it |
| evidence verification | `evidence.verification_method` + `evidence_status` + `numeric_rederivation` (+ optional `evidence_score`) |
| entity match | `entities.resolution_score` / `llm_confidence`; per‑fact via `facts.subject_entity_id` presence |
| relationship | `relationships.confidence` + `deterministic_signals` + `validation_action` |

No blended global score is stored. Any combined value is computed on read for
sorting only, from the fields above, and the formula lives in config (D19).
