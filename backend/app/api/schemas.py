"""Response models for the public API.

These are deliberately separate from the domain models in `app.models`. The
storage layer keeps things like `facts_json` and `fact_check_notes` as JSON
strings because that is how SQLite holds them; the API should hand the browser
parsed objects with obvious names. Keeping the two apart also means a change to
the DB layout is not automatically a change to the API contract.
"""

from pydantic import BaseModel, Field

from app.models import Article, FactCheckReport, FactSet, Rewrite
from app.services.moods import Mood


class MoodOut(BaseModel):
    """One option in the frontend's mood switcher."""

    key: str
    label: str
    description: str

    @classmethod
    def from_mood(cls, mood: Mood) -> "MoodOut":
        return cls(key=mood.key, label=mood.label, description=mood.description)


class FactCheckOut(BaseModel):
    """The fact-check result, shaped for the badge on the rewritten side.

    `verified`/`total` are what the badge counts ("Facts verified 14/14"), and
    they come from the programmatic layer, so the number on screen is a count
    of string matches actually performed - not a model's self-assessment.
    """

    status: str  # passed | warning | failed | unchecked
    verified: int
    total: int
    missing_facts: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    # ok | skipped | error - whether the second-layer auditor ran.
    auditor: str = "ok"
    summary: str = ""
    attempts: int = 1

    @classmethod
    def from_report(cls, report: FactCheckReport) -> "FactCheckOut":
        return cls(
            status=report.status,
            verified=report.verbatim_verified,
            total=report.verbatim_total,
            # Both layers' findings are surfaced together: a missing number
            # from layer 1 and a missing name from layer 2 are both things the
            # reader should see.
            missing_facts=report.missing_verbatim + report.llm.missing_facts,
            contradictions=report.llm.contradictions,
            auditor=report.llm.status,
            summary=report.summary,
            attempts=report.attempts,
        )

    @classmethod
    def unchecked(cls) -> "FactCheckOut":
        return cls(
            status="unchecked",
            verified=0,
            total=0,
            summary="This rewrite has not been fact-checked.",
        )


class RewriteOut(BaseModel):
    mood: str
    text: str
    fact_check: FactCheckOut
    model: str | None = None
    attempts: int = 1
    created_at: str
    from_cache: bool = False
    # The model's own claim about what it preserved. Shown for transparency,
    # never used as verification - see fact_checker.
    facts_preserved: list[str] = Field(default_factory=list)

    @classmethod
    def from_rewrite(cls, rewrite: Rewrite, from_cache: bool) -> "RewriteOut":
        report = rewrite.fact_check_report
        return cls(
            mood=rewrite.mood,
            text=rewrite.rewritten_text,
            fact_check=(
                FactCheckOut.from_report(report)
                if report is not None
                else FactCheckOut.unchecked()
            ),
            model=rewrite.model,
            attempts=rewrite.attempts,
            created_at=rewrite.created_at,
            from_cache=from_cache,
            facts_preserved=rewrite.facts_preserved,
        )


class RewriteError(BaseModel):
    """Why a rewrite could not be produced for this request.

    Returned inside a 200 response rather than as an HTTP error: the original
    article is still perfectly readable, and the comparison view degrades to
    showing one side with an explanation instead of an error page.
    """

    code: str  # llm_not_configured | llm_error | unknown_mood
    message: str


class FactsOut(BaseModel):
    """The anchor facts protecting this article, summarised for the UI."""

    total: int
    verbatim_total: int
    counts: dict[str, int] = Field(default_factory=dict)
    # Just the verbatim-required ones: these are what the badge's count is over.
    verbatim_items: list[str] = Field(default_factory=list)

    @classmethod
    def from_factset(cls, facts: FactSet) -> "FactsOut":
        return cls(
            total=len(facts.facts),
            verbatim_total=len(facts.verbatim_facts),
            counts=facts.counts,
            verbatim_items=[fact.text for fact in facts.verbatim_facts],
        )

    @classmethod
    def empty(cls) -> "FactsOut":
        return cls(total=0, verbatim_total=0)


class NewsListItem(BaseModel):
    """One card in the grid."""

    id: int
    title: str
    source_name: str
    source_url: str
    published_at: str | None = None
    fetched_at: str
    preview: str
    fact_count: int = 0
    cached_moods: list[str] = Field(default_factory=list)


class NewsList(BaseModel):
    items: list[NewsListItem]
    total: int
    limit: int
    offset: int


class NewsDetail(BaseModel):
    """Everything the comparison view needs in one request."""

    id: int
    title: str
    source_name: str
    source_url: str
    published_at: str | None = None
    fetched_at: str
    original_text: str
    facts: FactsOut
    mood: str | None = None
    rewrite: RewriteOut | None = None
    rewrite_error: RewriteError | None = None
    cached_moods: list[str] = Field(default_factory=list)

    @classmethod
    def from_article(cls, article: Article, facts: FactsOut, **extra) -> "NewsDetail":
        return cls(
            id=article.id,
            title=article.title,
            source_name=article.source_name,
            source_url=article.source_url,
            published_at=article.published_at,
            fetched_at=article.fetched_at,
            original_text=article.original_text,
            facts=facts,
            **extra,
        )
