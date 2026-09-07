"""Phase 2 — PDF ingestion + page-preserving text layer.

Covers: document creation & metadata, SHA-256 dedup/idempotency, page identity
& text preservation, deterministic page-aware chunking with valid offsets,
sparse/textless/error page detection, expected-error handling, and safe file
handling. No LLM / fact / reasoning code is exercised.
"""

from __future__ import annotations

import hashlib

import pymupdf
import pytest

from app import db
from app.config import get_settings
from app.ingest import IngestError, _chunk_ranges, ingest_pdf

# a body that comfortably clears the LOW_TEXT thresholds (chars + density)
PROSE = (
    "The company reported steady operating performance across its core segments "
    "during the period under review, with management highlighting disciplined cost "
    "control, improving working capital, and continued investment in network capacity. "
    "This paragraph exists only to give a synthetic test page a normal amount of text."
)


def _sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# migration sanity                                                           #
# --------------------------------------------------------------------------- #
def test_migration_applied(conn):
    assert db.schema_version(conn) >= 1
    doc_cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
    assert {"file_size", "mime_type"} <= doc_cols
    page_cols = {r["name"] for r in conn.execute("PRAGMA table_info(pages)")}
    assert {"extraction_status", "extraction_error", "extraction_meta"} <= page_cols
    chunk_cols = {r["name"] for r in conn.execute("PRAGMA table_info(chunks)")}
    assert "char_end" in chunk_cols
    # init_db is still idempotent after migrations
    db.init_db()
    assert db.schema_version(conn) >= 1


# --------------------------------------------------------------------------- #
# document ingestion + metadata                                              #
# --------------------------------------------------------------------------- #
def test_valid_pdf_creates_document(conn, make_pdf):
    pdf = make_pdf(["Hello world. Revenue was 1,234 units in FY24."])
    result = ingest_pdf(pdf, original_filename="report.pdf")

    assert result.status == "ingested"
    assert result.duplicate is False
    assert result.page_count == 1

    row = conn.execute("SELECT * FROM documents WHERE id = ?", (result.document_id,)).fetchone()
    assert row["sha256"] == _sha256(pdf) == result.sha256
    assert row["original_filename"] == "report.pdf"
    assert row["mime_type"] == "application/pdf"
    assert row["file_size"] == pdf.stat().st_size
    assert row["page_count"] == 1
    assert row["status"] == "ingested"
    assert row["uploaded_at"]


def test_ingest_run_recorded(conn, make_pdf):
    result = ingest_pdf(make_pdf(["page a", "page b"]))
    run = conn.execute(
        "SELECT * FROM runs WHERE document_id = ? AND run_type = 'ingest'",
        (result.document_id,),
    ).fetchone()
    assert run["status"] == "done"
    assert run["pages_processed"] == 2
    assert run["finished_at"]


def test_duplicate_upload_is_idempotent(conn, make_pdf):
    pdf = make_pdf(["identical bytes"])
    first = ingest_pdf(pdf, original_filename="a.pdf")
    second = ingest_pdf(pdf, original_filename="totally-different-name.pdf")

    assert second.document_id == first.document_id
    assert second.duplicate is True
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM pages WHERE document_id = ?", (first.document_id,)
    ).fetchone()[0] == 1
    # only one ingest run — the duplicate did no work
    assert conn.execute(
        "SELECT COUNT(*) FROM runs WHERE document_id = ?", (first.document_id,)
    ).fetchone()[0] == 1


# --------------------------------------------------------------------------- #
# page preservation                                                          #
# --------------------------------------------------------------------------- #
def test_page_count_and_indices(conn, make_pdf):
    result = ingest_pdf(make_pdf(["p0", "p1", "p2"]))
    idx = [r["page_index"] for r in conn.execute(
        "SELECT page_index FROM pages WHERE document_id = ? ORDER BY page_index",
        (result.document_id,),
    )]
    assert idx == [0, 1, 2]
    assert result.page_count == 3


def test_page_text_belongs_to_its_page(conn, make_pdf):
    result = ingest_pdf(make_pdf(["ALPHA one two three", "BRAVO four five six"]))
    p0, p1 = conn.execute(
        "SELECT text FROM pages WHERE document_id = ? ORDER BY page_index",
        (result.document_id,),
    ).fetchall()
    assert "ALPHA" in p0["text"] and "BRAVO" not in p0["text"]
    assert "BRAVO" in p1["text"] and "ALPHA" not in p1["text"]


def test_pages_linked_to_document(conn, make_pdf):
    result = ingest_pdf(make_pdf(["x"]))
    (fk,) = conn.execute(
        "SELECT document_id FROM pages WHERE document_id = ?", (result.document_id,)
    ).fetchone()
    assert fk == result.document_id


def test_printed_label_detected_when_line_is_just_a_number(conn, make_pdf):
    result = ingest_pdf(make_pdf(["Some prose about the business.\n\n7"]))
    (label,) = conn.execute(
        "SELECT printed_label FROM pages WHERE document_id = ?", (result.document_id,)
    ).fetchone()
    assert label == "7"


def test_printed_label_null_when_absent(conn, make_pdf):
    result = ingest_pdf(make_pdf(["Prose with no isolated page number anywhere in it."]))
    (label,) = conn.execute(
        "SELECT printed_label FROM pages WHERE document_id = ?", (result.document_id,)
    ).fetchone()
    assert label is None


def test_year_line_is_not_mistaken_for_page_label(conn, make_pdf):
    result = ingest_pdf(make_pdf(["Annual Report\n2024"]))
    (label,) = conn.execute(
        "SELECT printed_label FROM pages WHERE document_id = ?", (result.document_id,)
    ).fetchone()
    assert label is None  # 2024 > plausible page count


# --------------------------------------------------------------------------- #
# chunking                                                                   #
# --------------------------------------------------------------------------- #
def test_chunk_offsets_slice_back_to_page_text(conn, make_pdf, monkeypatch):
    monkeypatch.setenv("FKL_CHUNK_TARGET_CHARS", "40")
    monkeypatch.setenv("FKL_CHUNK_OVERLAP_CHARS", "10")
    monkeypatch.setenv("FKL_CHUNK_BOUNDARY_BACKOFF_CHARS", "8")
    get_settings.cache_clear()

    result = ingest_pdf(make_pdf([
        "one two three four five six seven eight nine ten eleven twelve thirteen"
    ]))
    page = conn.execute(
        "SELECT text FROM pages WHERE document_id = ?", (result.document_id,)
    ).fetchone()["text"]
    chunks = conn.execute(
        "SELECT seq, char_offset, char_end, text FROM chunks "
        "WHERE document_id = ? ORDER BY seq",
        (result.document_id,),
    ).fetchall()

    assert len(chunks) >= 2  # long page split
    prev_start = -1
    for c in chunks:
        assert page[c["char_offset"]:c["char_end"]] == c["text"]  # exact slice
        assert c["char_offset"] > prev_start                      # strictly increasing
        prev_start = c["char_offset"]
    assert chunks[0]["char_offset"] == 0
    assert chunks[-1]["char_end"] == len(page)


def test_chunking_is_deterministic(make_pdf, tmp_path, monkeypatch):
    pdf = make_pdf(["deterministic body " * 90])  # > default target -> multiple chunks

    def run_once(db_file):
        monkeypatch.setenv("FKL_DATABASE_PATH", str(db_file))
        monkeypatch.setenv("FKL_UPLOADS_DIR", str(db_file.parent / "up"))
        get_settings.cache_clear()
        db.init_db()
        res = ingest_pdf(pdf)
        c = db.connect()
        try:
            rows = c.execute(
                "SELECT seq, char_offset, char_end, text FROM chunks "
                "WHERE document_id = ? ORDER BY seq",
                (res.document_id,),
            ).fetchall()
            return [tuple(r) for r in rows]
        finally:
            c.close()

    a = run_once(tmp_path / "a.db")
    b = run_once(tmp_path / "b.db")
    assert a == b and len(a) >= 2


def test_chunks_never_cross_pages(conn, make_pdf, monkeypatch):
    monkeypatch.setenv("FKL_CHUNK_TARGET_CHARS", "30")
    monkeypatch.setenv("FKL_CHUNK_OVERLAP_CHARS", "5")
    get_settings.cache_clear()
    result = ingest_pdf(make_pdf(["AAAA " * 20, "BBBB " * 20]))
    rows = conn.execute(
        "SELECT page_index, text FROM chunks WHERE document_id = ?", (result.document_id,)
    ).fetchall()
    for r in rows:
        if r["page_index"] == 0:
            assert "BBBB" not in r["text"]
        else:
            assert "AAAA" not in r["text"]


def test_short_page_is_one_chunk(conn, make_pdf):
    result = ingest_pdf(make_pdf(["short"]))
    rows = conn.execute(
        "SELECT char_offset, char_end, text FROM chunks WHERE document_id = ?",
        (result.document_id,),
    ).fetchall()
    page = conn.execute(
        "SELECT text FROM pages WHERE document_id = ?", (result.document_id,)
    ).fetchone()["text"]
    assert len(rows) == 1
    assert (rows[0]["char_offset"], rows[0]["char_end"]) == (0, len(page))


def test_chunk_ranges_unit_properties():
    text = "word " * 500  # 2500 chars
    ranges = _chunk_ranges(text, target=200, overlap=40, backoff=30)
    assert ranges[0][0] == 0
    assert ranges[-1][1] == len(text)
    for (s, e), (ns, ne) in zip(ranges, ranges[1:], strict=False):
        assert ns > s and ne > e          # forward progress
        assert ns < e                     # consecutive chunks overlap
    for s, e in ranges:
        assert 0 <= s < e <= len(text)


# --------------------------------------------------------------------------- #
# sparse / textless / error pages                                            #
# --------------------------------------------------------------------------- #
def test_blank_page_flagged_empty_and_not_chunked(conn, make_pdf):
    result = ingest_pdf(make_pdf([PROSE, None]))
    pages = conn.execute(
        "SELECT page_index, extraction_status, char_count FROM pages "
        "WHERE document_id = ? ORDER BY page_index",
        (result.document_id,),
    ).fetchall()
    assert pages[0]["extraction_status"] == "TEXT_EXTRACTED"
    assert pages[1]["extraction_status"] == "EMPTY"
    assert pages[1]["char_count"] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE document_id = ? AND page_index = 1",
        (result.document_id,),
    ).fetchone()[0] == 0
    fail = conn.execute(
        "SELECT failure_type, reason FROM failures WHERE document_id = ? AND ref_table = 'pages'",
        (result.document_id,),
    ).fetchone()
    assert fail["failure_type"] == "ocr_page"
    assert fail["reason"] == "page_empty"
    assert result.status == "ingested"          # document still succeeds
    assert result.pages_empty == 1


def test_low_text_page_flagged(conn, make_pdf, monkeypatch):
    monkeypatch.setenv("FKL_OCR_MIN_CHARS", "5000")  # force a normal page to look sparse
    get_settings.cache_clear()
    result = ingest_pdf(make_pdf(["a modest amount of text on the page"]))
    page = conn.execute(
        "SELECT extraction_status FROM pages WHERE document_id = ?", (result.document_id,)
    ).fetchone()
    assert page["extraction_status"] == "LOW_TEXT"
    assert result.pages_low_text == 1
    # LOW_TEXT pages are still chunked (text may be usable)
    assert conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE document_id = ?", (result.document_id,)
    ).fetchone()[0] >= 1


def test_page_extraction_error_isolated(conn, make_pdf, monkeypatch):
    def boom(self, *a, **k):
        raise RuntimeError("simulated PyMuPDF failure")

    monkeypatch.setattr(pymupdf.Page, "get_text", boom)
    result = ingest_pdf(make_pdf(["p0", "p1"]))

    assert result.status == "ingested"            # per-page isolation, doc survives
    assert result.pages_extraction_error == 2
    assert result.chunk_count == 0
    rows = conn.execute(
        "SELECT extraction_status, extraction_error FROM pages WHERE document_id = ?",
        (result.document_id,),
    ).fetchall()
    assert all(r["extraction_status"] == "EXTRACTION_ERROR" for r in rows)
    assert all("simulated" in r["extraction_error"] for r in rows)
    assert conn.execute(
        "SELECT COUNT(*) FROM failures WHERE document_id = ? AND reason = 'page_extraction_error'",
        (result.document_id,),
    ).fetchone()[0] == 2


def test_page_extraction_meta_recorded(conn, make_pdf):
    import json

    result = ingest_pdf(make_pdf(["some blocks of text here"]))
    meta = json.loads(conn.execute(
        "SELECT extraction_meta FROM pages WHERE document_id = ?", (result.document_id,)
    ).fetchone()["extraction_meta"])
    assert "block_count" in meta and "image_count" in meta
    assert meta["block_count"] >= 1


# --------------------------------------------------------------------------- #
# expected error handling                                                    #
# --------------------------------------------------------------------------- #
def test_nonexistent_file(db_path, tmp_path):
    with pytest.raises(IngestError) as ei:
        ingest_pdf(tmp_path / "nope.pdf")
    assert ei.value.code == "file_not_found"


def test_empty_file(db_path, tmp_path):
    p = tmp_path / "empty.pdf"
    p.write_bytes(b"")
    with pytest.raises(IngestError) as ei:
        ingest_pdf(p)
    assert ei.value.code == "empty_file"


def test_not_a_pdf(db_path, tmp_path):
    p = tmp_path / "notpdf.pdf"
    p.write_bytes(b"just some text, definitely not a pdf")
    with pytest.raises(IngestError) as ei:
        ingest_pdf(p)
    assert ei.value.code == "not_a_pdf"


def test_corrupt_pdf(db_path, tmp_path):
    p = tmp_path / "corrupt.pdf"
    p.write_bytes(b"%PDF-1.4\nthis header is a lie\n%%EOF")
    with pytest.raises(IngestError) as ei:
        ingest_pdf(p)
    assert ei.value.code == "corrupt_pdf"


def test_file_too_large(db_path, make_pdf, monkeypatch):
    monkeypatch.setenv("FKL_MAX_UPLOAD_MB", "0")
    get_settings.cache_clear()
    with pytest.raises(IngestError) as ei:
        ingest_pdf(make_pdf(["x"]))
    assert ei.value.code == "file_too_large"


def test_too_many_pages(db_path, conn, make_pdf, monkeypatch):
    monkeypatch.setenv("FKL_MAX_PAGES", "1")
    get_settings.cache_clear()
    with pytest.raises(IngestError) as ei:
        ingest_pdf(make_pdf(["p0", "p1"]))
    assert ei.value.code == "too_many_pages"
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0  # nothing persisted


def test_failure_after_document_row_marks_it_failed(conn, make_pdf, monkeypatch):
    import app.ingest as ingest_mod

    calls = {"n": 0}
    real = ingest_mod._chunk_ranges

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom during extraction")
        return real(*a, **k)

    monkeypatch.setattr(ingest_mod, "_chunk_ranges", flaky)
    pdf = make_pdf(["content for the flaky run"])

    with pytest.raises(IngestError) as ei:
        ingest_pdf(pdf)
    assert ei.value.code == "ingest_failed"
    doc = conn.execute("SELECT id, status FROM documents").fetchone()
    assert doc["status"] == "failed"
    assert ei.value.document_id == doc["id"]
    assert conn.execute(
        "SELECT COUNT(*) FROM failures WHERE document_id = ? AND failure_type = 'run_error'",
        (doc["id"],),
    ).fetchone()[0] >= 1

    # retry the same file cleanly -> same document, no duplicate pages
    monkeypatch.setattr(ingest_mod, "_chunk_ranges", real)
    retry = ingest_pdf(pdf)
    assert retry.document_id == doc["id"]
    assert retry.status == "ingested"
    assert conn.execute(
        "SELECT COUNT(*) FROM pages WHERE document_id = ?", (doc["id"],)
    ).fetchone()[0] == 1


# --------------------------------------------------------------------------- #
# safe file handling                                                         #
# --------------------------------------------------------------------------- #
def test_unsafe_filename_cannot_escape_storage_dir(conn, make_pdf):
    pdf = make_pdf(["safe content"])
    result = ingest_pdf(pdf, original_filename="../../../../etc/passwd")

    row = conn.execute(
        "SELECT original_filename, stored_path FROM documents WHERE id = ?",
        (result.document_id,),
    ).fetchone()
    uploads = get_settings().uploads_dir.resolve()
    stored = __import__("pathlib").Path(row["stored_path"]).resolve()

    assert row["original_filename"] == "passwd"          # sanitised to a basename
    assert stored.parent == uploads                      # stored inside uploads dir
    assert stored.name == f"{result.sha256}.pdf"         # name is the checksum, not user input
    assert stored.is_file()
    assert list(uploads.glob("*.pdf")) == [stored]       # nothing else written


def test_dotdot_filename_becomes_placeholder(conn, make_pdf):
    result = ingest_pdf(make_pdf(["c"]), original_filename="..")
    (name,) = conn.execute(
        "SELECT original_filename FROM documents WHERE id = ?", (result.document_id,)
    ).fetchone()
    assert name == "upload.pdf"
