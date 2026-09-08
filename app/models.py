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
    # Phase 9 — LLM proposal vs deterministic validation (both persisted)
    llm_used: bool = False
    llm_proposed_category: str | None = None
    validation_action: Literal["accepted", "overridden", "downgraded", "not_applicable"] = (
        "not_applicable"
    )
    validation_notes: str | None = None


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


# --- Phase 6: context / numeric / date / unit normalization -----------

class NormalizationResult(BaseModel):
    """Outcome of ``app.normalize.apply_normalization`` for one fact. Deterministic."""

    fact_id: int
    lifecycle_state: str                     # 'NORMALIZED' on success, else 'GROUNDED'
    numeric_normalized: bool = False
    period_type: str | None = None           # None = no period; 'unknown' = unresolvable
    base_value: float | None = None
    magnitude_factor: float | None = None
    currency: str | None = None
    unit_norm: str | None = None
    failed: bool = False
    reason: str | None = None                # 'numeric_unparseable' etc.


class DocNormalizationSummary(BaseModel):
    """Outcome of ``app.normalize.normalize_document`` — lightweight observability
    (see also ``app.normalize.normalization_summary``)."""

    run_id: int
    document_id: int
    status: str
    examined: int = 0
    normalized: int = 0
    numeric_normalized: int = 0
    period_resolved: int = 0
    period_unknown: int = 0
    period_absent: int = 0
    normalization_failed: int = 0
    errors: int = 0
    by_period_type: dict[str, int] = Field(default_factory=dict)
    fy_convention: str = "unknown"
    fy_convention_source: str = "config_default"
    error: str | None = None


# --- Phase 7: entity resolution --------------------------------------

class EntityConfirmation(BaseModel):
    """One ``llm.confirm_entities`` result for a borderline cluster. The LLM only
    ever *confirms or splits* — deterministic code decides everything else."""

    same: bool                               # True = every surface is one entity
    confidence: float = 0.0
    canonical_label: str | None = None
    groups: list[list[str]] | None = None    # when same=False: the split
    error_code: str | None = None
    error_detail: str | None = None


class EntityLink(BaseModel):
    """Outcome of resolving one subject surface to an entity."""

    surface: str
    entity_id: int | None = None             # None = left unresolved (ambiguous)
    canonical_label: str | None = None
    match_method: str | None = None          # deterministic|similarity|llm|derived_fact|anaphora
    score: float | None = None
    created: bool = False                    # a new entity row was inserted


class DocResolutionSummary(BaseModel):
    """Outcome of ``app.entities.resolve_document`` — lightweight observability
    (see also ``app.entities.resolution_summary``)."""

    run_id: int
    document_id: int
    status: str
    surfaces: int = 0
    entities_created: int = 0
    entities_linked: int = 0
    aliases_added: int = 0
    derived_aliases: int = 0
    anaphora_resolved: int = 0
    ambiguous: int = 0
    promoted_eligible: int = 0
    errors: int = 0
    by_method: dict[str, int] = Field(default_factory=dict)
    error: str | None = None


# --- Phase 8: candidate retrieval + deterministic signals ------------

PeriodRelation = Literal[
    "equal", "same_year", "contains", "overlaps", "adjacent", "disjoint", "unknown", "missing"
]


class SignalSet(BaseModel):
    """Deterministic, inspectable comparison signals for one ordered candidate
    pair (see ``app.signals.compute``). Every field is derived only from data
    already persisted by Phases 1-7 — no LLM, no unit conversion beyond the
    Phase-6 normalized representation. A signal describes a difference; it never
    decides a relationship (that is Phase 9)."""

    # entity
    entity_relation: Literal["same", "different", "unresolved"]
    subject_entity_id_a: int | None = None
    subject_entity_id_b: int | None = None
    # predicate
    predicate_exact: bool = False
    predicate_similarity: float = 0.0        # rapidfuzz token_set_ratio, 0-1
    predicate_token_overlap: float = 0.0     # Jaccard over content tokens, 0-1
    # fact type
    fact_type_a: str
    fact_type_b: str
    fact_type_match: bool
    # numeric (populated only when both facts are numeric with a base_value)
    numeric_comparable: bool = False
    base_value_a: float | None = None
    base_value_b: float | None = None
    base_value_abs_diff: float | None = None
    base_value_delta_pct: float | None = None   # |a-b| / max(|a|,|b|)
    sign_match: bool | None = None
    percentage_vs_absolute: bool | None = None  # one is_percentage, the other not
    ratio_a_to_b: float | None = None
    unit_equivalent: bool | None = None         # comparable after Phase-6 normalization
    # unit / currency
    unit_relation: Literal["same", "different", "missing"] = "missing"
    currency_relation: Literal["same", "different", "missing"] = "missing"
    # period (reporting period only — never the document date / vintage)
    period_relation: PeriodRelation = "missing"
    # scope
    scope_relation: Literal["same", "overlap", "different", "missing"] = "missing"
    scope_conflict: list[str] = Field(default_factory=list)  # keys present in both, values differ
    # modality
    modality_a: str
    modality_b: str
    modality_relation: Literal["same", "different"]
    modality_comparable: bool                # both in {ASSERTED, HISTORICAL}
    # provenance
    same_document: bool
    publication_gap_days: int | None = None  # |publication_date_a - publication_date_b|
    vintage_differs: bool = False
    reasons: list[str] = Field(default_factory=list)


class CandidatePair(BaseModel):
    """One retrieved candidate pair. ``fact_a_id < fact_b_id`` always (the same
    canonical order ``relationships`` uses). A retrieved pair is a candidate for
    downstream relationship reasoning — nothing more."""

    fact_a_id: int
    fact_b_id: int
    retrieval_score: float
    retrieval_methods: list[str]     # {"entity","predicate_exact","predicate_similar","lexical"}
    signals: SignalSet


# --- Phase 9: relationship reasoning --------------------------------

ValidationAction = Literal["accepted", "overridden", "downgraded", "not_applicable"]


class RelationshipProposal(BaseModel):
    """The LLM's *semantic proposal* for one candidate pair (see
    ``app.llm.AnthropicRelationshipConfirmer``). Vocabulary is a plain ``str`` on
    purpose — deterministic code in ``app.reason`` validates and may override it."""

    relationship: str = ""            # raw category string from the model
    confidence: float = 0.0
    reason: str = ""
    context_differences: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    error_code: str | None = None    # api_error | malformed_response | invalid_category | ...
    error_detail: str | None = None


class RelationshipDecision(BaseModel):
    """Outcome of ``app.reason.reason_pair`` for one candidate pair — the final
    category plus everything needed to explain and reproduce it."""

    fact_a_id: int
    fact_b_id: int
    category: RelationshipCategory
    context_dimension: str | None = None
    confidence: float
    reasoning: str
    deterministic_signals: dict[str, Any] = Field(default_factory=dict)
    method: Literal["deterministic", "llm_confirmed", "uncertain_no_llm"]
    llm_used: bool = False
    llm_proposed_category: str | None = None
    validation_action: ValidationAction = "not_applicable"
    validation_notes: str | None = None


class DocReasoningSummary(BaseModel):
    """Outcome of ``app.reason.reason_document`` — lightweight observability
    (see also ``app.reason.reasoning_summary``)."""

    run_id: int
    document_id: int
    status: str
    candidate_pairs: int = 0
    deterministic_decisions: int = 0
    llm_confirmed_decisions: int = 0
    uncertain_decisions: int = 0
    relationships_created: int = 0
    relationships_existing: int = 0
    llm_calls: int = 0
    llm_errors: int = 0
    validation_overrides: int = 0
    skipped: int = 0
    errors: int = 0
    by_category: dict[str, int] = Field(default_factory=dict)
    error: str | None = None


# --- Phase 11: HTTP API (see docs/API_DESIGN.md) ------------------------

class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorOut(BaseModel):
    error: ErrorDetail


class NumericRepr(BaseModel):
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


class ReportingPeriod(BaseModel):
    raw: str | None = None
    start: str | None = None
    end: str | None = None
    type: str | None = None


class EvidenceOut(BaseModel):
    page_index: int | None = None
    printed_label: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    quote: str | None = None
    method: str | None = None
    verification_method: str | None = None
    fuzzy_score: float | None = None
    numeric_rederivation: str | None = None
    evidence_status: str | None = None
    evidence_score: float | None = None
    notes: str | None = None


class ReproOut(BaseModel):
    extraction_model: str | None = None
    prompt_version: str | None = None
    extraction_temperature: float | None = None


class EntityRef(BaseModel):
    id: int
    canonical_label: str | None = None


class FactOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    document_id: int
    document_title: str | None = None
    publisher: str | None = None
    disclosure_type: str | None = None
    publication_date: str | None = None
    data_vintage: str | None = None
    page_index: int
    printed_label: str | None = None
    lifecycle_state: str
    reasoning_eligible: bool
    subject_raw: str
    entity: EntityRef | None = None
    predicate: str
    predicate_norm: str | None = None
    object_raw: str
    fact_type: str
    numeric: NumericRepr | None = None
    value_text: str | None = None
    reporting_period: ReportingPeriod
    scope: dict[str, Any] | None = None
    qualifiers: list[str] = Field(default_factory=list)
    modality: str
    context_complete: bool = False
    evidence_status: str
    evidence: EvidenceOut | None = None
    repro: ReproOut
    # detail-only
    context_window: str | None = None
    raw_extraction: dict[str, Any] | None = None
    relationships: list[dict[str, Any]] | None = None


class FactsPage(BaseModel):
    items: list[FactOut]
    total: int


class RelationshipOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    category: str
    category_label: str
    context_dimension: str | None = None
    fact_a: FactOut
    fact_b: FactOut
    deterministic_signals: dict[str, Any] = Field(default_factory=dict)
    llm_used: bool = False
    llm_proposed_category: str | None = None
    validation_action: str = "not_applicable"
    validation_notes: str | None = None
    reasoning: str | None = None
    confidence: float | None = None
    created_at: str | None = None


class RelationshipsPage(BaseModel):
    items: list[RelationshipOut]
    total: int


class RunOut(BaseModel):
    run_id: int
    run_type: str
    status: str
    stage: str | None = None
    stats: dict[str, Any] = Field(default_factory=dict)
    model_name: str | None = None
    prompt_versions: dict[str, Any] | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None


class DocumentCounts(BaseModel):
    pages: int = 0
    facts: int = 0
    facts_unverified: int = 0
    facts_quarantined: int = 0
    eligible_facts: int = 0
    relationships: int = 0
    by_category: dict[str, int] = Field(default_factory=dict)
    failures: int = 0


class DocumentOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    sha256: str
    original_filename: str | None = None
    title: str | None = None
    publisher: str | None = None
    disclosure_type: str | None = None
    document_date: str | None = None
    publication_date: str | None = None
    data_vintage: str | None = None
    fy_convention: str | None = None
    fy_convention_source: str | None = None
    page_count: int | None = None
    status: str
    status_detail: str | None = None
    duplicate: bool = False
    uploaded_at: str | None = None
    processed_at: str | None = None
    counts: DocumentCounts | None = None
    latest_run: RunOut | None = None


class DocumentsPage(BaseModel):
    items: list[DocumentOut]
    total: int


class ProcessOut(BaseModel):
    run_id: int
    document_id: int
    status: str
    stage: str | None = None
    started_at: str | None = None


class StatusOut(BaseModel):
    document_id: int
    status: str
    run: RunOut | None = None


class AliasOut(BaseModel):
    surface: str
    normalized: str | None = None
    match_method: str | None = None
    score: float | None = None
    source_fact_id: int | None = None


class EntityOut(BaseModel):
    id: int
    canonical_label: str
    entity_type: str | None = None
    normalization_key: str | None = None
    alias_count: int = 0
    fact_count: int = 0
    resolution_method: str | None = None
    resolution_score: float | None = None
    llm_confirmed: bool = False
    llm_confidence: float | None = None
    aliases: list[AliasOut] | None = None
    sample_facts: list[FactOut] | None = None


class EntitiesPage(BaseModel):
    items: list[EntityOut]
    total: int


class FailureOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    failure_type: str
    reason: str
    ref_table: str | None = None
    ref_id: int | None = None
    detail: dict[str, Any] | list[Any] | str | None = None
    document_id: int | None = None
    run_id: int | None = None
    created_at: str | None = None
    fact: FactOut | None = None
    relationship: RelationshipOut | None = None


class FailuresPage(BaseModel):
    items: list[FailureOut]
    total: int
    counts_by_type: dict[str, int] = Field(default_factory=dict)
