# API Design

FastAPI. JSON everywhere except `POST /documents` (multipart). All list endpoints
paginate with `?limit=` (default 50, max 200) and `?offset=`. Errors use a common
body `{"error": {"code": str, "message": str}}` with standard HTTP status.

Contract is frozen before implementation (Phase 9 builds to it). Storage-agnostic
— nothing here assumes SQLite.

---

## Endpoints

### POST /documents  — upload
`multipart/form-data`, field `file` (PDF).

**Validation:** magic bytes `%PDF-`, extension `.pdf`, size ≤ `MAX_UPLOAD_MB`,
page count ≤ `MAX_PAGES`, not encrypted. Duplicate `sha256` → `200` with the
existing record and `"duplicate": true`.

**201** →
```json
{
  "id": 1,
  "sha256": "…",
  "original_filename": "01-delhivery-prospectus-2022-excerpt.pdf",
  "title": "Delhivery Limited — Prospectus",
  "publisher": null,
  "disclosure_type": null,
  "page_count": 100,
  "status": "uploaded",
  "uploaded_at": "2026-09-07T10:00:00Z"
}
```
Errors: `400 invalid_pdf`, `400 encrypted_pdf`, `413 file_too_large`,
`422 too_many_pages`.

### GET /documents  — list
`?status=` optional filter. **200** → `{"items": [<document>…], "total": 6}`.
`<document>` includes `status`, `status_detail`, and (when `done`) `counts`:
`{"pages": n, "facts": n, "facts_unverified": n, "relationships": n,
"by_category": {"CORROBORATES": n, "CONTRADICTS": n, "RECONCILES": n, "UNCERTAIN": n}}`.

### GET /documents/{id}  — detail
**200** → `<document>` + `counts` + `latest_run` (see run object below).
`404 document_not_found`.

### POST /documents/{id}/process  — run pipeline
Idempotent-ish: if a run is `running`, returns that run with `202`. Otherwise
starts a `BackgroundTask` and returns the new run.

**202** →
```json
{ "run_id": 4, "document_id": 1, "status": "running", "stage": "ingest", "started_at": "…" }
```
`404 document_not_found`, `409 already_processing` (only if a lock race loses).

### GET /documents/{id}/status  — poll
**200** →
```json
{
  "document_id": 1,
  "status": "processing",
  "run": {
    "run_id": 4, "run_type": "full", "status": "running", "stage": "extract",
    "stats": {"pages_processed": 100, "chunks_processed": 240, "llm_calls": 61,
              "facts_extracted": 130, "facts_grounded": 118,
              "facts_quarantined": 12, "relationships_produced": 0,
              "by_category": {}, "estimated_cost_usd": 0.42},
    "model_name": "claude-sonnet-5", "prompt_versions": {"extract": "v1"},
    "started_at": "…", "finished_at": null, "error": null
  }
}
```
`status` mirrors `documents.status`. On failure: `run.status="failed"`,
`run.error` set, HTTP still `200`.

### GET /facts  — list
Filters (all optional, AND-combined):
`document_id`, `entity_id`, `entity` (substring on alias/canonical),
`predicate` (substring on `predicate`/`predicate_norm`),
`type` (`numeric`|`semantic`),
`lifecycle_state` (`CANDIDATE|GROUNDED|NORMALIZED|ELIGIBLE_FOR_REASONING|QUARANTINED`),
`evidence_status` (`VERIFIED|PARTIAL|UNVERIFIED`),
`modality` (`ASSERTED|HISTORICAL|ESTIMATED|FORECAST|TARGET|UNCERTAIN`),
`reasoning_eligible` (bool), `q` (FTS over subject/predicate/object/value_text),
`period_overlaps` (ISO date or `FYxx` + a `fy_convention`).
Sort: `?sort=recent|page` (default `recent`).

**200** → `{"items": [<fact>…], "total": 130}` where `<fact>`:
```json
{
  "id": 42,
  "document_id": 3, "document_title": "…earnings presentation",
  "publisher": "…", "disclosure_type": "earnings presentation",
  "publication_date": "2024-05-17", "data_vintage": null,
  "page_index": 4, "printed_label": "5",
  "lifecycle_state": "ELIGIBLE_FOR_REASONING", "reasoning_eligible": true,
  "subject_raw": "Delhivery", "entity": {"id": 1, "canonical_label": "…"},
  "predicate": "revenue from services",
  "object_raw": "₹8,142 Cr",
  "fact_type": "numeric",
  "numeric": {
    "value_raw": "₹8,142 Cr", "numeric_value": 8142.0,
    "magnitude": "crore", "magnitude_factor": 1e7, "base_value": 8.142e10,
    "currency": "INR", "is_percentage": false, "percentage_ratio": null,
    "unit_raw": "₹ Cr", "unit_norm": "INR"
  },
  "value_text": null,
  "reporting_period": {"raw": "FY24", "start": "2023-04-01", "end": "2024-04-01", "type": "fiscal_year"},
  "scope": {"basis": "consolidated", "measure_note": "excl. traded goods"},
  "qualifiers": [], "modality": "HISTORICAL", "context_complete": true,
  "evidence_status": "VERIFIED",
  "evidence": {
    "page_index": 4, "printed_label": "5",
    "char_start": 1180, "char_end": 1223,
    "quote": "₹8,142 Cr\nFY24 revenue from services",
    "method": "text_layer",
    "verification_method": "exact", "fuzzy_score": null,
    "numeric_rederivation": "success", "evidence_status": "VERIFIED",
    "evidence_score": null,
    "notes": "Quote found verbatim on page 4; '8,142' re-parsed from the quote matches the extracted value."
  },
  "repro": {"extraction_model": "claude-sonnet-5", "prompt_version": "v1", "extraction_temperature": 0.0}
}
```

### GET /facts/{id}  — detail
**200** → `<fact>` + `context_window` (±200 chars from `pages.text` around the
span) + `raw_extraction` (`{model_name, prompt_version, raw_response}`) +
`relationships` (summaries). `404 fact_not_found`.

### GET /relationships  — list
Filters: `category` (`CORROBORATES|CONTRADICTS|DIFFERENT_CONTEXT|
TEMPORAL_EVOLUTION|UNCERTAIN`), `context_dimension`, `validation_action`,
`document_id` / `entity_id` (either side), `min_confidence`, `llm_used`.
Sort: `?sort=confidence|recent`.

**200** → `{"items": [<relationship>…], "total": 18}` where `<relationship>`:
```json
{
  "id": 7,
  "category": "DIFFERENT_CONTEXT",
  "category_label": "Reconciled by context",
  "context_dimension": "scope:basis",
  "fact_a": { "...": "<fact object, same shape as GET /facts item>" },
  "fact_b": { "...": "<fact object>" },
  "deterministic_signals": {
    "entity_match": 1.0, "predicate_sim": 0.94, "unit_equivalent": true,
    "base_value_delta_pct": 0.092, "period_relation": "equal",
    "scope_conflict": ["basis"], "modality_pair": ["HISTORICAL", "HISTORICAL"],
    "publication_gap_days": 0, "vintage_differs": false
  },
  "llm_used": true,
  "llm_proposed_category": "CONTRADICTS",
  "validation_action": "overridden",
  "validation_notes": "Periods equal and units equivalent, but scope_conflict={basis}: A standalone, B consolidated. A scope difference explains the ~9% gap → DIFFERENT_CONTEXT, not CONTRADICTS.",
  "reasoning": "Both state Delhivery FY24 revenue from operations; A standalone (₹74,540.82 mn), B consolidated (₹81,415.38 mn). The gap matches subsidiary revenue; not a conflict.",
  "confidence": 0.86
}
```

### GET /relationships/{id}  — detail
**200** → `<relationship>` with both full `<fact>` objects (incl.
`context_window`). `404 relationship_not_found`.

### GET /entities  — list
`?type=`, `?q=`. **200** → `{"items": [{"id","canonical_label","entity_type",
"alias_count","fact_count","resolution_score","llm_confirmed","llm_confidence"}], "total": n}`.

### GET /entities/{id}  — detail
**200** → entity + `aliases` (`[{surface, normalized, match_method, score,
source_fact_id}]`) + `fact_count` + sample facts. `match_method="derived_fact"`
aliases carry the `source_fact_id` of the rename statement. `404 entity_not_found`.

### GET /failures  — the "case 4" surface
Reads the `failures` table joined to its referents. Filter `?document_id=`,
`?failure_type=`.
```json
{
  "items": [
    {"id": 3, "failure_type": "grounding_failed",
     "reason": "quote_not_found_in_page", "ref_table": "facts", "ref_id": 91,
     "detail": {"quote": "…", "best_fuzzy_score": 71},
     "document_id": 2, "run_id": 9, "created_at": "…",
     "fact": {"...": "<fact object, lifecycle_state=QUARANTINED>"}},
    {"id": 4, "failure_type": "ocr_page",
     "reason": "page_char_count_below_threshold", "ref_table": "pages",
     "ref_id": 220, "detail": {"char_count": 61}, "document_id": 2, "run_id": 9},
    {"id": 5, "failure_type": "relationship_uncertain",
     "reason": "entity_match_below_threshold", "ref_table": "relationships",
     "ref_id": 40, "relationship": {"...": "<relationship object>"}}
  ],
  "total": 12,
  "counts_by_type": {"grounding_failed": 7, "ocr_page": 3, "relationship_uncertain": 2}
}
```

---

## Pydantic models (shape, not code)

- `DocumentOut`, `DocumentCounts`, `RunOut` (with `stats`, `model_name`, `prompt_versions`)
- `NumericRepr` — `{value_raw, numeric_value, magnitude, magnitude_factor, base_value, currency, is_percentage, percentage_ratio, unit_raw, unit_norm}`
- `ReportingPeriod` — `{raw, start, end, type}`
- `EvidenceOut` — `{page_index, printed_label, char_start, char_end, quote, method, verification_method, fuzzy_score, numeric_rederivation, evidence_status, evidence_score, notes}`
- `ReproOut` — `{extraction_model, prompt_version, extraction_temperature}`
- `EntityRef` — `{id, canonical_label}` (nullable)
- `FactOut` — public subset of `facts` + `document_title`/`publisher`/`disclosure_type`/`publication_date`/`data_vintage`, `lifecycle_state`, `reasoning_eligible`, `entity`, `numeric` (`NumericRepr`|null), `value_text`, `reporting_period`, `scope`, `qualifiers`, `modality`, `evidence_status`, `evidence`, `repro`
- `RelationshipSignals` — `{entity_match, predicate_sim, unit_equivalent, base_value_delta_pct, period_relation, scope_conflict, modality_pair, publication_gap_days, vintage_differs}`
- `RelationshipOut` — `{id, category, category_label, context_dimension, fact_a, fact_b, deterministic_signals, llm_used, llm_proposed_category, validation_action, validation_notes, reasoning, confidence}`
- `EntityOut`, `AliasOut`
- `FailureOut` — `{id, failure_type, reason, ref_table, ref_id, detail, document_id, run_id, created_at, fact?, relationship?}`
- `FailuresPage` — `{items: [FailureOut], total, counts_by_type}`
- `ErrorOut` — `{error: {code, message}}`

Request models: `DocumentUpload` (multipart), list-filter query params as
`FactQuery` / `RelationshipQuery` dependencies.

---

## UI ↔ API mapping

| UI page | calls |
|---|---|
| Documents | `POST /documents`, `GET /documents`, `POST /documents/{id}/process`, `GET /documents/{id}/status` (poll 2 s) |
| Facts | `GET /facts` (+filters), `GET /facts/{id}` on row expand |
| Relationships | `GET /relationships` (+category tabs), `GET /relationships/{id}` on card expand |
| Failures | `GET /failures` |
| Entities (secondary) | `GET /entities`, `GET /entities/{id}` |

Static assets (`index.html`, `app.js`, `style.css`) served from `GET /` and
`/static/*`. No auth (local prototype); note in README.
