"""Finding things. The only route from suspicion to certainty.

`plots.propagate` caps what talk can do: however sincerely a thing is said, the
listener comes away *suspecting* it. That cap is only half a design — on its own
it makes the world unknowable. This module is the other half. Evidence is a
physical object at a location, and finding it is what promotes a doubt into a
fact you can act on.

The consequence worth naming: because certainty now has a physical route, leaks
can be GENEROUS. Under the old model every rumour was a step toward omniscience,
so clues had to be rationed to keep the mystery alive. Here a conspiracy that
advances loudly just leaves more trail, and the game gets more interesting rather
than more solved.

Nothing in here is projected. Evidence is found, not perceived — standing in the
room is not enough, which is why the object can carry `fact_id` without
threatening the no-ids-in-prompts invariant.
"""

from __future__ import annotations

import random

from .models import Evidence, Stance, WorldState
from .resolution import Degree, Outcome

# What a find is worth. A clean success hands you the thing; scraping the check
# means you saw enough to be sure something is wrong and not enough to say what.
PARTIAL_DISCOUNT = 0.7


def evidence_at(world: WorldState, location: str,
                exclude_found_by: str | None = None) -> list[Evidence]:
    """Everything findable at a location, optionally minus what this character
    has already turned up — searching the same desk twice should not keep paying."""
    return [e for e in world.evidence.values()
            if e.location == location
            and (exclude_found_by is None or exclude_found_by not in e.found_by)]


def drop(world: WorldState, fact_id: str, location: str, kind: str = "trace",
         strength: float = 0.8, description: str = "",
         holder_char_id: str | None = None) -> Evidence | None:
    """Leave a trace of `fact_id` at `location`.

    Ids are generated rather than authored because most evidence is created at
    runtime by the plot machinery, and an authored id would be one more
    descriptive string (`ev_king_poisoned_vial`) sitting where it could leak.
    """
    if fact_id not in world.facts:
        return None
    item = Evidence(
        id=f"ev_{len(world.evidence):04d}",
        fact_id=fact_id,
        kind=kind,
        location=location,
        holder_char_id=holder_char_id,
        strength=max(0.0, min(1.0, strength)),
        description=description or f"a {kind}",
    )
    world.evidence[item.id] = item
    return item


def discover(world: WorldState, char_id: str, outcome: Outcome,
             rng: random.Random) -> list[str]:
    """A character searches where they stand. Returns ground-truth event lines.

    The roll has already happened elsewhere — `outcome` is the referee's check
    for whatever the player actually described doing. This function only decides
    what that degree of success turns up, which keeps dice ownership where it
    belongs (resolution.py) and stops this module growing a second rules engine.
    """
    character = world.characters.get(char_id)
    if character is None or not character.alive or not outcome.succeeded:
        return []

    findable = evidence_at(world, character.location, exclude_found_by=char_id)
    if not findable:
        return []

    # One thing per search. A search that emptied the room of clues would make
    # the second search pointless and the first one everything.
    item = rng.choice(findable)
    partial = outcome.degree is Degree.PARTIAL

    stance = Stance.SUSPECTS if partial else Stance.KNOWS
    confidence = item.strength * (PARTIAL_DISCOUNT if partial else 1.0)
    changed = world.grant_belief(char_id, item.fact_id, stance,
                                 round(confidence, 2), source=None)
    item.found_by.append(char_id)

    # A thing you have seen for yourself is no longer a thing Varys told you.
    # Clearing the source matters: if Varys is later exposed as a liar, every
    # belief still sourced to him becomes suspect — and this one should survive
    # that, because it no longer rests on him.
    held = world.believes(char_id, item.fact_id)
    if held is not None and not partial:
        held.source_char_id = None

    fact = world.facts[item.fact_id]
    what = item.description or f"a {item.kind}"
    if partial:
        return [f"{character.name} finds {what} — enough to be uneasy, "
                f"not enough to be sure"]
    return [f"{character.name} finds {what}: {fact.content}"
            + ("" if changed else " — nothing they did not already know")]
