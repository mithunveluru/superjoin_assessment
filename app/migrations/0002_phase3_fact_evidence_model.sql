-- Migration 2 (Phase 3): canonical fact + evidence persistence model.
--   * facts.fact_type widened to numeric|semantic|temporal|categorical
--   * facts.lifecycle_state gains RAW  (RAW -> CANDIDATE -> ... -> ELIGIBLE_FOR_REASONING)
--   * facts.raw_payload: the extractor's original structured output for this one fact
--   * evidence gains page_id / chunk_id FKs (explicit FACT->EVIDENCE->CHUNK/PAGE->DOCUMENT chain)
--   * evidence.verification_method gains 'unavailable' (vs 'unverified' = attempted, not found)
--   * evidence CHECK: char_start IS NULL OR char_end IS NULL OR char_start < char_end
-- CHECK-constraint changes need a table rebuild; foreign keys are toggled around it
-- and PRAGMA foreign_key_check is verified afterwards (see app/db.py). The affected
-- tables carry no rows until Phase 4 writes facts.

PRAGMA foreign_keys=OFF;
BEGIN;

CREATE TABLE facts__m2 (
    id                    INTEGER PRIMARY KEY,
    document_id           INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_index            INTEGER NOT NULL,
    run_id                INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    raw_extraction_id     INTEGER REFERENCES raw_extractions(id) ON DELETE SET NULL,
    lifecycle_state       TEXT NOT NULL DEFAULT 'CANDIDATE'
        CHECK (lifecycle_state IN
            ('RAW','CANDIDATE','GROUNDED','NORMALIZED','ELIGIBLE_FOR_REASONING','QUARANTINED')),
    quarantine_reason     TEXT,
    subject_raw           TEXT NOT NULL,
    subject_entity_id     INTEGER REFERENCES entities(id) ON DELETE SET NULL,
    predicate             TEXT NOT NULL,
    predicate_norm        TEXT NOT NULL DEFAULT '',
    object_raw            TEXT NOT NULL,
    fact_type             TEXT NOT NULL
        CHECK (fact_type IN ('numeric','semantic','temporal','categorical')),
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
    reporting_period_raw   TEXT,
    reporting_period_start TEXT,
    reporting_period_end   TEXT,
    reporting_period_type  TEXT
        CHECK (reporting_period_type IN
            ('instant','quarter','half_year','fiscal_year','calendar_year','range','unknown')),
    scope                  TEXT,
    qualifiers             TEXT,
    modality               TEXT NOT NULL DEFAULT 'ASSERTED'
        CHECK (modality IN ('ASSERTED','HISTORICAL','ESTIMATED','FORECAST','TARGET','UNCERTAIN')),
    context_complete       INTEGER NOT NULL DEFAULT 0,
    evidence_status        TEXT NOT NULL DEFAULT 'UNVERIFIED'
        CHECK (evidence_status IN ('VERIFIED','PARTIAL','UNVERIFIED')),
    reasoning_eligible     INTEGER NOT NULL DEFAULT 0,
    extraction_model       TEXT,
    prompt_version         TEXT,
    extraction_temperature REAL,
    extracted_at           TEXT,
    raw_payload            TEXT,          -- JSON: extractor output for this fact, pre-normalization
    embedding              BLOB,
    predicate_embedding    BLOB,
    created_at             TEXT NOT NULL,
    CHECK (lifecycle_state <> 'QUARANTINED' OR reasoning_eligible = 0),
    CHECK (reasoning_eligible = 0 OR evidence_status <> 'UNVERIFIED')
);

INSERT INTO facts__m2
    (id, document_id, page_index, run_id, raw_extraction_id, lifecycle_state, quarantine_reason,
     subject_raw, subject_entity_id, predicate, predicate_norm, object_raw, fact_type,
     value_raw, numeric_value, magnitude, magnitude_factor, base_value, currency,
     is_percentage, percentage_ratio, unit_raw, unit_norm, value_text,
     reporting_period_raw, reporting_period_start, reporting_period_end, reporting_period_type,
     scope, qualifiers, modality, context_complete, evidence_status, reasoning_eligible,
     extraction_model, prompt_version, extraction_temperature, extracted_at,
     embedding, predicate_embedding, created_at)
SELECT
     id, document_id, page_index, run_id, raw_extraction_id, lifecycle_state, quarantine_reason,
     subject_raw, subject_entity_id, predicate, predicate_norm, object_raw, fact_type,
     value_raw, numeric_value, magnitude, magnitude_factor, base_value, currency,
     is_percentage, percentage_ratio, unit_raw, unit_norm, value_text,
     reporting_period_raw, reporting_period_start, reporting_period_end, reporting_period_type,
     scope, qualifiers, modality, context_complete, evidence_status, reasoning_eligible,
     extraction_model, prompt_version, extraction_temperature, extracted_at,
     embedding, predicate_embedding, created_at
FROM facts;

DROP TABLE facts;
ALTER TABLE facts__m2 RENAME TO facts;

CREATE INDEX ix_facts_document        ON facts(document_id);
CREATE INDEX ix_facts_entity          ON facts(subject_entity_id);
CREATE INDEX ix_facts_predicate_norm  ON facts(predicate_norm);
CREATE INDEX ix_facts_type            ON facts(fact_type);
CREATE INDEX ix_facts_lifecycle       ON facts(lifecycle_state);
CREATE INDEX ix_facts_reasoning       ON facts(reasoning_eligible);
CREATE INDEX ix_facts_evidence_status ON facts(evidence_status);
CREATE INDEX ix_facts_run             ON facts(run_id);
CREATE INDEX ix_facts_period          ON facts(reporting_period_start, reporting_period_end);

CREATE TABLE evidence__m2 (
    id                  INTEGER PRIMARY KEY,
    fact_id             INTEGER NOT NULL UNIQUE REFERENCES facts(id) ON DELETE CASCADE,
    document_id         INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_index          INTEGER NOT NULL,
    page_id             INTEGER REFERENCES pages(id)  ON DELETE SET NULL,
    chunk_id            INTEGER REFERENCES chunks(id) ON DELETE SET NULL,
    printed_label       TEXT,
    char_start          INTEGER,
    char_end            INTEGER,
    quote               TEXT NOT NULL,
    method              TEXT NOT NULL DEFAULT 'text_layer'
        CHECK (method IN ('text_layer','ocr')),
    verification_method TEXT NOT NULL DEFAULT 'unverified'
        CHECK (verification_method IN
            ('exact','normalized_exact','fuzzy','unavailable','unverified')),
    fuzzy_score         REAL,
    numeric_rederivation TEXT NOT NULL DEFAULT 'not_applicable'
        CHECK (numeric_rederivation IN ('not_applicable','success','failed')),
    evidence_status     TEXT NOT NULL DEFAULT 'UNVERIFIED'
        CHECK (evidence_status IN ('VERIFIED','PARTIAL','UNVERIFIED')),
    evidence_score      REAL,
    notes               TEXT,
    created_at          TEXT NOT NULL,
    CHECK (char_start IS NULL OR char_end IS NULL OR char_start < char_end)
);

INSERT INTO evidence__m2
    (id, fact_id, document_id, page_index, printed_label, char_start, char_end, quote,
     method, verification_method, fuzzy_score, numeric_rederivation, evidence_status,
     evidence_score, notes, created_at)
SELECT
     id, fact_id, document_id, page_index, printed_label, char_start, char_end, quote,
     method, verification_method, fuzzy_score, numeric_rederivation, evidence_status,
     evidence_score, notes, created_at
FROM evidence;

DROP TABLE evidence;
ALTER TABLE evidence__m2 RENAME TO evidence;

CREATE INDEX ix_evidence_doc_page ON evidence(document_id, page_index);
CREATE INDEX ix_evidence_page     ON evidence(page_id);
CREATE INDEX ix_evidence_chunk    ON evidence(chunk_id);

COMMIT;
PRAGMA foreign_keys=ON;
