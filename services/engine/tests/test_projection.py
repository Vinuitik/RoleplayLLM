"""Leak tests.

These are the tests that matter. Everything else in the project is a game; this
is the part that makes the game possible. They are written as PROPERTIES over the
whole world rather than as spot-checks, because the leak you think to check for
is never the one that gets you.
"""

import json
from pathlib import Path

import pytest

from app.models import Stance, WorldState
from app.projection import project

SEED = Path(__file__).resolve().parents[1] / "app" / "world" / "seed.json"


@pytest.fixture
def world() -> WorldState:
    raw = json.loads(SEED.read_text(encoding="utf-8"))
    # Strip the _comment_* authoring notes; they're documentation, not state.
    return WorldState.model_validate(
        {k: v for k, v in raw.items() if not k.startswith("_comment")})


def test_seed_loads_and_validates(world):
    assert len(world.characters) == 10
    assert len(world.facts) >= 15
    assert len(world.plots) == 2
    assert world.player_id in world.characters


def test_every_belief_points_at_a_real_fact_and_character(world):
    """A dangling belief would silently vanish from projection — the character
    would 'know' something that never renders, which is invisible in play and
    maddening to debug."""
    for belief in world.beliefs:
        assert belief.fact_id in world.facts, f"unknown fact {belief.fact_id}"
        assert belief.char_id in world.characters, f"unknown char {belief.char_id}"


# ── the property ────────────────────────────────────────────────────────────

def test_projection_contains_exactly_the_beliefs_held(world):
    """THE invariant: for every character and every fact, the fact appears in
    that character's projection if and only if they hold a belief about it.

    Both directions matter. Forward catches leaks (seeing what you shouldn't).
    Backward catches erasure (not seeing what you should), which is the failure
    that makes NPCs act inexplicably ignorant.
    """
    for char_id in world.characters:
        projected = project(world, char_id)
        seen = {b.fact_id for b in projected.beliefs}
        held = {b.fact_id for b in world.beliefs_of(char_id)}
        assert seen == held, (
            f"{char_id}: leaked {seen - held}, lost {held - seen}")


def test_no_projection_contains_a_fact_the_character_does_not_hold(world):
    """The same property stated over CONTENT rather than ids — it would catch a
    regression where content leaked through some other field (concealing, a
    meter label) even while the belief list stayed correct."""
    for char_id in world.characters:
        projected = project(world, char_id)
        held_content = {world.facts[b.fact_id].content
                        for b in world.beliefs_of(char_id)}
        blob = json.dumps(projected.prompt_payload())
        for fact in world.facts.values():
            if fact.content in held_content:
                continue
            assert fact.content not in blob, (
                f"{char_id}'s projection leaked: {fact.content!r}")


# ── the named case from the board ───────────────────────────────────────────

def test_master_of_coin_cannot_see_the_poison(world):
    """Stagg is deep in his own conspiracy and knows nothing of the poison. If
    this ever passes for the wrong reason, the property test above is the safety
    net."""
    projected = project(world, "stagg")
    blob = json.dumps(projected.prompt_payload())
    assert "poison" not in blob.lower()
    assert world.facts["f_king_poisoned"].content not in blob


def test_player_cannot_see_the_conspiracy_at_turn_zero(world):
    """The game is only interesting if Orys starts ignorant."""
    projected = project(world, "orys")
    blob = json.dumps(projected.prompt_payload())
    for fact_id in ("f_king_poisoned", "f_aerion_conspiracy",
                    "f_stagg_embezzling", "f_ferrow_letters"):
        assert world.facts[fact_id].content not in blob


# ── truth blindness ─────────────────────────────────────────────────────────

def test_is_true_never_appears_in_any_projection(world):
    """No projection may carry a truth value, for anyone, ever — including the
    narrator's view of the player. A narrator that knows a belief is false hedges
    ('you *think* he died of old age'), and that hedge tells the player there is
    something to doubt without disclosing one hidden fact. Leak by tone."""
    for char_id in world.characters:
        payload = project(world, char_id).prompt_payload()
        blob = json.dumps(payload)
        assert "is_true" not in blob
        for belief in payload["beliefs"]:
            assert "is_true" not in belief


def test_a_false_belief_is_indistinguishable_from_a_true_one(world):
    """Orys is certain the king is dying of old age. He is wrong. His projection
    must present that exactly as it presents things he is right about — same
    stance, same confidence, no marker of any kind."""
    projected = project(world, "orys")
    by_content = {b.content: b for b in projected.beliefs}

    false_belief = by_content[world.facts["f_king_old_age"].content]
    true_belief = by_content[world.facts["f_king_ill"].content]

    assert world.facts["f_king_old_age"].is_true is False
    assert world.facts["f_king_ill"].is_true is True
    # The engine knows they differ. The projection cannot tell.
    assert false_belief.stance == true_belief.stance == Stance.KNOWS
    assert false_belief.confidence == true_belief.confidence == 1.0
    assert false_belief.model_dump().keys() == true_belief.model_dump().keys()


def test_fact_ids_never_reach_a_prompt(world):
    """Ids are authored and therefore descriptive — 'f_king_poisoned' announces
    the secret even where the content was filtered. fact_id is kept on the object
    for the engine and excluded from every serialization."""
    for char_id in world.characters:
        blob = json.dumps(project(world, char_id).prompt_payload())
        for fact_id in world.facts:
            assert fact_id not in blob, f"{char_id}'s payload leaked id {fact_id}"


def test_engine_can_still_reach_fact_id(world):
    """The exclusion must be serialization-only — the engine needs the handle to
    resolve a belief back to its fact."""
    projected = project(world, "orys")
    assert all(b.fact_id in world.facts for b in projected.beliefs)


# ── hidden meters ───────────────────────────────────────────────────────────

def test_hidden_meters_are_not_projected(world):
    """conspiracy_readiness is the ticking clock the player is racing. Seeing it
    would give away both that there is a conspiracy and how close it is."""
    hidden = world.meters["conspiracy_readiness"]
    assert hidden.visible_to_player is False
    for char_id in world.characters:
        blob = json.dumps(project(world, char_id).prompt_payload())
        assert hidden.label not in blob


# ── perception ──────────────────────────────────────────────────────────────

def test_characters_elsewhere_are_not_present(world):
    """Aerion is on Dragonstone. He must never turn up in the small council's
    'present' list, or the narrator will happily put him in the room."""
    projected = project(world, "orys")
    present_names = {c.name for c in projected.present}
    assert "Aerion" not in present_names
    assert {"Mycella Ferrow", "Byren Stagg"} <= present_names
