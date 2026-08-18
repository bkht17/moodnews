"""Data access for the `rewrites` table - which doubles as the rewrite cache.

An LLM rewrite is slow and costs money, so every (news_id, mood) pair is
generated once and reused. The table's UNIQUE (news_id, mood) constraint makes
that guarantee structural rather than a matter of calling code remembering to
check: a concurrent duplicate generation can still happen, but it cannot
produce a duplicate row.
"""

import json
import sqlite3

from app.core.database import get_connection
from app.models import Rewrite, utcnow_iso

_COLUMNS = (
    "id, news_id, mood, rewritten_text, facts_preserved_json, "
    "fact_check_status, fact_check_notes, model, attempts, created_at"
)


def _to_rewrite(row: sqlite3.Row) -> Rewrite:
    return Rewrite(**dict(row))


def get_rewrite(news_id: int, mood: str) -> Rewrite | None:
    """The cached rewrite for this article and mood, if one exists."""
    with get_connection() as conn:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM rewrites WHERE news_id = ? AND mood = ?",
            (news_id, mood),
        ).fetchone()
    return _to_rewrite(row) if row else None


def save_rewrite(
    *,
    news_id: int,
    mood: str,
    rewritten_text: str,
    facts_preserved: list[str] | None = None,
    fact_check_status: str = "unchecked",
    fact_check_notes: str | None = None,
    model: str | None = None,
    attempts: int = 1,
) -> Rewrite:
    """Insert or replace the cached rewrite for (news_id, mood).

    Upsert rather than insert so regenerating - after a failed fact check, or
    with `--force` from the CLI - refreshes the cache entry in place instead of
    colliding with it.
    """
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO rewrites
                (news_id, mood, rewritten_text, facts_preserved_json,
                 fact_check_status, fact_check_notes, model, attempts, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (news_id, mood) DO UPDATE SET
                rewritten_text       = excluded.rewritten_text,
                facts_preserved_json = excluded.facts_preserved_json,
                fact_check_status    = excluded.fact_check_status,
                fact_check_notes     = excluded.fact_check_notes,
                model                = excluded.model,
                attempts             = excluded.attempts,
                created_at           = excluded.created_at
            """,
            (
                news_id,
                mood,
                rewritten_text,
                json.dumps(facts_preserved or [], ensure_ascii=False),
                fact_check_status,
                fact_check_notes,
                model,
                attempts,
                utcnow_iso(),
            ),
        )
    stored = get_rewrite(news_id, mood)
    assert stored is not None  # just written inside the same connection scope
    return stored


def list_rewrites(news_id: int) -> list[Rewrite]:
    """Every cached mood for one article."""
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM rewrites WHERE news_id = ? ORDER BY mood",
            (news_id,),
        ).fetchall()
    return [_to_rewrite(row) for row in rows]


def cached_moods(news_id: int) -> list[str]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT mood FROM rewrites WHERE news_id = ? ORDER BY mood",
            (news_id,),
        ).fetchall()
    return [row["mood"] for row in rows]


def delete_rewrite(news_id: int, mood: str) -> bool:
    with get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM rewrites WHERE news_id = ? AND mood = ?", (news_id, mood)
        )
        return cursor.rowcount > 0


def count_rewrites() -> int:
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM rewrites").fetchone()[0]
