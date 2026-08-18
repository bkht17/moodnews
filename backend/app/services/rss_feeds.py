"""RSS source registry.

Four feeds were picked deliberately:
  * three general news agencies from different editorial traditions
    (UK / US / Qatar), so the grid is not one outlet's worldview, and
  * one technology feed, so the moods have non-political material to work with.

Using several independent feeds also removes the single point of failure: if
one host is down or rate-limits us, the fetcher still fills the grid from the
rest (see news_fetcher.fetch_all).

All four are public RSS endpoints requiring no API key or registration.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class FeedSource:
    name: str
    url: str
    category: str


FEEDS: list[FeedSource] = [
    FeedSource(
        name="BBC News",
        url="https://feeds.bbci.co.uk/news/world/rss.xml",
        category="world",
    ),
    FeedSource(
        name="NPR",
        url="https://feeds.npr.org/1001/rss.xml",
        category="world",
    ),
    FeedSource(
        name="Al Jazeera",
        url="https://www.aljazeera.com/xml/rss/all.xml",
        category="world",
    ),
    FeedSource(
        name="Ars Technica",
        url="https://feeds.arstechnica.com/arstechnica/index",
        category="technology",
    ),
]
