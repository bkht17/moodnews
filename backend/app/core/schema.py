"""Database schema and initialisation.

The DDL lives here as plain SQL so the storage model is readable at a glance.
`init_db()` is idempotent (CREATE TABLE IF NOT EXISTS) and is called on every
application startup, which doubles as a lightweight migration entry point.

Design notes:
  * `news.source_url` is UNIQUE - it is the natural de-duplication key across
    repeated feed polls, so re-fetching never produces duplicate articles.
  * `news.facts_json` holds the "anchor facts" extracted from original_text
    (numbers, dates, names, quotes, places). It is the ground truth the
    fact-check pipeline verifies rewrites against; NULL until extraction runs.
  * `rewrites` has UNIQUE(news_id, mood): that pair is the cache key, so a
    given article is only ever rewritten once per mood.
  * Timestamps are stored as ISO-8601 UTC strings. SQLite has no native date
    type, and ISO-8601 sorts lexicographically, so ORDER BY still works.
"""

import logging

from app.core.database import get_connection

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS news (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    original_text TEXT NOT NULL,
    summary       TEXT,
    source_name   TEXT NOT NULL,
    source_url    TEXT NOT NULL UNIQUE,
    author        TEXT,
    published_at  TEXT,
    fetched_at    TEXT NOT NULL,
    facts_json    TEXT
);

CREATE INDEX IF NOT EXISTS idx_news_published_at ON news (published_at DESC);

CREATE TABLE IF NOT EXISTS rewrites (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    news_id              INTEGER NOT NULL REFERENCES news (id) ON DELETE CASCADE,
    mood                 TEXT NOT NULL,
    rewritten_text       TEXT NOT NULL,
    facts_preserved_json TEXT,
    fact_check_status    TEXT NOT NULL DEFAULT 'unknown',
    fact_check_notes     TEXT,
    model                TEXT,
    attempts             INTEGER NOT NULL DEFAULT 1,
    created_at           TEXT NOT NULL,
    UNIQUE (news_id, mood)
);

CREATE INDEX IF NOT EXISTS idx_rewrites_news_mood ON rewrites (news_id, mood);
"""


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA_SQL)
    logger.info("Database schema ready")
