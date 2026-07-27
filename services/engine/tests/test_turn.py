"""Tests for time advancement and the perception filter.

The LLM is stubbed out throughout — these check the engine's contract with the
model, not the model itself: that a hostile or broken reply cannot freeze the
clock, leak a fact, or halt a turn.
"""

import json
from pathlib import Path

import pytest

from app import llm, turn as turn_mod
from app.gametime import (ARCHETYPE_HOURS, MAX_HOURS, MIN_HOURS, coerce_hours,
                          describe_time, phase_of_day)
from app.models import WorldState
from app.projection import project
from app.resolution import make_rng
from app.turn import Event, visible_events

SEED = Path(__file__).resolve().parents[1] / "app" / "world" / "seed.json"


@pytest.fixture
def world() -> WorldState:
    raw = json.loads(SEED.read_text(encoding="utf-8"))
    return WorldState.model_validate(
        {k: v for k, v in raw.items() if not k.startswith("_comment")})


# ── time ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("hour,expected", [
    (0.0, "night"), (3.0, "night"), (6.0, "dawn"), (9.0, "morning"),
    (13.0, "afternoon"), (19.0, "evening"), (22.0, "night"), (23.99, "night"),
])
def test_phase_of_day(hour, expected):
    """The model is told in words whether it is night — it never has to infer it
    from a raw hour, which is a thing models get wrong constantly."""
    assert phase_of_day(hour) == expected


def test_describe_time_never_produces_a_sixtieth_minute():
    assert describe_time(2, 7.999) == "day 2, dawn (08:00)"
    assert describe_time(0, 19.5) == "day 0, evening (19:30)"


def test_omitted_hours_falls_back_to_the_archetype_default():
    """THE failure this exists for: the model says nothing about time, and
    without a default the clock would simply not move."""
    hours, note = coerce_hours(None, "travel")
    assert hours == ARCHETYPE_HOURS["travel"]
    assert note is None


def test_zero_hours_is_raised_to_the_floor():
    """A model returning 0 must not be able to freeze the world."""
    hours, note = coerce_hours(0, "speak")
    assert hours == MIN_HOURS
    assert "below floor" in note


def test_absurd_hours_is_clamped_not_obeyed():
    """One hallucinated number must not skip the entire game."""
    hours, note = coerce_hours(99999, "speak")
    assert hours == MAX_HOURS
    assert "above ceiling" in note


@pytest.mark.parametrize("raw,expected", [
    ("3", 3.0), ("2.5 hours", 2.5), ("4h", 4.0), ("1 hr", 1.0),
])
def test_hours_parses_the_shapes_models_actually_return(raw, expected):
    hours, _ = coerce_hours(raw, "speak")
    assert hours == expected


@pytest.mark.parametrize("garbage", ["soon", "", "a while", {}, [], "NaN"])
def test_unparseable_hours_degrades_to_default_with_a_warning(garbage):
    hours, note = coerce_hours(garbage, "search")
    assert hours == ARCHETYPE_HOURS["search"]
    if garbage not in ("",):
        assert note is not None


def test_phase_reaches_every_projection(world):
    """Every prompt gets time in words, for free, without the caller remembering."""
    world.clock.hour = 22.5
    for char_id in world.characters:
        view = project(world, char_id)
        assert view.phase == "night"
        assert "night" in view.time_of_day


# ── perception filter ───────────────────────────────────────────────────────

def test_narrator_never_receives_events_from_another_room(world):
    """The leak that undoes everything else: NPC turns resolve across the whole
    map, and an unfiltered event list hands the player a poisoning three rooms
    away while the narrator 'faithfully narrates only what it was given'."""
    events = [
        Event(location="royal_apartments",
              text="Ollivar tips the vial into the king's cup", actors=["ollivar"]),
        Event(location="small_council",
              text="Stagg shuffles the ledgers", actors=["stagg"]),
    ]
    perceived = visible_events(world, events, "orys")   # orys is in small_council
    assert perceived == ["Stagg shuffles the ledgers"]


def test_participants_perceive_their_own_events_regardless_of_location(world):
    """Location bookkeeping can lag a move; taking part in something is proof
    enough that you witnessed it."""
    events = [Event(location="dragonstone", text="a bargain is struck",
                    actors=["orys", "aerion"])]
    assert visible_events(world, events, "orys") == ["a bargain is struck"]


def test_dead_characters_perceive_nothing(world):
    world.characters["orys"].alive = False
    events = [Event(location="small_council", text="something happens")]
    assert visible_events(world, events, "orys") == []


def test_private_npc_conversation_is_invisible_to_the_player(world):
    """Two NPCs talking in another room is exactly what should NOT reach the
    narrator — the player walking in later to find everyone already knows is the
    feature."""
    events = [Event(location="royal_apartments",
                    text='Ollivar tells Sela: "say nothing"',
                    actors=["ollivar", "sela"])]
    assert visible_events(world, events, "orys") == []


# ── the belief-index protocol ───────────────────────────────────────────────

def test_npc_cannot_speak_a_fact_it_does_not_hold(world, monkeypatch):
    """The core anti-invention trick: speakers return an INDEX into their own
    beliefs. An out-of-range index (a model trying to reference fact #999, or a
    fact it imagined) is dropped rather than fabricating anything."""
    monkeypatch.setattr(llm, "get_intention", lambda view, note="": llm.Intention(
        action="speak", target="Byren Stagg", reveals_belief=999, truthful=True))

    before = len(world.beliefs)
    events = turn_mod.npc_turn(world, "ferrow", make_rng(1))
    assert not any("tells" in e.text for e in events)
    assert len(world.beliefs) == before


def _always_discloses(monkeypatch):
    """Pin the disclosure gate open.

    The gate is a probability, and a test of belief PROPAGATION should not also
    be a test of whether a d100 came up. disclosure.py is tested exactly, on its
    own, in test_disclosure.py; here we only care what happens once a character
    has decided to speak.
    """
    monkeypatch.setattr(turn_mod.disclosure, "will_disclose",
                        lambda *a, **k: (True, 1.0))


def test_npc_speech_moves_a_belief_it_actually_holds(world, monkeypatch):
    """The legitimate path: index 0 of the speaker's own list propagates."""
    _always_discloses(monkeypatch)
    view = project(world, "ferrow")
    monkeypatch.setattr(llm, "get_intention", lambda v, note="": llm.Intention(
        action="speak", target="Byren Stagg", reveals_belief=0, truthful=True,
        speech="You should know something."))

    turn_mod.npc_turn(world, "ferrow", make_rng(1))
    assert world.believes("stagg", view.beliefs[0].fact_id) is not None


def test_a_lie_is_recorded_with_its_source(world, monkeypatch):
    _always_discloses(monkeypatch)
    view = project(world, "ferrow")
    monkeypatch.setattr(llm, "get_intention", lambda v, note="": llm.Intention(
        action="speak", target="Byren Stagg", reveals_belief=0, truthful=False))

    turn_mod.npc_turn(world, "ferrow", make_rng(1))
    belief = world.believes("stagg", view.beliefs[0].fact_id)
    assert belief.source_char_id == "ferrow"


def test_naming_someone_absent_does_not_reach_them(world, monkeypatch):
    """An NPC cannot address someone across the map — Aerion is on Dragonstone.

    Speech still HAPPENS (it is an act performed in a room, not a message), and
    the people actually present still hear it; Aerion simply is not one of them
    and is not named in the prose.
    """
    _always_discloses(monkeypatch)
    view = project(world, "ferrow")
    monkeypatch.setattr(llm, "get_intention", lambda v, note="": llm.Intention(
        action="speak", target="Aerion", reveals_belief=0, truthful=True))

    fact_id = view.beliefs[0].fact_id
    before = world.believes("aerion", fact_id)
    before = before.model_copy() if before else None

    events = turn_mod.npc_turn(world, "ferrow", make_rng(1))

    assert not any("Aerion" in e.text for e in events)
    # Aerion is on Dragonstone: whatever he held before, nothing reached him.
    assert world.believes("aerion", fact_id) == before


# ── speech is heard by the room ─────────────────────────────────────────────

def test_speech_reaches_every_witness_not_only_the_addressee(world, monkeypatch):
    """The bug this replaces: `propagate` ran for the addressee alone, so a man
    standing three feet away heard nothing. Group conversation mostly falls out
    of fixing it — a scene is just a room where speech lands on everyone in it."""
    _always_discloses(monkeypatch)
    world.characters["crowe"].location = "small_council"
    view = project(world, "ferrow")
    fact_id = view.beliefs[0].fact_id
    monkeypatch.setattr(llm, "get_intention", lambda v, note="": llm.Intention(
        action="speak", target="Byren Stagg", reveals_belief=0, truthful=True))

    turn_mod.npc_turn(world, "ferrow", make_rng(1))

    assert world.believes("stagg", fact_id) is not None    # addressed
    assert world.believes("crowe", fact_id) is not None    # merely present
    assert world.believes("orys", fact_id) is not None     # the player, no special case


def test_speech_does_not_reach_another_room(world, monkeypatch):
    _always_discloses(monkeypatch)
    view = project(world, "ferrow")
    fact_id = view.beliefs[0].fact_id
    monkeypatch.setattr(llm, "get_intention", lambda v, note="": llm.Intention(
        action="speak", target="Byren Stagg", reveals_belief=0, truthful=True))

    turn_mod.npc_turn(world, "ferrow", make_rng(1))
    assert world.believes("ollivar", fact_id) is None   # royal_apartments


def test_a_withheld_fact_moves_nothing_and_leaks_no_prose(world, monkeypatch):
    """On a failed disclosure roll the model's own `speech` string is discarded
    too — it is free text that may well contain the very thing being withheld."""
    monkeypatch.setattr(turn_mod.disclosure, "will_disclose",
                        lambda *a, **k: (False, 0.03))
    view = project(world, "ferrow")
    fact_id = view.beliefs[0].fact_id
    monkeypatch.setattr(llm, "get_intention", lambda v, note="": llm.Intention(
        action="speak", target="Byren Stagg", reveals_belief=0, truthful=True,
        speech="THE KING WAS POISONED BY AERION."))

    before = len(world.beliefs)
    events = turn_mod.npc_turn(world, "ferrow", make_rng(1))

    assert len(world.beliefs) == before
    assert not any("POISONED" in e.text for e in events)
    assert any("thinks better of it" in e.text for e in events)


def test_turn_survives_total_provider_outage(world, monkeypatch):
    """Every provider down should cost the world some colour, not end the game."""
    def dead(*args, **kwargs):
        raise llm.LLMUnavailable("all providers exhausted")

    monkeypatch.setattr(llm, "complete_json", dead)
    monkeypatch.setattr(llm, "complete_text", dead)

    result = turn_mod.play_turn(world, "ask Stagg about the ledgers", seed=1)
    assert result.turn == 1
    assert world.clock.absolute_hour > 8.0      # clock still advanced
    assert result.narration                      # degraded, but present


def test_full_turn_advances_the_world_and_narrates_only_perceived_events(
        world, monkeypatch):
    monkeypatch.setattr(llm, "get_intention", lambda v, note="": llm.Intention(
        action="observe", reveals_belief=llm.NO_FACT))
    monkeypatch.setattr(llm, "get_referee", lambda v, a: llm.RefereeCall(
        difficulty="moderate", opposing_stat="guile", archetype="speak",
        hours_elapsed=2))

    captured = {}

    def fake_narration(view, events, action=""):
        captured["events"] = events
        return "The council chamber is cold.\n\n> Press Stagg\n> Leave\n> Wait"

    monkeypatch.setattr(llm, "get_narration", fake_narration)

    result = turn_mod.play_turn(world, "ask Stagg about the ledgers", seed=7)

    assert result.turn == 1
    assert world.clock.hour == pytest.approx(10.0)      # 8.0 + 2h
    assert result.suggested_actions == ["Press Stagg", "Leave", "Wait"]
    assert "Press Stagg" not in result.narration
    # Nothing from another room reached the narrator.
    assert not any("royal_apartments" in e for e in captured["events"])


def test_suggestions_are_tolerated_when_the_narrator_ignores_the_format(
        world, monkeypatch):
    """A mangled scene is worse than no suggestions."""
    monkeypatch.setattr(llm, "get_intention", lambda v, note="": llm.Intention())
    monkeypatch.setattr(llm, "get_referee", lambda v, a: llm.RefereeCall())
    monkeypatch.setattr(llm, "get_narration",
                        lambda v, e, a="": "Just prose, no suggestions.")

    result = turn_mod.play_turn(world, "wait", seed=1)
    assert result.narration == "Just prose, no suggestions."
    assert result.suggested_actions == []


def test_same_seed_and_turn_reproduce_the_same_rolls(world, monkeypatch):
    """Rewind depends on the RNG being derived from (seed, turn) rather than
    carried in a long-lived generator."""
    monkeypatch.setattr(llm, "get_intention", lambda v, note="": llm.Intention())
    monkeypatch.setattr(llm, "get_referee", lambda v, a: llm.RefereeCall(
        hours_elapsed=1))
    monkeypatch.setattr(llm, "get_narration", lambda v, e, a="": "scene")

    import copy
    snapshot = copy.deepcopy(world)
    first = turn_mod.play_turn(world, "wait", seed="fixed",
                               enable_conversations=False)
    second = turn_mod.play_turn(snapshot, "wait", seed="fixed",
                                enable_conversations=False)
    assert [e.detail for e in first.events] == [e.detail for e in second.events]
