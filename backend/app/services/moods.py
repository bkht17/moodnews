"""The moods an article can be rewritten in.

One registry, used by three consumers: the rewrite prompt takes `instruction`,
the API exposes the list at /moods, and the frontend's mood switcher renders
`label` and `description`. Adding a mood means adding an entry here and
nothing else.

Each `instruction` describes *voice only* - rhythm, word choice, what to lean
into. None of them may license a change of substance: the factual constraints
live in the system prompt (see rewriter.SYSTEM_PROMPT) and are identical for
every mood, so a mood can never be the reason a fact moved. The awkward pair
is `ironic` and `dramatic`, where the natural way to write the tone is to
exaggerate; both instructions therefore say explicitly where the tone must
stop, and dramatic is told to find its drama in what the facts already are.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Mood:
    key: str
    label: str
    description: str
    instruction: str


MOODS: list[Mood] = [
    Mood(
        key="neutral",
        label="Neutral",
        description="Plain newswire register",
        instruction=(
            "Write in flat, professional newswire style. Short declarative "
            "sentences, no adjectives of judgement, no emotional colouring, no "
            "rhetorical questions. This is the baseline: it should read like a "
            "wire report that takes no position on how anyone should feel."
        ),
    ),
    Mood(
        key="joyful",
        label="Joyful",
        description="Warm, upbeat, hopeful",
        instruction=(
            "Write with warmth and optimism. Favour bright, energetic verbs, "
            "and where the facts allow it, frame events in terms of what was "
            "gained, achieved or made possible. Where the subject matter is "
            "grim, do not pretend otherwise and do not celebrate harm: find "
            "the warmth in resilience, solidarity or help arriving, and keep "
            "the reporting of the harm itself matter-of-fact."
        ),
    ),
    Mood(
        key="sad",
        label="Sad",
        description="Sombre and mournful",
        instruction=(
            "Write in a sombre, elegiac register. Slower sentences, quiet word "
            "choices, attention to loss and to what things cost the people "
            "involved. Melancholy, not melodrama: no wailing, no pity, and no "
            "invented suffering beyond what the facts state."
        ),
    ),
    Mood(
        key="ironic",
        label="Ironic",
        description="Dry, wry, understated",
        instruction=(
            "Write with dry, understated irony - the raised eyebrow of a "
            "columnist who has seen this before. Deadpan juxtaposition, gentle "
            "understatement, the occasional wry aside. The irony must come "
            "from arranging the real facts against each other, never from "
            "exaggerating them, mocking victims, or implying something the "
            "article does not report. If the story involves death or serious "
            "suffering, drop the irony to near zero and stay respectful."
        ),
    ),
    Mood(
        key="dramatic",
        label="Dramatic",
        description="Cinematic and urgent",
        instruction=(
            "Write with cinematic urgency: vivid framing, tension, a sense of "
            "stakes and momentum. Draw the drama out of what actually "
            "happened - the scale, the timing, the reversal - and never out of "
            "invented peril, speculation about what might happen next, or "
            "inflated numbers. Heighten the telling, not the events."
        ),
    ),
    Mood(
        key="formal",
        label="Formal",
        description="Official and precise",
        instruction=(
            "Write in the register of an official communiqué or a legal "
            "summary: full sentences, precise and impersonal phrasing, no "
            "contractions, no colloquialisms, measured and deliberate "
            "throughout. Formality is a matter of diction, not of adding "
            "caveats or hedges the original does not contain."
        ),
    ),
]

MOODS_BY_KEY: dict[str, Mood] = {mood.key: mood for mood in MOODS}
DEFAULT_MOOD = "neutral"


def get_mood(key: str) -> Mood | None:
    return MOODS_BY_KEY.get((key or "").strip().lower())


def mood_keys() -> list[str]:
    return [mood.key for mood in MOODS]
