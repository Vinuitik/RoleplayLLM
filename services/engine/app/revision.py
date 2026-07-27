"""Belief revision — what happens when someone is told something they disagree with.

Without this module a character is a set, not a mind: hear "the king was
poisoned" and "the king died of age" and you simply hold both, forever, with no
tension between them. Nothing can ever be *changed*, only added — which means
misinformation cannot work, because a lie that merely appends is a lie nobody
had to be talked out of anything to believe.

So contradiction is first-class. Facts declare what they are incompatible with,
and receiving one contests the others.

Three properties worth preserving:

1. **Content is never authored by a model.** A liar picks one of their OWN
   beliefs and asserts its authored contradiction. "Invent a convincing lie" is
   inexpressible, exactly as "invent a fact" already was. This is the whole
   reason contradiction lives in the seed data rather than in a prompt.

2. **Who told you matters more than how loudly.** The contest weights each side
   by the listener's regard for its source. A trusted friend's quiet correction
   beats a hated rival's confident assertion, which is what makes trust worth
   spending and betrayal worth something.

3. **Losing a contest does not erase a belief, it shakes it.** Confidence decays
   toward doubt and only vanishes when it falls below `FORGET_BELOW`. People
   under pressure get uncertain long before they change their minds, and a
   character who flips cleanly on one conversation reads as a puppet.
"""

from __future__ import annotations

from .models import Stance, WorldState

# How much a lost contest costs the losing side's confidence.
SHAKEN = 0.5
# Below this a belief stops being held at all — the edge is dropped rather than
# lingering at 0.02 and cluttering every projection the character ever gets.
FORGET_BELOW = 0.12

# Regard runs -3..+3. A hated source is not *disbelieved* outright — people
# believe unpleasant things from people they dislike all the time — but their
# word carries roughly a third the weight of a trusted one.
MIN_CREDIBILITY = 0.35
MAX_CREDIBILITY = 1.0


def contradictions_of(world: WorldState, fact_id: str) -> list[str]:
    """Facts incompatible with this one, in both directions.

    Symmetrised at read time rather than at load: the seed declares the pair
    once ("the comfortable lie contradicts the truth") and an author cannot
    create a half-edge that silently works in only one direction.
    """
    fact = world.facts.get(fact_id)
    if fact is None:
        return []
    out = {f for f in fact.contradicts if f in world.facts}
    for other_id, other in world.facts.items():
        if other_id != fact_id and fact_id in other.contradicts:
            out.add(other_id)
    return sorted(out)


def credibility(world: WorldState, listener_id: str, source_id: str | None) -> float:
    """How much weight `listener` gives anything coming from `source`.

    A source of None is first-hand — something they saw themselves — and carries
    full weight. That is what makes evidence beat testimony structurally rather
    than by being handed a bigger number.
    """
    if source_id is None:
        return MAX_CREDIBILITY
    listener = world.characters.get(listener_id)
    if listener is None:
        return MIN_CREDIBILITY
    regard = listener.relationships.get(source_id, 0.0)
    # -3 -> MIN, 0 -> midpoint, +3 -> MAX
    span = MAX_CREDIBILITY - MIN_CREDIBILITY
    return MIN_CREDIBILITY + span * ((max(-3.0, min(3.0, regard)) + 3.0) / 6.0)


def receive(world: WorldState, listener_id: str, fact_id: str,
            confidence: float, stance: Stance = Stance.SUSPECTS,
            source_id: str | None = None) -> tuple[bool, list[str]]:
    """Take in a claim, contesting whatever it contradicts.

    Returns (changed, notes) — notes are ground-truth lines for the DM panel, so
    a mind changing is visible rather than mysterious.

    The incoming claim can LOSE. Telling a devoted man his king was murdered by
    the friend he trusts is not a state update; it is an argument, and it is one
    you can lose while making him slightly less sure of everything.
    """
    if fact_id not in world.facts or listener_id not in world.characters:
        return False, []

    notes: list[str] = []
    incoming_weight = confidence * credibility(world, listener_id, source_id)

    for rival_id in contradictions_of(world, fact_id):
        held = world.believes(listener_id, rival_id)
        if held is None:
            continue

        held_weight = held.confidence * credibility(world, listener_id,
                                                    held.source_char_id)
        name = world.characters[listener_id].name

        if incoming_weight > held_weight:
            # The new claim wins, but winning shakes the old belief rather than
            # deleting it: people get uncertain long before they change sides.
            held.confidence = round(held.confidence * SHAKEN, 3)
            held.stance = Stance.SUSPECTS
            notes.append(f"[revision] {name} is shaken on '{rival_id}' "
                         f"-> {held.confidence}")
            if held.confidence < FORGET_BELOW:
                world.beliefs.remove(held)
                notes.append(f"[revision] {name} no longer holds '{rival_id}'")
        else:
            # The listener holds their ground; the claim lands weaker for it.
            confidence = round(confidence * SHAKEN, 3)
            notes.append(f"[revision] {name} resists '{fact_id}' "
                         f"(holds '{rival_id}') -> {confidence}")

    if confidence < FORGET_BELOW:
        notes.append(f"[revision] claim '{fact_id}' did not take")
        return False, notes

    changed = world.grant_belief(listener_id, fact_id, stance,
                                 round(confidence, 3), source=source_id)
    return changed, notes
