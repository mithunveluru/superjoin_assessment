"""Shared fixtures. Every test runs against throwaway SQLite + uploads
directories so nothing touches the project's real ``data/`` or ``uploads/``."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pymupdf
import pytest

from app import db
from app.config import get_settings


@pytest.fixture
def db_path(tmp_path, monkeypatch) -> Iterator[str]:
    """Point the app at isolated database + uploads directories for one test."""
    path = tmp_path / "knowledge.db"
    monkeypatch.setenv("FKL_DATABASE_PATH", str(path))
    monkeypatch.setenv("FKL_UPLOADS_DIR", str(tmp_path / "uploads"))
    get_settings.cache_clear()
    yield str(path)
    get_settings.cache_clear()


@pytest.fixture
def conn(db_path) -> Iterator[sqlite3.Connection]:
    """An initialised database + an open connection."""
    db.init_db()
    connection = db.connect()
    try:
        yield connection
    finally:
        connection.close()


PdfFactory = Callable[..., Path]


@pytest.fixture
def make_pdf(tmp_path) -> PdfFactory:
    """Build a tiny synthetic PDF from a list of page bodies.

    ``pages`` is a list where each item is either a string (text drawn on the
    page) or ``None`` (a blank page — no text layer). Returns the file path.
    """
    counter = {"n": 0}

    def _wrap(body: str, width_chars: int = 88) -> str:
        # PyMuPDF's insert_text does not wrap; pre-wrap so long bodies are fully
        # placed on the page and round-trip through get_text().
        out_lines: list[str] = []
        for para in body.split("\n"):
            line = ""
            for word in para.split(" "):
                if line and len(line) + 1 + len(word) > width_chars:
                    out_lines.append(line)
                    line = word
                else:
                    line = f"{line} {word}".strip()
            out_lines.append(line)
        return "\n".join(out_lines)

    def _make(pages: list[str | None], *, name: str | None = None,
              width: float = 595.0, height: float = 900.0) -> Path:
        counter["n"] += 1
        doc = pymupdf.open()
        for body in pages:
            page = doc.new_page(width=width, height=height)
            if body:
                page.insert_text((40, 50), _wrap(body), fontsize=9)
        out = tmp_path / (name or f"synthetic_{counter['n']}.pdf")
        doc.save(out)
        doc.close()
        return out

    return _make
