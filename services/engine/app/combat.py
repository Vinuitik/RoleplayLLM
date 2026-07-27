"""Engagements — resolving a battle instead of a conversation.

The point of this module is not that the game needs battles. It is that it
proves the redesign worked: nothing here touches projection, beliefs, scenes or
prompts. It is a second *action vocabulary* with its own arithmetic, plugged in
through `actions.json` tags, and the epistemics carry over untouched — a
commander's report of the battle is a belief like any other, held at a
confidence, from a source who may be lying about it.

## The arithmetic

Standard force-ratio modelling, the same shape every wargame uses:

**Effective strength**, not headcount. Men are multiplied by quality, by morale,
and by posture. A fortified defender counts for far more than his numbers,
which is the entire reason anyone ever builds a wall.

**Lanchester's LINEAR law** for casualties, which is the pre-gunpowder case and
the one that gets taught for it.

The distinction matters and is easy to get backwards. Lanchester's *square* law
governs aimed fire, where every unit can engage any target, so concentration
compounds and a 2:1 advantage behaves like 4:1. That is modern combat. Medieval
melee is **frontage-limited**: only the men in the front rank are actually
fighting, and the ninety ranks behind them are waiting their turn. Attrition
rates therefore stay roughly independent of how deep the formation is, and
losses scale *linearly* with the strength ratio.

The practical consequence is the one every medieval commander cared about:
raw numbers matter much less than quality, ground and morale, and doubling your
army does not double your effect. A battle engine built on the square law makes
numbers overwhelming and turns terrain into decoration.

`ATTRITION_EXPONENT` selects the law, so a scenario with massed archers or
gunpowder can move to 2.0 without touching anything else.

**Then dice.** The ratio sets the difficulty; a d20 decides the day. A hopeless
assault can still break through and an overwhelming one can still founder,
because `resolution.py` owns verdicts here exactly as it does everywhere else.
The commander's stat is a modifier, never an override — the same division of
labour as every other check in the engine.
"""

from __future__ import annotations

import math
import random

from pydantic import BaseModel, Field

from .resolution import Degree, Outcome, resolve

# Terrain and preparation multipliers on effective strength. A dug-in defender
# is worth roughly triple his numbers, which is about where military planning
# rules of thumb put the attacker's required advantage.
POSTURE = {
    "rout": 0.4,
    "open": 1.0,        # meeting engagement, no advantage either way
    "prepared": 1.5,    # chosen ground, formed up
    "fortified": 2.5,   # walls, earthworks, a river at your back
    "siege": 3.5,       # a proper fortress
}

# Casualties as a share of the losing force at parity, before the ratio bites.
BASE_LOSS = 0.18

# Lanchester's LINEAR law (1.0) is the pre-gunpowder default: melee is
# frontage-limited, so numerical advantage does not compound. Set to 2.0 for the
# SQUARE law if a scenario is built on aimed fire, where it does.
ATTRITION_EXPONENT = 1.0
# The winner still bleeds. A battle where the victor loses nothing is a
# cutscene, not an engagement.
WINNER_LOSS_SHARE = 0.35


class Force(BaseModel):
    """One side of an engagement. Deliberately plain numbers — this is exactly
    the kind of state `numerics.Meter` already evolves over time, so a campaign
    is meters feeding forces feeding meters."""

    id: str
    name: str
    men: int = 0
    # 1-5, same scale as character stats: levies to household knights.
    quality: int = 2
    # 0.0-1.0. Multiplies strength directly; a broken force is worth nothing
    # regardless of how many bodies are still nominally in the line.
    morale: float = 1.0
    posture: str = "open"
    # A character id, if someone is leading. Their stat becomes the roll modifier.
    commander_id: str | None = None
    commander_stat: int = 2

    def effective(self) -> float:
        """Strength that actually counts, before the square law."""
        return (max(0, self.men)
                * (0.5 + 0.25 * max(1, min(5, self.quality)))
                * max(0.0, min(1.0, self.morale))
                * POSTURE.get(self.posture, 1.0))


class EngagementResult(BaseModel):
    attacker_id: str = ""
    defender_id: str = ""
    attacker_won: bool = False
    degree: Degree = Degree.PARTIAL
    attacker_losses: int = 0
    defender_losses: int = 0
    attacker_morale: float = 1.0
    defender_morale: float = 1.0
    ratio: float = 1.0
    detail: str = ""
    events: list[str] = Field(default_factory=list)


def _difficulty_from_ratio(ratio: float) -> int:
    """Turn a force ratio into a difficulty on the existing ladder.

    Parity is a hard check, not a coin flip: attacking an equal, prepared enemy
    should usually fail, which is why armies manoeuvre instead of charging.
    """
    if ratio <= 0:
        return 26
    # log2 so each doubling of advantage is a fixed step down the ladder.
    steps = math.log2(max(0.05, ratio))
    return int(max(5, min(26, round(18 - 4 * steps))))


def resolve_engagement(attacker: Force, defender: Force,
                       rng: random.Random) -> EngagementResult:
    """Fight it. Pure and seeded — same inputs, same battle, every replay."""
    att_eff = attacker.effective()
    def_eff = defender.effective()
    ratio = att_eff / def_eff if def_eff > 0 else 99.0

    difficulty = _difficulty_from_ratio(ratio)
    outcome: Outcome = resolve(attacker.commander_stat, difficulty, rng)
    attacker_won = outcome.succeeded

    # Lanchester's LINEAR law by default: melee is frontage-limited, so a
    # numerical edge does not compound the way aimed fire does. Quality, ground
    # and morale are already folded into `effective()`, and under the linear law
    # they are what actually decide the day — which is the medieval result.
    power = (att_eff / def_eff) ** ATTRITION_EXPONENT if def_eff > 0 else 99.0
    if attacker_won:
        loser_share = min(0.9, BASE_LOSS * power)
        winner_share = min(0.9, BASE_LOSS / max(power, 0.05) * WINNER_LOSS_SHARE)
        att_share, def_share = winner_share, loser_share
    else:
        loser_share = min(0.9, BASE_LOSS / max(power, 0.05))
        winner_share = min(0.9, BASE_LOSS * power * WINNER_LOSS_SHARE)
        att_share, def_share = loser_share, winner_share

    # A decisive result costs the loser far more: that is what a rout IS.
    if outcome.degree in (Degree.CRITICAL_SUCCESS, Degree.CRITICAL_FAILURE):
        if attacker_won:
            def_share = min(0.95, def_share * 1.8)
        else:
            att_share = min(0.95, att_share * 1.8)
    elif outcome.degree is Degree.PARTIAL:
        # Ground taken, nothing settled. Both sides bleed, neither breaks.
        att_share *= 0.7
        def_share *= 0.7

    att_losses = int(round(attacker.men * att_share))
    def_losses = int(round(defender.men * def_share))

    # Morale follows losses, and it is what actually ends battles — armies stop
    # fighting long before they stop existing.
    att_morale = round(max(0.0, attacker.morale - att_share * 1.2), 3)
    def_morale = round(max(0.0, defender.morale - def_share * 1.2), 3)
    if not attacker_won:
        att_morale = round(max(0.0, att_morale - 0.1), 3)
    else:
        def_morale = round(max(0.0, def_morale - 0.1), 3)

    verdict = "carries" if attacker_won else "is thrown back"
    events = [
        f"{attacker.name} {verdict} against {defender.name} "
        f"({outcome.degree.value.replace('_', ' ')})",
        f"{attacker.name} loses {att_losses} of {attacker.men}; "
        f"{defender.name} loses {def_losses} of {defender.men}",
    ]
    if def_morale <= 0.25 and attacker_won:
        events.append(f"{defender.name} breaks and runs")
    if att_morale <= 0.25 and not attacker_won:
        events.append(f"{attacker.name} breaks and runs")

    return EngagementResult(
        attacker_id=attacker.id, defender_id=defender.id,
        attacker_won=attacker_won, degree=outcome.degree,
        attacker_losses=att_losses, defender_losses=def_losses,
        attacker_morale=att_morale, defender_morale=def_morale,
        ratio=round(ratio, 3),
        detail=(f"eff {att_eff:.0f} vs {def_eff:.0f} (ratio {ratio:.2f}) "
                f"-> DC {difficulty}; {outcome.detail}"),
        events=events)


def apply_result(attacker: Force, defender: Force,
                 result: EngagementResult) -> None:
    """Write the outcome back onto the forces. Separate from resolution so a
    battle can be previewed, logged, or rewound without being committed."""
    attacker.men = max(0, attacker.men - result.attacker_losses)
    defender.men = max(0, defender.men - result.defender_losses)
    attacker.morale = result.attacker_morale
    defender.morale = result.defender_morale
    if defender.men == 0 or defender.morale <= 0.1:
        defender.posture = "rout"
    if attacker.men == 0 or attacker.morale <= 0.1:
        attacker.posture = "rout"
