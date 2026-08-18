"""Mood rewriting: the same reporting, a different voice, identical facts.

Prompt design
-------------
The work is split across two messages, deliberately:

  * The **system prompt** carries the factual contract. It is constant for
    every mood and every article, so no mood instruction can dilute it, and it
    states the rules as prohibitions ("do not change", "do not add") because
    those are checkable - the fact-check pipeline in `fact_checker` verifies
    the same list the model was given.

  * The **user prompt** carries the variable parts: the requested mood's voice
    instruction, the article, and - crucially - the anchor facts extracted in
    `fact_extractor`, listed explicitly and grouped by type. Telling the model
    *which* strings must survive is far more reliable than asking it to
    "preserve all facts" and hoping its idea of a fact matches ours. It is
    also the same list verification runs against, so the model is being asked
    to satisfy exactly the test it will be marked against.

The reply is requested as JSON with two fields:

    {"rewritten_text": "...", "facts_preserved": ["93", "Tuesday", ...]}

`rewritten_text` is the product. `facts_preserved` is the model's own claim
about what it kept; it is stored and displayed, but it is never evidence -
verification always re-derives the answer from the text. A model that has
dropped a number is exactly the model that will also claim it did not.

Retries
-------
`generate_rewrite` accepts `strict_feedback`. On a first attempt it is empty;
when the fact-check rejects a rewrite, the pipeline calls again with the
specific facts that went missing, which turns the retry into a targeted
correction rather than a reroll of the same dice. Caching (see
`rewrite_repository`) means each (article, mood) pair only pays for this once.
"""

import logging
import threading

from app.core.config import get_settings
from app.models import Article, FactSet, Rewrite, RewriteDraft
from app.repositories import news_repository, rewrite_repository
from app.services import fact_checker
from app.services.fact_extractor import ensure_facts
from app.services.llm_client import LLMError, get_llm_client
from app.services.moods import Mood, get_mood

logger = logging.getLogger(__name__)

# Anchor facts are quoted in the prompt; keep the list bounded so a fact-dense
# article cannot crowd the article itself out of the context window.
MAX_FACTS_IN_PROMPT = 40
MAX_QUOTE_CHARS = 300

# One in-flight generation per (article, mood). Without this, a reader flicking
# through the mood switcher - or two readers on the same article - would start
# several identical rewrites, each an LLM call nobody needed: the cache only
# helps once the first one has finished writing. Sync API handlers run in
# FastAPI's worker threads, so a threading lock is the right primitive here.
_generation_locks: dict[tuple[int, str], threading.Lock] = {}
_locks_guard = threading.Lock()


def _generation_lock(news_id: int, mood_key: str) -> threading.Lock:
    key = (news_id, mood_key)
    with _locks_guard:
        return _generation_locks.setdefault(key, threading.Lock())


SYSTEM_PROMPT = """\
You are a precise news rewriting engine. You rewrite news articles in a \
requested emotional tone while keeping every fact exactly as reported.

ABSOLUTE RULES - these override the tone instruction in every case:
1. NUMBERS, DATES AND TIMES must appear exactly as in the original article. \
Do not round, convert, re-format, recalculate or omit them. "93" stays "93"; \
"18 August 2026" stays "18 August 2026".
2. DIRECT QUOTATIONS must be reproduced word for word inside quotation marks. \
Do not paraphrase, shorten, extend or re-attribute any quote.
3. NAMES of people, organisations and places must be spelled exactly as in the \
original, and must keep the roles and relationships the original gives them.
4. DO NOT ADD FACTS. No new numbers, dates, names, places, quotes, causes, \
consequences or context, however plausible. If it is not in the original, it \
does not go in the rewrite.
5. DO NOT REMOVE FACTS. Every fact in the original must still be present.
6. DO NOT SPECULATE about causes, motives, or what happens next, and do not \
insert your own opinion or commentary.

What you may change: sentence structure, rhythm, word choice, ordering of \
material, and the emotional colouring of the language. That is all. The \
rewrite is the same reporting in a different voice, not a different story.

Length: within roughly 20% of the original.

Reply with a single JSON object and nothing else:
{"rewritten_text": "<the rewritten article>", "facts_preserved": ["<each \
anchor fact you kept, copied exactly as it appears in your rewrite>"]}"""


def _format_facts(facts: FactSet) -> str:
    """Render anchor facts for the prompt, grouped by type, strictest first."""
    sections: list[str] = []
    labels = {
        "number": "NUMBERS - must appear character-for-character",
        "date": "DATES AND TIMES - must appear character-for-character",
        "quote": "DIRECT QUOTES - must appear word-for-word inside quotation marks",
        "name": "NAMES - spelling and role must not change",
        "place": "PLACES - spelling must not change",
    }
    budget = MAX_FACTS_IN_PROMPT

    for fact_type, label in labels.items():
        of_type = facts.by_type(fact_type)[:budget]
        if not of_type:
            continue
        budget -= len(of_type)
        lines = [
            f'  - "{fact.text[:MAX_QUOTE_CHARS]}"'
            + ("…" if len(fact.text) > MAX_QUOTE_CHARS else "")
            for fact in of_type
        ]
        sections.append(f"{label}:\n" + "\n".join(lines))
        if budget <= 0:
            break

    return "\n\n".join(sections) if sections else "  (none extracted)"


def build_user_prompt(
    article: Article,
    facts: FactSet,
    mood: Mood,
    strict_feedback: str | None = None,
) -> str:
    parts = [
        f"REQUESTED MOOD: {mood.label.lower()}",
        f"TONE INSTRUCTION:\n{mood.instruction}",
        (
            "ANCHOR FACTS - every item below must appear in your rewrite, "
            "unchanged:\n" + _format_facts(facts)
        ),
        f"ORIGINAL ARTICLE\nHeadline: {article.title}\n\n{article.original_text}",
    ]

    if strict_feedback:
        # A retry after a failed fact check: name what went wrong, because a
        # generic "try harder" tends to produce the same omission again.
        parts.append(
            "IMPORTANT - YOUR PREVIOUS ATTEMPT FAILED VERIFICATION.\n"
            f"{strict_feedback}\n"
            "Rewrite the article again in the same mood. Keep the tone, but "
            "this time make sure every listed fact appears exactly as written "
            "above. Prefer a plainer sentence over losing a fact: accuracy "
            "beats style. Copy numbers, dates and quotes across literally, "
            "character for character."
        )

    parts.append(
        'Reply with JSON only: {"rewritten_text": "...", "facts_preserved": [...]}'
    )
    return "\n\n".join(parts)


def generate_rewrite(
    article: Article,
    facts: FactSet,
    mood: Mood,
    strict_feedback: str | None = None,
) -> RewriteDraft:
    """One rewriting call. Raises LLMError on failure; does not persist."""
    client = get_llm_client()
    settings_temperature = _rewrite_temperature(strict_feedback)

    payload = client.complete_json(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=build_user_prompt(article, facts, mood, strict_feedback),
        temperature=settings_temperature,
    )

    text = (payload.get("rewritten_text") or "").strip()
    if not text:
        raise LLMError("Model reply contained no rewritten_text")

    claimed = payload.get("facts_preserved") or []
    if not isinstance(claimed, list):
        claimed = [str(claimed)]

    return RewriteDraft(
        rewritten_text=text,
        facts_preserved=[str(item) for item in claimed],
        model=client.model,
    )


def _rewrite_temperature(strict_feedback: str | None) -> float:
    """Creative latitude on the first pass, tighter on a corrective retry."""
    temperature = get_settings().llm_rewrite_temperature
    return min(temperature, 0.3) if strict_feedback else temperature


def get_or_create_rewrite(
    news_id: int, mood_key: str, *, force: bool = False
) -> tuple[Rewrite, bool]:
    """Return the rewrite for (news_id, mood), generating it only if needed.

    Returns (rewrite, from_cache). `force` regenerates and overwrites the
    cached entry - used by the CLI and by a future "regenerate" action.

    Raises LookupError for an unknown article or mood, and LLMError when
    generation fails.
    """
    mood = get_mood(mood_key)
    if mood is None:
        raise LookupError(f"Unknown mood: {mood_key}")

    if not force:
        cached = rewrite_repository.get_rewrite(news_id, mood.key)
        if cached is not None:
            logger.debug("Cache hit for article %s / %s", news_id, mood.key)
            return cached, True

    with _generation_lock(news_id, mood.key):
        # Re-check inside the lock: whoever held it before us may have just
        # generated exactly what this request wanted.
        if not force:
            cached = rewrite_repository.get_rewrite(news_id, mood.key)
            if cached is not None:
                logger.debug(
                    "Cache hit after waiting for article %s / %s", news_id, mood.key
                )
                return cached, True
        return _generate_and_store(news_id, mood), False


def _generate_and_store(news_id: int, mood: Mood) -> Rewrite:
    """Generate, fact-check, retry once if needed, and persist. Assumes the
    caller holds this (article, mood)'s generation lock."""
    article = news_repository.get_article(news_id)
    if article is None:
        raise LookupError(f"Unknown article: {news_id}")

    # Ground truth first: a rewrite must never run against an article whose
    # anchor facts have not been established, or there is nothing to verify
    # it against afterwards.
    facts = ensure_facts(article)

    logger.info("Rewriting article %s as '%s'", news_id, mood.key)
    draft = generate_rewrite(article, facts, mood)
    report = fact_checker.check_rewrite(article, facts, draft.rewritten_text)

    if report.is_failed:
        # One corrective retry, told exactly which facts went missing. Only
        # one: a model that fails the same list twice is not going to get it
        # on the third attempt, and the user is owed an answer, not a stall.
        logger.warning(
            "Fact check failed for article %s / %s (%s); retrying strictly",
            news_id,
            mood.key,
            report.summary,
        )
        try:
            retry_draft = generate_rewrite(
                article,
                facts,
                mood,
                strict_feedback=fact_checker.build_strict_feedback(report),
            )
        except LLMError as exc:
            # Keep the first attempt and its failed status rather than losing
            # the work; the flag travels with it either way.
            logger.warning("Strict retry failed to generate: %s", exc)
        else:
            retry_report = fact_checker.check_rewrite(
                article, facts, retry_draft.rewritten_text, attempts=2
            )
            # Keep whichever attempt verified better - a retry that came back
            # worse is not an improvement just because it came second.
            if fact_checker.rank(retry_report) > fact_checker.rank(report):
                draft, report = retry_draft, retry_report
            report.attempts = 2

    if report.is_failed:
        # Deliberately still stored and served, flagged rather than hidden:
        # the UI shows the warning badge and the reader can compare against
        # the original, which is always displayed beside it.
        logger.warning(
            "Serving article %s / %s with a FAILED fact check: %s",
            news_id,
            mood.key,
            report.summary,
        )

    return rewrite_repository.save_rewrite(
        news_id=news_id,
        mood=mood.key,
        rewritten_text=draft.rewritten_text,
        facts_preserved=draft.facts_preserved,
        fact_check_status=report.status,
        fact_check_notes=report.model_dump_json(),
        model=draft.model,
        attempts=report.attempts,
    )


def recheck_rewrite(news_id: int, mood_key: str) -> Rewrite:
    """Re-run both fact-check layers on a cached rewrite, without regenerating.

    Useful after the checker itself changes, and for inspecting a stored
    rewrite without paying for another rewriting call.
    """
    mood = get_mood(mood_key)
    if mood is None:
        raise LookupError(f"Unknown mood: {mood_key}")

    cached = rewrite_repository.get_rewrite(news_id, mood.key)
    if cached is None:
        raise LookupError(f"No cached rewrite for article {news_id} / {mood.key}")

    article = news_repository.get_article(news_id)
    if article is None:
        raise LookupError(f"Unknown article: {news_id}")

    facts = ensure_facts(article)
    report = fact_checker.check_rewrite(
        article, facts, cached.rewritten_text, attempts=cached.attempts
    )
    return rewrite_repository.save_rewrite(
        news_id=news_id,
        mood=mood.key,
        rewritten_text=cached.rewritten_text,
        facts_preserved=cached.facts_preserved,
        fact_check_status=report.status,
        fact_check_notes=report.model_dump_json(),
        model=cached.model,
        attempts=cached.attempts,
    )
