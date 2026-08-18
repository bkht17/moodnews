"""Domain models shared between the fetcher, the repository and the API."""

import json
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


# --- Rewrites ---------------------------------------------------------------


class RewriteDraft(BaseModel):
    """What the rewriting LLM returned, before it is verified or stored."""

    rewritten_text: str
    # The model's own account of which facts it kept. Useful for debugging and
    # shown in the API, but never trusted as evidence: a model claiming it
    # preserved everything is exactly the failure mode the fact-check exists
    # to catch, so verification always re-derives this from the text itself.
    facts_preserved: list[str] = Field(default_factory=list)
    attempts: int = 1
    model: str | None = None


class Rewrite(BaseModel):
    """A stored rewrite: one article in one mood, cached in the DB."""

    id: int
    news_id: int
    mood: str
    rewritten_text: str
    facts_preserved_json: str | None = None
    # passed | warning | failed | unchecked - see the fact-check pipeline.
    fact_check_status: str = "unchecked"
    fact_check_notes: str | None = None
    model: str | None = None
    attempts: int = 1
    created_at: str

    @property
    def facts_preserved(self) -> list[str]:
        if not self.facts_preserved_json:
            return []
        try:
            return json.loads(self.facts_preserved_json)
        except ValueError:
            return []

    @property
    def fact_check_report(self) -> "FactCheckReport | None":
        """The stored fact-check detail, or None for older/unchecked rows."""
        if not self.fact_check_notes:
            return None
        try:
            return FactCheckReport.model_validate_json(self.fact_check_notes)
        except ValueError:
            return None


# --- Fact checking ----------------------------------------------------------


class LLMVerdict(BaseModel):
    """What the auditor pass reported. Parsed defensively from model JSON."""

    all_facts_present: bool | None = None
    missing_facts: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    # ok | skipped | error - whether the auditor actually ran and answered.
    status: str = "ok"
    error: str | None = None


class FactCheckReport(BaseModel):
    """The combined result of both fact-check layers.

    Serialised into `rewrites.fact_check_notes`, so the API and the frontend
    badge read real counts rather than a bare status string.
    """

    # passed | warning | failed
    status: str = "unchecked"
    # Layer 1, programmatic: numbers, dates and quotes matched against the text.
    verbatim_total: int = 0
    verbatim_verified: int = 0
    missing_verbatim: list[str] = Field(default_factory=list)
    # Layer 2, the LLM auditor.
    llm: LLMVerdict = Field(default_factory=LLMVerdict)
    attempts: int = 1
    summary: str = ""

    @property
    def is_failed(self) -> bool:
        return self.status == "failed"


class FetchReport(BaseModel):
    """Outcome of a fetch run, returned by the CLI and logged on startup."""

    inserted: int = 0
    skipped_duplicates: int = 0
    skipped_too_short: int = 0
    feeds_ok: list[str] = Field(default_factory=list)
    feeds_failed: dict[str, str] = Field(default_factory=dict)
    total_in_db: int = 0
