"""Centralised configuration. Every tunable lives here, backed by env vars
(prefix ``FKL_``) and an optional ``.env`` file. Nothing dataset-specific.

The LLM *API key* is the one exception to the prefix: it is read from whatever
env var ``llm_api_key_env`` names (default ``ANTHROPIC_API_KEY``), so real
secrets never sit under a project-specific name and never get committed.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FKL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- storage -------------------------------------------------------------
    database_path: Path = PROJECT_ROOT / "data" / "knowledge.db"
    uploads_dir: Path = PROJECT_ROOT / "uploads"

    # --- LLM (used from Phase 4) ------------------------------------------------
    llm_provider: str = "anthropic"
    llm_model: str = "claude-sonnet-5"
    llm_api_key_env: str = "ANTHROPIC_API_KEY"
    # Sonnet 5 rejects sampling params (temperature/top_p/top_k) — the extraction
    # client does not send temperature. Kept for reproducibility docs / older models.
    llm_temperature: float = 0.0
    llm_effort: str = "low"          # output_config.effort for structured extraction
    llm_max_tokens: int = 8192       # per-chunk extraction output cap
    llm_timeout_seconds: float = 120.0
    prompt_version: str = "v1"       # extraction prompt version (app/prompts/extraction_<v>.md)
    relationship_prompt_version: str = "v1"  # app/prompts/relationship_<v>.md (Phase 9)

    # --- embeddings (used from Phase 8) --------------------------------------
    embedding_provider: str = "fastembed"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384

    # --- entity resolution (Phase 7). All lists are language-generic, never
    # dataset-specific (no "<brand> -> the Company" style alias seeding). ------
    entity_prompt_version: str = "v1"           # app/prompts/entity_confirm_<v>.md
    entity_legal_suffixes: tuple[str, ...] = (
        "limited", "ltd", "llp", "inc", "incorporated", "corp", "corporation",
        "company", "co", "plc", "pvt", "private", "gmbh", "sa", "nv", "ag",
        "pte", "llc", "lp", "group", "holdings", "holding", "sarl", "bv", "oyj",
    )
    entity_person_honorifics: tuple[str, ...] = (
        "mr", "mrs", "ms", "miss", "dr", "prof", "shri", "smt", "sri", "hon",
        "sir", "mx",
    )
    entity_anaphora: tuple[str, ...] = (
        "the company", "the group", "the issuer", "the bank", "the corporation",
        "the firm", "the registrant", "our company", "the parent",
        "the organisation", "the organization", "the entity",
    )
    entity_rename_predicates: tuple[str, ...] = (
        "formerly known as", "formerly", "former name", "changed its name",
        "changed name", "renamed", "incorporated as", "originally incorporated",
        "previously known as", "erstwhile", "name was changed",
    )
    # rapidfuzz 0-100: token_set_ratio gates the high-recall block, token_sort_ratio
    # gates an auto-merge that needs no LLM.
    entity_block_fuzzy_threshold: float = 88.0
    entity_merge_fuzzy_threshold: float = 94.0

    # --- retrieval: starting defaults; tuned by the eval harness (Phase 10) --
    retrieval_top_k: int = 15
    retrieval_candidate_threshold: float = 0.55
    predicate_similarity_threshold: float = 0.60
    relationship_confidence_threshold: float = 0.50
    # retrieval_score = (w_entity·same_entity + w_predicate·predicate_sim
    #                    + w_bm25·lexical_overlap) / (w_entity + w_predicate + w_bm25)
    # w_embedding is reserved for the deferred embedding rung (Phase 8 ships
    # deterministic lexical retrieval only — no fastembed in this environment).
    retrieval_weight_embedding: float = 0.5
    retrieval_weight_predicate: float = 0.3
    retrieval_weight_bm25: float = 0.2
    retrieval_weight_entity: float = 0.4
    # a content-token block is dropped once it covers more facts than this (the
    # token is not discriminative); entity / exact-predicate blocks are never dropped
    retrieval_bucket_max: int = 400
    # a token-only candidate pair needs at least this many shared content tokens
    retrieval_min_shared_tokens: int = 2

    # --- deterministic numeric comparison ---------------------------------------
    numeric_equivalence_tolerance: float = 0.02
    numeric_contradiction_threshold: float = 0.15

    # --- ingestion limits / knobs -----------------------------------------------
    max_upload_mb: int = 25
    max_pages: int = 300
    max_chunks_per_doc: int = 4000
    max_llm_calls_per_doc: int = 800
    fy_convention_default: str = "apr-mar"

    # page source-quality thresholds (Phase 2). A page with 0 chars is EMPTY;
    # with 0 < chars < ocr_min_chars, or below the density floor, it is LOW_TEXT
    # (a signal for a later phase to consider OCR — Phase 2 never runs OCR).
    ocr_min_chars: int = 100
    low_text_density_per_kchar2: float = 0.35  # chars per 1000 pt^2 of page area

    # deterministic page-aware chunking (Phase 2). Values are not sacred; later
    # phases may retune. Chunks are exact slices of page text: for every chunk,
    # page_text[char_offset:char_end] == chunk.text .
    chunk_target_chars: int = 1200
    chunk_overlap_chars: int = 150
    chunk_boundary_backoff_chars: int = 200

    # evidence verification (Phase 5). Fuzzy matching is a *last resort* for PDF
    # formatting artifacts only — a numeric/unit token guard runs first, so
    # changed numbers/currencies/units can never pass fuzzy.
    verify_fuzzy_threshold: float = 90.0        # rapidfuzz partial_ratio, 0–100
    verify_fuzzy_min_quote_chars: int = 12      # shorter quotes never fuzzy-match
    verify_numeric_tolerance: float = 1e-6

    # --- app ------------------------------------------------------------------
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    log_level: str = "info"

    def llm_api_key(self) -> str | None:
        """The live LLM API key, or None if unset. Read at call time so tests
        and deploys can set it after import."""
        return os.environ.get(self.llm_api_key_env)


@lru_cache
def get_settings() -> Settings:
    """Process-wide singleton. Call ``get_settings.cache_clear()`` in tests
    after mutating the environment."""
    return Settings()
