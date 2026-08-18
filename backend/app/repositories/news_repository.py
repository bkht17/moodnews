"""Data access for the `news` table.

Kept as plain parameterised SQL: every statement below binds its values, so
feed-supplied strings can never be interpolated into a query.
"""

import sqlite3

from app.core.database import get_connection
from app.models import Article, ArticleDraft, utcnow_iso

_COLUMNS = (
    "id, title, original_text, summary, source_name, source_url, "
    "author, published_at, fetched_at, facts_json"
)


def _to_article(row: sqlite3.Row) -> Article:
    return Article(**dict(row))


def insert_article(draft: ArticleDraft) -> int | None:
    """Insert one article, ignoring it if source_url is already stored.

    Returns the new row id, or None when the article was a duplicate.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO news
                (title, original_text, summary, source_name, source_url,
                 author, published_at, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                draft.title,
                draft.original_text,
                draft.summary,
                draft.source_name,
                draft.source_url,
                draft.author,
                draft.published_at,
                utcnow_iso(),
            ),
        )
        # rowcount is 0 when INSERT OR IGNORE hit the UNIQUE(source_url) guard.
        return cursor.lastrowid if cursor.rowcount else None


def list_articles(limit: int = 50, offset: int = 0) -> list[Article]:
    """Newest first. Articles without a publish date fall back to fetch time."""
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT {_COLUMNS} FROM news
            ORDER BY COALESCE(published_at, fetched_at) DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    return [_to_article(row) for row in rows]


def get_article(news_id: int) -> Article | None:
    with get_connection() as conn:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM news WHERE id = ?", (news_id,)
        ).fetchone()
    return _to_article(row) if row else None


def count_articles() -> int:
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]


def set_facts_json(news_id: int, facts_json: str) -> None:
    """Attach extracted anchor facts to an article (used by fact extraction)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE news SET facts_json = ? WHERE id = ?", (facts_json, news_id)
        )


def articles_without_facts() -> list[Article]:
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM news WHERE facts_json IS NULL"
        ).fetchall()
    return [_to_article(row) for row in rows]
