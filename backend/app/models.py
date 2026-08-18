"""Domain models shared between the fetcher, the repository and the API."""

from datetime import datetime, timezone

from pydantic import BaseModel, Field


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string (the DB timestamp format)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ArticleDraft(BaseModel):
    """A parsed feed entry, before it is written to the database."""

    title: str
    original_text: str
    summary: str | None = None
    source_name: str
    source_url: str
    author: str | None = None
    published_at: str | None = None


class Article(ArticleDraft):
    """A stored article, as read back from the `news` table."""

    id: int
    fetched_at: str
    facts_json: str | None = None


class FetchReport(BaseModel):
    """Outcome of a fetch run, returned by the CLI and logged on startup."""

    inserted: int = 0
    skipped_duplicates: int = 0
    skipped_too_short: int = 0
    feeds_ok: list[str] = Field(default_factory=list)
    feeds_failed: dict[str, str] = Field(default_factory=dict)
    total_in_db: int = 0
