"""SQLite access: connection setup, schema init + forward-only migrations,
transactions. No ORM, no DAO layer — callers use ``sqlite3`` rows directly.

``schema.sql`` is the genesis schema (``PRAGMA user_version = 0``). Every change
after Phase 1 is an entry in ``_MIGRATIONS`` applied in order and recorded in
``user_version``; fresh databases and Phase-1/2 databases converge to the same
shape. Migrations are additive where possible; a CHECK-constraint change uses the
SQLite table-redefinition procedure (create new / copy / drop / rename) with
foreign keys briefly disabled and ``PRAGMA foreign_key_check`` verified after —
safe because the affected tables carry no rows until Phase 4 writes facts.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.config import get_settings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
MIGRATIONS_DIR = Path(__file__).with_name("migrations")

# Forward-only migrations, applied when PRAGMA user_version < version. Each SQL
# file lives in app/migrations/ so it stays readable and out of the linter's way.
_MIGRATION_FILES: list[tuple[int, str]] = [
    (1, "0001_phase2_ingestion.sql"),
    (2, "0002_phase3_fact_evidence_model.sql"),
    (3, "0003_phase5_verification.sql"),
]
_MIGRATIONS: list[tuple[int, str]] = [
    (version, (MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
    for version, name in _MIGRATION_FILES
]

CURRENT_SCHEMA_VERSION = _MIGRATIONS[-1][0] if _MIGRATIONS else 0


def _resolve_path(database_path: str | Path | None) -> Path:
    return Path(database_path) if database_path is not None else get_settings().database_path


def connect(database_path: str | Path | None = None, *,
            check_same_thread: bool = True) -> sqlite3.Connection:
    """Open a connection with the pragmas this app relies on. Caller closes it.

    ``check_same_thread=False`` is for the FastAPI request-scoped connection only:
    Starlette may run a sync dependency's teardown (``conn.close()``) on a
    different threadpool thread than its setup. The connection is still used by
    exactly one request, sequentially — never shared concurrently.
    """
    path = _resolve_path(database_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _apply_migrations(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for target, sql in _MIGRATIONS:
        if target <= version:
            continue
        conn.executescript(sql)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                f"migration {target} left foreign-key violations: "
                f"{[tuple(v) for v in violations[:5]]}"
            )
        conn.execute(f"PRAGMA user_version = {int(target)}")  # PRAGMA can't be parameterised
        version = target


def init_db(database_path: str | Path | None = None) -> None:
    """Create the genesis schema if absent, then apply pending migrations.
    Idempotent: safe to call on a fresh, Phase-1, or fully-migrated database."""
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = connect(database_path)
    try:
        conn.executescript(schema_sql)
        _apply_migrations(conn)
        conn.commit()
    finally:
        conn.close()


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Commit on success, roll back and re-raise on any exception."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def table_names(conn: sqlite3.Connection) -> list[str]:
    """User tables (excludes SQLite internals and FTS shadow tables)."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "AND name NOT LIKE 'facts_fts_%' "
        "ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]
