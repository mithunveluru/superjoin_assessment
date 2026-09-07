-- Migration 1 (Phase 2): PDF ingestion metadata + per-page source-quality
-- signal + explicit chunk end offset. Additive (ADD COLUMN only).

ALTER TABLE documents ADD COLUMN file_size INTEGER;
ALTER TABLE documents ADD COLUMN mime_type TEXT;

ALTER TABLE pages ADD COLUMN extraction_status TEXT NOT NULL
    DEFAULT 'TEXT_EXTRACTED'
    CHECK (extraction_status IN
           ('TEXT_EXTRACTED','LOW_TEXT','EMPTY','EXTRACTION_ERROR'));
ALTER TABLE pages ADD COLUMN extraction_error TEXT;
-- extraction_meta JSON: block_count, image_count, text_density, printed_label_candidate
ALTER TABLE pages ADD COLUMN extraction_meta  TEXT;

ALTER TABLE chunks ADD COLUMN char_end INTEGER;   -- char_offset is the start
