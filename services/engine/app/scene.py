"""Scenes — several people in a room, talking in sequence.

Replaces the old dyad-only `converse()`, which could only ever run one NPC pair
per turn and gave each of them exactly one line into the void.

## The relay

A scene runs in PASSES. Within a pass each participant speaks once, in salience
order, and **each speaker sees everything said before them in the scene**:

    pass 1:  A speaks having heard nothing        -> A1
             B speaks having heard (A1)           -> B1
             C speaks having heard (A1, B1)       -> C1
    pass 2:  A speaks having heard (A1, B1, C1)   -> A2
             ...

That is the whole trick, and it is what makes this a conversation rather than
three monologues stapled together. It also means the cost is exactly
`participants x passes` cheap calls, which is why both are capped.

Three passes, because a conversation has a shape: someone raises a thing,
others add to or resist it, and it settles. `PASS_ROLES` names them and the
name goes into the prompt, so the last pass reads as a conclusion instead of
the model looping forever on the opening.

## Onstage vs offstage — where the entire cost model lives

`ONSTAGE` scenes are the ones the player is physically in. They pay for prose:
`participants x passes` intention calls plus one narration.

`OFFSTAGE` scenes cost **zero tokens**. Every mechanic that matters —
disclosure, propagation, belief revision — is pure engine code and needs no
model at all. The scene resolves into a `SceneRecord`: who was there, what
moved, what was rolled. Nobody writes prose for a conversation the player will
almost certainly never hear about.

If the player *later* learns of it — finds a letter, breaks a witness — the
record is rendered then, through their projection, as a report rather than a
transcript. Prose materialises exactly once, at the moment it matters, and only
for the small fraction of offstage scenes that ever surface.

This is what makes genuine group conversation affordable: you only ever pay for
rooms the player is standing in.
"""

from __future__ import annotations

import random

from pydantic import BaseModel, Field

from . import disclosure, llm
from .models import Event, Stance, WorldState
from .plots import propagate_verbose
from .projection import project

# A room where nine people each speak three times is nine times the cost and a
# third of the legibility. Five is about where a scene stops reading as a scene.
MAX_PARTICIPANTS = 5
MAX_PASSES = 3

# A conversation has a shape. Naming the passes and putting the name in the
# prompt is what stops the model re-opening the subject on the final pass.
PASS_ROLES = ("opening", "development", "conclusion")


class Mode:
    ONSTAGE = "onstage"     # the player is here; pays for prose
    OFFSTAGE = "offstage"   # resolves mechanically, zero tokens


class Exchange(BaseModel):
    """One person speaking once. The engine's record of it, not its prose."""

    pass_no: int = 0
    role: str = ""
    speaker_id: str = ""
    # Whether they chose to reach for a belief at all, and whether it came out.
    disclosed: bool = False
    chance: float = 0.0
    truthful: bool = True
    # ENGINE ONLY. Present so a record can be rendered later for someone who
    # earns it; excluded from every serialization, exactly like ProjectedBelief.
    fact_id: str | None = Field(default=None, exclude=True)
    said_fact_id: str | None = Field(default=None, exclude=True)
    line: str = ""
    heard_by: list[str] = Field(default_factory=list)


class SceneRecord(BaseModel):
    """What happened in a room, kept whether or not anyone wrote it down.

    An offstage scene produces one of these and nothing else. It is the thing a
    player can later discover, and the thing the DM panel audits.
    """

    id: str = ""
    at_hour: float = 0.0
    turn: int = 0
    location: str = ""
    mode: str = Mode.OFFSTAGE
    participants: list[str] = Field(default_factory=list)
    exchanges: list[Exchange] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ── who speaks, and in what order ───────────────────────────────────────────

def salience(world: WorldState, char_id: str, others: list[str]) -> float:
    """How much this character has reason to open their mouth right now.

    Cheap, deterministic, no model. It exists so the first voice in a scene is
    the one with something at stake rather than whoever happens to sort first in
    a dict — which is what the old pair-picker did, and it read as random
    because it was.
    """
    character = world.characters.get(char_id)
    if character is None or not character.alive:
        return -1.0

    score = character.candor
    # Something recently learned is worth saying.
    recent = [b for b in world.beliefs_of(char_id)
              if b.turn_acquired >= world.clock.turn - 2]
    score += 0.3 * min(len(recent), 3)
    # Strong feelings in either direction get people talking.
    if others:
        score += 0.2 * max(abs(character.relationships.get(o, 0.0)) for o in others)
    # Someone with something to hide is more likely to steer, not less.
    score += 0.15 * min(len(character.hides), 3)
    return score


def order_speakers(world: WorldState, participants: list[str]) -> list[str]:
    return sorted(participants,
                  key=lambda c: -salience(world, c, [p for p in participants if p != c]))


# ── the primitive: one person says one thing to a room ──────────────────────

def speak_in_room(world: WorldState, speaker_id: str, view, intention,
                  rng: random.Random, target_id: str | None = None
                  ) -> tuple[list[Event], Exchange | None, list[str]]:
    """Say one of your own beliefs, out loud, where people can hear it.

    Two things are load-bearing.

    **Speech reaches every witness, not just the addressee.** `target` shapes
    only the prose — who you are looking at while you say it.

    **The engine gates disclosure, the model only proposes it.** On a failed roll
    nothing propagates AND the model's own `speech` string is discarded: it is
    free text that may well contain the very thing being withheld, so the only
    safe withheld line is one we write ourselves.
    """
    index = intention.reveals_belief
    if not (0 <= index < len(view.beliefs)):
        return [], None, []

    belief = view.beliefs[index]
    speaker = world.characters[speaker_id]

    audience = [c.id for c in world.characters.values()
                if c.alive and c.location == speaker.location and c.id != speaker_id]
    if not audience:
        return [], None, []

    disclosed, chance = disclosure.will_disclose(
        world, speaker_id, audience, belief.fact_id, rng)

    exchange = Exchange(speaker_id=speaker_id, disclosed=disclosed, chance=chance,
                        truthful=intention.truthful, fact_id=belief.fact_id,
                        heard_by=audience)

    if not disclosed:
        exchange.line = f"{speaker.name} says nothing of it"
        return [Event(
            location=speaker.location,
            text=f"{speaker.name} begins to say something, then thinks better of it",
            actors=[speaker_id],
            detail=f"WITHHELD fact={belief.fact_id} p={chance:.2f} room={audience}",
        )], exchange, []

    from .plots import spoken_fact
    said_id = spoken_fact(world, belief.fact_id, intention.truthful)
    exchange.said_fact_id = said_id

    notes: list[str] = []
    changed: list[str] = []
    for listener_id in audience:
        did, listener_notes = propagate_verbose(
            world, speaker_id, listener_id, belief.fact_id,
            truthful=intention.truthful)
        notes += listener_notes
        if did:
            changed.append(listener_id)

    # What they actually said, as prose. Falls back to the CONTENT OF WHAT WAS
    # ASSERTED, not of what the speaker privately holds — otherwise a liar's
    # fallback line states the truth they were busy concealing.
    line = intention.speech or world.facts[said_id].content
    exchange.line = line

    addressee = (world.characters[target_id].name
                 if target_id and target_id in world.characters else None)
    text = (f'{speaker.name} tells {addressee}: "{line}"' if addressee
            else f'{speaker.name} says: "{line}"')
    if addressee and len(audience) > 1:
        overhearing = [world.characters[c].name for c in audience if c != target_id]
        if overhearing:
            text += f" — within earshot of {', '.join(overhearing)}"

    event = Event(
        location=speaker.location,
        text=text,
        actors=[speaker_id] + audience,
        detail=(f"conveyed fact={belief.fact_id} said={said_id} "
                f"truthful={intention.truthful} p={chance:.2f} "
                f"heard_by={audience} changed={changed}"))
    return [event], exchange, notes


# ── the relay ───────────────────────────────────────────────────────────────

def _transcript(world: WorldState, exchanges: list[Exchange]) -> str:
    """What has been said so far, as the next speaker heard it.

    Only spoken lines — a withheld belief leaves no trace in the transcript,
    because from inside the room nothing was said.
    """
    lines = [f"  {world.characters[e.speaker_id].name}: \"{e.line}\""
             for e in exchanges if e.disclosed and e.line]
    return "\n".join(lines)


def run_scene(world: WorldState, location: str, participants: list[str],
              rng: random.Random, mode: str = Mode.ONSTAGE,
              situation: str = "", passes: int = MAX_PASSES,
              player_id: str | None = None) -> tuple[list[Event], SceneRecord]:
    """Run a conversation between several people in one room.

    ONSTAGE calls a model once per speaker per pass, each seeing the transcript
    so far. OFFSTAGE calls no model at all — see `run_offstage`.
    """
    participants = [c for c in participants
                    if c in world.characters and world.characters[c].alive]
    participants = participants[:MAX_PARTICIPANTS]
    record = SceneRecord(
        id=f"sc_{world.clock.turn:04d}_{location}",
        at_hour=world.clock.absolute_hour, turn=world.clock.turn,
        location=location, mode=mode, participants=list(participants))

    if len(participants) < 2:
        return [], record

    if mode == Mode.OFFSTAGE:
        return [], run_offstage(world, location, participants, rng, record)

    events: list[Event] = []
    speakers = order_speakers(world, participants)

    for pass_no in range(min(passes, MAX_PASSES)):
        role = PASS_ROLES[min(pass_no, len(PASS_ROLES) - 1)]
        for speaker_id in speakers:
            # The player is a participant but is never voiced by a model —
            # they speak by taking their turn.
            if speaker_id == player_id:
                continue
            speaker = world.characters[speaker_id]
            if not speaker.alive or speaker.location != location:
                continue

            view = project(world, speaker_id)
            note = _scene_note(world, record, role, situation, speaker_id)
            intention = llm.get_intention(view, note)
            if intention.reveals_belief == llm.NO_FACT and not intention.speech:
                continue

            target_id = _resolve_target(world, intention.target, location)
            spoken, exchange, notes = speak_in_room(
                world, speaker_id, view, intention, rng, target_id)
            if exchange is not None:
                exchange.pass_no = pass_no
                exchange.role = role
                record.exchanges.append(exchange)
            record.notes += notes
            events += spoken

    return events, record


def _scene_note(world: WorldState, record: SceneRecord, role: str,
                situation: str, speaker_id: str) -> str:
    """The situation note handed to one speaker — everything said so far, plus
    where in the conversation we are."""
    others = [world.characters[p].name for p in record.participants
              if p != speaker_id]
    said = _transcript(world, record.exchanges)
    guidance = {
        "opening": "The conversation is just starting. Raise what matters to you.",
        "development": "Respond to what has been said. Add, press, or deflect.",
        "conclusion": "This is the last thing you will say here. Settle it, "
                      "commit to something, or close the subject.",
    }.get(role, "")
    return (
        f"{situation}\n\n"
        f"You are in conversation with {', '.join(others) or 'no one'}.\n"
        + (f"SAID SO FAR:\n{said}\n\n" if said else "Nothing has been said yet.\n\n")
        + guidance
    )


def run_offstage(world: WorldState, location: str, participants: list[str],
                 rng: random.Random,
                 record: SceneRecord | None = None) -> SceneRecord:
    """A conversation the player cannot perceive. **Zero LLM calls.**

    Every mechanic that matters is pure engine code, so there is nothing a model
    is needed for. Each participant reaches for their most-recently-acquired
    belief — the thing on their mind — and the disclosure roll decides whether it
    comes out. Propagation and revision then run exactly as they would onstage.

    The world moves. Nobody writes about it. If the player later earns the
    knowledge, `render_recalled` writes it then.
    """
    if record is None:
        record = SceneRecord(
            id=f"sc_{world.clock.turn:04d}_{location}",
            at_hour=world.clock.absolute_hour, turn=world.clock.turn,
            location=location, mode=Mode.OFFSTAGE, participants=list(participants))

    for speaker_id in order_speakers(world, participants):
        audience = [p for p in participants if p != speaker_id]
        if not audience:
            continue
        held = world.beliefs_of(speaker_id)
        if not held:
            continue

        # The thing most on their mind: newest first, then most certain.
        belief = sorted(held, key=lambda b: (-b.turn_acquired, -b.confidence))[0]
        # Lying offstage is driven by risk rather than by a model's whim: a
        # speaker with a reason to conceal this fact twists it instead.
        truthful = belief.fact_id not in world.characters[speaker_id].hides

        disclosed, chance = disclosure.will_disclose(
            world, speaker_id, audience, belief.fact_id, rng)
        exchange = Exchange(speaker_id=speaker_id, disclosed=disclosed,
                            chance=chance, truthful=truthful,
                            fact_id=belief.fact_id, heard_by=audience)

        if disclosed:
            from .plots import spoken_fact
            exchange.said_fact_id = spoken_fact(world, belief.fact_id, truthful)
            for listener_id in audience:
                _did, notes = propagate_verbose(world, speaker_id, listener_id,
                                                belief.fact_id, truthful=truthful)
                record.notes += notes
        record.exchanges.append(exchange)

    return record


def render_recalled(world: WorldState, record: SceneRecord, char_id: str) -> str:
    """Write an offstage scene up, for someone who has since earned it.

    A REPORT, not a transcript: the player learns that a conversation happened
    and roughly what moved in it, filtered through what they now believe. This
    is the only place an offstage scene ever costs a token, and it is paid once,
    at the moment it matters.
    """
    if not record.exchanges:
        return ""
    lines = []
    for exchange in record.exchanges:
        speaker = world.characters.get(exchange.speaker_id)
        if speaker is None:
            continue
        if not exchange.disclosed:
            lines.append(f"{speaker.name} kept their counsel")
            continue
        # Only render content the recaller now holds a belief about. Learning
        # THAT a meeting happened must not hand over what was said in it.
        said_id = exchange.said_fact_id or exchange.fact_id
        if said_id and world.believes(char_id, said_id) is not None:
            lines.append(f"{speaker.name} spoke of: {world.facts[said_id].content}")
        else:
            lines.append(f"{speaker.name} said something you could not make out")

    view = project(world, char_id)
    where = record.location
    return llm.get_scene_report(view, where, lines)


def _resolve_target(world: WorldState, name: str, location: str) -> str | None:
    """Match a model-returned NAME back to a character id, restricted to people
    actually present — an NPC cannot address someone across the map."""
    if not name:
        return None
    wanted = name.strip().lower()
    for char_id, character in world.characters.items():
        if not character.alive or character.location != location:
            continue
        if wanted in (character.name.lower(), char_id.lower()):
            return char_id
    for char_id, character in world.characters.items():
        if (character.alive and character.location == location
                and wanted in character.name.lower()):
            return char_id
    return None
