-- Migration 3 (Phase 5): evidence verification.
--   * evidence.verification_method gains 'recovered_exact'
--     (offsets were wrong but the exact quote occurs once in the chunk -> span corrected)
--   * evidence.verified_at: when the verifier last ran against this row
-- CHECK change -> rebuild evidence with the SQLite table-redefinition procedure
-- (foreign keys off inside a transaction, PRAGMA foreign_key_check verified after).

PRAGMA foreign_keys=OFF;
BEGIN;

CREATE TABLE evidence__m3 (
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
            ('exact','normalized_exact','recovered_exact','fuzzy','unavailable','unverified')),
    fuzzy_score         REAL,
    numeric_rederivation TEXT NOT NULL DEFAULT 'not_applicable'
        CHECK (numeric_rederivation IN ('not_applicable','success','failed')),
    evidence_status     TEXT NOT NULL DEFAULT 'UNVERIFIED'
        CHECK (evidence_status IN ('VERIFIED','PARTIAL','UNVERIFIED')),
    evidence_score      REAL,
    notes               TEXT,
    verified_at         TEXT,
    created_at          TEXT NOT NULL,
    CHECK (char_start IS NULL OR char_end IS NULL OR char_start < char_end)
);

INSERT INTO evidence__m3
    (id, fact_id, document_id, page_index, page_id, chunk_id, printed_label,
     char_start, char_end, quote, method, verification_method, fuzzy_score,
     numeric_rederivation, evidence_status, evidence_score, notes, created_at)
SELECT
     id, fact_id, document_id, page_index, page_id, chunk_id, printed_label,
     char_start, char_end, quote, method, verification_method, fuzzy_score,
     numeric_rederivation, evidence_status, evidence_score, notes, created_at
FROM evidence;

DROP TABLE evidence;
ALTER TABLE evidence__m3 RENAME TO evidence;

CREATE INDEX ix_evidence_doc_page ON evidence(document_id, page_index);
CREATE INDEX ix_evidence_page     ON evidence(page_id);
CREATE INDEX ix_evidence_chunk    ON evidence(chunk_id);
CREATE INDEX ix_evidence_status   ON evidence(evidence_status);

COMMIT;
PRAGMA foreign_keys=ON;
