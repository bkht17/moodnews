"""Small operational CLI.

    python -m app.cli init-db         # create tables
    python -m app.cli fetch           # fetch all feeds now
    python -m app.cli extract-facts   # mine anchor facts from stored articles
    python -m app.cli show-facts 3    # inspect one article's anchor facts
    python -m app.cli stats           # what is currently stored

Inside Docker:  docker compose exec backend python -m app.cli fetch
"""

import argparse
import logging
import sys

from app.core.schema import init_db
from app.models import FactSet
from app.repositories import news_repository
from app.services.fact_extractor import backfill_facts, extract_facts
from app.services.news_fetcher import fetch_all


def _cmd_init_db() -> int:
    init_db()
    print("Schema created.")
    return 0


def _cmd_fetch() -> int:
    init_db()
    report = fetch_all()
    print(f"Inserted:    {report.inserted}")
    print(f"Duplicates:  {report.skipped_duplicates}")
    print(f"Feeds OK:    {', '.join(report.feeds_ok) or 'none'}")
    if report.feeds_failed:
        for name, error in report.feeds_failed.items():
            print(f"Feed FAILED: {name} -> {error}")
    print(f"Total in DB: {report.total_in_db}")
    # Every stored article needs its ground truth before it can be rewritten.
    print(f"Facts extracted for {backfill_facts()} new article(s)")
    # A run that stored nothing at all is a failure worth a non-zero exit code.
    return 0 if report.total_in_db else 1


def _cmd_extract_facts() -> int:
    init_db()
    print(f"Extracted anchor facts for {backfill_facts()} article(s).")
    return 0


def _cmd_show_facts(news_id: int) -> int:
    """Print one article's anchor facts - the ground truth for its rewrites."""
    init_db()
    article = news_repository.get_article(news_id)
    if article is None:
        print(f"No article with id {news_id}")
        return 1

    facts = (
        FactSet.model_validate_json(article.facts_json)
        if article.facts_json
        else extract_facts(article.original_text, article.title)
    )
    print(f"{article.title}\n{article.source_url}\n")
    for fact_type in ("number", "date", "quote", "name", "place"):
        of_type = facts.by_type(fact_type)
        if not of_type:
            continue
        marker = "!" if of_type[0].verbatim_required else " "
        print(f"{fact_type.upper()} ({len(of_type)}){marker}")
        for fact in of_type:
            seen = f" x{fact.occurrences}" if fact.occurrences > 1 else ""
            print(f"    {fact.text[:90]}{seen}")
        print()
    print(f"{len(facts.verbatim_facts)} fact(s) must appear verbatim in a rewrite.")
    return 0


def _cmd_stats() -> int:
    init_db()
    articles = news_repository.list_articles(limit=100)
    print(f"{len(articles)} article(s) stored\n")
    for article in articles:
        if article.facts_json:
            facts = FactSet.model_validate_json(article.facts_json)
            fact_summary = f"{len(facts.facts):>3} facts ({len(facts.verbatim_facts)} verbatim)"
        else:
            fact_summary = "  no facts yet"
        print(
            f"[{article.id:>3}] {article.source_name:<14} "
            f"{article.published_at or article.fetched_at}  "
            f"{len(article.original_text):>5} chars  {fact_summary}  "
            f"{article.title[:50]}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")

    parser = argparse.ArgumentParser(prog="app.cli", description="MoodNews tools")
    parser.add_argument(
        "command",
        choices=["init-db", "fetch", "extract-facts", "show-facts", "stats"],
    )
    parser.add_argument(
        "news_id", nargs="?", type=int, help="article id, for show-facts"
    )
    args = parser.parse_args(argv)

    if args.command == "show-facts":
        if args.news_id is None:
            parser.error("show-facts needs an article id, e.g. show-facts 3")
        return _cmd_show_facts(args.news_id)

    return {
        "init-db": _cmd_init_db,
        "fetch": _cmd_fetch,
        "extract-facts": _cmd_extract_facts,
        "stats": _cmd_stats,
    }[args.command]()


if __name__ == "__main__":
    sys.exit(main())
