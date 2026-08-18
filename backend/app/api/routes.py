"""HTTP endpoints.

    GET /moods            the mood switcher's options
    GET /news             the grid
    GET /news/{id}        one article, optionally with a rewrite in a mood

The detail endpoint is where the work happens: with `?mood=` it serves the
cached rewrite, and generates one on demand when there is no cache entry.

Both handlers are defined with `def` rather than `async def` on purpose.
Everything underneath - sqlite3 and the OpenAI SDK - is synchronous and
blocking, and FastAPI runs sync handlers in a worker thread, so a rewrite that
takes twenty seconds does not stall the event loop and every other request
with it.
"""

import logging

from fastapi import APIRouter, HTTPException, Path, Query

from app.api.schemas import (
    FactsOut,
    MoodOut,
    NewsDetail,
    NewsList,
    NewsListItem,
    RewriteError,
    RewriteOut,
)
from app.models import FactSet
from app.repositories import news_repository, rewrite_repository
from app.services.llm_client import LLMError, LLMNotConfigured
from app.services.moods import MOODS, get_mood, mood_keys
from app.services.rewriter import get_or_create_rewrite

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/moods", response_model=list[MoodOut], tags=["moods"])
def list_moods() -> list[MoodOut]:
    """Available rewriting moods, in display order."""
    return [MoodOut.from_mood(mood) for mood in MOODS]


@router.get("/news", response_model=NewsList, tags=["news"])
def list_news(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> NewsList:
    """The news grid: newest first, with a short preview per card."""
    articles = news_repository.list_articles(limit=limit, offset=offset)
    moods_by_article = rewrite_repository.cached_moods_map(
        [article.id for article in articles]
    )

    items = [
        NewsListItem(
            id=article.id,
            title=article.title,
            source_name=article.source_name,
            source_url=article.source_url,
            published_at=article.published_at,
            fetched_at=article.fetched_at,
            # `summary` is the preview built at fetch time; fall back to the
            # head of the article for anything stored before that existed.
            preview=article.summary or article.original_text[:240],
            fact_count=_fact_count(article.facts_json),
            cached_moods=moods_by_article.get(article.id, []),
        )
        for article in articles
    ]
    return NewsList(
        items=items,
        total=news_repository.count_articles(),
        limit=limit,
        offset=offset,
    )


@router.get("/news/{news_id}", response_model=NewsDetail, tags=["news"])
def get_news(
    news_id: int = Path(..., ge=1),
    mood: str | None = Query(
        None,
        description=(
            "Rewrite the article in this mood. Served from cache when "
            "available, generated on demand otherwise. Omit to return the "
            "original text only."
        ),
    ),
) -> NewsDetail:
    """One article: the original, and beside it the rewrite in `mood`.

    A rewrite that cannot be produced - no API key, upstream failure - is
    reported in `rewrite_error` with a 200, because the original article is
    still worth serving. A rewrite that *was* produced but failed its fact
    check is returned normally with `fact_check.status == "failed"`: that is a
    result, not an error, and hiding it would defeat the point of checking.
    """
    article = news_repository.get_article(news_id)
    if article is None:
        raise HTTPException(status_code=404, detail=f"No article with id {news_id}")

    detail_kwargs = {
        "facts": _facts_out(article.facts_json),
        "mood": mood,
        "cached_moods": rewrite_repository.cached_moods(news_id),
    }

    if mood is None:
        return NewsDetail.from_article(article, **detail_kwargs)

    if get_mood(mood) is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown mood '{mood}'. Available: {', '.join(mood_keys())}",
        )

    try:
        rewrite, from_cache = get_or_create_rewrite(news_id, mood)
    except LLMNotConfigured as exc:
        detail_kwargs["rewrite_error"] = RewriteError(
            code="llm_not_configured", message=str(exc)
        )
    except LLMError as exc:
        logger.warning("Rewrite failed for article %s / %s: %s", news_id, mood, exc)
        detail_kwargs["rewrite_error"] = RewriteError(
            code="llm_error",
            message=f"The rewriting service could not be reached: {exc}",
        )
    else:
        detail_kwargs["rewrite"] = RewriteOut.from_rewrite(rewrite, from_cache)
        # Generating one may have added a mood to the cache.
        detail_kwargs["cached_moods"] = rewrite_repository.cached_moods(news_id)
        # Facts are extracted lazily by the rewriter if they were missing.
        if not article.facts_json:
            refreshed = news_repository.get_article(news_id)
            if refreshed is not None:
                detail_kwargs["facts"] = _facts_out(refreshed.facts_json)

    return NewsDetail.from_article(article, **detail_kwargs)


def _parse_facts(facts_json: str | None) -> FactSet | None:
    if not facts_json:
        return None
    try:
        return FactSet.model_validate_json(facts_json)
    except ValueError:
        logger.warning("Ignoring unreadable facts_json")
        return None


def _facts_out(facts_json: str | None) -> FactsOut:
    facts = _parse_facts(facts_json)
    return FactsOut.from_factset(facts) if facts else FactsOut.empty()


def _fact_count(facts_json: str | None) -> int:
    facts = _parse_facts(facts_json)
    return len(facts.facts) if facts else 0
