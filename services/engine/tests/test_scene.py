"""Tests for group scenes: the relay, the caps, and the offstage cost model.

Two things are being proven. That a scene is a CONVERSATION — each speaker sees
what was said before them — and that offstage scenes move the world without
spending a single token, which is the thing that makes the onstage ones
affordable at all.
"""

import json
from pathlib import Path

import pytest

from app import llm, scene as scene_mod, turn as turn_mod
from app.models import WorldState
from app.resolution import make_rng
from app.scene import MAX_PARTICIPANTS, MAX_PASSES, Mode

SEED = Path(__file__).resolve().parents[1] / "app" / "world" / "seed.json"


@pytest.fixture
def world() -> WorldState:
    raw = json.loads(SEED.read_text(encoding="utf-8"))
    return WorldState.model_validate(
        {k: v for k, v in raw.items() if not k.startswith("_comment")})


@pytest.fixture
def council(world) -> WorldState:
    """Four people in one room, so a scene is actually a group."""
    for char_id in ("orys", "ferrow", "stagg", "crowe"):
        world.characters[char_id].location = "small_council"
    return world


def _open_gate(monkeypatch):
    monkeypatch.setattr(scene_mod.disclosure, "will_disclose",
                        lambda *a, **k: (True, 1.0))


# ── the relay: each speaker hears the ones before them ──────────────────────

def test_each_speaker_sees_everything_said_before_them(council, monkeypatch):
    """A reads nothing -> A1; B reads (A1) -> B1; C reads (A1,B1) -> C1.

    This is the entire difference between a conversation and three monologues
    stapled together.
    """
    _open_gate(monkeypatch)
    seen: list[str] = []

    def capture(view, note=""):
        seen.append(note)
        return llm.Intention(action="speak", reveals_belief=0, truthful=True,
                             speech=f"{view.self_name} speaks")

    monkeypatch.setattr(llm, "get_intention", capture)
    scene_mod.run_scene(council, "small_council",
                        ["orys", "ferrow", "stagg", "crowe"], make_rng(1),
                        mode=Mode.ONSTAGE, passes=1, player_id="orys")

    assert len(seen) >= 3
    assert "Nothing has been said yet" in seen[0]
    # Every later speaker was handed a transcript, and it only grows.
    transcripts = [n for n in seen if "SAID SO FAR" in n]
    assert transcripts, "no speaker after the first received the transcript"
    assert len(transcripts[-1]) > len(transcripts[0])


def test_a_withheld_belief_leaves_no_trace_in_the_transcript(council, monkeypatch):
    """From inside the room, nothing was said — so the next speaker cannot
    react to a thing that never left anyone's mouth."""
    monkeypatch.setattr(scene_mod.disclosure, "will_disclose",
                        lambda *a, **k: (False, 0.01))
    seen: list[str] = []
    monkeypatch.setattr(llm, "get_intention", lambda v, note="": (
        seen.append(note) or llm.Intention(action="speak", reveals_belief=0,
                                           speech="a secret")))

    scene_mod.run_scene(council, "small_council", ["ferrow", "stagg", "crowe"],
                        make_rng(1), mode=Mode.ONSTAGE, passes=1)
    assert not any("SAID SO FAR" in n for n in seen)


def test_the_passes_are_named_so_the_last_one_reads_as_a_conclusion(council,
                                                                    monkeypatch):
    _open_gate(monkeypatch)
    seen: list[str] = []
    monkeypatch.setattr(llm, "get_intention", lambda v, note="": (
        seen.append(note) or llm.Intention(action="speak", reveals_belief=0,
                                           speech="x")))

    scene_mod.run_scene(council, "small_council", ["ferrow", "stagg"],
                        make_rng(1), mode=Mode.ONSTAGE, passes=3)
    joined = "\n".join(seen)
    assert "last thing you will say" in joined


# ── the caps ────────────────────────────────────────────────────────────────

def test_participants_are_capped(council, monkeypatch):
    _open_gate(monkeypatch)
    monkeypatch.setattr(llm, "get_intention",
                        lambda v, note="": llm.Intention(action="observe"))
    everyone = list(council.characters)
    _events, record = scene_mod.run_scene(council, "small_council", everyone,
                                          make_rng(1), mode=Mode.ONSTAGE, passes=1)
    assert len(record.participants) <= MAX_PARTICIPANTS


def test_passes_are_capped_however_many_are_asked_for(council, monkeypatch):
    _open_gate(monkeypatch)
    calls = []
    monkeypatch.setattr(llm, "get_intention", lambda v, note="": (
        calls.append(1) or llm.Intention(action="speak", reveals_belief=0)))

    scene_mod.run_scene(council, "small_council", ["ferrow", "stagg"],
                        make_rng(1), mode=Mode.ONSTAGE, passes=99)
    assert len(calls) <= 2 * MAX_PASSES


def test_the_player_is_present_but_never_voiced_by_a_model(council, monkeypatch):
    """The player is a participant — everything said reaches them — but they
    speak by taking their turn, not by a model puppeting them."""
    _open_gate(monkeypatch)
    voiced: list[str] = []
    monkeypatch.setattr(llm, "get_intention", lambda v, note="": (
        voiced.append(v.self_name) or llm.Intention(action="observe")))

    scene_mod.run_scene(council, "small_council",
                        ["orys", "ferrow", "stagg"], make_rng(1),
                        mode=Mode.ONSTAGE, passes=1, player_id="orys")
    assert "Orys Ashwood" not in voiced


def test_a_scene_needs_at_least_two_people(world, monkeypatch):
    called = []
    monkeypatch.setattr(llm, "get_intention", lambda v, note="": (
        called.append(1) or llm.Intention()))
    events, record = scene_mod.run_scene(world, "sept", ["wyl"], make_rng(1),
                                         mode=Mode.ONSTAGE)
    assert events == [] and record.exchanges == [] and not called


# ── offstage: the cost model ────────────────────────────────────────────────

def test_an_offstage_scene_spends_no_tokens(council, monkeypatch):
    """THE load-bearing property. Every mechanic that matters is pure engine
    code, so a conversation the player cannot perceive needs no model at all."""
    def explode(*args, **kwargs):
        raise AssertionError("an offstage scene called a model")

    monkeypatch.setattr(llm, "get_intention", explode)
    monkeypatch.setattr(llm, "complete_json", explode)
    monkeypatch.setattr(llm, "complete_text", explode)

    record = scene_mod.run_offstage(council, "royal_apartments",
                                    ["queen", "ollivar", "king"], make_rng(3))
    assert record.mode == Mode.OFFSTAGE
    assert record.exchanges, "nothing happened at all"


def test_an_offstage_scene_still_moves_beliefs(world):
    """Zero tokens must not mean zero consequence — otherwise the world stops
    living the moment the player looks away."""
    world.characters["crowe"].location = "royal_apartments"
    world.characters["crowe"].candor = 1.0
    before = len(world.beliefs)
    for _ in range(6):
        scene_mod.run_offstage(world, "royal_apartments",
                               ["queen", "ollivar", "crowe"], make_rng(7))
    assert len(world.beliefs) > before


def test_offstage_scenes_are_groups_not_pairs(council):
    record = scene_mod.run_offstage(council, "small_council",
                                    ["ferrow", "stagg", "crowe"], make_rng(1))
    assert len(record.participants) == 3


def test_a_scene_record_never_serializes_a_fact_id(council):
    """Same protection as ProjectedBelief: the record carries fact ids for the
    engine and drops them from every serialization."""
    record = scene_mod.run_offstage(council, "small_council",
                                    ["ferrow", "stagg", "crowe"], make_rng(1))
    blob = json.dumps(record.model_dump(mode="json"))
    assert "fact_id" not in blob
    assert "f_king_poisoned" not in blob


# ── room selection ──────────────────────────────────────────────────────────

def test_rooms_are_chosen_by_pressure_not_by_a_coin_flip(world):
    """The old picker flipped a coin then took two random co-located NPCs. The
    same world should now produce the same rooms every time."""
    first = turn_mod.offstage_groups(world, make_rng(1), exclude_location="nowhere")
    second = turn_mod.offstage_groups(world, make_rng(999), exclude_location="nowhere")
    assert first == second


def test_the_players_own_room_is_never_resolved_offstage(world):
    """It is onstage by definition — resolving it offstage would silently skip
    the prose the player is standing there to read."""
    groups = turn_mod.offstage_groups(world, make_rng(1),
                                      exclude_location="small_council")
    flat = [c for g in groups for c in g]
    assert not any(world.characters[c].location == "small_council" for c in flat)


def test_offstage_scene_count_is_capped(world):
    groups = turn_mod.offstage_groups(world, make_rng(1), exclude_location="nowhere")
    assert len(groups) <= turn_mod.MAX_OFFSTAGE_SCENES
