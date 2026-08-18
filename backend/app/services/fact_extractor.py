"""Anchor-fact extraction: the ground truth the fact-check pipeline defends.

Before an article is ever handed to the rewriting LLM we mine it for the pieces
that must survive a change of tone untouched:

    numbers   quantities, percentages, money, counts
    dates     absolute dates, years, weekdays, clock times
    quotes    direct speech, i.e. anything inside quotation marks
    names     people, organisations, proper nouns generally
    places    countries and cities, via a small gazetteer plus preposition cues

The result is stored in `news.facts_json` and is later used twice: it is pasted
into the rewrite prompt as "these must not change", and it is the checklist the
two-layer fact-check verifies the rewrite against.

Why regex rather than NER
-------------------------
This is deliberately dependency-free pattern matching. It is strong exactly
where it matters most - numbers, dates and quoted speech are highly regular and
are the facts a tone rewrite is most likely to corrupt - and weak on entity
recognition, where it over-captures (any capitalised phrase looks like a name)
and cannot tell a person from an organisation.

    NER UPGRADE POINT: swapping `_extract_entities` for a
    statistical NER model (spaCy `en_core_web_sm` with PERSON / ORG / GPE / LOC
    labels, or HuggingFace `dslim/bert-base-NER`) would fix both weaknesses and
    give typed entities for free. The Fact/FactSet contract below would not
    change, so it is a drop-in replacement: only that one function would.

Over-capture is the intended failure mode. A spurious "fact" costs an extra
line in the prompt; a missed number is a fact the pipeline can no longer
protect. The verbatim-required set (numbers/dates/quotes) is kept precise, and
the noisier name/place sets are verified more leniently - see
`Fact.verbatim_required`.
"""

import logging
import re

from app.models import Article, Fact, FactSet
from app.repositories import news_repository

logger = logging.getLogger(__name__)

# Per-type caps. Anchor facts go into every rewrite prompt, so an article that
# is one long table of numbers must not blow up the token budget.
MAX_PER_TYPE = {
    "number": 25,
    "date": 15,
    "quote": 10,
    "name": 25,
    "place": 15,
}

# Characters of surrounding text stored with each fact. The LLM auditor uses it
# to tell "93 deaths" from an unrelated "93" elsewhere in the article.
CONTEXT_RADIUS = 60


# --- Building blocks --------------------------------------------------------

_MONTH = (
    r"(?:January|February|March|April|May|June|July|August|September|October"
    r"|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)\.?"
)
# "May" is excluded from the standalone-month pattern: on its own it is far more
# often the modal verb at the start of a sentence than the month.
_MONTH_STANDALONE = (
    r"(?:January|February|March|April|June|July|August|September|October"
    r"|November|December)"
)
_WEEKDAY = r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
_SCALE = r"(?:million|billion|trillion|thousand|bn|m)"

# Order matters: the first pattern to claim a span wins, so the longest and
# most specific forms are listed first ("18 August 2026" before "2026").
_DATE_PATTERNS: list[str] = [
    r"\b\d{4}-\d{2}-\d{2}\b",                                   # 2026-08-18
    rf"\b\d{{1,2}}\s+{_MONTH}\s+\d{{4}}\b",                     # 18 August 2026
    rf"\b{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b",   # August 18, 2026
    r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",                             # 18/08/2026
    rf"\b\d{{1,2}}\s+{_MONTH}\b",                               # 18 August
    rf"\b{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?\b",               # August 18
    r"\b\d{1,2}:\d{2}\s?(?:a\.m\.|p\.m\.|am|pm|AM|PM)?"
    r"(?:\s?(?:GMT|UTC|ET|EST|EDT|PT|CET))?\b",                 # 14:30 GMT
    r"\b\d{1,2}\s?(?:a\.m\.|p\.m\.|am|pm)\b",                   # 9 a.m.
    r"\b(?:19|20)\d{2}s\b",                                     # 1990s
    rf"\b{_WEEKDAY}\b",                                         # Monday
    r"\b(?:19|20)\d{2}\b",                                      # 2026
    rf"\b{_MONTH_STANDALONE}\b",                                # August
]

_NUMBER_PATTERNS: list[str] = [
    # Money, optionally scaled: $5 million, £1.2bn
    rf"[$€£¥]\s?\d[\d,]*(?:\.\d+)?(?:\s?{_SCALE})?\b",
    # Percentages: 18%, 18 percent, 18 per cent
    r"\b\d[\d,]*(?:\.\d+)?\s?(?:%|per\s?cent(?:age)?|percent)",
    # Scaled counts: 2.5 million
    rf"\b\d[\d,]*(?:\.\d+)?\s?{_SCALE}\b",
    # Ordinals: 3rd
    r"\b\d+(?:st|nd|rd|th)\b",
    # Bare numbers, including decimals and thousands separators
    r"\b\d[\d,]*(?:\.\d+)?\b",
]

# Straight, curly and guillemet quotation marks. The inner text is captured.
_QUOTE_PATTERNS: list[str] = [
    r"“([^“”]{8,500})”",
    r"\"([^\"]{8,500})\"",
    r"«([^»]{8,500})»",
]

# A capitalised run, allowing internal lowercase particles ("Bank of England",
# "Ursula von der Leyen") and acronyms (NATO, UN).
_PARTICLE = r"(?:of|the|de|del|della|van|von|der|den|bin|al|da|di|du|la|le)"
_NAME_PATTERN = (
    r"\b(?:[A-Z][\w'’\-]*|[A-Z]{2,})"
    # Each following capitalised token may be joined by a run of lowercase
    # particles, so "Ursula von der Leyen" and "Bank of England" stay whole.
    rf"(?:(?:\s+{_PARTICLE})*\s+(?:[A-Z][\w'’\-]*|[A-Z]{{2,}}))*"
)

# Locative cue: a capitalised phrase directly after one of these prepositions
# is probably somewhere rather than someone.
_LOCATIVE_LEAD = re.compile(
    r"\b(?:in|from|at|near|across|outside|inside|toward|towards|into|around)\s+$"
)

# A small gazetteer. Not exhaustive by design - the cue pattern above catches
# what it misses, and a real NER model (see module docstring) would replace it.
_GAZETTEER = {
    # Countries / regions
    "Afghanistan", "Africa", "Algeria", "Argentina", "Asia", "Australia",
    "Austria", "Bangladesh", "Belgium", "Brazil", "Britain", "Canada", "Chile",
    "China", "Colombia", "Congo", "Cuba", "Denmark", "Egypt", "England",
    "Ethiopia", "Europe", "Finland", "France", "Gaza", "Germany", "Ghana",
    "Greece", "Haiti", "Hungary", "India", "Indonesia", "Iran", "Iraq",
    "Ireland", "Israel", "Italy", "Japan", "Jordan", "Kenya", "Lebanon",
    "Libya", "Malaysia", "Mexico", "Morocco", "Myanmar", "Netherlands",
    "Nigeria", "Norway", "Pakistan", "Palestine", "Peru", "Philippines",
    "Poland", "Portugal", "Qatar", "Romania", "Russia", "Rwanda",
    "Saudi Arabia", "Scotland", "Senegal", "Singapore", "Somalia",
    "South Africa", "South Korea", "Spain", "Sudan", "Sweden", "Switzerland",
    "Syria", "Taiwan", "Tanzania", "Thailand", "Turkey", "Uganda", "Ukraine",
    "Venezuela", "Vietnam", "Wales", "Yemen", "Zimbabwe",
    "United States", "United Kingdom", "North Korea", "New Zealand",
    # Cities
    "Amsterdam", "Ankara", "Athens", "Baghdad", "Bangkok", "Barcelona",
    "Beijing", "Beirut", "Berlin", "Brussels", "Budapest", "Buenos Aires",
    "Cairo", "Chicago", "Copenhagen", "Damascus", "Delhi", "Doha", "Dubai",
    "Dublin", "Edinburgh", "Geneva", "Glasgow", "Granada", "Hong Kong",
    "Istanbul", "Jakarta", "Jerusalem", "Johannesburg", "Kabul", "Karachi",
    "Kyiv", "Lagos", "Lima", "Lisbon", "London", "Los Angeles", "Madrid",
    "Manchester", "Manila", "Melbourne", "Miami", "Milan", "Moscow", "Mumbai",
    "Nairobi", "New York", "Osaka", "Oslo", "Ottawa", "Paris", "Portland",
    "Prague", "Pyongyang", "Rio de Janeiro", "Riyadh", "Rome", "San Francisco",
    "Sao Paulo", "Seoul", "Shanghai", "Singapore", "Stockholm", "Sydney",
    "Taipei", "Tehran", "Tel Aviv", "Tokyo", "Toronto", "Vienna", "Warsaw",
    "Washington", "Wellington", "Zurich",
}

# Capitalised words that are almost always sentence-initial ordinary English
# rather than a name. Only ever applied to *single-word* candidates.
_COMMON_CAPITALISED = {
    "A", "About", "According", "Additionally", "After", "Again", "All",
    "Almost", "Along", "Already", "Also", "Although", "Always", "American",
    "Among", "An", "And", "Another", "Any", "Are", "around", "As", "At",
    "Back", "Because", "Before", "Behind", "Being", "Below", "Best", "Better",
    "Between", "Both", "But", "By", "Can", "Compared", "Could", "Currently",
    "Despite", "Did", "Do", "Does", "During", "Each", "Earlier", "Early",
    "Even", "Every", "Finally", "First", "Following", "For", "From", "Further",
    "Get", "Given", "Had", "Has", "Have", "He", "Her", "Here", "His",
    "How", "However", "I", "If", "In", "Instead", "Into", "Is", "It", "Its",
    "Just", "Last", "Later", "Like", "Many", "May", "Meanwhile", "More",
    "Most", "Much", "My", "Never", "New", "Next", "No", "None", "Nor", "Not",
    "Now", "Of", "Off", "On", "Once", "One", "Only", "Or", "Other", "Others",
    "Our", "Out", "Over", "Perhaps", "Recently", "Second", "See", "Several",
    "She", "Should", "Since", "So", "Some", "Still", "Such", "Than", "That",
    "The", "Their", "Then", "There", "These", "They", "Third", "This",
    "Those", "Though", "Three", "Through", "Thus", "To", "Today", "Two",
    "Under", "Unlike", "Until", "Up", "Us", "Using", "Very", "Was", "We",
    "Were", "What", "When", "Where", "Whether", "Which", "While", "Who",
    "Why", "Will", "With", "Within", "Without", "Would", "Yet", "You", "Your",
}

# Boilerplate that survives article scraping and is not a fact about the story.
_NAME_NOISE = {
    "Advertisement", "Getty", "Getty Images", "Image", "Image Source",
    "Images", "Photo", "Read More", "Reuters", "Share", "Subscribe",
    "Copyright", "All Rights Reserved", "Enlarge", "Listen", "Watch",
}

_COMPILED_DATES = [re.compile(p) for p in _DATE_PATTERNS]
_COMPILED_NUMBERS = [re.compile(p) for p in _NUMBER_PATTERNS]
_COMPILED_QUOTES = [re.compile(p, re.DOTALL) for p in _QUOTE_PATTERNS]
_COMPILED_NAME = re.compile(_NAME_PATTERN)
_WHITESPACE = re.compile(r"\s+")


# --- Normalisation ----------------------------------------------------------


def normalise(value: str) -> str:
    """Comparison form of a fact: collapsed whitespace, unified punctuation.

    Shared with the fact-check layer so extraction and verification always
    agree on what "the same string" means.
    """
    text = _WHITESPACE.sub(" ", value).strip()
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("–", "-").replace("—", "-")
    return text


def digit_core(value: str) -> str:
    """The digits of a numeric fact, free of separators: "1,200" -> "1200".

    Lets the checker accept a rewrite that formats a number differently while
    still catching one that *changes* it.
    """
    return re.sub(r"[^\d.]", "", value).rstrip(".")


# --- Span bookkeeping -------------------------------------------------------


def _overlaps(span: tuple[int, int], taken: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(start < t_end and end > t_start for t_start, t_end in taken)


def _context(text: str, start: int, end: int) -> str:
    snippet = text[max(0, start - CONTEXT_RADIUS) : end + CONTEXT_RADIUS]
    return _WHITESPACE.sub(" ", snippet).strip()


# --- Extractors -------------------------------------------------------------


def _collect(
    text: str,
    patterns: list[re.Pattern[str]],
    fact_type: str,
    taken: list[tuple[int, int]],
    *,
    verbatim: bool,
) -> list[Fact]:
    """Run patterns in priority order, claiming spans as they match.

    Claiming spans is what stops "2026" inside "18 August 2026" from also being
    reported as a bare number, and keeps each fact counted once.
    """
    found: dict[str, Fact] = {}

    for pattern in patterns:
        for match in pattern.finditer(text):
            span = match.span()
            if _overlaps(span, taken):
                continue
            surface = match.group(0).strip()
            if not surface:
                continue
            taken.append(span)

            key = normalise(surface).lower()
            if key in found:
                found[key].occurrences += 1
                continue
            found[key] = Fact(
                type=fact_type,
                text=surface,
                matcher=normalise(surface),
                context=_context(text, *span),
                verbatim_required=verbatim,
            )

    return list(found.values())


def _extract_quotes(text: str) -> list[Fact]:
    """Direct speech. Quotes get their own pass over the full text.

    Deliberately *not* span-claiming against the other extractors: a number or
    a name inside a quotation is still a fact in its own right and must be
    protected independently of the quote that contains it.
    """
    found: dict[str, Fact] = {}

    for pattern in _COMPILED_QUOTES:
        for match in pattern.finditer(text):
            inner = normalise(match.group(1))
            # Quotation marks around one or two words are usually a scare quote
            # or a product name ("cobalt blue"), not reported speech. Requiring
            # three words keeps short real quotes ("I am innocent") and drops
            # those, which matters because quotes are verbatim-required: a
            # scare quote pinned as ground truth is a false failure waiting to
            # happen.
            if len(inner) < 12 or inner.count(" ") < 2:
                continue
            key = inner.lower()
            if key in found:
                found[key].occurrences += 1
                continue
            found[key] = Fact(
                type="quote",
                text=inner,
                matcher=inner,
                context=_context(text, *match.span()),
                verbatim_required=True,
            )

    return list(found.values())


def _strip_possessive(surface: str) -> str:
    """"Zimbabwe's" -> "Zimbabwe", so the gazetteer still recognises it."""
    for suffix in ("'s", "’s", "'", "’"):
        if surface.endswith(suffix) and len(surface) > len(suffix) + 2:
            return surface[: -len(suffix)]
    return surface


def _extract_entities(text: str, taken: list[tuple[int, int]]) -> list[Fact]:
    """Capitalised runs left over once dates and numbers are claimed, split
    into places and names.

    Names and places are found in a single pass and then classified, rather
    than by scanning for known place names first. Scanning first gets
    "Bank of England" wrong: the gazetteer claims "England" and the
    organisation is lost. Matching the whole capitalised run and only then
    asking what it is keeps multi-word entities intact.

    Classification happens per *surface form* rather than per occurrence, so
    one entity cannot be reported as a place in one sentence and a name in the
    next. In order:
      1. the run is exactly a gazetteer entry                     -> place
      2. some occurrence follows a locative preposition, and the
         run is not an acronym-bearing organisation ("BBC Africa") -> place
      3. otherwise                                                 -> name
    """
    # surface -> occurrences, first span, whether any occurrence looked locative
    seen: dict[str, dict] = {}

    for match in _COMPILED_NAME.finditer(text):
        span = match.span()
        if _overlaps(span, taken):
            continue
        surface = _strip_possessive(match.group(0).strip(" '’-"))
        if not surface or surface in _NAME_NOISE:
            continue

        words = surface.split()
        if surface not in _GAZETTEER:
            # Single capitalised words are the ambiguous case: keep acronyms
            # and anything that is not ordinary sentence-initial English.
            if len(words) == 1:
                if surface in _COMMON_CAPITALISED or len(surface) < 3:
                    continue
                if not surface.isupper() and _starts_sentence(text, span[0]):
                    # Cannot tell a name from a capitalised first word here;
                    # the same name almost always recurs mid-sentence
                    # elsewhere, where it is unambiguous, so dropping this
                    # occurrence is cheap.
                    continue
            elif words[0] in _COMMON_CAPITALISED:
                # "The Guardian" -> "Guardian"; drop the leading filler word.
                surface = " ".join(words[1:])
                if not surface:
                    continue

        taken.append(span)
        entry = seen.get(surface.lower())
        locative = bool(_LOCATIVE_LEAD.search(text[: span[0]]))
        if entry is None:
            seen[surface.lower()] = {
                "surface": surface,
                "span": span,
                "locative": locative,
                "occurrences": 1,
            }
        else:
            entry["occurrences"] += 1
            entry["locative"] = entry["locative"] or locative

    facts: list[Fact] = []
    for entry in seen.values():
        surface = entry["surface"]
        # An acronym in the run means an organisation ("BBC Africa"), even
        # where the sentence reads like a location.
        has_acronym = any(w.isupper() and len(w) > 1 for w in surface.split())
        if surface in _GAZETTEER:
            fact_type = "place"
        elif entry["locative"] and not has_acronym:
            fact_type = "place"
        else:
            fact_type = "name"

        facts.append(
            Fact(
                type=fact_type,
                text=surface,
                matcher=normalise(surface),
                context=_context(text, *entry["span"]),
                occurrences=entry["occurrences"],
            )
        )

    return facts


def _starts_sentence(text: str, index: int) -> bool:
    """True when the character at `index` opens the text or a new sentence."""
    before = text[:index].rstrip()
    return not before or before[-1] in ".!?“\"'"


# --- Public API -------------------------------------------------------------


def extract_facts(text: str, title: str | None = None) -> FactSet:
    """Mine anchor facts from an article.

    The title is scanned too - headlines carry the headline number ("93 dead")
    that the body may only imply.
    """
    source = f"{title}. {text}" if title else text

    facts: list[Fact] = _extract_quotes(source)

    # One shared span map across the remaining types: priority runs
    # dates -> numbers -> entities, most specific first.
    taken: list[tuple[int, int]] = []
    facts += _collect(source, _COMPILED_DATES, "date", taken, verbatim=True)
    facts += _collect(source, _COMPILED_NUMBERS, "number", taken, verbatim=True)
    facts += _extract_entities(source, taken)

    # Most-repeated first, so the per-type cap keeps the article's load-bearing
    # facts and drops the incidental ones.
    trimmed: list[Fact] = []
    for fact_type, cap in MAX_PER_TYPE.items():
        of_type = [f for f in facts if f.type == fact_type]
        of_type.sort(key=lambda f: (-f.occurrences, -len(f.text)))
        trimmed.extend(of_type[:cap])

    counts = {t: len([f for f in trimmed if f.type == t]) for t in MAX_PER_TYPE}
    return FactSet(facts=trimmed, counts=counts)


def ensure_facts(article: Article) -> FactSet:
    """Return the article's anchor facts, extracting and persisting on first use.

    Called before every rewrite, so a rewrite can never run against an article
    whose ground truth has not been established yet.
    """
    if article.facts_json:
        try:
            return FactSet.model_validate_json(article.facts_json)
        except ValueError:
            # Stored JSON predates a schema change or is corrupt: re-extract.
            logger.warning("Unreadable facts_json for article %s; re-extracting", article.id)

    facts = extract_facts(article.original_text, article.title)
    news_repository.set_facts_json(article.id, facts.model_dump_json())
    article.facts_json = facts.model_dump_json()
    logger.info(
        "Extracted %d anchor facts for article %s (%s)",
        len(facts.facts),
        article.id,
        facts.counts,
    )
    return facts


def backfill_facts() -> int:
    """Extract facts for every stored article that has none yet."""
    pending = news_repository.articles_without_facts()
    for article in pending:
        try:
            ensure_facts(article)
        except Exception:  # one bad article must not stop the backfill
            logger.exception("Fact extraction failed for article %s", article.id)
    if pending:
        logger.info("Fact extraction backfill complete: %d article(s)", len(pending))
    return len(pending)
