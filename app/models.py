"""Pydantic models used across phases (see docs/API_DESIGN.md). Grows one
section per phase; no logic here.

The controlled vocabularies below (``Literal[...]``) mirror the CHECK constraints
in app/schema.sql + app/db.py migrations, and the frozensets in app/facts.py.
schema.sql is the source of truth; keep the three in sync."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# --- Phase 1: health ---------------------------------------------------------


class DatabaseHealth(BaseModel):
    path: str
    ok: bool
    tables: int


class LLMHealth(BaseModel):
    provider: str
    model: str
    api_key_present: bool


class HealthResponse(BaseModel):
    status: str
    version: str
    database: DatabaseHealth
    llm: LLMHealth


# --- Phase 2: ingestion ----------------------------------------------------


class IngestResult(BaseModel):
    """Outcome of ``app.ingest.ingest_pdf``. Not an API response yet — a
    service-level return value the Phase 11 API will wrap."""

    document_id: int
    sha256: str
    status: str  # documents.status: 'ingested' | 'failed' | ...
    duplicate: bool
    page_count: int
    pages_text_extracted: int
    pages_low_text: int
    pages_empty: int
    pages_extraction_error: int
    chunk_count: int


# --- Phase 3: canonical fact / evidence / relationship persistence --------

FactType = Literal["numeric", "semantic", "temporal", "categorical"]
LifecycleState = Literal[
    "RAW", "CANDIDATE", "GROUNDED", "NORMALIZED", "ELIGIBLE_FOR_REASONING", "QUARANTINED"
]
Modality = Literal["ASSERTED", "HISTORICAL", "ESTIMATED", "FORECAST", "TARGET", "UNCERTAIN"]
EvidenceStatus = Literal["VERIFIED", "PARTIAL", "UNVERIFIED"]
VerificationMethod = Literal["exact", "normalized_exact", "fuzzy", "unavailable", "unverified"]
NumericRederivation = Literal["not_applicable", "success", "failed"]
ReportingPeriodType = Literal[
    "instant", "quarter", "half_year", "fiscal_year", "calendar_year", "range", "unknown"
]
RelationshipCategory = Literal[
    "CORROBORATES", "CONTRADICTS", "DIFFERENT_CONTEXT", "TEMPORAL_EVOLUTION", "UNCERTAIN"
]


class FactIn(BaseModel):
    """Input DTO for ``app.facts.insert_fact``. CLAIM + CONTEXT + PROVENANCE +
    lifecycle. One model serves numeric, semantic, temporal, and categorical
    facts — the numeric-representation fields are simply unused for non-numeric."""

    # --- claim ---
    document_id: int
    page_index: int
    subject_raw: str
    predicate: str
    object_raw: str
    fact_type: FactType
    predicate_norm: str = ""
    subject_entity_id: int | None = None
    value_text: str | None = None

    # --- numeric representation (raw + normalized; nothing lost) ---
    value_raw: str | None = None
    numeric_value: float | None = None
    magnitude: str | None = None
    magnitude_factor: float | None = None
    base_value: float | None = None
    currency: str | None = None
    is_percentage: bool = False
    percentage_ratio: float | None = None
    unit_raw: str | None = None
    unit_norm: str | None = None

    # --- context ---
    reporting_period_raw: str | None = None
    reporting_period_start: str | None = None
    reporting_period_end: str | None = None
    reporting_period_type: ReportingPeriodType | None = None
    scope: dict[str, Any] | None = None
    qualifiers: list[str] | None = None
    modality: Modality = "ASSERTED"
    context_complete: bool = False

    # --- lifecycle / grounding (this phase persists; later phases transition) ---
    lifecycle_state: LifecycleState = "CANDIDATE"
    evidence_status: EvidenceStatus = "UNVERIFIED"
    reasoning_eligible: bool = False
    quarantine_reason: str | None = None

    # --- provenance / reproducibility ---
    run_id: int | None = None
    raw_extraction_id: int | None = None
    raw_payload: dict[str, Any] | list[Any] | str | None = None
    extraction_model: str | None = None
    prompt_version: str | None = None
    extraction_temperature: float | None = None
    extracted_at: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> FactIn:
        if self.fact_type == "numeric" and self.value_raw is None and self.numeric_value is None:
            raise ValueError("numeric fact requires value_raw and/or numeric_value")
        if (
            self.numeric_value is not None
            and self.magnitude_factor is not None
            and self.base_value is not None
        ):
            expect = self.numeric_value * self.magnitude_factor
            if abs(self.base_value - expect) > max(1.0, abs(expect)) * 1e-6:
                raise ValueError(
                    f"base_value {self.base_value} != numeric_value*magnitude_factor {expect}"
                )
        if self.reasoning_eligible and self.evidence_status == "UNVERIFIED":
            raise ValueError("reasoning_eligible fact cannot have UNVERIFIED evidence_status")
        if self.lifecycle_state == "QUARANTINED" and self.reasoning_eligible:
            raise ValueError("QUARANTINED fact cannot be reasoning_eligible")
        return self


class EvidenceIn(BaseModel):
    """Input DTO for ``app.facts.attach_evidence``. The persistence layer
    resolves ``page_id`` from (document_id, page_index) and validates the
    FACT -> EVIDENCE -> (CHUNK ->) PAGE -> DOCUMENT chain + offset validity."""

    document_id: int
    page_index: int
    quote: str
    page_id: int | None = None
    chunk_id: int | None = None
    printed_label: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    method: Literal["text_layer", "ocr"] = "text_layer"
    verification_method: VerificationMethod = "unverified"
    fuzzy_score: float | None = None
    numeric_rederivation: NumericRederivation = "not_applicable"
    evidence_status: EvidenceStatus = "UNVERIFIED"
    evidence_score: float | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _offsets_paired(self) -> EvidenceIn:
        if (self.char_start is None) != (self.char_end is None):
            raise ValueError("char_start and char_end must be provided together or both omitted")
        if self.char_start is not None and self.char_start >= self.char_end:  # type: ignore[operator]
            raise ValueError("char_start must be < char_end")
        return self


class RelationshipIn(BaseModel):
    """Input DTO for ``app.facts.add_relationship``. Phase 3 only persists;
    no relationship is inferred here. Stored canonically as (min, max) fact id."""

    fact_a_id: int
    fact_b_id: int
    category: RelationshipCategory
    context_dimension: str | None = None
    reasoning: str | None = None
    confidence: float | None = None
    deterministic_signals: dict[str, Any] = Field(default_factory=dict)
    run_id: int | None = None
    model_name: str | None = None
    prompt_version: str | None = None


# --- Phase 4: candidate fact extraction ----------------------------------

# The LLM contract. Vocabulary fields are plain `str` on purpose: the LLM
# *proposes* structure; deterministic code in app/extract.py validates it
# (app/facts.FACT_TYPES etc.), so one bad candidate is rejected without failing
# the whole response. extra="forbid" -> an unknown key is a structural violation.
class RawCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str
    predicate: str
    object: str
    fact_type: str
    raw_value_text: str | None = None
    parsed_value: float | None = None
    unit: str | None = None
    magnitude: str | None = None
    currency: str | None = None
    percentage: bool = False
    ratio: float | None = None
    reporting_period: str | None = None
    period_type: str | None = None
    scope: str | None = None
    qualifiers: list[str] | None = None
    modality: str | None = None
    quote: str
    char_start: int
    char_end: int


class RawExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facts: list[RawCandidate] = Field(default_factory=list)


class LLMExtraction(BaseModel):
    """What ``app.llm`` returns for one chunk — always carries the raw text so
    ``facts.raw_payload`` / ``raw_extractions.raw_response`` can preserve it."""

    raw_text: str | None
    parsed: RawExtraction | None
    error_code: str | None = None          # api_error | auth | rate_limit | timeout |
    error_detail: str | None = None        # malformed_response | truncated_response | refusal
    model: str
    prompt_version: str
    stop_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


class ExtractionRunResult(BaseModel):
    """Outcome of ``app.extract.extract_document`` — service return value +
    lightweight observability (see also ``app.extract.extraction_summary``)."""

    run_id: int
    document_id: int
    status: str                            # runs.status: 'done' | 'failed'
    chunks_processed: int
    candidates_generated: int
    candidates_persisted: int
    candidates_rejected: int
    extraction_errors: int                 # chunk-level LLM/API failures
    facts_by_type: dict[str, int] = Field(default_factory=dict)
    estimated_cost_usd: float = 0.0
    error: str | None = None


# --- Phase 5: evidence verification -------------------------------------

class VerificationResult(BaseModel):
    """Outcome of verifying one candidate fact's evidence against persisted
    Phase-2 source text. Deterministic; no LLM."""

    fact_id: int
    evidence_id: int | None = None
    evidence_status: EvidenceStatus                         # VERIFIED | PARTIAL | UNVERIFIED
    verification_method: str                                # exact | normalized_exact |
    numeric_rederivation: NumericRederivation               #   recovered_exact | fuzzy | unverified
    lifecycle_state: str                                    # facts.lifecycle_state after this step
    recovered: bool = False                                 # offsets were corrected
    fuzzy_score: float | None = None
    reason: str                                             # deterministic outcome code
    detail: str | None = None


class DocVerificationSummary(BaseModel):
    """Outcome of ``app.verify.verify_document`` — lightweight observability
    (see also ``app.verify.verification_summary``)."""

    run_id: int
    document_id: int
    status: str
    examined: int = 0
    verified: int = 0
    partial: int = 0
    unverified: int = 0
    quarantined: int = 0
    recovered: int = 0
    numeric_mismatches: int = 0
    ambiguous_matches: int = 0
    errors: int = 0
    by_method: dict[str, int] = Field(default_factory=dict)
    error: str | None = None
