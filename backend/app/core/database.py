"""SQLite connection plumbing.

We talk to SQLite through the stdlib `sqlite3` driver rather than an ORM: the
data model is three flat tables, the queries are simple, and keeping the SQL
explicit makes the schema (and the facts_json / fact_check_* columns) easy to
read in review. Table DDL is added in the storage section.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.core.config import get_settings


def get_db_path() -> Path:
    path = Path(get_settings().db_path)
    # Make sure the mounted volume directory exists before SQLite touches it.
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path(), timeout=30.0)
    conn.row_factory = sqlite3.Row
    # WAL keeps reads from blocking the background fetch/rewrite writes.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """Transactional connection: commits on success, rolls back on error."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def healthcheck() -> bool:
    try:
        with get_connection() as conn:
            conn.execute("SELECT 1").fetchone()
        return True
    except sqlite3.Error:
        return False
