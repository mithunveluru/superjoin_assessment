# Decisions

ADR-style. Each: decision, why, rejected alternatives, revisit trigger.

---

## D1 — SQLite, not Postgres/pgvector

**Decision:** single-file SQLite (`sqlite3` stdlib), WAL mode.
**Why:** 6 PDFs, ~few-thousand facts. `pip install` + `uvicorn`, zero infra,
nothing to explain that isn't the reasoning engine. Assignment explicitly prefers
"a smaller, understandable prototype".
**Rejected:** Postgres + pgvector (Docker, extension build, migrations — infra tax
with no payoff at this scale); a dedicated vector DB (same).
**Revisit when:** > ~10³ documents or > ~10⁵ facts, or concurrent writers. Schema
is storage-agnostic; DATA_MODEL DDL ports to Postgres with type tweaks only.

## D2 — In-process brute-force vector search

**Decision:** store `float32` embeddings as `BLOB`; cosine via normalized numpy
dot product over an in-memory matrix.
**Why:** O(n·dim) per query is microseconds here. No extension, no index to build.
**Rejected:** `sqlite-vec` / `faiss` / pgvector ANN — premature.
**Revisit when:** retrieval latency shows up in a profile (~1e5 facts). Interface
`top_k(fact_id) -> [(id, score)]` hides the swap.
`ponytail: brute force scan; upgrade path is a drop-in.`

## D3 — Static HTML UI, no framework, no build step

**Decision:** `index.html` + `app.js` (vanilla `fetch`) + one CSS file, served by
FastAPI.
**Why:** the UI requirement is "prioritize clarity over visual polish" and show
upload/status/facts/evidence/relationships/reasoning/failures. That's tables and
cards. Next.js adds a Node toolchain, a build, and a second thing to run for a
reviewer.
**Rejected:** Next.js / React / Vite (toolchain tax); server-side templating with
Jinja (fine, but plain JSON+fetch is even less to own).
**Revisit when:** the UI needs real interactivity (graph exploration, bulk edit).
The API contract already supports a richer client.

## D4 — One FastAPI process, `BackgroundTask` for processing

**Decision:** ingestion→reasoning runs in-process as a FastAPI `BackgroundTask`;
status polled via `runs`.
**Why:** a queue/worker is operational overhead for a prototype that processes one
doc at a time in a demo.
**Rejected:** Celery/RQ + Redis (infra); synchronous processing (a 100-page PDF
would block the request for minutes).
**Revisit when:** many concurrent uploads or multi-minute PDFs — move `pipeline.run`
behind a worker; `runs` table already models async state.

## D5 — Claude for extraction & relationship reasoning; local model for embeddings

**Decision:** `anthropic` SDK, `claude-sonnet-5` for `extract_facts`,
`confirm_entities`, `classify_relationship`. Embeddings from `fastembed`
(`BAAI/bge-small-en-v1.5`, ONNX, CPU, offline).
**Why:** Sonnet 5 has a 1M context, strong structured-output support, and is
~1/2.5 the price of Opus per token ($2/$10 vs $5/$25 per MTok) — the extraction
workload is high-volume and schema-bounded, so the cheaper capable model is the
right default for a cost-sensitive prototype. Anthropic has no first-party
embeddings endpoint; a local ONNX model keeps the API-key surface to exactly one
secret and costs nothing per call.
**Rejected:** Opus 5 as the default (better, but the per-token cost isn't
justified for bulk structured extraction — it's the documented upgrade if
extraction quality is measured short); OpenAI/Voyage for embeddings (a second
API key and vendor for no benefit at this scale); a full `sentence-transformers`
model (drags in torch — `fastembed` is ONNX, ~50 MB).
**Revisit when:** extraction accuracy on the eval harness is the bottleneck → try
`claude-opus-5` for `extract_facts` only; or Haiku 4.5 for cost if volume grows.
Model id is a config value.
**Note:** all LLM calls go through `app/llm.py` (3 functions). Swapping provider
or model touches that one file.

## D6 — Fact = CLAIM + CONTEXT + EVIDENCE, one schema for numeric & semantic

**Decision:** a single `facts` table; `fact_type` distinguishes `numeric`
(`value_num`) from `semantic` (`value_text`); every other column (context,
provenance, confidence) is shared.
**Why:** the corpora need both ("₹8,142 Cr for FY24, consolidated" and "Barasia
resigned effective 2024-07-01"). A finance-only schema fails the macro corpus and
the semantic Delhivery facts.
**Rejected:** separate tables per fact type (join pain, duplicated context
columns); a generic EAV blob (loses queryability of period/scope).

## D7 — Context is columns; raw is always kept

**Decision:** `period_*`, `unit`, `currency`, `scope` (JSON), `qualifiers`
(JSON), `modality` are first-class and queryable; every normalized value has a
`*_raw` sibling.
**Why:** the reconciliation cases (consolidated vs standalone, FY24 vs FY2025/26,
month reading vs annual average) are *only* solvable if context survives
extraction. Overwriting raw destroys auditability.
**Rejected:** normalizing values into a single comparable number and dropping
context (would turn every Case-3 into a false Case-2).

## D8 — Entity resolution: cluster deterministically, confirm with one LLM call; no seeded aliases

**Decision:** blocking (generic suffix/honorific normalization + fuzzy + cosine)
→ union-find → one `confirm_entities` LLM call per cluster. Rename statements in
the text become alias edges (`source=derived_fact`).
**Why:** satisfies "recognized as the same entity without hard-coded aliases".
The only lists in config are *language-generic* (legal suffixes, honorifics), not
"Delhivery"→"the Company".
**Rejected:** a hand-maintained alias map (forbidden by the assignment); pure LLM
free-scan over all surface forms (unbounded cost, no blocking); pure fuzzy match
(merges "Delhivery" with "Delhivery Robotics").
**As built (Phase 7):** exact `normalization_key` match → deterministic merge;
`token_set_ratio ≥ 88` is the recall block; `token_sort_ratio ≥ 94` (order- and
length-sensitive, so a bare name vs "<name> Robotics" scores ~67) is the
no-LLM auto-merge; anything between goes to one `confirm_entities` call, and with
no LLM client the surface is left unresolved (`entity_ambiguous`) rather than
merged. Union-find was not needed — surface counts per document are small, so a
pairwise pass suffices; the cosine rung and `app/embed.py` moved to Phase 8
(which needs `facts.embedding` anyway and where `fastembed` gets pulled in).
Anaphora ("the Company") resolves to the document's dominant entity, not by
similarity.
**Revisit when:** cluster precision drops — add a cheap "is X a subsidiary/segment
of Y, not Y itself" check in the confirm prompt; add the cosine block once
`facts.embedding` exists if fuzz-only recall misses reworded different-surface
names.

## D9 — Verification is *described*, not scored (revised)

**Decision:** no `1.0 / 0.6 / 0.0` pseudo-confidence. Store
`evidence.verification_method` (`exact | normalized_exact | fuzzy | unverified`),
`numeric_rederivation` (`not_applicable | success | failed`), derived
`evidence_status` (`VERIFIED | PARTIAL | UNVERIFIED`), and a plain-language
`notes`. A numeric `evidence_score` is **optional** and, if present, is a pure
function of those explicit signals. `relationships.confidence` exists but is
computed from explicit signal agreement with constants in config (D19), shown
next to the signal table so the reviewer sees the basis.
**Why:** "do not create fake precision." Method + status is auditable and honest;
an arbitrary 0.6 is neither.
**Rejected:** blended calibrated probability (needs labelled data = manually
entered answers); fixed magic weights presented as confidence.
**Supersedes** the Phase-0 "four 0–1 scores" framing.

## D10 — Explicit fact lifecycle; failed facts are kept (revised)

**Decision:** `lifecycle_state ∈ {CANDIDATE, GROUNDED, NORMALIZED,
ELIGIBLE_FOR_REASONING, QUARANTINED}` + `evidence_status` + `reasoning_eligible`.
Each pipeline stage owns exactly one transition. Grounding failure ⇒
`QUARANTINED` + a `failures` row; the fact stays queryable and appears in
`GET /failures`. Only `reasoning_eligible=1` facts enter `relationships`
(schema CHECK bars the contradictions: `QUARANTINED ⇒ not eligible`,
`eligible ⇒ evidence_status != UNVERIFIED`).
**Why:** the assignment requires explicit failure handling *and* inspectability.
A boolean `verified` throws away the failure surface and conflates "not yet
processed" with "failed".
**Rejected:** delete-on-failure; a single `verified` flag; keeping ineligible
facts silently in the same list as eligible ones.

## D11 — LLM proposes, deterministic logic validates and decides (revised)

**Decision:** the LLM produces a *semantic proposal* (candidate facts; entity
cluster confirmation; a relationship category + reasoning from a structured
packet). Deterministic code produces every *factual* value (numeric
representation, period algebra, `base_value` deltas, unit equivalence,
scope-conflict, vintage/modality signals) and then runs `signals.validate()`,
which can **override or downgrade** the LLM's proposed relationship category.
`relationships` stores `llm_proposed_category`, final `category`,
`validation_action`, and `validation_notes`.
**Why:** deterministic code is testable, reproducible, free, and explainable; the
LLM must not have the last word on factual consistency (correction I). Concrete
guards: equal-after-normalization numbers can't be `CONTRADICTS`; disjoint
historical periods lean `TEMPORAL_EVOLUTION`; a scope difference that explains a
gap ⇒ `DIFFERENT_CONTEXT`; mixed modality ⇒ never `CONTRADICTS`.
**Rejected:** "LLM classifies, we log it" (unaccountable); "pure rules" (can't
judge predicate synonymy or whether a scope difference is *the* explanation).

## D12 — Five relationship categories incl. TEMPORAL_EVOLUTION (revised)

**Decision:** internal categories `CORROBORATES`, `CONTRADICTS`,
`DIFFERENT_CONTEXT`, `TEMPORAL_EVOLUTION`, `UNCERTAIN`, with an optional
`context_dimension` (`time | scope:* | units | modality | vintage`). UI presents
`DIFFERENT_CONTEXT` as "Reconciled by context".
**Why:** "different dates" can mean a *genuine change over time* (a value that
really moved, a director who really did resign later), not an apparent
contradiction to reconcile and not a real disagreement. Collapsing that into
`DIFFERENT_CONTEXT` mislabels temporal series; collapsing into `CONTRADICTS` is
worse. `TEMPORAL_EVOLUTION` is the honest label and is decided from
`period_relation=disjoint` + matching `HISTORICAL/ASSERTED` modality.
**Rejected:** four categories + a flag; a `SUPERSEDES` category for vintage
(covered by `TEMPORAL_EVOLUTION` + `context_dimension=vintage`); more categories
(diminishing returns).
**Supersedes** Phase-0 D12 (`RECONCILES` only).

## D13 — Dual page numbering (PDF index + printed label)

**Decision:** store both `pdf_page_index` (0-based file position) and
`printed_label`; evidence offsets are into the single page's text.
**Why:** the starter excerpts renumber and jump ("page numbers … may therefore
jump between retained sections"). A reviewer needs the printed label to find the
page in the original; the pipeline needs the stable index.
**Rejected:** printed label only (ambiguous, sometimes absent); PDF index only
(reviewer can't cross-reference the source filing).

## D14 — Generalization enforced by a grep, not by trust

**Decision:** `grep -ri 'delhivery\|macroeconom\|economic survey\|<any starter
entity/filename>' app/` must return nothing outside `tests/`. All tunables are
generic config. Prompts describe fact/relationship *shape*, never instances.
**Why:** "must not rely on hard-coded facts, filenames, schemas, or
document-specific rules" — made checkable.
**Rejected:** relying on code review alone.

## D15 — Cost & resource guards from day one

**Decision:** `MAX_UPLOAD_MB`, `MAX_PAGES`, `MAX_CHUNKS`, `MAX_LLM_CALLS`,
client timeout + bounded retries; per-run `cost_estimate_usd` in stats.
**Why:** an unseen 300-page PDF shouldn't run up an unbounded bill or hang the
process. Assignment: "If the project requires a paid service, include enough
sample output…" → also commit `samples/`.
**Rejected:** no limits (a single large test PDF could cost dollars and minutes).

## D16 — Modality is a controlled set, not free text

**Decision:** `modality ∈ {ASSERTED, HISTORICAL, ESTIMATED, FORECAST, TARGET,
UNCERTAIN}` on every fact (default `ASSERTED`), extracted by the LLM and CHECK-ed
by the DB. `signals.validate()` uses `modality_pair` — mixed modality (e.g.
`FORECAST` vs `HISTORICAL`) can never be `CONTRADICTS`.
**Why:** a forecast that differs from a later actual, or a target vs an
achievement, is not a contradiction. Without modality the system would
manufacture false conflicts (correction D). Six values cover the corpora
("first advance estimate", "projected", "expected to", "aim of ₹…").
**Rejected:** boolean `is_projection` (loses target/estimate/hypothetical);
free-text modality (not comparable).

## D17 — Four provenance concepts kept separate

**Decision:** distinct fields: fact `reporting_period_*`; document
`document_date`, `publication_date`, `data_vintage`. `FY25` / `FY2025/26` are
resolved through each document's detected `fy_convention` before any comparison.
**Why:** the macro corpus reports the *same period* at different vintages/dates
(Economic Survey Jan 2025 "advance estimate" vs RBI May 2025 "provisional" vs IMF
Nov 2025 projection). Merging these into one "date" makes revisions look like
contradictions (correction F). Separated, they drive `TEMPORAL_EVOLUTION` /
`DIFFERENT_CONTEXT(vintage)`.
**Rejected:** a single `date` per fact; assuming FY labels are comparable
across publishers.

## D18 — Preserve raw LLM output and reproducibility metadata

**Decision:** `raw_extractions` stores the verbatim structured response per chunk
before transformation. Every LLM-derived row carries `model_name`,
`prompt_version`, `temperature` (0), `run_id`; `runs.settings` snapshots the
thresholds in force.
**Why:** needed for debugging, demonstrating failures, prompt/model iteration,
and reproducibility (corrections K, L). Cheap — one TEXT column per chunk.
**Rejected:** keeping only the transformed facts (can't tell an extraction bug
from a transformation bug); relying on logs (not queryable, not shipped).

## D19 — Retrieval weights & thresholds are config, tuned by the eval harness

**Decision:** `retrieval_weight_{embedding,predicate,bm25}`, `top_k`,
`candidate_threshold`, `predicate_similarity_threshold`,
`relationship_confidence_threshold`, `numeric_equivalence_tolerance`,
`numeric_contradiction_threshold` are all `Settings` fields. Phase-0 numbers are
*starting defaults*; Phase 10's harness sweeps them against expected-property
specs and the chosen values are written into `.env.example` with a comment citing
the tuning table.
**Why:** "do not hard-code arbitrary weights unless evaluation shows they work"
(correction H). Config + a sweep makes the choice evidence-based and visible.
**Rejected:** baking `0.5/0.3/0.2` into code; guessing thresholds and never
checking.

## D20 — Evaluation is a first-class deliverable with expected *properties*

**Decision:** `evaluation/cases/{corroboration,contradiction,contextual,failure}/`
hold property specs (e.g. "a cross-document `CORROBORATES` with differing raw
wording and both sides `VERIFIED` exists"), not answer keys. `evaluation/harness.py`
runs the pipeline and checks them, plus a guard test that disabling the grounding
gate flips a property to FAIL. Phase 10, before the API.
**Why:** the assignment's four cases must be *demonstrated by the system*, and
"do not build a fake evaluator that checks hard-coded facts" (correction N).
Properties generalize to unseen PDFs; answer keys don't.
**Rejected:** asserting specific figures/relationships; a smoke test that only
checks the code runs.

## D21 — PyMuPDF for PDF parsing

**Decision:** PyMuPDF (`fitz`) for metadata + per-page text + geometry;
`tesseract` (via `pytesseract`) only as an OCR fallback for near-empty pages.
**Why:** one fast in-process C-backed library covers text, page size, and word
boxes; no shell-out for the common path; permissive-enough licence for a
prototype. Matches the stack the reviewer specified.
**Rejected:** `pdfplumber` + `pypdf` (two libs for what one does; slower on the
A3 report); `pdftotext` shell-out (loses geometry, adds a subprocess).
**Revisit when:** table-heavy pages (RBI appendix) need real structure — add a
layout/table extractor before chunking; the evidence model already allows bbox.

## D22 — Phase 8 retrieval: high recall first, lexical baseline, signals not scores

**Decision:** candidate retrieval (`app/retrieve.py`) is a two-stage,
**read-only**, deterministic pass over `ELIGIBLE_FOR_REASONING` facts. Stage A
blocks on shared structure (resolved entity, exact normalized predicate, ≥ N
shared content tokens) via an inverted index, ranks with an explicit
config-weighted `retrieval_score`, and keeps the top `FKL_RETRIEVAL_TOP_K` per
fact. Stage B (`app/signals.py`) attaches a full `SignalSet` of explicit
comparison signals. Candidate pairs are **returned, not persisted** — no
candidate-pair table, no migration.
**Why:**
- *High recall first, precision later.* A retrieved pair is only a *candidate for
  downstream reasoning*; Phase 9 decides `CORROBORATES` / `CONTRADICTS` / etc.
  Retrieval must not drop a pair just because periods, scope, currency, or
  modality differ — those differences are exactly what Phase 9 reconciles, so
  they are emitted as **signals**, never used as retrieval exclusions.
- *No relationship classification in Phase 8.* `CandidatePair` has no `category`
  field. `relationships` is untouched.
- *Explicit signals, not one opaque number.* Every signal
  (`period_relation`, `scope_conflict`, `unit_equivalent`,
  `base_value_delta_pct`, `modality_comparable`, `publication_gap_days`, …) is a
  named, inspectable field on `SignalSet`. `retrieval_score` is a documented
  weighted formula over `Settings` values — not a truth score, not a
  contradiction score.
- *Transient pairs.* Persisting a candidate-pair table would add a migration and
  a second place lifecycle state could leak. Deterministic recomputation gives
  "re-run adds no pairs" for free, and Phase 9 can persist what it keeps.
**Embedding decision — deferred (again).** The planned `name+context cosine` rung
and `app/embed.py` (deferred from Phase 7 to Phase 8) are **still deferred**:
`fastembed` / `numpy` are not installed in this environment, and the brief is
explicit that embeddings must not be forced when the environment can't support a
robust local implementation. `facts.embedding` / `facts.predicate_embedding`
stay NULL; `retrieval_weight_embedding` stays in `Settings` as a reserved knob.
The deterministic path (entity block + `rapidfuzz` predicate similarity + token
overlap) covers the acceptance set — the same-entity block retrieves synonym
predicates ("employees" ↔ "headcount") with zero shared tokens. Lexical-only
retrieval of true synonyms across *different* entities is the known gap; adding
the embedding rung is the documented next enhancement (see RISKS).
**Candidate ranking.**
`retrieval_score = (w_entity·[same entity] + w_predicate·predicate_sim +
w_bm25·lexical_overlap) / (w_entity + w_predicate + w_bm25)`. Entity and
exact-predicate pairs bypass `FKL_RETRIEVAL_CANDIDATE_THRESHOLD` (they are strong
structural candidates); everything else must clear it. `FKL_RETRIEVAL_TOP_K` is
the final bound, so `#pairs ≤ top_k · n_eligible` always holds. All weights and
thresholds are `Settings` fields (D19) — Phase 10's harness tunes them.
**Rejected:** an embedding-only or single-score retriever (opaque, and no local
model here); persisting candidate pairs (migration + lifecycle surface for no
Phase-8 benefit); letting a context mismatch prune a pair (destroys the
reconciliation cases); classifying relationships in retrieval (that is Phase 9).
**Revisit when:** the eligible set outgrows ~1e4 facts (swap the inverted index
for an ANN index — `retrieve_candidates` return type hides it), or Phase 10
shows lexical recall missing cross-entity synonym pairs (add the embedding rung).

## D23 — Phase 9 reasoning: deterministic verdict first and final; LLM proposes, never decides

**Decision:** `app/reason.py` classifies a Phase-8 `CandidatePair` in two stages.
`deterministic_verdict(signals)` runs a fixed, config-thresholded rule ladder
that returns a final category for every case it can settle
(`CORROBORATES` / `CONTRADICTS` / `DIFFERENT_CONTEXT` / `TEMPORAL_EVOLUTION` /
`UNCERTAIN`). Only when it returns `None` — a genuinely *semantic* question
(predicate synonymy, statement polarity) — is a confirmer consulted, and its
proposal is re-checked by `_validate_llm`, which can `accept` / `override` /
`downgrade` it. Deterministic logic therefore has the first and the last word.
**Why:**
- The signals for numeric equality/inequality, units, currency, period, scope,
  modality, and provenance are already computed deterministically (Phase 6/8).
  Letting the LLM re-decide them would reintroduce non-determinism and cost for
  no gain, and would risk it "reconciling" a real contradiction or inventing one.
- The assignment's core risk is a *false* CONTRADICTS/CORROBORATES. Conservative
  deterministic rules + an aggressive `UNCERTAIN` default (used whenever entity,
  period, predicate, or comparison semantics are unclear) is the safe posture.
- The system must run with **no API key**. Every deterministic category is
  produced without one; a missing confirmer only means the residual semantic
  pairs become `UNCERTAIN`, never that the run fails.
**Category definitions (conservative).** `CORROBORATES` needs same entity,
equivalent predicate, compatible period (`equal`/`overlaps` for numeric),
no scope conflict, comparable modality, and — for numbers — unit-equivalence with
`base_value_delta_pct ≤ FKL_NUMERIC_EQUIVALENCE_TOLERANCE`. `CONTRADICTS` needs
the same context *and* `base_value_delta_pct > FKL_NUMERIC_CONTRADICTION_THRESHOLD`
(the gap between the two tolerances is `UNCERTAIN`, not a coin flip).
`DIFFERENT_CONTEXT` is chosen whenever a `scope_conflict`, a currency/unit
difference, a period-type difference, or a modality difference could explain the
gap — `context_dimension` names it. `TEMPORAL_EVOLUTION` is distinct periods
(`adjacent`/`disjoint`/`same_year`) + comparable modality + a changed value/
statement — "different numbers across different periods" is never a contradiction.
`UNCERTAIN` is the default for everything unsettled.
**Persistence.** Reuses the Phase-3 `relationships` table and `add_relationship`
(canonical `(min,max)`, first-write-wins → idempotent). Phase 3 already reserved
`llm_used` / `llm_proposed_category` / `validation_action` / `validation_notes`;
Phase 9 wires them into `RelationshipIn` and the INSERT (additive, backward
compatible — no migration). Every `UNCERTAIN` also writes a
`failures(relationship_uncertain)` row so `GET /failures` (Phase 11) surfaces the
non-decisions. A relationship is only ever written between two
`ELIGIBLE_FOR_REASONING` facts (guard in `reason_pair` and in `add_relationship`).
**LLM contract.** `AnthropicRelationshipConfirmer.classify_relationship` receives
a minimal structured packet (the two facts' fields + evidence quotes + the signal
set — never the corpus), returns strict json-schema output, and is explicitly
instructed not to invent values/periods/units/scope, not to override evidence or
normalized numbers, not to convert currencies, and to prefer `UNCERTAIN`. Prompt
is versioned (`app/prompts/relationship_v1.md`, `FKL_RELATIONSHIP_PROMPT_VERSION`).
**Confidence** is a documented function of the signals, not a probability — it
scales with the value delta relative to the two tolerances, is capped at the
LLM's own confidence when used, and at 0.6 when either side is `PARTIAL`.
**Rejected:** "LLM classifies, we log it" (unaccountable, non-deterministic,
needs a key); pure rules with no semantic step (can't judge predicate synonymy or
statement polarity); a second relationship store or a schema redesign (the
Phase-3 table already fits); forcing a binary CORROBORATES/CONTRADICTS instead of
`UNCERTAIN`; re-running retrieval inside Phase 9.
**Revisit when:** Phase 10's harness has labelled properties — tune the tolerance
knobs and the confidence formula against them; consider an `add_relationship`
upsert if re-running reasoning in place (not just on a fresh DB) becomes a need.
