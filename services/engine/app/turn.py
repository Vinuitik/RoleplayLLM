"""The turn loop. Where every other module meets.

Order is the design, not a detail:

    tick  ->  player action  ->  resolve  ->  NPC turns  ->  conversations
          ->  FILTER BY PERCEPTION  ->  narrate  ->  present

The filter step is the one people leave out, and it quietly undoes everything
else. NPC turns produce resolved events across the whole map — including a
poisoning three rooms away. Hand that list to the narrator and it will tell the
player about it, having faithfully "narrated only what it was given". Projection
protects the *inputs* to a model; `visible_events` protects the *outputs* of the
engine. Both are needed.

Narration is last and receives only already-decided, already-filtered facts. It
is a mouth, not a brain.
"""

from __future__ import annotations

import random

from pydantic import BaseModel

from . import disclosure, evidence as evidence_mod, llm
from .gametime import ARCHETYPE_HOURS, coerce_hours, describe_time
from .models import ModifierKind, Stance, WorldState
from .numerics import add_modifier
from .plots import propagate, tick, witnesses
from .projection import perceives, project
from .resolution import (Outcome, difficulty_from_label, make_rng, resolve_opposed,
                         stat_of)


class Event(BaseModel):
    """A thing that happened, tagged with where — so perception can filter it."""

    location: str
    text: str
    # Characters who directly took part; they always perceive it even if the
    # location bookkeeping is imperfect.
    actors: list[str] = []
    # Engine-side detail (dice arithmetic) — DM panel only, never narrated.
    detail: str = ""


class TurnResult(BaseModel):
    narration: str = ""
    suggested_actions: list[str] = []
    events: list[Event] = []
    dm_log: list[str] = []
    day: int = 0
    hour: float = 0.0
    turn: int = 0
    time_of_day: str = ""


def _rng_for(world: WorldState, seed: int | str | None) -> random.Random:
    """Per-turn RNG derived from the seed AND the turn number.

    Deriving it rather than carrying one long-lived generator is what makes
    rewind work: replaying turn 7 from a snapshot reproduces turn 7's rolls
    exactly, because the generator depends only on (seed, turn).
    """
    return make_rng(f"{seed}:{world.clock.turn}")


# ── NPC turns ───────────────────────────────────────────────────────────────

def npc_turn(world: WorldState, char_id: str, rng: random.Random,
             situation_note: str = "") -> list[Event]:
    """One NPC acts. Projected view in, structured intention out, dice decide."""
    character = world.characters[char_id]
    if not character.alive:
        return []

    view = project(world, char_id)
    intention = llm.get_intention(view, situation_note)
    events: list[Event] = []

    target_id = _resolve_target(world, intention.target, character.location)

    # Speech is where beliefs move between people — including lies. Note there
    # is no `and target_id` here any more: speaking is an act performed in a
    # room, not a message addressed to one person. A speaker with no valid target
    # still says it, and everyone standing there still hears it.
    if intention.reveals_belief != llm.NO_FACT:
        events += _speak(world, char_id, view, intention, rng, target_id)

    if intention.action in ("scheme", "confront", "search") and target_id:
        outcome = _contest(world, char_id, target_id, intention.action, rng)
        events.append(Event(
            location=character.location,
            text=f"{character.name} moves against "
                 f"{world.characters[target_id].name} ({intention.action}) — "
                 f"{'succeeds' if outcome.succeeded else 'fails'}",
            actors=[char_id, target_id],
            detail=outcome.detail))
    elif intention.speech and not events:
        events.append(Event(
            location=character.location,
            text=f'{character.name} says: "{intention.speech}"',
            actors=[char_id]))

    return events


def _speak(world: WorldState, speaker_id: str, view, intention,
           rng: random.Random, target_id: str | None = None) -> list[Event]:
    """Say one of your own beliefs, out loud, in a room.

    Two things are load-bearing here.

    **Speech reaches every witness, not just the addressee.** This was a genuine
    bug: `propagate` ran for `target_id` alone, so a man standing three feet away
    heard nothing. `target` now shapes only the PROSE — who you are looking at
    while you say it. Fixing it is also what makes group conversation mostly fall
    out for free: a scene with several people in it is just a room where speech
    lands on all of them, with no separate machinery for it.

    **The engine gates disclosure, the model only proposes it.** The index
    protocol already made inventing a fact inexpressible; it did nothing about a
    conspirator cheerfully volunteering the conspiracy. `disclosure` rolls
    candor + trust-in-the-room - risk. On a failure nothing propagates AND the
    model's own `speech` line is discarded — it is free text that may well
    contain the very thing being withheld, so the only safe withheld line is one
    we write ourselves.
    """
    index = intention.reveals_belief
    if not (0 <= index < len(view.beliefs)):
        return []

    belief = view.beliefs[index]
    speaker = world.characters[speaker_id]

    # Everyone alive in the room hears it. The addressee is one of them.
    audience = [cid for cid in witnesses(world, speaker.location)
                if cid != speaker_id]
    if not audience:
        return []

    disclosed, chance = disclosure.will_disclose(
        world, speaker_id, audience, belief.fact_id, rng)

    if not disclosed:
        return [Event(
            location=speaker.location,
            text=f"{speaker.name} begins to say something, then thinks better of it",
            actors=[speaker_id],
            detail=(f"WITHHELD fact={belief.fact_id} p={chance:.2f} "
                    f"room={audience}"))]

    changed = [cid for cid in audience
               if propagate(world, speaker_id, cid, belief.fact_id,
                            truthful=intention.truthful)]

    line = intention.speech or belief.content
    addressee = (world.characters[target_id].name if target_id in world.characters
                 else None) if target_id else None
    text = (f'{speaker.name} tells {addressee}: "{line}"' if addressee
            else f'{speaker.name} says: "{line}"')
    if addressee and len(audience) > 1:
        overhearing = [world.characters[c].name for c in audience if c != target_id]
        text += f" — within earshot of {', '.join(overhearing)}"

    return [Event(
        location=speaker.location,
        text=text,
        actors=[speaker_id] + audience,
        detail=(f"conveyed fact={belief.fact_id} truthful={intention.truthful} "
                f"p={chance:.2f} heard_by={audience} changed={changed}"))]


def _contest(world: WorldState, actor_id: str, target_id: str,
             action: str, rng: random.Random) -> Outcome:
    stat_by_action = {"scheme": "guile", "confront": "presence", "search": "wits"}
    actor = world.characters[actor_id]
    target = world.characters[target_id]
    stat_name = stat_by_action.get(action, "wits")
    return resolve_opposed(stat_of(actor.stats, stat_name),
                           stat_of(target.stats, "resolve"), rng)


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
    # Partial match ("Varys" for "Mycella Ferrow" won't hit; "Mycella" will).
    for char_id, character in world.characters.items():
        if (character.alive and character.location == location
                and wanted in character.name.lower()):
            return char_id
    return None


# ── NPC <-> NPC conversation ────────────────────────────────────────────────

def converse(world: WorldState, a_id: str, b_id: str,
             rng: random.Random) -> list[Event]:
    """Two NPCs talk without the player. Each speaks from their OWN projection,
    so neither can mention what they don't know, and either may lie.

    This is what makes the world feel alive between scenes: information moves
    through the court on its own, and the player can walk into a room where
    everyone already knows something they don't.
    """
    events: list[Event] = []
    for speaker_id, listener_id in ((a_id, b_id), (b_id, a_id)):
        speaker = world.characters[speaker_id]
        if not speaker.alive:
            continue
        view = project(world, speaker_id)
        note = (f"You are speaking privately with "
                f"{world.characters[listener_id].name}.")
        intention = llm.get_intention(view, note)
        if intention.reveals_belief != llm.NO_FACT:
            events += _speak(world, speaker_id, view, intention, rng, listener_id)
    return events


def _conversation_pairs(world: WorldState, rng: random.Random,
                        exclude: str) -> list[tuple[str, str]]:
    """Pick NPC pairs who share a room and have reason to talk.

    Deliberately at most one pair per turn: each conversation costs two LLM
    calls, and the court gossiping constantly is both expensive and noisy.
    """
    by_location: dict[str, list[str]] = {}
    for character in world.characters.values():
        if character.alive and character.id != exclude:
            by_location.setdefault(character.location, []).append(character.id)

    candidates = [group for group in by_location.values() if len(group) >= 2]
    if not candidates or rng.random() > 0.5:
        return []
    group = rng.choice(candidates)
    a, b = rng.sample(group, 2)
    return [(a, b)]


# ── perception filter ───────────────────────────────────────────────────────

def visible_events(world: WorldState, events: list[Event], char_id: str) -> list[str]:
    """THE filter between resolution and narration.

    An event reaches the narrator only if the player took part in it or was in
    the room. Without this the narrator receives every resolved outcome in the
    world and reports them all — projection would protect the NPCs' prompts while
    the narration leaked everything anyway.
    """
    return [e.text for e in events
            if char_id in e.actors or perceives(world, char_id, e.location)]


# ── the loop ────────────────────────────────────────────────────────────────

def play_turn(world: WorldState, player_action: str,
              seed: int | str | None = None,
              enable_conversations: bool = True) -> TurnResult:
    """One full turn. `world` is mutated in place; snapshot before calling if you
    want to rewind."""
    player_id = world.player_id
    player = world.characters[player_id]
    dm_log: list[str] = []

    # 1. Referee the player's action FIRST — it tells us how long the turn takes,
    #    and the clock must advance by that amount, not by a fixed step.
    view = project(world, player_id)
    referee = llm.get_referee(view, player_action) if player_action.strip() else None

    if referee is not None:
        hours, note = coerce_hours(referee.hours_elapsed, referee.archetype)
        if note:
            dm_log.append(f"[time] {note}")
        dm_log.append(f"[referee] {referee.difficulty} vs {referee.opposing_stat} "
                      f"({referee.archetype}, {hours}h) — {referee.reasoning}")
    else:
        hours = ARCHETYPE_HOURS["wait"]
        dm_log.append(f"[time] no action given; {hours}h pass")

    # 2. The world moves, whether or not the player did anything.
    rng = _rng_for(world, seed)
    dm_log += [f"[tick] {e}" for e in tick(world, rng, hours=hours)]

    events: list[Event] = []

    # 3. Resolve the player's attempt.
    if referee is not None:
        events += _resolve_player_action(world, player_action, referee, rng, dm_log)

    # 4. NPCs in the player's location react; NPCs elsewhere live their own lives.
    situation_note = player_action.strip()[:200]
    for char_id in witnesses(world, player.location):
        if char_id == player_id:
            continue
        events += npc_turn(world, char_id, rng, situation_note)

    # 5. Two NPCs may talk privately. The player may never see this — that is the
    #    point; information moves through the court on its own.
    if enable_conversations:
        for a_id, b_id in _conversation_pairs(world, rng, exclude=player_id):
            conversation = converse(world, a_id, b_id, rng)
            events += conversation
            dm_log += [f"[private] {e.text}" for e in conversation]

    # 6. Filter to what the player could possibly perceive, THEN narrate.
    perceived = visible_events(world, events, player_id)
    narration_view = project(world, player_id)
    raw = llm.get_narration(narration_view, perceived, player_action)
    narration, suggestions = _split_suggestions(raw)

    dm_log += [f"[event] {e.text}" + (f"  |  {e.detail}" if e.detail else "")
               for e in events]
    world.chronicle.extend(e.text for e in events)

    return TurnResult(
        narration=narration,
        suggested_actions=suggestions,
        events=events,
        dm_log=dm_log,
        day=world.clock.day,
        hour=world.clock.hour,
        turn=world.clock.turn,
        time_of_day=describe_time(world.clock.day, world.clock.hour),
    )


def _resolve_player_action(world: WorldState, action_text: str, referee,
                           rng: random.Random, dm_log: list[str]) -> list[Event]:
    """Known-shape actions could resolve by table; novel ones get the referee's
    difficulty. Either way the DICE decide — the referee only set the problem."""
    player = world.characters[world.player_id]
    difficulty = difficulty_from_label(referee.difficulty)
    stat_name = _actor_stat_for(referee.opposing_stat)
    from .resolution import resolve
    outcome = resolve(stat_of(player.stats, stat_name), difficulty, rng)
    dm_log.append(f"[player] {outcome.detail}")

    events = [Event(
        location=player.location,
        text=f"{player.name} attempts: {action_text} — "
             f"{outcome.degree.value.replace('_', ' ')}",
        actors=[world.player_id],
        detail=outcome.detail)]

    # Searching is how certainty is earned. Evidence is never perceived by
    # standing in the room — the trail a scheme leaves sits there until somebody
    # actually looks, and looking is a check like any other.
    if (referee.archetype or "").strip().lower() == "search":
        for line in evidence_mod.discover(world, world.player_id, outcome, rng):
            dm_log.append(f"[found] {line}")
            events.append(Event(location=player.location, text=line,
                                actors=[world.player_id]))

    return events


def _actor_stat_for(opposing_stat: str) -> str:
    """The referee names what RESISTS; the player rolls the natural counterpart."""
    return {"guile": "wits", "wits": "guile", "presence": "presence",
            "might": "might", "resolve": "presence"}.get(
        (opposing_stat or "").strip().lower(), "wits")


def _split_suggestions(raw: str) -> tuple[str, list[str]]:
    """Pull the '> ' suggestion lines out of the narrator's reply.

    Tolerant by design: if the narrator ignores the format we return its prose
    with no suggestions rather than showing the player a mangled scene.
    """
    lines = (raw or "").splitlines()
    prose, suggestions = [], []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(">"):
            suggestion = stripped.lstrip("> ").strip()
            if suggestion:
                suggestions.append(suggestion)
        else:
            prose.append(line)
    return "\n".join(prose).strip(), suggestions[:3]
