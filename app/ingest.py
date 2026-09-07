"""PDF ingestion: PDF file -> document record -> page records -> page-preserving
text -> chunks. Corpus-agnostic. No fact extraction, no LLM, no OCR.

The invariant this layer exists to guarantee:

    any text a later phase uses is addressable as
    (document_id, page_index, char_start, char_end)
    and  pages.text[char_start:char_end] == chunks.text  (header_prefix_len == 0 here)

Callers (a future upload API, a smoke script) pass a path to a PDF already
written to local disk. ``ingest_pdf`` is the whole public surface.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pymupdf

from app import db
from app.config import Settings, get_settings
from app.models import IngestResult

log = logging.getLogger("fkl.ingest")

PDF_MAGIC = b"%PDF-"
_ROMAN_RE = re.compile(r"^m{0,3}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$", re.IGNORECASE)


class IngestError(RuntimeError):
    """Expected, inspectable ingestion failure. ``code`` is a stable short slug."""

    def __init__(self, code: str, *, document_id: int | None = None, detail: str | None = None):
        self.code = code
        self.document_id = document_id
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


# --------------------------------------------------------------------------- #
# helpers (pure)                                                             #
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _safe_display_name(name: str | None) -> str:
    """A filename kept only for display. Never used as a filesystem path."""
    base = os.path.basename(str(name or "").replace("\\", "/"))
    base = re.sub(r"[\x00-\x1f]", "", base).strip()
    if base in ("", ".", ".."):
        base = "upload.pdf"
    return base[:255]


def _classify_page(
    char_count: int, width: float, height: float, *, had_error: bool,
    ocr_min_chars: int, density_floor: float,
) -> tuple[str, float | None]:
    """Return (extraction_status, text_density_per_kchar2 | None)."""
    if had_error:
        return "EXTRACTION_ERROR", None
    if char_count == 0:
        return "EMPTY", 0.0
    density: float | None = None
    if width > 0 and height > 0:
        density = round(char_count / (width * height / 1000.0), 3)
    if char_count < ocr_min_chars or (density is not None and density < density_floor):
        return "LOW_TEXT", density
    return "TEXT_EXTRACTED", density


def _detect_printed_label(text: str, page_count: int) -> str | None:
    """Best-effort, corpus-agnostic. Fires only when a whole line is *just* a
    page-number-ish token. NULL is an acceptable, common result."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    plausible_max = max(page_count + 20, 50)
    for cand in (lines[-1], lines[0]):
        if re.fullmatch(r"\d{1,4}", cand):
            return cand if int(cand) <= plausible_max else None
        m = re.fullmatch(r"(?:page|p\.?)\s*(\d{1,4})", cand, re.IGNORECASE)
        if m:
            return m.group(1) if int(m.group(1)) <= plausible_max else None
        if 1 <= len(cand) <= 6 and _ROMAN_RE.fullmatch(cand):
            return cand.lower()
    return None


def _chunk_ranges(text: str, target: int, overlap: int, backoff: int) -> list[tuple[int, int]]:
    """Deterministic page-aware ranges. Each (s, e) satisfies text[s:e] == chunk.
    Consecutive chunks overlap by ~``overlap``; cuts prefer a nearby newline/space."""
    n = len(text)
    if n == 0:
        return []
    if n <= target:
        return [(0, n)]
    step = max(1, target - overlap)
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < n:
        hard_end = min(start + target, n)
        end = hard_end
        if hard_end < n:
            floor = max(start + 1, hard_end - backoff)
            for i in range(hard_end - 1, floor - 1, -1):
                if text[i] in "\n ":
                    end = i + 1  # keep the boundary char in this chunk
                    break
        ranges.append((start, end))
        if end >= n:
            break
        nxt = end - overlap
        start = nxt if nxt > start else start + step
    return ranges


def _summary(conn, document_id: int, sha256: str, *, duplicate: bool) -> IngestResult:
    doc = conn.execute(
        "SELECT status, page_count FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    by = {
        r["extraction_status"]: r["c"]
        for r in conn.execute(
            "SELECT extraction_status, COUNT(*) AS c FROM pages "
            "WHERE document_id = ? GROUP BY extraction_status",
            (document_id,),
        )
    }
    chunk_count = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE document_id = ?", (document_id,)
    ).fetchone()[0]
    return IngestResult(
        document_id=document_id,
        sha256=sha256,
        status=doc["status"],
        duplicate=duplicate,
        page_count=doc["page_count"] or 0,
        pages_text_extracted=by.get("TEXT_EXTRACTED", 0),
        pages_low_text=by.get("LOW_TEXT", 0),
        pages_empty=by.get("EMPTY", 0),
        pages_extraction_error=by.get("EXTRACTION_ERROR", 0),
        chunk_count=chunk_count,
    )


def _record_page_failure(
    conn, run_id, document_id, page_id, failure_type: str, reason: str, detail: dict,
) -> None:
    conn.execute(
        "INSERT INTO failures "
        "(run_id, document_id, failure_type, ref_table, ref_id, reason, detail, created_at) "
        "VALUES (?, ?, ?, 'pages', ?, ?, ?, ?)",
        (run_id, document_id, failure_type, page_id, reason, json.dumps(detail), _now()),
    )


def _mark_failed(conn, document_id: int | None, run_id: int | None, reason: str) -> None:
    """Best-effort: never leave a document falsely marked successful."""
    reason = reason[:500]
    try:
        with db.transaction(conn):
            if document_id is not None:
                conn.execute(
                    "UPDATE documents SET status = 'failed', status_detail = ? WHERE id = ?",
                    (reason, document_id),
                )
            if run_id is not None:
                conn.execute(
                    "UPDATE runs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                    (reason, _now(), run_id),
                )
            if run_id is not None:
                conn.execute(
                    "INSERT INTO failures "
                    "(run_id, document_id, failure_type, ref_table, ref_id, reason, created_at) "
                    "VALUES (?, ?, 'run_error', 'documents', ?, ?, ?)",
                    (run_id, document_id, document_id, reason, _now()),
                )
    except Exception:  # noqa: BLE001 - failure bookkeeping must not mask the real error
        log.exception("failed to record ingest failure for document_id=%s", document_id)


# --------------------------------------------------------------------------- #
# public entry point                                                         #
# --------------------------------------------------------------------------- #
def ingest_pdf(
    source: str | os.PathLike[str],
    *,
    original_filename: str | None = None,
    database_path: str | os.PathLike[str] | None = None,
    settings: Settings | None = None,
) -> IngestResult:
    """Ingest one PDF from a local path. Idempotent by SHA-256.

    Raises ``IngestError`` (with a stable ``.code``) for expected problems:
    ``file_not_found``, ``file_unreadable``, ``empty_file``, ``file_too_large``,
    ``not_a_pdf``, ``corrupt_pdf``, ``encrypted_pdf``, ``empty_pdf``,
    ``too_many_pages``, ``too_many_chunks``, ``ingest_failed``.
    """
    settings = settings or get_settings()
    src = Path(os.fspath(source)).expanduser()

    # ---- pre-DB validation (nothing persisted on failure) ----
    if not src.is_file():
        raise IngestError("file_not_found", detail=str(src))
    size = src.stat().st_size
    if size == 0:
        raise IngestError("empty_file")
    if size > settings.max_upload_mb * 1024 * 1024:
        raise IngestError("file_too_large", detail=f"{size} bytes > {settings.max_upload_mb} MB")
    try:
        with src.open("rb") as fh:
            head = fh.read(1024)
    except OSError as e:
        raise IngestError("file_unreadable", detail=str(e)) from e
    if not head.lstrip()[:64].startswith(PDF_MAGIC):
        raise IngestError("not_a_pdf")

    sha = _sha256(src)

    try:
        doc = pymupdf.open(src)
    except Exception as e:  # pymupdf.FileDataError and friends
        raise IngestError("corrupt_pdf", detail=str(e)[:200]) from e

    conn = db.connect(database_path)
    document_id: int | None = None
    run_id: int | None = None
    try:
        if doc.needs_pass:
            raise IngestError("encrypted_pdf")
        page_count = doc.page_count
        if page_count == 0:
            raise IngestError("empty_pdf")
        if page_count > settings.max_pages:
            raise IngestError("too_many_pages", detail=f"{page_count} > {settings.max_pages}")

        # ---- dedup ----
        existing = conn.execute(
            "SELECT id, status FROM documents WHERE sha256 = ?", (sha,)
        ).fetchone()
        if existing and existing["status"] in ("ingested", "processing", "done"):
            log.info("duplicate ingest: sha=%s document_id=%s", sha[:12], existing["id"])
            return _summary(conn, existing["id"], sha, duplicate=True)
        document_id = existing["id"] if existing else None

        display_name = _safe_display_name(original_filename or src.name)
        settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        stored_path = settings.uploads_dir / f"{sha}.pdf"
        if not stored_path.exists():
            shutil.copyfile(src, stored_path)

        # ---- T1: create/reset the document + open an ingest run (committed) ----
        with db.transaction(conn):
            if document_id is None:
                document_id = conn.execute(
                    "INSERT INTO documents "
                    "(sha256, stored_path, original_filename, file_size, mime_type, "
                    " page_count, status, uploaded_at) "
                    "VALUES (?, ?, ?, ?, 'application/pdf', ?, 'ingesting', ?)",
                    (sha, str(stored_path), display_name, size, page_count, _now()),
                ).lastrowid
            else:
                conn.execute(
                    "UPDATE documents SET status = 'ingesting', status_detail = NULL, "
                    "stored_path = ?, original_filename = ?, file_size = ?, "
                    "mime_type = 'application/pdf', page_count = ? WHERE id = ?",
                    (str(stored_path), display_name, size, page_count, document_id),
                )
                conn.execute("DELETE FROM chunks   WHERE document_id = ?", (document_id,))
                conn.execute("DELETE FROM pages    WHERE document_id = ?", (document_id,))
                conn.execute("DELETE FROM failures WHERE document_id = ?", (document_id,))
            run_id = conn.execute(
                "INSERT INTO runs (document_id, run_type, status, stage, started_at) "
                "VALUES (?, 'ingest', 'running', 'extract', ?)",
                (document_id, _now()),
            ).lastrowid

        # ---- extract every page in memory (no DB, per-page isolation) ----
        pages_data: list[dict] = []
        for idx in range(page_count):
            had_error = False
            err: str | None = None
            try:
                page = doc[idx]
                text = page.get_text("text")
                blocks = len(page.get_text("blocks"))
                images = len(page.get_images(full=True))
                width, height = float(page.rect.width), float(page.rect.height)
            except Exception as e:  # noqa: BLE001 - isolate a bad page, keep going
                text, blocks, images, width, height = "", 0, 0, 0.0, 0.0
                had_error, err = True, repr(e)[:500]

            status, density = _classify_page(
                len(text), width, height, had_error=had_error,
                ocr_min_chars=settings.ocr_min_chars,
                density_floor=settings.low_text_density_per_kchar2,
            )
            label = None if had_error else _detect_printed_label(text, page_count)
            ranges = (
                _chunk_ranges(
                    text, settings.chunk_target_chars,
                    settings.chunk_overlap_chars, settings.chunk_boundary_backoff_chars,
                )
                if status in ("TEXT_EXTRACTED", "LOW_TEXT")
                else []
            )
            pages_data.append(
                {
                    "idx": idx, "text": text, "char_count": len(text),
                    "width": width, "height": height, "status": status, "err": err,
                    "label": label, "ranges": ranges,
                    "meta": {
                        "block_count": blocks, "image_count": images,
                        "text_density_per_kchar2": density,
                        "printed_label_candidate": label,
                    },
                }
            )

        total_chunks = sum(len(p["ranges"]) for p in pages_data)
        if total_chunks > settings.max_chunks_per_doc:
            raise IngestError(
                "too_many_chunks", document_id=document_id,
                detail=f"{total_chunks} > {settings.max_chunks_per_doc}",
            )

        # ---- T2: persist pages + chunks + failures + finalise (atomic) ----
        with db.transaction(conn):
            for p in pages_data:
                page_id = conn.execute(
                    "INSERT INTO pages "
                    "(document_id, page_index, printed_label, text, char_count, width, height, "
                    " extraction_method, extraction_status, extraction_error, extraction_meta) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'text_layer', ?, ?, ?)",
                    (
                        document_id, p["idx"], p["label"], p["text"], p["char_count"],
                        p["width"], p["height"], p["status"], p["err"],
                        json.dumps(p["meta"], ensure_ascii=False),
                    ),
                ).lastrowid
                for seq, (s, e) in enumerate(p["ranges"]):
                    conn.execute(
                        "INSERT INTO chunks "
                        "(document_id, page_index, seq, char_offset, char_end, "
                        " header_prefix_len, text) VALUES (?, ?, ?, ?, ?, 0, ?)",
                        (document_id, p["idx"], seq, s, e, p["text"][s:e]),
                    )
                if p["status"] in ("LOW_TEXT", "EMPTY"):
                    _record_page_failure(
                        conn, run_id, document_id, page_id, "ocr_page",
                        f"page_{p['status'].lower()}",
                        {"page_index": p["idx"], "char_count": p["char_count"]},
                    )
                elif p["status"] == "EXTRACTION_ERROR":
                    _record_page_failure(
                        conn, run_id, document_id, page_id, "run_error",
                        "page_extraction_error",
                        {"page_index": p["idx"], "error": p["err"]},
                    )
            conn.execute(
                "UPDATE runs SET status = 'done', stage = 'complete', finished_at = ?, "
                "pages_processed = ?, chunks_processed = ? WHERE id = ?",
                (_now(), page_count, total_chunks, run_id),
            )
            conn.execute(
                "UPDATE documents SET status = 'ingested', page_count = ? WHERE id = ?",
                (page_count, document_id),
            )

        result = _summary(conn, document_id, sha, duplicate=False)
        log.info(
            "ingested document_id=%s pages=%s chunks=%s text=%s low_text=%s empty=%s error=%s",
            document_id, page_count, total_chunks, result.pages_text_extracted,
            result.pages_low_text, result.pages_empty, result.pages_extraction_error,
        )
        return result

    except IngestError as e:
        if document_id is not None:
            _mark_failed(conn, document_id, run_id, e.code)
            e.document_id = document_id
        raise
    except Exception as e:  # noqa: BLE001 - convert to an inspectable IngestError
        _mark_failed(conn, document_id, run_id, f"unexpected: {e!r}")
        raise IngestError("ingest_failed", document_id=document_id, detail=repr(e)[:200]) from e
    finally:
        conn.close()
        doc.close()
