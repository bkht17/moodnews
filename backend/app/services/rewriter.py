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

from app.core.config import get_settings
from app.models import Article, FactSet, Rewrite, RewriteDraft
from app.repositories import news_repository, rewrite_repository
from app.services.fact_extractor import ensure_facts
from app.services.llm_client import LLMError, get_llm_client
from app.services.moods import Mood, get_mood

logger = logging.getLogger(__name__)

# Anchor facts are quoted in the prompt; keep the list bounded so a fact-dense
# article cannot crowd the article itself out of the context window.
MAX_FACTS_IN_PROMPT = 40
MAX_QUOTE_CHARS = 300


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

    article = news_repository.get_article(news_id)
    if article is None:
        raise LookupError(f"Unknown article: {news_id}")

    # Ground truth first: a rewrite must never run against an article whose
    # anchor facts have not been established, or there is nothing to verify
    # it against afterwards.
    facts = ensure_facts(article)

    logger.info("Rewriting article %s as '%s'", news_id, mood.key)
    draft = generate_rewrite(article, facts, mood)

    # Stored unverified for now: the fact-check pipeline is layered on top of
    # this call in the next step, and will set a real status before saving.
    stored = rewrite_repository.save_rewrite(
        news_id=news_id,
        mood=mood.key,
        rewritten_text=draft.rewritten_text,
        facts_preserved=draft.facts_preserved,
        fact_check_status="unchecked",
        model=draft.model,
        attempts=draft.attempts,
    )
    return stored, False
