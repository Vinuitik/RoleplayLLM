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

from . import (disclosure, evidence as evidence_mod, llm, lore,
               scene as scene_mod)
from .actions import default_table
from .gametime import ARCHETYPE_HOURS, coerce_hours, describe_time
from .models import Event, ModifierKind, Stance, WorldState
from .numerics import add_modifier
from .plots import propagate, tick, witnesses
from .projection import perceives, project
from .resolution import (Outcome, difficulty_from_label, make_rng, resolve_opposed,
                         stat_of)
from .scene import Mode, SceneRecord

# How many offstage scenes may run in one turn. They cost zero tokens, so this
# is a legibility cap on the DM panel rather than a budget.
MAX_OFFSTAGE_SCENES = 3


def world_actions(world: WorldState):
    """The action vocabulary this world speaks.

    A scenario may ship its own table; one that does not gets the default court
    vocabulary, so nothing that worked before needs a new file to keep working.
    """
    if world.actions:
        from .actions import from_rows
        return from_rows([a.model_dump() for a in world.actions])
    return default_table()


class TurnResult(BaseModel):
    narration: str = ""
    suggested_actions: list[str] = []
    events: list[Event] = []
    dm_log: list[str] = []
    day: int = 0
    hour: float = 0.0
    turn: int = 0
    time_of_day: str = ""
    # Scenes that ran this turn. Offstage ones carry no prose — they are the
    # record a player can later discover. DM panel and store, never the browser.
    scenes: list[SceneRecord] = []


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
    """One character says one thing, out loud, in a room.

    Delegates to `scene.speak_in_room` — the same primitive the group relay
    uses, so a lone NPC blurting something and a five-person council scene go
    through identical disclosure, propagation and revision. There is no separate
    "conversation" code path any more, which is what stopped the two drifting.
    """
    events, _exchange, _notes = scene_mod.speak_in_room(
        world, speaker_id, view, intention, rng, target_id)
    return events


def _contest(world: WorldState, actor_id: str, target_id: str,
             action: str, rng: random.Random) -> Outcome:
    """One character moves against another. Which stats are involved is read
    from the scenario's action table, not hardcoded here — that dict was one of
    the three things making this a court engine rather than a generic one."""
    table = world_actions(world)
    row = table.get(action)
    actor = world.characters[actor_id]
    target = world.characters[target_id]
    return resolve_opposed(stat_of(actor.stats, row.actor_stat),
                           stat_of(target.stats, row.opposing_stat), rng)


def _resolve_target(world: WorldState, name: str, location: str) -> str | None:
    """Match a model-returned NAME back to a character id, restricted to people
    actually present — an NPC cannot address someone across the map."""
    return scene_mod._resolve_target(world, name, location)


# ── offstage scenes ─────────────────────────────────────────────────────────

def offstage_groups(world: WorldState, rng: random.Random,
                    exclude_location: str) -> list[list[str]]:
    """Which rooms have a conversation worth resolving this turn.

    Replaces the old `_conversation_pairs`, which flipped a coin and then picked
    two co-located NPCs at random. That read as random because it was: gossip
    fired for no reason, between no one in particular, and always in pairs.

    Now a room is a candidate because someone in it has PRESSURE — something
    recently learned, someone they feel strongly about, something to hide. The
    salience score already used to order speakers is reused to rank rooms, and
    the top few run. Groups, not pairs: a room of four resolves as a room of
    four, because offstage scenes cost nothing.
    """
    by_location: dict[str, list[str]] = {}
    for character in world.characters.values():
        if (character.alive and character.location != exclude_location):
            by_location.setdefault(character.location, []).append(character.id)

    scored = []
    for location, group in by_location.items():
        if len(group) < 2:
            continue
        pressure = max(scene_mod.salience(world, c, [g for g in group if g != c])
                       for c in group)
        scored.append((pressure, location, group[:scene_mod.MAX_PARTICIPANTS]))

    scored.sort(key=lambda s: -s[0])
    return [group for _p, _loc, group in scored[:MAX_OFFSTAGE_SCENES]]


def converse(world: WorldState, a_id: str, b_id: str,
             rng: random.Random) -> list[Event]:
    """Two named NPCs talk. Kept as a thin wrapper over the relay so existing
    callers and tests keep working; new code should use `scene.run_scene`."""
    location = world.characters[a_id].location
    events, _record = scene_mod.run_scene(
        world, location, [a_id, b_id], rng, mode=Mode.ONSTAGE, passes=1)
    return events



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
              enable_conversations: bool = True,
              scene_passes: int = scene_mod.MAX_PASSES,
              establish_lore: bool = True) -> TurnResult:
    """One full turn. `world` is mutated in place; snapshot before calling if you
    want to rewind.

    `scene_passes` is the cost dial for the one expensive thing in the turn: the
    onstage group conversation costs `people_present x passes` cheap calls. Set
    it to 1 for a fast turn, 0 to fall back to one-line-each NPC reactions.

    `establish_lore` controls just-in-time truth: when the player asks about
    something worldgen never covered, the engine establishes it as a real fact
    before anyone opens their mouth. See lore.py.
    """
    player_id = world.player_id
    player = world.characters[player_id]
    dm_log: list[str] = []

    # 1. Referee the player's action FIRST — it tells us how long the turn takes,
    #    and the clock must advance by that amount, not by a fixed step.
    view = project(world, player_id)
    referee = llm.get_referee(view, player_action) if player_action.strip() else None

    if referee is not None:
        hours, note = coerce_hours(referee.hours_elapsed, referee.archetype,
                                   table=world_actions(world))
        if note:
            dm_log.append(f"[time] {note}")
        dm_log.append(f"[referee] {referee.difficulty} vs {referee.opposing_stat} "
                      f"({referee.archetype}, {hours}h) — {referee.reasoning}")
    else:
        hours = world_actions(world).hours_for("wait")
        dm_log.append(f"[time] no action given; {hours}h pass")

    # 2. The world moves, whether or not the player did anything.
    rng = _rng_for(world, seed)
    dm_log += [f"[tick] {e}" for e in tick(world, rng, hours=hours)]

    events: list[Event] = []

    # 3. Resolve the player's attempt.
    if referee is not None:
        events += _resolve_player_action(world, player_action, referee, rng, dm_log)

    # 3b. GAP FILLING. If the player asked about something the record does not
    #     cover, establish the truth NOW — a real fact, with a real is_true, held
    #     by whoever would plausibly know it, plus the plausible lie about it.
    #
    #     This runs BEFORE anyone speaks, which is the whole point: by the time
    #     an NPC decides what to say, the answer exists in the world and they
    #     either hold it or they don't. The alternative — letting a model invent
    #     a number in its prose — puts the "truth" in a transcript where nobody
    #     else in the world knows it and the next character asked contradicts it.
    if establish_lore and _looks_like_a_question(player_action):
        if not lore.has_coverage(world, player_action):
            _true_id, _false_id, lore_log = lore.establish(world, player_action, rng)
            dm_log += lore_log

    # 4. ONSTAGE. The player's room is a scene, not a queue of monologues: each
    #    NPC present speaks having heard everyone before them. This is the only
    #    place in the turn that pays for a group conversation, and it is the only
    #    place the player can actually hear one.
    situation_note = player_action.strip()[:200]
    scenes: list[SceneRecord] = []
    present = [c for c in witnesses(world, player.location) if c != player_id]

    if len(present) >= 2 and scene_passes > 0:
        spoken, record = scene_mod.run_scene(
            world, player.location, present + [player_id], rng,
            mode=Mode.ONSTAGE, situation=situation_note,
            passes=scene_passes, player_id=player_id)
        events += spoken
        scenes.append(record)
        dm_log.append(f"[scene] onstage at {player.location}: "
                      f"{len(record.participants)} present, "
                      f"{len(record.exchanges)} exchanges")
        dm_log += [f"[revision] {n}" for n in record.notes]
    else:
        # One NPC (or none) — a scene of one is just a person acting.
        for char_id in present:
            events += npc_turn(world, char_id, rng, situation_note)

    # 5. OFFSTAGE. Rooms with pressure resolve mechanically — disclosure,
    #    propagation and revision all run, and NOT ONE TOKEN IS SPENT. The player
    #    may never learn any of it happened; if they later do, the record is
    #    written up then (scene.render_recalled). This is the entire cost model:
    #    you pay for rooms you are standing in, and nothing else.
    if enable_conversations:
        for group in offstage_groups(world, rng, exclude_location=player.location):
            record = scene_mod.run_offstage(
                world, world.characters[group[0]].location, group, rng)
            scenes.append(record)
            moved = sum(1 for e in record.exchanges if e.disclosed)
            dm_log.append(f"[offstage] {record.location}: "
                          f"{len(group)} present, {moved} spoke (0 tokens)")
            dm_log += [f"[revision] {n}" for n in record.notes]

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
        scenes=scenes,
    )


def _resolve_player_action(world: WorldState, action_text: str, referee,
                           rng: random.Random, dm_log: list[str]) -> list[Event]:
    """Known-shape actions could resolve by table; novel ones get the referee's
    difficulty. Either way the DICE decide — the referee only set the problem."""
    player = world.characters[world.player_id]
    table = world_actions(world)
    difficulty = difficulty_from_label(referee.difficulty)
    stat_name = _actor_stat_for(referee.archetype, referee.opposing_stat, table)
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
    if table.has_tag(referee.archetype, "discovery"):
        for line in evidence_mod.discover(world, world.player_id, outcome, rng):
            dm_log.append(f"[found] {line}")
            events.append(Event(location=player.location, text=line,
                                actors=[world.player_id]))

    return events


# Question words that suggest the player is asking about the world rather than
# acting on it. Deliberately generous: a false positive costs one cheap coverage
# check (usually a local string match, no model call at all), while a false
# negative means a question goes unanswered and the world looks thin.
_QUESTION_MARKERS = ("how many", "how much", "how strong", "how far", "how long",
                     "what is", "what are", "who is", "who are", "where is",
                     "where are", "when did", "when will", "why did", "ask ",
                     "asks ", "enquire", "inquire", "question ")


def _looks_like_a_question(action_text: str) -> bool:
    text = (action_text or "").strip().lower()
    if not text:
        return False
    return "?" in text or any(marker in text for marker in _QUESTION_MARKERS)


def _actor_stat_for(archetype: str, opposing_stat: str,
                    table=None) -> str:
    """What the PLAYER rolls. Read from the action table by archetype.

    Previously a hardcoded inversion of the opposing stat, which quietly assumed
    a court vocabulary: it mapped "guile resists" to "roll wits" because that is
    what intrigue looks like. A battle wants "fortification resists" to mean
    "roll might", and that is a data question, not a code one. The old inversion
    survives only as the fallback for an archetype the table does not know.
    """
    table = table or default_table()
    row = table.get(archetype)
    if row.id in table.actions:
        return row.actor_stat
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
