"""Time advancement — kept out of the model's hands as far as possible.

LLMs forget to advance time. Left to itself a model will narrate a journey to
Dragonstone and a three-day siege and still think it is Tuesday morning, because
nothing in its context forces the clock forward.

Two mechanisms fix that, and neither relies on the model remembering:

1. **Elapsed time is a required field with an engine-side default.** The model
   proposes `hours_elapsed`; the engine clamps it to a sane band and substitutes
   the action archetype's default whenever the value is missing, non-numeric or
   absurd. Forgetting is not a state the schema permits — the worst case is the
   default, never a frozen clock.

2. **Phase of day is computed and handed back.** No model ever has to infer
   "is it night?" from `hour: 21.5`. It is told, in words, every single turn.

The model still gets a real say — it is genuinely better than a lookup table at
knowing that "ride to Dragonstone and back" is two days while "ask Varys one
question" is ten minutes. It just cannot express "no time passed at all" by
saying nothing.
"""

from __future__ import annotations

# Hard band. Below the floor a turn would leave the clock effectively frozen;
# above the ceiling a single hallucinated number could skip the whole game.
MIN_HOURS = 0.05      # three minutes — a single line of dialogue
MAX_HOURS = 72.0      # three days — long journeys still fit in one turn

# Per-archetype fallback, used when the model omits or mangles the field. These
# are also quoted in the prompt so its estimates land in a familiar range.
ARCHETYPE_HOURS = {
    "speak": 0.25,      # one exchange
    "converse": 0.75,   # a real conversation
    "observe": 0.5,     # watching a room, reading a face
    "search": 2.0,      # tossing a study for the ledger
    "move": 1.0,        # crossing the keep
    "travel": 12.0,     # leaving the city
    "scheme": 3.0,      # arranging something quietly
    "confront": 0.5,
    "rest": 8.0,        # the deliberate time-jump
    "wait": 4.0,
}
DEFAULT_ARCHETYPE = "speak"

PHASES = (
    (5.0, "night"),        # 00:00-05:00
    (8.0, "dawn"),
    (12.0, "morning"),
    (17.0, "afternoon"),
    (21.0, "evening"),
    (24.0, "night"),
)


def phase_of_day(hour: float) -> str:
    """Words, not numbers. Handed to every prompt so nothing has to infer it."""
    hour = float(hour) % 24.0
    for boundary, name in PHASES:
        if hour < boundary:
            return name
    return "night"


def describe_time(day: int, hour: float) -> str:
    """Human-readable stamp for prompts and the DM panel: 'day 2, evening (19:30)'."""
    hours = int(hour) % 24
    minutes = int(round((hour - int(hour)) * 60))
    if minutes == 60:            # 7.999 -> 08:00, never 07:60
        hours, minutes = (hours + 1) % 24, 0
    return f"day {day}, {phase_of_day(hour)} ({hours:02d}:{minutes:02d})"


def coerce_hours(proposed, archetype: str = DEFAULT_ARCHETYPE) -> tuple[float, str | None]:
    """Turn whatever the model returned into a usable number of hours.

    Returns (hours, note) where note is a DM-panel warning when the proposal had
    to be corrected — so a model that keeps returning nonsense is visible rather
    than silently papered over.
    """
    default = ARCHETYPE_HOURS.get(archetype, ARCHETYPE_HOURS[DEFAULT_ARCHETYPE])

    if proposed is None:
        return default, None      # omission is expected; not worth a warning

    # Models return "2", "2.5", "about 2 hours", 2, 2.5, or None.
    if isinstance(proposed, str):
        cleaned = proposed.strip().lower()
        for suffix in ("hours", "hour", "hrs", "hr", "h"):
            cleaned = cleaned.removesuffix(suffix).strip()
        try:
            proposed = float(cleaned)
        except ValueError:
            return default, f"unparseable hours_elapsed {proposed!r}, used {default}h"

    try:
        hours = float(proposed)
    except (TypeError, ValueError):
        return default, f"non-numeric hours_elapsed {proposed!r}, used {default}h"

    if hours != hours or hours in (float("inf"), float("-inf")):
        return default, f"non-finite hours_elapsed, used {default}h"
    if hours < MIN_HOURS:
        # Covers the common failure directly: the model returned 0 and the clock
        # would not have moved at all.
        return MIN_HOURS, f"hours_elapsed {hours} below floor, used {MIN_HOURS}h"
    if hours > MAX_HOURS:
        return MAX_HOURS, f"hours_elapsed {hours} above ceiling, used {MAX_HOURS}h"
    return hours, None
