-- Fact Knowledge Layer — SQLite GENESIS schema (PRAGMA user_version = 0).
-- Authoritative shape is docs/DATA_MODEL.md. Every statement is idempotent.
-- Applied by app.db.init_db(); enable PRAGMA foreign_keys=ON on every connection.
-- Changes AFTER Phase 1 are additive migrations in app/db.py (_MIGRATIONS),
-- keyed by PRAGMA user_version — do not edit tables below to add later columns.

-- ---------------------------------------------------------------------------
-- documents : one uploaded PDF + its provenance (four separate concepts)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
    id                   INTEGER PRIMARY KEY,
    sha256               TEXT NOT NULL UNIQUE,
    stored_path          TEXT NOT NULL,
    original_filename    TEXT,                       -- display only, never logic
    title                TEXT,
    publisher            TEXT,
    disclosure_type      TEXT,
    document_date        TEXT,                       -- date the document bears
    publication_date     TEXT,                       -- when released / filed
    data_vintage         TEXT,                       -- revision/series label
    fy_convention        TEXT NOT NULL DEFAULT 'unknown'
        CHECK (fy_convention IN ('apr-mar','jan-dec','jul-jun','unknown')),
    fy_convention_source TEXT NOT NULL DEFAULT 'config_default'
        CHECK (fy_convention_source IN ('detected','config_default','override')),
    page_count           INTEGER,
    status               TEXT NOT NULL DEFAULT 'uploaded'
        CHECK (status IN ('uploaded','ingesting','ingested','processing','done','failed')),
    status_detail        TEXT,
    uploaded_at          TEXT NOT NULL,
    processed_at         TEXT
);
CREATE INDEX IF NOT EXISTS ix_documents_status ON documents(status);

-- ---------------------------------------------------------------------------
-- pages : one row per PDF page; pages.text is the offset reference for evidence
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pages (
    id                INTEGER PRIMARY KEY,
    document_id       INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_index        INTEGER NOT NULL,             -- 0-based position in the file
    printed_label     TEXT,                         -- page number as printed
    text              TEXT NOT NULL DEFAULT '',
    char_count        INTEGER NOT NULL DEFAULT 0,
    width             REAL,
    height            REAL,
    extraction_method TEXT NOT NULL DEFAULT 'text_layer'
        CHECK (extraction_method IN ('text_layer','ocr','mixed','empty')),
    UNIQUE (document_id, page_index)
);

-- ---------------------------------------------------------------------------
-- chunks : LLM input units; never cross a page boundary
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chunks (
    id                INTEGER PRIMARY KEY,
    document_id       INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_index        INTEGER NOT NULL,
    seq               INTEGER NOT NULL,             -- chunk order within the page
    char_offset       INTEGER NOT NULL,            -- start of body within pages.text
    header_prefix_len INTEGER NOT NULL DEFAULT 0,  -- prepended table-header context
    text              TEXT NOT NULL,
    UNIQUE (document_id, page_index, seq)
);
CREATE INDEX IF NOT EXISTS ix_chunks_doc_page ON chunks(document_id, page_index);

-- ---------------------------------------------------------------------------
-- runs : processing observability (one per pipeline stage or a full run)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS runs (
    id                     INTEGER PRIMARY KEY,
    document_id            INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    run_type               TEXT NOT NULL
        CHECK (run_type IN ('ingest','extract','ground','normalize','resolve','retrieve','reason','full')),
    status                 TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running','done','failed')),
    stage                  TEXT,
    started_at             TEXT NOT NULL,
    finished_at            TEXT,
    pages_processed        INTEGER NOT NULL DEFAULT 0,
    chunks_processed       INTEGER NOT NULL DEFAULT 0,
    facts_extracted        INTEGER NOT NULL DEFAULT 0,
    facts_grounded         INTEGER NOT NULL DEFAULT 0,
    facts_quarantined      INTEGER NOT NULL DEFAULT 0,
    relationships_produced INTEGER NOT NULL DEFAULT 0,
    llm_calls              INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd     REAL,
    model_name             TEXT,
    prompt_versions        TEXT,                    -- JSON object
    settings               TEXT,                    -- JSON snapshot for reproducibility
    error                  TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_document ON runs(document_id);
CREATE INDEX IF NOT EXISTS ix_runs_status   ON runs(status);
CREATE INDEX IF NOT EXISTS ix_runs_type     ON runs(run_type);

-- ---------------------------------------------------------------------------
-- raw_extractions : verbatim LLM output, before any transformation
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_extractions (
    id               INTEGER PRIMARY KEY,
    run_id           INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    document_id      INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_index       INTEGER NOT NULL,
    chunk_id         INTEGER REFERENCES chunks(id) ON DELETE SET NULL,
    model_name       TEXT NOT NULL,
    prompt_version   TEXT NOT NULL,
    temperature      REAL,
    request_settings TEXT,                          -- JSON
    raw_response     TEXT NOT NULL,                 -- untransformed structured JSON
    item_count       INTEGER,
    parse_error      TEXT,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_raw_extractions_run     ON raw_extractions(run_id);
CREATE INDEX IF NOT EXISTS ix_raw_extractions_docpage ON raw_extractions(document_id, page_index);

-- ---------------------------------------------------------------------------
-- entities / entity_aliases : general-purpose resolution, no seeded aliases
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entities (
    id                INTEGER PRIMARY KEY,
    canonical_label   TEXT NOT NULL,
    entity_type       TEXT
        CHECK (entity_type IN ('org','person','place','metric','product','other')),
    normalization_key TEXT,
    resolution_method TEXT
        CHECK (resolution_method IN ('deterministic','similarity','llm_confirmed')),
    resolution_score  REAL,
    llm_confirmed     INTEGER NOT NULL DEFAULT 0,
    llm_confidence    REAL,
    created_run_id    INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_entities_normkey ON entities(normalization_key);
CREATE INDEX IF NOT EXISTS ix_entities_type    ON entities(entity_type);

CREATE TABLE IF NOT EXISTS entity_aliases (
    id             INTEGER PRIMARY KEY,
    entity_id      INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    surface        TEXT NOT NULL,
    normalized     TEXT,
    match_method   TEXT
        CHECK (match_method IN ('deterministic','similarity','llm','derived_fact')),
    score          REAL,
    source_fact_id INTEGER REFERENCES facts(id) ON DELETE SET NULL,
    created_at     TEXT NOT NULL,
    UNIQUE (entity_id, surface)
);
CREATE INDEX IF NOT EXISTS ix_entity_aliases_entity ON entity_aliases(entity_id);
CREATE INDEX IF NOT EXISTS ix_entity_aliases_norm   ON entity_aliases(normalized);

-- ---------------------------------------------------------------------------
-- facts : the core. lifecycle + full numeric representation + context +
--         modality + grounding/eligibility + reproducibility
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS facts (
    id                    INTEGER PRIMARY KEY,
    document_id           INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_index            INTEGER NOT NULL,
    run_id                INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    raw_extraction_id     INTEGER REFERENCES raw_extractions(id) ON DELETE SET NULL,

    lifecycle_state       TEXT NOT NULL DEFAULT 'CANDIDATE'
        CHECK (lifecycle_state IN ('CANDIDATE','GROUNDED','NORMALIZED','ELIGIBLE_FOR_REASONING','QUARANTINED')),
    quarantine_reason     TEXT,

    subject_raw           TEXT NOT NULL,
    subject_entity_id     INTEGER REFERENCES entities(id) ON DELETE SET NULL,
    predicate             TEXT NOT NULL,
    predicate_norm        TEXT NOT NULL DEFAULT '',
    object_raw            TEXT NOT NULL,
    fact_type             TEXT NOT NULL CHECK (fact_type IN ('numeric','semantic')),

    -- numeric representation (every piece recoverable; see DATA_MODEL worked table)
    value_raw             TEXT,
    numeric_value         REAL,
    magnitude             TEXT,
    magnitude_factor      REAL,
    base_value            REAL,
    currency              TEXT,
    is_percentage         INTEGER NOT NULL DEFAULT 0,
    percentage_ratio      REAL,
    unit_raw              TEXT,
    unit_norm             TEXT,
    value_text            TEXT,

    -- context
    reporting_period_raw   TEXT,
    reporting_period_start TEXT,
    reporting_period_end   TEXT,
    reporting_period_type  TEXT
        CHECK (reporting_period_type IN ('instant','quarter','half_year','fiscal_year','calendar_year','range','unknown')),
    scope                  TEXT,                     -- JSON object
    qualifiers             TEXT,                     -- JSON array
    modality               TEXT NOT NULL DEFAULT 'ASSERTED'
        CHECK (modality IN ('ASSERTED','HISTORICAL','ESTIMATED','FORECAST','TARGET','UNCERTAIN')),
    context_complete       INTEGER NOT NULL DEFAULT 0,

    -- grounding / eligibility
    evidence_status        TEXT NOT NULL DEFAULT 'UNVERIFIED'
        CHECK (evidence_status IN ('VERIFIED','PARTIAL','UNVERIFIED')),
    reasoning_eligible     INTEGER NOT NULL DEFAULT 0,

    -- reproducibility
    extraction_model       TEXT,
    prompt_version         TEXT,
    extraction_temperature REAL,
    extracted_at           TEXT,

    -- vectors (nullable until Phase 8)
    embedding              BLOB,
    predicate_embedding    BLOB,

    created_at             TEXT NOT NULL,

    -- invariants (docs/DATA_MODEL.md §Invariants 1 & 2)
    CHECK (lifecycle_state <> 'QUARANTINED' OR reasoning_eligible = 0),
    CHECK (reasoning_eligible = 0 OR evidence_status <> 'UNVERIFIED')
);
CREATE INDEX IF NOT EXISTS ix_facts_document        ON facts(document_id);
CREATE INDEX IF NOT EXISTS ix_facts_entity          ON facts(subject_entity_id);
CREATE INDEX IF NOT EXISTS ix_facts_predicate_norm  ON facts(predicate_norm);
CREATE INDEX IF NOT EXISTS ix_facts_type            ON facts(fact_type);
CREATE INDEX IF NOT EXISTS ix_facts_lifecycle       ON facts(lifecycle_state);
CREATE INDEX IF NOT EXISTS ix_facts_reasoning       ON facts(reasoning_eligible);
CREATE INDEX IF NOT EXISTS ix_facts_evidence_status ON facts(evidence_status);
CREATE INDEX IF NOT EXISTS ix_facts_run             ON facts(run_id);
CREATE INDEX IF NOT EXISTS ix_facts_period          ON facts(reporting_period_start, reporting_period_end);

-- ---------------------------------------------------------------------------
-- facts_fts : plain FTS5, populated explicitly by the persistence layer
-- ---------------------------------------------------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    fact_id UNINDEXED, subject_raw, predicate, object_raw, value_text
);

-- ---------------------------------------------------------------------------
-- evidence : one row per fact from GROUNDED on; describes how it was verified
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evidence (
    id                  INTEGER PRIMARY KEY,
    fact_id             INTEGER NOT NULL UNIQUE REFERENCES facts(id) ON DELETE CASCADE,
    document_id         INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_index          INTEGER NOT NULL,
    printed_label       TEXT,
    char_start          INTEGER,
    char_end            INTEGER,
    quote               TEXT NOT NULL,
    method              TEXT NOT NULL DEFAULT 'text_layer'
        CHECK (method IN ('text_layer','ocr')),
    verification_method TEXT NOT NULL DEFAULT 'unverified'
        CHECK (verification_method IN ('exact','normalized_exact','fuzzy','unverified')),
    fuzzy_score         REAL,
    numeric_rederivation TEXT NOT NULL DEFAULT 'not_applicable'
        CHECK (numeric_rederivation IN ('not_applicable','success','failed')),
    evidence_status     TEXT NOT NULL DEFAULT 'UNVERIFIED'
        CHECK (evidence_status IN ('VERIFIED','PARTIAL','UNVERIFIED')),
    evidence_score      REAL,                        -- optional; only from explicit signals
    notes               TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_evidence_doc_page ON evidence(document_id, page_index);

-- ---------------------------------------------------------------------------
-- relationships : LLM proposes, signals.validate() decides (both stored)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS relationships (
    id                    INTEGER PRIMARY KEY,
    fact_a_id             INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    fact_b_id             INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    category              TEXT NOT NULL
        CHECK (category IN ('CORROBORATES','CONTRADICTS','DIFFERENT_CONTEXT','TEMPORAL_EVOLUTION','UNCERTAIN')),
    context_dimension     TEXT,
    deterministic_signals TEXT NOT NULL,             -- JSON object
    llm_used              INTEGER NOT NULL DEFAULT 0,
    llm_proposed_category TEXT,
    validation_action     TEXT NOT NULL DEFAULT 'not_applicable'
        CHECK (validation_action IN ('accepted','overridden','downgraded','not_applicable')),
    validation_notes      TEXT,
    reasoning             TEXT,
    confidence            REAL,
    model_name            TEXT,
    prompt_version        TEXT,
    run_id                INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    created_at            TEXT NOT NULL,
    UNIQUE (fact_a_id, fact_b_id),
    CHECK (fact_a_id < fact_b_id)
);
CREATE INDEX IF NOT EXISTS ix_relationships_category ON relationships(category);
CREATE INDEX IF NOT EXISTS ix_relationships_fact_a   ON relationships(fact_a_id);
CREATE INDEX IF NOT EXISTS ix_relationships_fact_b   ON relationships(fact_b_id);
CREATE INDEX IF NOT EXISTS ix_relationships_run      ON relationships(run_id);

-- ---------------------------------------------------------------------------
-- failures : unified inspectable failure surface; nothing is discarded
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS failures (
    id           INTEGER PRIMARY KEY,
    run_id       INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    document_id  INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    failure_type TEXT NOT NULL
        CHECK (failure_type IN (
            'extraction_unparsed','grounding_failed','context_incomplete','ocr_page',
            'normalization_failed','entity_ambiguous','relationship_uncertain','run_error')),
    ref_table    TEXT,
    ref_id       INTEGER,
    reason       TEXT NOT NULL,
    detail       TEXT,                               -- JSON
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_failures_run  ON failures(run_id);
CREATE INDEX IF NOT EXISTS ix_failures_type ON failures(failure_type);
