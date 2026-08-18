"""Fetch real news articles from public RSS feeds and store them in SQLite.

Pipeline per feed:
    download feed -> parse entries (feedparser) -> clean the HTML snippet ->
    optionally fetch the article page for a fuller excerpt -> store.

Robustness choices worth noting:
  * Each feed is fetched independently and its failure is recorded rather than
    raised, so one dead host cannot empty the grid.
  * Articles are de-duplicated by source_url at the database level
    (UNIQUE constraint), so the fetcher is safe to run repeatedly.
  * Article-page scraping is best-effort: on any failure we fall back to the
    RSS snippet instead of dropping the article.
"""

import logging
import re
from datetime import datetime, timezone

import feedparser
import httpx
from bs4 import BeautifulSoup

from app.core.config import get_settings
from app.models import ArticleDraft, FetchReport
from app.repositories import news_repository
from app.services.rss_feeds import FEEDS, FeedSource

logger = logging.getLogger(__name__)

# Some feeds serve a default page to unknown clients; identify ourselves.
USER_AGENT = "MoodNewsBot/0.1 (+https://github.com/; RSS reader for a demo app)"
REQUEST_TIMEOUT = 20.0

# Below this length the RSS snippet is too thin to rewrite or to mine facts
# from, so we try to fetch the article page for a fuller excerpt.
FULL_TEXT_THRESHOLD = 500
# Articles shorter than this even after scraping are skipped entirely.
MIN_STORE_LENGTH = 180
# Upper bound on stored text: keeps LLM prompts (and their cost) predictable.
MAX_TEXT_LENGTH = 6000
# Length of the card preview shown in the grid.
PREVIEW_LENGTH = 240


def _clean_text(raw_html: str | None) -> str:
    """Strip markup and collapse whitespace out of an RSS snippet."""
    if not raw_html:
        return ""
    text = BeautifulSoup(raw_html, "lxml").get_text(" ")
    return re.sub(r"\s+", " ", text).strip()


def _parse_published(entry) -> str | None:
    """Normalise a feed entry's date to an ISO-8601 UTC string."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return (
        datetime(*parsed[:6], tzinfo=timezone.utc)
        .isoformat(timespec="seconds")
    )


def _make_preview(text: str) -> str:
    if len(text) <= PREVIEW_LENGTH:
        return text
    # Cut on a word boundary so the card preview does not end mid-word.
    return text[:PREVIEW_LENGTH].rsplit(" ", 1)[0] + "…"


def _fetch_article_body(client: httpx.Client, url: str) -> str:
    """Best-effort extraction of the article body from the source page.

    Deliberately simple heuristics (<article>/<main>, then paragraph tags) - no
    third-party readability dependency. Any failure returns "" and the caller
    keeps the RSS snippet.
    """
    try:
        response = client.get(url, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.debug("Could not fetch article page %s: %s", url, exc)
        return ""

    soup = BeautifulSoup(response.text, "lxml")
    for tag in soup(["script", "style", "noscript", "aside", "figure"]):
        tag.decompose()

    container = soup.find("article") or soup.find("main") or soup
    paragraphs = [
        re.sub(r"\s+", " ", p.get_text(" ")).strip()
        for p in container.find_all("p")
    ]
    # Drop bylines, cookie notices and other one-line furniture.
    body = " ".join(p for p in paragraphs if len(p) > 60)
    return body[:MAX_TEXT_LENGTH].strip()


def fetch_feed(
    client: httpx.Client, source: FeedSource, limit: int
) -> list[ArticleDraft]:
    """Download and parse one feed into article drafts."""
    response = client.get(source.url, follow_redirects=True)
    response.raise_for_status()

    parsed = feedparser.parse(response.content)
    drafts: list[ArticleDraft] = []

    for entry in parsed.entries[: limit * 2]:  # over-fetch: some get skipped
        if len(drafts) >= limit:
            break

        link = (entry.get("link") or "").strip()
        title = _clean_text(entry.get("title"))
        if not link or not title:
            continue

        snippet = _clean_text(
            entry.get("summary") or entry.get("description") or ""
        )
        text = snippet

        # Only pay for a page fetch when the feed snippet is too thin.
        if len(text) < FULL_TEXT_THRESHOLD:
            body = _fetch_article_body(client, link)
            if len(body) > len(text):
                text = body

        text = text[:MAX_TEXT_LENGTH].strip()
        if len(text) < MIN_STORE_LENGTH:
            logger.debug("Skipping short article: %s", link)
            continue

        drafts.append(
            ArticleDraft(
                title=title,
                original_text=text,
                summary=_make_preview(snippet or text),
                source_name=source.name,
                source_url=link,
                author=_clean_text(entry.get("author")) or None,
                published_at=_parse_published(entry),
            )
        )

    return drafts


def fetch_all() -> FetchReport:
    """Fetch every configured feed and persist new articles. Never raises."""
    settings = get_settings()
    report = FetchReport()

    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    with httpx.Client(timeout=REQUEST_TIMEOUT, headers=headers) as client:
        for source in FEEDS:
            try:
                drafts = fetch_feed(client, source, settings.max_articles_per_feed)
            except Exception as exc:  # one bad feed must not stop the others
                logger.warning("Feed failed: %s (%s)", source.name, exc)
                report.feeds_failed[source.name] = str(exc)
                continue

            report.feeds_ok.append(source.name)
            for draft in drafts:
                if news_repository.insert_article(draft) is None:
                    report.skipped_duplicates += 1
                else:
                    report.inserted += 1

            logger.info("%s: %d entries parsed", source.name, len(drafts))

    report.total_in_db = news_repository.count_articles()
    logger.info(
        "Fetch complete: %d new, %d duplicates, %d total in DB (%d/%d feeds ok)",
        report.inserted,
        report.skipped_duplicates,
        report.total_in_db,
        len(report.feeds_ok),
        len(FEEDS),
    )
    return report


def ensure_articles() -> FetchReport | None:
    """Populate the database on startup when it holds too few articles."""
    settings = get_settings()
    existing = news_repository.count_articles()
    if existing >= settings.min_articles:
        logger.info("Database already holds %d articles; skipping fetch", existing)
        return None
    logger.info("Only %d articles stored; fetching from RSS feeds", existing)
    return fetch_all()
