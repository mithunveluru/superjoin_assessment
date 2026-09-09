# Demo walkthrough (≤ 3 minutes)

Everything below is real functionality in this repo. The relationship examples
come from **`scripts/seed_demo.py`**, a deterministic **synthetic** two-document
fixture (entity `Acme`) — it is a *validation fixture*, not corpus-derived data.
A live Gemini extraction run on one real Delhivery PDF **was** performed
(27 pages → 53 facts, 51 grounded); no cross-document relationship between two
*real* documents is claimed. See README → *Validation status*.

## Setup (once, off camera)

```bash
pip install -r requirements.txt
python scripts/seed_demo.py           # deterministic, offline, no key
uvicorn app.main:app                  # http://localhost:8000/
```

The demo DB has 2 synthetic documents, 1 resolved entity, 7 facts (6 verified,
1 quarantined) and 15 relationships across all five categories.

---

## Script

### 0:00–0:20 — Purpose & architecture

- One line: *"extract grounded facts from PDFs, then reason about how they relate
  across documents without turning a context difference into a false
  contradiction."*
- Show the pipeline strip:
  `PDF → ingest → extract → verify → normalize → resolve → retrieve → reason → API/UI`.
- One sentence on the split: *"LLMs interpret meaning; deterministic code
  verifies evidence, normalizes numbers, and makes the final relationship
  call."*

### 0:20–0:50 — Ingest a real PDF

- **Documents** view → choose
  `starter-datasets/delhivery/03-delhivery-q4-fy24-earnings-presentation.pdf`
  → **Upload PDF**.
- Point at the new row: status `ingested`, page count (27), the counts columns.
- Click **Process**. Narrate: *"this runs the pipeline in the background against
  Gemini; without a key — or once the free-tier budget is spent — the extract
  stage fails and the row shows `failed` with the reason inline. The failure
  path is honest, not hidden."* (Status polls every 2 s.)
- Switch to the pre-seeded synthetic documents for the rest — *"these two are the
  labelled synthetic fixture so the reasoning views have data."*

### 0:50–1:20 — A grounded fact

- **Facts** view. Filter `lifecycle_state = ELIGIBLE_FOR_REASONING`.
- Expand `Acme · revenue from operations · 81,415.38 million`.
- Show: the **verbatim quote**, the document + printed page / PDF page, the
  numeric representation (`base_value = 8.141538e10`, `currency = INR`, `unit` via crore↔million), the
  reporting period (`FY24`, resolved to `[2023-04-01 → 2024-04-01)`), the scope,
  the modality, the **verification detail** (`verification_method = exact`), and
  the ±200-char **context window**.
- One line: *"the fact is only reasoning-eligible because this chain —
  FACT → EVIDENCE → CHUNK → PAGE → DOCUMENT — resolves and the quote was
  re-derived from the page."*

### 1:20–1:55 — Relationships: corroboration, contradiction, context

- **Relationships** view, category tabs.
- **CORROBORATES** — open the card: Fact A `81,415.38 million` (doc A) vs
  Fact B `8,142 Cr` (doc B), different documents, different wording. Expand:
  the signals table shows `unit_equivalent = true` (crore↔million after
  normalization) and `base_value_delta_pct ≈ 6e-5`. *"Same proposition, different
  units, both grounded."*
- **CONTRADICTS** — profit after tax `5,000.00 million` vs `8,000.00 million`, same
  entity, same period FY24, `scope_conflict` empty, `base_value_delta_pct = 0.375`
  (> the contradiction threshold), both `HISTORICAL`. *"Materially incompatible
  under comparable context — this is a real contradiction."*
- **DIFFERENT_CONTEXT** — revenue `81,415.38 million` (consolidated) vs
  `74,540.82 million` (standalone), FY24. Expand: `scope_conflict` is non-empty → the reasoning line
  says *"scope differs … different slices"*. *"The values differ ~8 %, but a
  scope difference explains it, so the system does **not** call this a
  contradiction."*

### 1:55–2:20 — Temporal evolution & uncertainty

- **TEMPORAL_EVOLUTION** — revenue FY24 vs FY23, `Δ ≈ 26 %`, `period_relation =
  adjacent`, both `HISTORICAL`. *"Different numbers across different periods is a
  value that moved over time, not a disagreement."*
- **UNCERTAIN** tab — a `revenue from operations` vs `profit after tax` pair,
  `predicate_similarity ≈ 0.36`. *"Same entity and period, but the predicates are
  not the same measure — the system abstains rather than guess. 8 of 15 pairs
  here are UNCERTAIN by design."*

### 2:20–2:40 — Failures

- **Failures** view. Point at `grounding_failed` for the quarantined fact
  (`Acme · permanent employees · 99,999`) with reason `quote_not_found in source
  text`, and the linked fact showing `QUARANTINED`.
- One line: *"a fabricated or un-verifiable extraction is quarantined — kept and
  inspectable, promoted nowhere, absent from every relationship."*
- The `relationship_uncertain` rows are the UNCERTAIN pairs, also surfaced here.

### 2:40–3:00 — Close

- *"Deterministic core, LLM only for genuine semantic ambiguity, and the
  deterministic layer always has the final say — an LLM `CONTRADICTS` on
  equal-after-normalization numbers is overridden to `CORROBORATES`."*
- *"441 tests, ruff clean, no network. A real Delhivery PDF was extracted with
  live Gemini (53 facts, 51 grounded); the reasoning demo uses a labelled
  synthetic fixture so all five categories are present. Limitations and next
  steps are in `docs/RISKS.md`."*
