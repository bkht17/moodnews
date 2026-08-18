"""Small operational CLI.

    python -m app.cli init-db   # create tables
    python -m app.cli fetch     # fetch all feeds now
    python -m app.cli stats     # what is currently stored

Inside Docker:  docker compose exec backend python -m app.cli fetch
"""

import argparse
import logging
import sys

from app.core.schema import init_db
from app.repositories import news_repository
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
    # A run that stored nothing at all is a failure worth a non-zero exit code.
    return 0 if report.total_in_db else 1


def _cmd_stats() -> int:
    init_db()
    articles = news_repository.list_articles(limit=100)
    print(f"{len(articles)} article(s) stored\n")
    for article in articles:
        print(
            f"[{article.id:>3}] {article.source_name:<14} "
            f"{article.published_at or article.fetched_at}  "
            f"{len(article.original_text):>5} chars  {article.title[:60]}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")

    parser = argparse.ArgumentParser(prog="app.cli", description="MoodNews tools")
    parser.add_argument("command", choices=["init-db", "fetch", "stats"])
    args = parser.parse_args(argv)

    return {
        "init-db": _cmd_init_db,
        "fetch": _cmd_fetch,
        "stats": _cmd_stats,
    }[args.command]()


if __name__ == "__main__":
    sys.exit(main())
