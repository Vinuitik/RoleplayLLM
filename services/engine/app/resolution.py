"""Resolution — where outcomes are actually decided. No LLM reaches this file.

This is what makes consequences real. A model asked to adjudicate its own story
will soften it: the beloved character survives, the desperate gambit lands. Dice
do not care. The LLM proposes an intention and may set the difficulty of a novel
action; the verdict is arithmetic, and the arithmetic is here.

Every function is pure and takes its `rng` explicitly. A global RNG would make
tests order-dependent and rewind impossible — replaying from a snapshot has to
produce the same rolls, or the whole save/rewind feature is a lie.
"""

from __future__ import annotations

import random
from enum import Enum

from pydantic import BaseModel

from .models import Stats


class Degree(str, Enum):
    """Four outcomes, not two. A flat pass/fail makes a political sim feel like a
    slot machine; partial success ("you get the meeting, but Varys now knows you
    wanted it") is where the interesting play lives."""

    CRITICAL_SUCCESS = "critical_success"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILURE = "failure"
    CRITICAL_FAILURE = "critical_failure"


class Outcome(BaseModel):
    degree: Degree
    total: int
    difficulty: int
    margin: int
    roll: int
    stat_value: int
    modifier: int = 0
    # Rendered for the DM panel: the full arithmetic, so you can always see why
    # something happened rather than trusting it.
    detail: str = ""

    @property
    def succeeded(self) -> bool:
        return self.degree in (Degree.CRITICAL_SUCCESS, Degree.SUCCESS, Degree.PARTIAL)


def make_rng(seed: int | str | None = None) -> random.Random:
    """A private RNG instance. Never `random.seed()` — that mutates global state
    and makes two concurrent games interfere with each other."""
    return random.Random(seed)


def roll_d20(rng: random.Random) -> int:
    return rng.randint(1, 20)


def resolve(stat_value: int, difficulty: int, rng: random.Random,
            modifier: int = 0) -> Outcome:
    """Opposed roll: d20 + stat + modifier vs difficulty.

    Natural 1 and natural 20 are decided before the sum, so an overwhelming
    advantage can still blow up and a hopeless attempt can still land — which is
    what keeps a scene tense when the numbers say it shouldn't be.
    """
    roll = roll_d20(rng)
    total = roll + stat_value + modifier
    margin = total - difficulty

    if roll == 20:
        degree = Degree.CRITICAL_SUCCESS
    elif roll == 1:
        degree = Degree.CRITICAL_FAILURE
    elif margin >= 5:
        degree = Degree.SUCCESS
    elif margin >= 0:
        degree = Degree.PARTIAL
    else:
        degree = Degree.FAILURE

    detail = (f"d20({roll}) + stat({stat_value})"
              f"{f' + mod({modifier})' if modifier else ''} "
              f"= {total} vs DC {difficulty} -> {degree.value}")

    return Outcome(degree=degree, total=total, difficulty=difficulty,
                   margin=margin, roll=roll, stat_value=stat_value,
                   modifier=modifier, detail=detail)


def resolve_opposed(actor_stat: int, target_stat: int, rng: random.Random,
                    modifier: int = 0) -> Outcome:
    """Character vs character. The defender's stat sets the difficulty, on a base
    of 10 so an evenly-matched pair sits near a coin flip."""
    return resolve(actor_stat, 10 + target_stat, rng, modifier)


def stat_of(stats: Stats, name: str, default: int = 2) -> int:
    """Look up a stat by name, tolerating whatever the LLM calls it.

    The referee call returns a stat name as free text, so this normalizes and
    falls back rather than raising — a hallucinated stat name should cost the
    action a small accuracy penalty, never crash the turn.
    """
    return getattr(stats, (name or "").strip().lower(), default)


# Difficulty ladder shared by the rules table and the LLM referee prompt, so
# "hard" means the same number in both paths.
DIFFICULTY = {
    "trivial": 5,
    "easy": 10,
    "moderate": 14,
    "hard": 18,
    "severe": 22,
    "near_impossible": 26,
}
DEFAULT_DIFFICULTY = DIFFICULTY["moderate"]


def difficulty_from_label(label: str | int | None) -> int:
    """Accept either a rung name or a raw number from the referee.

    Raw numbers are clamped to the ladder's range: an LLM that returns DC 900
    should get a very hard check, not an unwinnable one.
    """
    if isinstance(label, (int, float)):
        return int(max(DIFFICULTY["trivial"], min(DIFFICULTY["near_impossible"], label)))
    return DIFFICULTY.get((label or "").strip().lower(), DEFAULT_DIFFICULTY)
