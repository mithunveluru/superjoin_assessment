# Design Considerations & Known Risks

These are deliberate engineering positions taken during design, not a list of
unfinished work. Each names a real risk in the problem and how the architecture
is set up to handle it in the phase that owns it (see
[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md)).

## Repository scope

`superjoin_assessment/` is its own standalone Git repository. It was authored
inside an unrelated outer directory that carries backup history for other work;
that history is irrelevant to this assignment and is not inherited. The outer
repository is left untouched.

## Retrieval calibration

Retrieval weights and thresholds (`FKL_RETRIEVAL_*`,
`FKL_*_THRESHOLD`) are **configuration, not settled values**. They have starting
defaults so the pipeline runs, but they are not claimed to be correct. Phase 10's
evaluation harness sweeps them against **both** corpora and records the chosen
values with the run that justified them. They are not tuned now, and no code path
depends on a specific number.

## Fiscal-year interpretation

Fiscal-year notation (`FY24`, `FY 2023-24`, `FY2025/26`, `Fiscal 2021`,
`Q2 FY25`, `2024-25`) is interpreted **from document context**, not from
per-document rules baked into the pipeline. The normalizer:

- resolves a label only against the owning document's detected convention;
- **preserves ambiguity** — an unresolvable label yields
  `reporting_period_type='unknown'` and the fact is not promoted to reasoning,
  rather than being silently coerced;
- never assumes two different FY labels denote the same period without resolving
  both.

A manual `fy_convention` override exists **only as an administrative escape
hatch** (`documents.fy_convention_source='override'`), never as a required step
in normal processing.

**Phase 6 (done)** implements this in `app/normalize.py`:

- `detect_fy_convention` reads the document's own text for "financial / fiscal
  year end(ed|ing) … <Month>". A March end → `apr-mar`, December → `jan-dec`,
  June → `jul-jun`. **Any other end‑month (e.g. September) is not representable**
  in the `fy_convention` CHECK and is left `unknown` rather than approximated —
  the affected periods then normalize to `type='unknown'` and are not promoted to
  reasoning. Widening the vocabulary is an additive migration if a corpus needs
  it.
- When detection finds nothing, the document's convention falls back to
  `settings.fy_convention_default` (`apr-mar`) with
  `fy_convention_source='config_default'`. This is a **starting default, not a
  claim about the document** — it is recorded on the row so a reviewer can see
  which facts rest on an assumed convention, and an `override` supersedes it.
- Date parsing in `parse_period` / `_parse_date` is a **hand-written matcher**
  (ISO, `DD-MM-YYYY`, `DD Mon YYYY`, `Mon 'YY`, `Month YYYY`, bare year). It is
  deliberately narrow: an unrecognised phrase yields `type='unknown'` (preserved
  ambiguity), never a guessed date. Locale-specific or highly irregular period
  phrasing outside this set is a known coverage gap — the failure mode is a
  non-promoted fact, not a wrong one.

## Contradiction availability in the starter data

It is **not assumed** that the starter datasets contain a naturally occurring
contradiction. The Phase 10 evaluation harness will:

1. search for a naturally occurring contradiction first;
2. report honestly if none is found;
3. optionally exercise contradiction detection with a **clearly labelled
   synthetic fixture** kept in `evaluation/cases/`, never presented as a
   corpus-derived result.

A manufactured contradiction is never surfaced as if it came from the documents.

## Semantic reasoning — false corroboration / false contradiction

The central reasoning risk: two facts can look equal or look contradictory while
actually differing in **time, scope, units, definitions, modality, counting
basis, or data vintage**. The relationship engine (Phase 9) therefore pairs
deterministic comparison signals (period relation, unit equivalence,
`base_value` delta, scope conflict, modality pair, vintage difference,
publication gap) with a **constrained** LLM semantic step: the LLM proposes an
interpretation, deterministic logic validates it against those signals and has
the final say. Not implemented yet.

## Candidate retrieval — lexical baseline, broad by design (Phase 8, done)

`app/retrieve.py` + `app/signals.py` generate candidate pairs and deterministic
comparison signals. It is **read-only** and does **no** relationship
classification. Known risks, each an accepted position for this phase:

- **Lexical-only retrieval — false negatives.** Predicate matching is
  `rapidfuzz.token_set_ratio` + token overlap. Two facts that are the same
  measure but share no tokens after normalization ("employees" vs "headcount",
  "CAD" vs "current account deficit") are retrieved **only** when they also share
  a resolved `subject_entity_id` (the entity block) or ≥ N shared content tokens.
  A cross-entity synonym pair with neither is **missed**. The fix is the deferred
  embedding rung (below); until then the entity block is the safety net and the
  eval harness (Phase 10) is where the gap gets measured.
- **Broad entity blocking — false positives.** Every pair of eligible facts about
  the same entity is a candidate, so a busy entity produces many pairs that are
  not meaningfully comparable (revenue vs an unrelated count). This is
  deliberate — "high recall first, precision later" — and bounded by
  `FKL_RETRIEVAL_TOP_K` (`#pairs ≤ top_k · n_eligible`). Phase 9 + the
  `SignalSet` (low `predicate_similarity`, `fact_type` mismatch, `unit`
  mismatch) are what filter them; Phase 10 tunes `top_k` /
  `candidate_threshold` against expected-property specs.
- **Context mismatch is a signal, not a filter.** FY24-vs-FY25 revenue,
  global-vs-India scope, actual-vs-forecast — all retrieved, with
  `period_relation` / `scope_conflict` / `modality_comparable` set. If Phase 9
  under-uses those signals it could mislabel a reconciliation case; that risk
  lives in Phase 9, not here.
- **Embeddings deferred.** `fastembed` / `numpy` are not installed and the brief
  forbids forcing embeddings. `facts.embedding` stays NULL, `app/embed.py` is not
  created, `retrieval_weight_embedding` is a reserved knob. **Lexical predicate
  similarity is the current deterministic baseline; semantic embeddings are a
  future improvement** — a `name+context cosine` block would add the missing
  cross-entity synonym recall and let `retrieval_weight_embedding` become live.
  Risks a future embedding rung must watch: a local ONNX model download at first
  use (offline-friendly but not zero-setup), non-reproducible vectors across
  model versions (store `embedding_model` + `embedding_dim`), and treating a high
  cosine as proof of equivalence (it never is — it only widens the candidate
  set).
- **Quadratic scaling.** Retrieval is near-linear in the *output* size via an
  inverted index, not a full O(n²) DB scan, and oversized non-discriminative
  token buckets are dropped (`FKL_RETRIEVAL_BUCKET_MAX`). At the corpus scale
  here (hundreds–thousands of eligible facts) this is microseconds. Beyond ~1e4
  eligible facts, swap the inverted index for an ANN index — the
  `retrieve_candidates` signature hides the change.
- **Dependencies / API.** No new dependency, no network, no API key. `rapidfuzz`
  was already present from Phase 5.
- **Deterministic real-corpus smoke only.** As in Phases 4–7, meaningful
  end-to-end validation needs `ANTHROPIC_API_KEY` for extraction. The Phase-8
  smoke (`scripts/smoke_retrieve.py`, and a scratchpad LLM-free harvest) proves
  retrieval is **bounded, blocked, cross-document and byte-identical on re-run**
  over real ingested page text; it does not prove the *usefulness* of the
  candidate set, which depends on real extracted facts.

## Entity resolution — heuristics with a bias to *not* merge

**Phase 7 (done)** resolves subject surfaces with generic config only (legal
suffixes, honorifics, anaphora words, rename predicates — no dataset aliases).
Known limits, each chosen so the failure mode is *under*-merging (a fact stays
linked to its own entity or to nothing), never a wrong merge:

- **No embeddings.** The planned "name + context cosine" block is deferred to
  Phase 8 (which needs `facts.embedding` regardless). Until then, two surfaces
  that are the same entity but share no tokens after normalization (a true
  rebrand with no rename sentence in the text, an acronym vs its expansion) are
  **not** merged. `token_set_ratio ≥ 88` still blocks acronym-ish overlaps for
  the LLM to confirm.
- **Anaphora → dominant entity.** "the Company" / "the Group" resolve to the
  document's most-referenced entity. In a filing that discusses a parent *and* a
  subsidiary at similar frequency this can attach an anaphor to the wrong one;
  an exact frequency tie yields `entity_ambiguous` (unresolved) instead.
- **`entity_type` is a one-line guess** (honorific → person, else org); the first
  surface seen for a `normalization_key` sets it. It is metadata, not used in any
  gate.
- **No LLM in this environment.** Every borderline cluster (fuzzy-blocked, not
  auto-mergeable) is recorded as `entity_ambiguous` and left unresolved. A live
  `confirm_entities` call is what turns those into merges/splits; the path is
  covered by tests with a fake confirmer.
- **Auto-merge threshold** (`token_sort_ratio ≥ 94`) and the block threshold
  (`token_set_ratio ≥ 88`) are starting values, not tuned against a labelled
  set — Phase 10's harness is where they get evidence. Both are conservative:
  auto-merge is order/length-sensitive, so subset names ("<name>" vs
  "<name> Robotics") fall well short.

## PDF layout / context separation

In real filings, tables, multi-column layouts, running headers, footnotes, and
charts routinely separate a value from its subject, unit, or period. **Phase 2
(done)** provides the page-aware extraction: verbatim per-page text, preserved
character offsets (`pages.text[char_offset:char_end] == chunks.text`), page
dimensions and block/image counts retained in `pages.extraction_meta`, and a
per-page source-quality status (`TEXT_EXTRACTED` / `LOW_TEXT` / `EMPTY` /
`EXTRACTION_ERROR`) with `LOW_TEXT`/`EMPTY` pages also recorded in `failures` as
an OCR/layout signal. It does **not** do table structure recognition or run OCR.
The verbatim evidence quote per fact and the verification gate that
**quarantines** any fact whose quote or number cannot be re-derived from the page
are Phases 4–5, not yet built. Smoke tests on representative filings confirmed
multi-column/table pages linearize (reading order roughly preserved, not
guaranteed) — deferred to a future table-aware extractor.

## Dependency licensing (PyMuPDF)

PyMuPDF (imported as `pymupdf`) is used for PDF parsing (Phase 2) and is
AGPL-licensed. That is acceptable for a hiring-assignment prototype. It would need
review before any closed-source commercial reuse (alternatives: `pypdfium2`,
`pdfminer.six`). Noted as a future consideration; not changed.

## LLM extraction quality is unverified in this environment

Phase 4's structural pipeline (LLM call → deterministic validation → persistence
→ provenance chain) is proven on real ingested chunks, but **extraction quality**
— whether the model returns *meaningful atomic claims* rather than noise, and
whether its `char_start`/`char_end` are actually correct — can only be checked
with a live `ANTHROPIC_API_KEY` (`scripts/smoke_extract.py`), which was not
available here. Risks a live run must be watched for: over-extraction (every
number becomes a "fact"), quotes/offsets that don't line up with the chunk (these
are *rejected* structurally if offsets fall outside the chunk, but a model can
return a valid-range offset whose text doesn't match its quote — Phase 5's
verification gate is the backstop), and table-linearized text (Phase 2 known
limitation) yielding facts with the wrong subject/period. The prompt
(`app/prompts/extraction_v1.md`) is written against these; it is versioned so it
can be revised without touching code.

## Structured-output wire contract may need adjustment on a live call

`app/llm.py` requests `output_config.format` with a hand-written `json_schema`
and no sampling params (Sonnet 5 rejects `temperature`/`top_p`/`top_k`). This is
correct per the current API docs but has not been exercised against the live
endpoint in this environment. If a live call rejects the schema shape, the fix is
localized to `EXTRACTION_JSON_SCHEMA` / the `messages.create` call in `app/llm.py`
— the extraction service, validation, and tests (which use a fake client) are
unaffected.

## Cost of a full extraction run

One `extract` run makes one LLM call per chunk. The starter corpus is ~260–350
chunks per large document; at Sonnet 5 rates that is roughly a dollar or two per
document (`runs.estimated_cost_usd` accumulates the real figure from `usage`).
`max_llm_calls_per_doc` caps a runaway document. Batch API / caching are future
levers, not built.

## Verification recovers evidence spans, never claims

Phase 5's `recovered_exact` rung updates `evidence.char_start`/`char_end` when the
LLM's offsets were wrong but the exact quote occurs **once** in the chunk. It only
ever moves the *pointer*; the quote, `raw_payload`, and every fact field are
untouched. A quote occurring more than once → `ambiguous_quote_match` → quarantine
(never a guess). If a future change makes recovery less conservative (normalized
recovery, cross-chunk search) it must preserve this: correcting a claim's *value*
to match the source would destroy evaluation integrity.

## Fuzzy verification threshold is a starting value

`FKL_VERIFY_FUZZY_THRESHOLD` (90) and the numeric/unit token guard were chosen
conservatively and validated on synthetic + real persisted text, not tuned
against a labelled set. Fuzzy only ever yields `PARTIAL` (never `VERIFIED`) and
only after every number / currency / unit token in the quote is found verbatim in
the source, so a too-low threshold cannot let a changed value through — it can
only let through a formatting-different quote that should perhaps have failed. The
Phase 10 evaluation harness is where this threshold gets evidence.

## PARTIAL (fuzzy) facts can still become reasoning-eligible

Per the Phase-0 design, `mark_reasoning_eligible` accepts `PARTIAL` evidence
(invariant 2 only bars `UNVERIFIED`). Phase 5 does **not** change that rule — the
smallest safe change was to make the weakness *legible*: a Phase-5 `PARTIAL` is
always `verification_method='fuzzy'` with a recorded `fuzzy_score`, never `exact`.
The relationship engine (Phase 9) is responsible for capping confidence on
`fuzzy`/`PARTIAL` evidence; that is not yet implemented.

## CHECK-constraint changes need a table rebuild (SQLite)

SQLite cannot alter a `CHECK` constraint in place, so migration 2 (Phase 3)
rebuilds `facts` and `evidence` with the standard table-redefinition procedure
(create new / `INSERT … SELECT` / drop / rename, foreign keys briefly off inside a
transaction, `PRAGMA foreign_key_check` verified after). This is safe now because
those tables carry no rows until Phase 4 — the copy is empty. Once real facts
exist, a future `CHECK` change would copy live data: still correct with this
procedure, but slower and higher-stakes, so closed vocabularies (`fact_type`,
`lifecycle_state`, relationship `category`) should be treated as near-frozen.
Additive changes (`ADD COLUMN`, new tables) remain cheap and are preferred.

## Undirected relationship storage

Relationships are stored canonically as `(min, max)` fact id — undirected, one
row per pair. The five categories in this assignment are effectively symmetric
for storage. Where direction matters (old→new for `TEMPORAL_EVOLUTION`) it is
derived from the two facts' `reporting_period_*` at reasoning time, not stored on
the edge. If a later phase needs a first-class direction, that is an `ADD COLUMN`.

## Test-dependency hardening

`starlette >= 1.6` prefers the `httpx2` package for its `TestClient`; adding
`httpx2` to the dev dependencies clears that deprecation notice. One further
notice remains from `starlette.testclient` importing a deprecated
`anyio.abc.BlockingPortal` alias — this is internal to those libraries, does not
affect behaviour, and will resolve on their next release. Left as a
dependency-hardening note rather than pinned around.
