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


# --- Anchor facts -----------------------------------------------------------


class Fact(BaseModel):
    """One "anchor fact" mined from an article's original text.

    `text` is the surface form exactly as it appears in the original. `matcher`
    is the normalised form the programmatic fact-check compares against, so
    that a rewrite writing "1200" instead of "1,200" is not a false failure.
    """

    type: str  # number | date | quote | name | place
    text: str
    matcher: str
    context: str | None = None
    occurrences: int = 1
    # True for numbers, dates and quotes: the programmatic layer of the
    # fact-check requires these to appear verbatim in the rewritten text.
    # Names and places are checked more leniently plus by the LLM auditor,
    # because a rewrite may legitimately say "the president" on second mention.
    verbatim_required: bool = False


class FactSet(BaseModel):
    """Everything extracted from one article; serialised into news.facts_json."""

    version: int = 1
    extracted_at: str = Field(default_factory=utcnow_iso)
    facts: list[Fact] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)

    @property
    def verbatim_facts(self) -> list[Fact]:
        """The non-negotiable facts: numbers, dates and quotes."""
        return [f for f in self.facts if f.verbatim_required]

    def by_type(self, fact_type: str) -> list[Fact]:
        return [f for f in self.facts if f.type == fact_type]


class FetchReport(BaseModel):
    """Outcome of a fetch run, returned by the CLI and logged on startup."""

    inserted: int = 0
    skipped_duplicates: int = 0
    skipped_too_short: int = 0
    feeds_ok: list[str] = Field(default_factory=list)
    feeds_failed: dict[str, str] = Field(default_factory=dict)
    total_in_db: int = 0
