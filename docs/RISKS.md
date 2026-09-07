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
in normal processing. Fiscal-year detection and interpretation belong to the
normalization phase (Phase 6); the Phase 2 ingestion layer does not interpret
fiscal years — `documents.fy_convention` stays `unknown` / `config_default`.

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

## Test-dependency hardening

`starlette >= 1.6` prefers the `httpx2` package for its `TestClient`; adding
`httpx2` to the dev dependencies clears that deprecation notice. One further
notice remains from `starlette.testclient` importing a deprecated
`anyio.abc.BlockingPortal` alias — this is internal to those libraries, does not
affect behaviour, and will resolve on their next release. Left as a
dependency-hardening note rather than pinned around.
