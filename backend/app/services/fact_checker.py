"""Two-layer verification that a rewrite changed the voice and nothing else.

This module is the reason the app can claim facts are preserved rather than
merely asking an LLM nicely to preserve them.

Layer 1 - programmatic (this is the guarantee)
----------------------------------------------
Every fact extracted as `verbatim_required` - numbers, dates, direct quotes -
must be findable in the rewritten text. It is a string search, so it cannot be
talked out of its answer, and its verdict is binding: one missing number is a
failed check no matter what any model says about it. Matching allows only
differences that cannot change meaning:

    numbers  exact, else the digit core: "1,200" satisfies "1200" and vice
             versa, but "1,300" satisfies neither
    dates    exact, else all component tokens present: "August 18, 2026" and
             "18 August 2026" are the same date, "19 August" is not
    quotes   exact, else ignoring punctuation and case, so straight vs curly
             quotation marks pass while a changed word does not

Names and places are deliberately *not* in this layer. A rewrite may say "the
president" on second mention, and failing it for that would train the pipeline
to cry wolf; those go to layer 2.

Layer 2 - LLM auditor
---------------------
A second, separate call at temperature 0 with its own system prompt, cast as an
adversarial fact-checker rather than a writer. It sees the original, the anchor
facts and the rewrite, and answers a narrow question - what is missing, what is
contradicted - in JSON. It catches what regex cannot: a dropped name, a
reversed causal claim, an invented consequence, a quote re-attributed to the
wrong speaker.

The two layers are combined, never traded off, in `_decide_status`. Layer 1 can
only fail a rewrite; layer 2 can only downgrade one further. A failing check is
always recorded and surfaced - a flagged rewrite is shown to the user with its
warning, never quietly swapped for the original or hidden.
"""

import json
import logging
import re

from app.core.config import get_settings
from app.models import Article, Fact, FactCheckReport, FactSet, LLMVerdict
from app.services.fact_extractor import digit_core, normalise
from app.services.llm_client import LLMError, get_llm_client

logger = logging.getLogger(__name__)

# The auditor is shown a bounded slice of the anchor facts, matching what the
# rewriter was given, so both calls are arguing about the same list.
MAX_FACTS_IN_AUDIT = 40
MAX_TEXT_IN_AUDIT = 8000


AUDITOR_SYSTEM_PROMPT = """\
You are a meticulous fact-checking auditor for a news desk. You are given an \
original news article, a list of anchor facts extracted from it, and a \
rewritten version of that article in a different emotional tone.

Your only job is to determine whether the rewrite is factually faithful to the \
original. You are auditing facts, not writing quality.

Judge ONLY these things:
- Is every anchor fact still present in the rewritten text? A fact may be \
phrased differently, but numbers, dates and quoted words must be identical.
- Does the rewrite state anything that contradicts the original: a changed \
number, a different date, an altered quote, a quote given to the wrong \
speaker, a reversed cause or outcome, a changed relationship between people or \
organisations?
- Does the rewrite add any fact that is not in the original: a new number, \
name, place, date, quote, cause, consequence or piece of context? Report added \
facts as contradictions.

Do NOT flag any of the following, they are the point of the exercise:
- emotional colouring, tone, mood, or loaded word choice
- different sentence structure, ordering, or length
- a person referred to by role or pronoun after being named once
- omission of atmospheric detail that carries no fact

Be strict and literal, and be specific: quote the exact offending text. If \
nothing is wrong, return empty lists.

Reply with a single JSON object and nothing else:
{"all_facts_present": true or false, "missing_facts": ["..."], \
"contradictions": ["..."]}"""


# --- Layer 1: programmatic verification -------------------------------------


def _digit_normalised(text: str) -> str:
    """Drop thousands separators so "1,200" and "1200" compare equal."""
    return re.sub(r"(?<=\d)[,\s](?=\d\d\d\b)", "", text)


def _alnum_only(text: str) -> str:
    """Lowercase, punctuation-free form used for quote comparison."""
    return re.sub(r"[^a-z0-9\s]", "", text.lower())


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class _Haystacks:
    """The rewritten text pre-normalised into the forms each check needs."""

    def __init__(self, rewritten_text: str) -> None:
        self.plain = normalise(rewritten_text)
        self.lower = self.plain.lower()
        self.digits = _digit_normalised(self.plain)
        self.alnum = _collapse(_alnum_only(self.plain))


def _fact_present(fact: Fact, hay: _Haystacks) -> bool:
    """Is this anchor fact still in the rewritten text?

    Tolerances differ by type; see the module docstring. Each is chosen so that
    only meaning-preserving differences pass.
    """
    matcher = fact.matcher
    if matcher in hay.plain or matcher.lower() in hay.lower:
        return True

    if fact.type == "number":
        core = digit_core(matcher)
        # Bare digits, ignoring separators. Guarded on a non-empty core so a
        # number fact that is somehow all punctuation cannot match everything.
        return bool(core) and core in _digit_normalised(hay.digits).replace(",", "")

    if fact.type == "date":
        # Order-independent: every component of the date must be there, so a
        # reformatted date passes but a changed one does not.
        tokens = [t for t in re.split(r"[^A-Za-z0-9]+", matcher) if t]
        return bool(tokens) and all(t.lower() in hay.lower for t in tokens)

    if fact.type == "quote":
        # Punctuation- and case-insensitive: straight vs curly quotation marks
        # and sentence-final punctuation differ harmlessly; words may not.
        needle = _collapse(_alnum_only(matcher))
        return bool(needle) and needle in hay.alnum

    # Names and places are verified by the auditor, not here.
    return True


def check_verbatim(facts: FactSet, rewritten_text: str) -> tuple[int, int, list[str]]:
    """Layer 1. Returns (total, verified, missing) over verbatim-required facts."""
    hay = _Haystacks(rewritten_text)
    required = facts.verbatim_facts
    missing = [fact.text for fact in required if not _fact_present(fact, hay)]
    return len(required), len(required) - len(missing), missing


# --- Layer 2: LLM auditor ---------------------------------------------------


def _facts_for_audit(facts: FactSet) -> str:
    """The anchor facts as compact JSON, grouped by type, for the auditor."""
    grouped: dict[str, list[str]] = {}
    remaining = MAX_FACTS_IN_AUDIT
    for fact in facts.facts:
        if remaining <= 0:
            break
        grouped.setdefault(fact.type, []).append(fact.text)
        remaining -= 1
    return json.dumps(grouped, ensure_ascii=False, indent=2)


def _coerce_list(value: object) -> list[str]:
    """Models occasionally answer with a string, or with objects. Take it all."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, str):
                if item.strip():
                    out.append(item.strip())
            elif isinstance(item, dict):
                # e.g. {"fact": "93", "reason": "..."} - keep it readable.
                out.append(
                    "; ".join(f"{k}: {v}" for k, v in item.items() if v is not None)
                )
            else:
                out.append(str(item))
        return out
    return [str(value)]


def verify_with_llm(
    article: Article, facts: FactSet, rewritten_text: str
) -> LLMVerdict:
    """Layer 2. Never raises: an auditor that cannot run is reported, not fatal.

    A failure here downgrades the result to a warning rather than passing it,
    because "we could not check" is not the same as "we checked and it is fine".
    """
    settings = get_settings()
    if not settings.llm_configured:
        return LLMVerdict(status="skipped", error="LLM_API_KEY is not configured")

    user_prompt = (
        "ORIGINAL ARTICLE\n"
        f"Headline: {article.title}\n\n"
        f"{article.original_text[:MAX_TEXT_IN_AUDIT]}\n\n"
        "ANCHOR FACTS EXTRACTED FROM THE ORIGINAL (JSON)\n"
        f"{_facts_for_audit(facts)}\n\n"
        "REWRITTEN VERSION TO AUDIT\n"
        f"{rewritten_text[:MAX_TEXT_IN_AUDIT]}\n\n"
        "Audit the rewrite against the original and the anchor facts. "
        'Reply with JSON only: {"all_facts_present": bool, '
        '"missing_facts": [...], "contradictions": [...]}'
    )

    try:
        payload = get_llm_client().complete_json(
            system_prompt=AUDITOR_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            # Verification must be deterministic and reproducible: same inputs,
            # same verdict. Configured to 0 by default (see .env.example).
            temperature=settings.llm_verify_temperature,
            max_tokens=1500,
        )
    except LLMError as exc:
        logger.warning("Fact-check auditor call failed: %s", exc)
        return LLMVerdict(status="error", error=str(exc))

    present = payload.get("all_facts_present")
    return LLMVerdict(
        all_facts_present=present if isinstance(present, bool) else None,
        missing_facts=_coerce_list(payload.get("missing_facts")),
        contradictions=_coerce_list(payload.get("contradictions")),
        status="ok",
    )


# --- Combining both layers --------------------------------------------------


def _decide_status(
    missing_verbatim: list[str], verdict: LLMVerdict
) -> tuple[str, str]:
    """Fold both layers into one status plus a human-readable summary.

    failed   layer 1 found a missing number/date/quote, or the auditor found a
             contradiction - both mean the rewrite states something the
             original does not
    warning  layer 1 is clean but the auditor reported missing facts, or could
             not run at all
    passed   both layers clean
    """
    if missing_verbatim:
        shown = ", ".join(f'"{m}"' for m in missing_verbatim[:5])
        return "failed", (
            f"{len(missing_verbatim)} required fact(s) missing from the "
            f"rewrite: {shown}"
        )

    if verdict.contradictions:
        return "failed", (
            f"Auditor found {len(verdict.contradictions)} contradiction(s): "
            f"{verdict.contradictions[0][:200]}"
        )

    if verdict.status == "skipped":
        return "warning", (
            "Numbers, dates and quotes verified; LLM auditor skipped "
            "(no API key configured)."
        )

    if verdict.status == "error":
        return "warning", (
            "Numbers, dates and quotes verified; LLM auditor unavailable "
            f"({verdict.error})."
        )

    if verdict.missing_facts:
        return "warning", (
            f"Numbers, dates and quotes verified; auditor flagged "
            f"{len(verdict.missing_facts)} possibly missing detail(s): "
            f"{verdict.missing_facts[0][:200]}"
        )

    if verdict.all_facts_present is False:
        # The auditor said no but named nothing. Believe the "no", flag it as
        # soft since there is nothing specific to act on.
        return "warning", (
            "Numbers, dates and quotes verified; auditor reported the rewrite "
            "is not fully faithful but named no specific fact."
        )

    return "passed", "All anchor facts verified by both checks."


def check_rewrite(
    article: Article, facts: FactSet, rewritten_text: str, attempts: int = 1
) -> FactCheckReport:
    """Run both layers over a candidate rewrite and report the combined result."""
    total, verified, missing = check_verbatim(facts, rewritten_text)

    # The auditor runs even when layer 1 already failed: its findings make the
    # strict retry prompt specific about everything that is wrong, not just the
    # part a regex could see.
    verdict = verify_with_llm(article, facts, rewritten_text)
    status, summary = _decide_status(missing, verdict)

    report = FactCheckReport(
        status=status,
        verbatim_total=total,
        verbatim_verified=verified,
        missing_verbatim=missing,
        llm=verdict,
        attempts=attempts,
        summary=summary,
    )
    logger.info(
        "Fact check for article %s: %s (%d/%d verbatim facts, %d contradiction(s))",
        article.id,
        status,
        verified,
        total,
        len(verdict.contradictions),
    )
    return report


def build_strict_feedback(report: FactCheckReport) -> str:
    """Turn a failed report into instructions for the corrective retry.

    Naming the exact strings that went missing is what makes the second attempt
    a correction rather than another roll of the dice.
    """
    lines: list[str] = []
    if report.missing_verbatim:
        lines.append(
            "These facts from the original were MISSING or ALTERED in your "
            "rewrite. Each must appear exactly as written here:"
        )
        lines += [f'  - "{fact}"' for fact in report.missing_verbatim[:15]]
    if report.llm.contradictions:
        lines.append(
            "A fact-checking auditor found these CONTRADICTIONS with the "
            "original. Do not state any of these:"
        )
        lines += [f"  - {item}" for item in report.llm.contradictions[:10]]
    if report.llm.missing_facts:
        lines.append("The auditor also reported these details as missing:")
        lines += [f"  - {item}" for item in report.llm.missing_facts[:10]]
    return "\n".join(lines) or report.summary


def rank(report: FactCheckReport) -> tuple[int, int]:
    """Sort key for picking the better of two attempts.

    Status first, then how many verbatim facts survived, so a retry is only
    kept when it is genuinely an improvement.
    """
    order = {"passed": 3, "warning": 2, "unchecked": 1, "failed": 0}
    return order.get(report.status, 0), report.verbatim_verified
