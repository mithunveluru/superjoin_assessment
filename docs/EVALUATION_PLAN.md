# Evaluation Plan (revised)

Evaluation is a **first-class deliverable** (Phase 10, before the API). Two
layers:

1. **Unit tests** — deterministic components, exact expected values, no corpus
   dependence (or a tiny recorded LLM fixture).
2. **Evaluation harness** — runs the real pipeline on a corpus and checks
   **expected properties**, never hard-coded answers. Also sweeps the config
   thresholds so their values are chosen from data (DECISIONS D19).

`pytest` markers: default suite is offline; `-m llm` runs the parts that call
Claude (cached DB, runs once).

---

## 1. Directory layout

```
evaluation/
  harness.py                 # run pipeline on a corpus, check properties, sweep thresholds
  cases/
    corroboration/*.yaml     # expected-property specs (NOT answer keys)
    contradiction/*.yaml
    contextual/*.yaml        # DIFFERENT_CONTEXT + TEMPORAL_EVOLUTION
    failure/*.yaml
  reports/                   # harness output (gitignored except a sample)
tests/
  test_eval_harness.py       # asserts the harness itself is honest
```

A property spec is corpus-scoped and states a **shape**, e.g.:

```yaml
# evaluation/cases/corroboration/cross_document_numeric.yaml
corpus: delhivery
property: >
  There exists a CORROBORATES relationship whose two facts are from
  different documents, whose object_raw strings differ, whose unit_norm or
  magnitude differ, both facts evidence_status in (VERIFIED, PARTIAL) with at
  least one VERIFIED, base_value_delta_pct <= numeric_equivalence_tolerance.
must_hold: true
```

No spec names an entity, a figure, or a filename.

---

## 2. Unit test coverage

| Area | File | Asserts |
|---|---|---|
| Config | `test_config.py` | defaults; `FKL_*` env override; `llm_api_key()` reflects `ANTHROPIC_API_KEY` |
| DB / schema | `test_db.py` | all tables + indexes present; `init_db` idempotent; `foreign_keys`/`journal_mode` pragmas; FK + every documented `CHECK` (lifecycle vocab, `fact_a_id<fact_b_id`, the two cross-column fact invariants) raise; insert/read round-trip; `transaction()` rollback |
| Ingestion (Ph2) | `test_ingest.py` | `pages` count == PyMuPDF page count per starter PDF; offset round-trip via owning chunk; no chunk crosses a page; provenance fields populated where present; reject non-PDF/oversize/encrypted; sha256 dedup |
| Fact/evidence store (Ph3) | `test_facts_store.py` | numeric + semantic round-trip incl. every numeric-representation column + JSON; `facts_fts` findable after insert; `quarantine()` sets state + `reasoning_eligible=0` + writes `failures`; helpers reject invariant violations |
| Extraction (Ph4) | `test_extract.py` | every `RawFact` validates; `modality` in the controlled set; offsets within page; `raw_extractions` row per chunk; malformed item → `failures(extraction_unparsed)`, not dropped; boilerplate page → ~0 facts; parser covered offline by a recorded fixture |
| Grounding (Ph5) | `test_ground.py` | exact match ⇒ `page.text[cs:ce]==quote` + `VERIFIED`; fabricated quote ⇒ `UNVERIFIED` + `QUARANTINED` + `failures` row + absent from any eligible set; whitespace-mangled real quote ⇒ `normalized_exact`/`fuzzy` + `PARTIAL`; numeric not re-derivable ⇒ `numeric_rederivation=failed` |
| Normalization (Ph6) | `test_normalize.py` | DATA_MODEL numeric table + period table reproduce exactly; `(452)`→−452; `₹8,142 crore`→base 8.142e10 with all pieces; `5%`→ratio 0.05; `781 Bps`→0.0781; `FY2025/26`→(2025-04-01,2026-04-01) under a resolved convention; **every set normalized field has a non-null `*_raw`**; unparseable → `failures(normalization_failed)`, not promoted |
| Entities (Ph7) | `test_entities.py` | {"Delhivery","Delhivery Limited","the Company","SSN Logistics Private Limited"} → one entity + one `derived_fact` alias; distinct person stays separate; two different orgs don't merge; LLM used only for borderline clusters; **module contains no starter entity strings** |
| Retrieval (Ph8) | `test_retrieve.py` | pair count ≤ `top_k · n_eligible`; every pair shares a block; idempotent re-run; changing `FKL_RETRIEVAL_TOP_K` changes output; a DATASET_ANALYSIS candidate (C1) is retrieved |
| Signals (Ph8) | `test_signals.py` | `unit_equivalent`: crore↔million yes, %↔ratio yes, INR≠USD, %≠count; `base_value_delta_pct` on `base_value`; `period_relation` equal/overlaps/contains/disjoint/unknown; `scope_conflict` keys; `modality_pair`; `vintage_differs`; `publication_gap_days` |
| Reasoning (Ph9) | `test_reason.py` | synthetic pairs → each of the 5 categories with correct `validation_action`; **LLM `CONTRADICTS` on equal-after-normalization numbers → overridden to `CORROBORATES`**; **disjoint historical periods + differing value → `TEMPORAL_EVOLUTION`, not `CONTRADICTS`**; `scope_conflict` explaining a gap → `DIFFERENT_CONTEXT` with `context_dimension`; `FORECAST` vs `HISTORICAL` → never `CONTRADICTS`; ineligible fact never in a relationship; `confidence` capped when a side is `PARTIAL` |
| Harness honesty (Ph10) | `test_eval_harness.py` | disabling the grounding gate flips a `must_hold` property to FAIL; a fabricated-value pair fails the corroboration property |

---

## 3. Evaluation harness (Phase 10)

`python -m evaluation.harness --corpus tests/data/<name>` :

1. Ingest + process every PDF in the corpus (real pipeline).
2. For each `evaluation/cases/**/*.yaml` matching the corpus, evaluate the
   property as a SQL/þython predicate over the resulting DB → PASS / FAIL, with
   the supporting rows printed.
3. **Threshold sweep:** for a small grid of
   `candidate_threshold × top_k × predicate_similarity_threshold ×
   relationship_confidence_threshold`, re-run steps 2 and report a table:
   (config → #properties PASS, #relationships, #UNCERTAIN, #quarantined). The
   config that maximises PASS while keeping `UNCERTAIN`/quarantine sane is written
   to `.env.example` with a comment citing the run.
4. Global invariants checked every run: every relationship has evidence on both
   sides; **no `UNVERIFIED` / non-`reasoning_eligible` fact participates**; every
   `TEMPORAL_EVOLUTION`/`DIFFERENT_CONTEXT` has a `context_dimension`; every
   `UNCERTAIN` and every `failures` row has a `reason`.

---

## 4. The four required cases as properties

The harness asserts the **property**; the README/video shows the **actual
example** the run found (§6). No expected-answer table in code. Design targets
reference DATASET_ANALYSIS §12 candidates but are not asserted literally.

### Case 1 — Corroboration, expressed differently
`∃` `CORROBORATES` with `fact_a.document_id ≠ fact_b.document_id`, differing
`object_raw`, differing `unit_norm`/`magnitude` **or** `predicate`, both sides
`evidence_status ∈ {VERIFIED, PARTIAL}` (≥1 `VERIFIED`),
`base_value_delta_pct ≤ numeric_equivalence_tolerance`.
Target: C1 (₹8,142 crore ≡ ₹81,415.38 million, FY24).

### Case 2 — Genuine or likely contradiction
`∃` `CONTRADICTS` with: same entity, `predicate_sim ≥ predicate_similarity_
threshold`, `unit_equivalent`, `period_relation ∈ {equal, overlaps}`,
`scope_conflict = ∅`, `modality_pair` both in `{HISTORICAL, ASSERTED}`,
`base_value_delta_pct > numeric_contradiction_threshold`, both sides grounded,
`validation_action ∈ {accepted, ...}` (i.e. deterministic logic did **not**
demote it), `reasoning` explains the conflict.
Target: a real figure clash the run surfaces (e.g. an overlapping-period
same-scope numeric disagreement). **Not** the "director active vs resigned"
shape — that is `TEMPORAL_EVOLUTION` (Case 3 family) unless periods overlap.
If the corpus yields no face-value contradiction, the run **records that**
(harness note) and Case 2 is shown from two *real extracted facts* placed in the
relationship view with the system's own reasoning, clearly labelled as
constructed.

### Case 3 — Apparent contradiction explained by context
`∃` a `DIFFERENT_CONTEXT` **or** `TEMPORAL_EVOLUTION` relationship where
`base_value_delta_pct > numeric_contradiction_threshold` (it *looks* like a
conflict) but `context_dimension` is set and `reasoning` names the reconciling
dimension; both sides grounded.
Targets: C2 (`scope:basis`), C3 (`scope:counting_basis`), C4 (`time` →
`TEMPORAL_EVOLUTION`), C8/C9/C10 (`vintage` / `time`).

### Case 4 — Extraction or reasoning failure, and its handling
`GET /failures` (and the `failures` table) is non-empty with ≥1 of:
`extraction_unparsed`, `grounding_failed`, `context_incomplete`, `ocr_page`,
`normalization_failed`, `entity_ambiguous`, `relationship_uncertain`. Each row
has a machine-readable `reason`. The failing fact is `QUARANTINED` (or not
promoted) and **absent from every relationship**. README "Limitations and Next
Steps" names the concrete improvement (e.g. layout-aware table extraction for the
RBI appendix; a second-pass re-ask for `context_complete=0` facts).

---

## 5. Guard tests (the harness must have teeth)

- With the grounding gate disabled, a `must_hold` corroboration property → FAIL.
- With a synthetic pair of fabricated equal values injected, the Case-1 property
  → FAIL (it must not be satisfiable by unsupported facts).
- With `signals.validate()` disabled, the "LLM `CONTRADICTS` on equal numbers"
  reasoning test → FAIL.

---

## 6. Observed examples (filled after Phase 13 — observations, not logic)

Emitted by the harness from an actual run; used only for the README/video. Format
per assignment (both sides shown for cases 1–3):

```
CASE 1 — CORROBORATES
  Fact A: <subject> · <predicate> · <value_raw>   [<doc>, printed p.<x> / PDF p.<i>]
    evidence (<verification_method>): "<verbatim quote>"
  Fact B: <subject> · <predicate> · <value_raw>   [<doc>, printed p.<y> / PDF p.<j>]
    evidence (<verification_method>): "<verbatim quote>"
  signals: <json>   llm_proposed: <cat>   final: CORROBORATES (<validation_action>)
  reasoning: "<system reasoning>"   confidence: <n>

CASE 2 — CONTRADICTS            … same shape …
CASE 3 — DIFFERENT_CONTEXT / TEMPORAL_EVOLUTION (context_dimension=<…>)   … same shape …
CASE 4 — FAILURE
  failure_type: <type>   reason: <reason>   ref: <table>/<id>   location: <doc/page>
  handling: <text>   improvement: <text>
```

---

## 7. Demo strategy (≤ 3 minutes)

Single live screen recording, no cuts hiding state, no hard-coded UI examples.

| t | Beat | Shown |
|---|---|---|
| 0:00–0:25 | Upload & process | drag a starter PDF (+ ideally one unseen PDF); status `uploaded → processing → done`; `runs` stats (pages, chunks, facts_extracted/grounded/quarantined, relationships, cost) |
| 0:25–0:50 | Grounded fact | Facts view, filter to one entity, expand: `value_raw` → `base_value`, reporting period, scope, modality, and the verification detail (`verification_method`, `numeric_rederivation`, `notes`) |
| 0:50–1:25 | Corroboration (Case 1) | Relationships → CORROBORATES → the revenue card: Fact A (₹ crore, deck) vs Fact B (₹ million, annual report), both quotes, signals (`unit_equivalent`, `base_value_delta_pct≈0`), proposed-vs-final category, reasoning |
| 1:25–2:00 | Contradiction (Case 2) | CONTRADICTS card: same entity/period/scope, values clash, both quotes, `validation_action=accepted`, reasoning |
| 2:00–2:35 | Context / temporal (Case 3) | a card where values look contradictory; badge `Reconciled by context` or `Temporal evolution`; `context_dimension` (`scope:basis` / `time` / `vintage`); reasoning names the dimension; both quotes |
| 2:35–3:00 | Failure (Case 4) | Failures view: a quarantined extraction or OCR/table page or `relationship_uncertain`, each with `failure_type` + `reason`; one line on handling + improvement |

Key-less review: `samples/` holds exported `GET /facts` + `/relationships` JSON +
screenshots for both corpora.
