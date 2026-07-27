"""The clock and the schemes that run on it. No LLM reaches this file either.

This is what makes the world a simulation instead of a chat: `tick()` advances
time, meters and plots WHETHER OR NOT THE PLAYER ACTS. Sit still for three turns
and the conspiracy is three stages closer to killing you.

Exposure is the counterweight. Every time a plot advances there is a chance a
character adjacent to it catches wind — gaining a SUSPECTED belief, not a known
one. That's the only reason the player ever gets a thread to pull: without it,
hidden state is perfectly hidden and therefore invisible, which is just an
unwinnable game with extra steps.
"""

from __future__ import annotations

import random

from . import evidence as evidence_mod
from .models import Stance, WorldState
from .numerics import (advance_meters, apply_pending_modifiers,
                       prune_expired_modifiers)

# The hard ceiling on what being told something can do to you. See propagate().
TALK_CEILING = 0.6
# A twisted account convinces less than a plain one, and leaves the listener
# with a doubt that corroboration or evidence can still overturn.
LIE_DISCOUNT = 0.65


def tick(world: WorldState, rng: random.Random, hours: float = 1.0) -> list[str]:
    """Advance the world by `hours`. Returns ground-truth events (DM panel /
    chronicle), NOT narration — these are unfiltered and must pass through
    perception before any of them reach the player.

    Order matters: modifiers settle before meters advance (so a fresh buff
    affects this step), meters before plots (so a plot gated on a meter reads the
    current value), and expiry last (so a modifier lasts its full final turn).
    """
    events: list[str] = []

    world.clock.turn += 1
    world.clock.hour += hours
    while world.clock.hour >= 24.0:
        world.clock.hour -= 24.0
        world.clock.day += 1

    events += apply_pending_modifiers(world)
    events += advance_meters(world, world.clock.absolute_hour)
    events += _advance_plots(world, rng)
    events += prune_expired_modifiers(world)

    world.chronicle.extend(events)
    return events


def _advance_plots(world: WorldState, rng: random.Random) -> list[str]:
    events: list[str] = []
    for plot in world.plots.values():
        if not plot.active or plot.complete:
            continue
        if rng.random() >= plot.advance_chance:
            continue

        plot.stage += 1
        stage_name = (plot.stages[plot.stage] if plot.stage < len(plot.stages)
                      else "consummated")
        events.append(f"[plot] {plot.name} advanced to '{stage_name}'")

        # The stage's fact becomes real for the conspirators themselves. They act
        # on it; nobody else knows it yet.
        fact_id = (plot.stage_facts[plot.stage]
                   if plot.stage < len(plot.stage_facts) else None)
        if fact_id and fact_id in world.facts:
            for member_id in plot.members:
                if member_id in world.characters:
                    world.grant_belief(member_id, fact_id, Stance.KNOWS, 1.0)

        if fact_id:
            events += _roll_exposure(world, plot, fact_id, rng)

        if plot.complete:
            plot.active = False
            events.append(f"[plot] {plot.name} has run its course")
    return events


TRACE_KINDS = ("a hastily folded letter", "a ledger line that does not balance",
               "a discarded vial", "a boot-print in the ash",
               "a seal broken and pressed again")


def _roll_exposure(world: WorldState, plot, fact_id: str,
                   rng: random.Random) -> list[str]:
    """A scheme that moves leaves a MARK, not a hunch.

    This used to grant a suspicion directly to a nearby character — suspicion
    from nowhere, with no object behind it and nothing for the player to pull on.
    The scheme was discoverable only by being adjacent at the right moment.

    Now exposure drops `Evidence` at a member's location. That is the whole
    difference between "someone feels uneasy" and "there is a letter in the ash
    grate": a conspiracy becomes a physical trail the player can walk. Anyone
    standing there may notice it in passing (a doubt, at low confidence — the
    servant who saw the wrong person leave the wrong room), but the thing itself
    stays where it fell, and finding it properly is what confers certainty.
    """
    if rng.random() >= plot.exposure_chance:
        return []

    member_locations = sorted({world.characters[m].location
                               for m in plot.members if m in world.characters})
    if not member_locations:
        return []

    location = rng.choice(member_locations)
    trace = evidence_mod.drop(
        world, fact_id, location,
        kind="trace",
        strength=round(rng.uniform(0.7, 0.95), 2),
        description=rng.choice(TRACE_KINDS))
    if trace is None:
        return []

    events = [f"[exposure] {plot.name} leaves {trace.description} "
              f"at {location} (strength {trace.strength})"]

    # Someone in the room may half-notice. A doubt, never the thing itself —
    # noticing is not finding, and the trace stays put for whoever looks properly.
    bystanders = [c.id for c in world.characters.values()
                  if c.alive and c.id not in plot.members
                  and c.location == location
                  and world.believes(c.id, fact_id) is None]
    if bystanders:
        witness_id = rng.choice(bystanders)
        confidence = round(rng.uniform(0.2, 0.5), 2)
        world.grant_belief(witness_id, fact_id, Stance.SUSPECTS, confidence)
        witness = world.characters[witness_id]
        events.append(f"[exposure] {witness.name} grows suspicious "
                      f"(confidence {confidence}) about {plot.name}")
    return events


def propagate(world: WorldState, speaker_id: str, listener_id: str,
              fact_id: str, truthful: bool = True) -> bool:
    """One character tells another something. Returns True if the listener's
    beliefs changed.

    **Talk has a ceiling.** Assertion alone can never produce `Stance.KNOWS` and
    never exceeds `TALK_CEILING`, no matter how sincere the speaker or how many
    times it is repeated. This is the structural reason the court does not
    collapse into omniscient opponents: without it, every conversation is a step
    toward everyone knowing everything, and the only lever left is making clues
    rare — which makes the game unwinnable rather than difficult.

    Certainty has exactly one route, and it is physical: `evidence.discover`.
    That inversion is what lets leaks be generous. A scheme that advances loudly
    should leave more trail, not solve itself.

    `truthful=False` is how a lie enters the world: the belief is recorded with
    `source_char_id` set to the speaker, so discovering the deception later lets
    every belief sourced to them be re-examined. Note the listener's belief looks
    identical either way from inside their own head — which is the point.
    """
    changed, _notes = propagate_verbose(world, speaker_id, listener_id, fact_id,
                                        truthful)
    return changed


def spoken_fact(world: WorldState, fact_id: str, truthful: bool) -> str | None:
    """Which fact actually leaves the speaker's mouth.

    Truthfully, the one they hold. Deceitfully, its authored CONTRADICTION —
    which is what makes a lie a lie rather than a mumbled truth. Ollivar, who
    knows the king was poisoned, says the king died of age; the listener acquires
    a specific false belief that later has to be argued back out of them.

    A fact with no authored contradiction cannot be inverted, so the liar falls
    back to stating it unconvincingly. That fallback is why `contradicts` can be
    added to a seed incrementally without anything breaking.
    """
    from .revision import contradictions_of

    if truthful:
        return fact_id
    rivals = contradictions_of(world, fact_id)
    return rivals[0] if rivals else fact_id


def propagate_verbose(world: WorldState, speaker_id: str, listener_id: str,
                      fact_id: str, truthful: bool = True
                      ) -> tuple[bool, list[str]]:
    """`propagate` plus the DM-panel notes explaining any mind that changed."""
    from .revision import receive

    if fact_id not in world.facts or listener_id not in world.characters:
        return False, []

    said = spoken_fact(world, fact_id, truthful)
    inverted = said != fact_id

    # An outright lie is asserted with full conviction — that is the point of
    # telling it. An unconvincing half-truth is the fallback when the fiction
    # gives the speaker nothing to invert into.
    confidence = TALK_CEILING if (truthful or inverted) else TALK_CEILING * LIE_DISCOUNT

    return receive(world, listener_id, said, round(confidence, 2),
                   stance=Stance.SUSPECTS, source_id=speaker_id)


def witnesses(world: WorldState, location: str) -> list[str]:
    """Everyone alive who would see something happen at `location`."""
    return [c.id for c in world.characters.values()
            if c.alive and c.location == location]
