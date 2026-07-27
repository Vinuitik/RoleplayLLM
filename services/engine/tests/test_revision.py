"""Tests for belief revision and deliberate misinformation.

The thing being proven here is that a mind can be CHANGED, not just added to.
Without contradiction a character is a set: hear "poisoned" and "died of age"
and hold both forever, which means a lie costs nothing to tell and nothing to
undo, and no one ever has to be talked out of anything.
"""

import json
from pathlib import Path

import pytest

from app import revision
from app.models import Stance, WorldState
from app.plots import propagate, propagate_verbose, spoken_fact

SEED = Path(__file__).resolve().parents[1] / "app" / "world" / "seed.json"


@pytest.fixture
def world() -> WorldState:
    raw = json.loads(SEED.read_text(encoding="utf-8"))
    return WorldState.model_validate(
        {k: v for k, v in raw.items() if not k.startswith("_comment")})


def _clean(world: WorldState, char_id: str) -> None:
    """Strip a character's seeded beliefs so a test starts from a known slate.

    Worth doing explicitly: the seed already has Orys holding `f_king_old_age`
    at certainty, sourced to Ollivar — the comfortable lie he was fed before the
    game began. That is exactly right for play and exactly wrong for a unit test
    that wants to watch one specific claim land.
    """
    world.beliefs = [b for b in world.beliefs if b.char_id != char_id]


# ── contradiction is symmetric ──────────────────────────────────────────────

def test_contradiction_is_read_in_both_directions(world):
    """The seed declares the pair once. An author must not be able to create a
    half-edge that silently works in only one direction."""
    assert "f_king_poisoned" in revision.contradictions_of(world, "f_king_old_age")
    assert "f_king_old_age" in revision.contradictions_of(world, "f_king_poisoned")


def test_a_fact_contradicting_nothing_is_handled(world):
    assert revision.contradictions_of(world, "f_king_ill") == []


# ── deliberate misinformation ───────────────────────────────────────────────

def test_a_lie_asserts_the_contradiction_not_a_weaker_truth(world):
    """This is what makes a lie a lie. Ollivar, who knows the king was poisoned,
    says the king died of age — and the listener acquires that specific false
    belief, which later has to be argued back out of them."""
    assert spoken_fact(world, "f_king_poisoned", truthful=False) == "f_king_old_age"
    assert spoken_fact(world, "f_king_poisoned", truthful=True) == "f_king_poisoned"


def test_the_listener_acquires_the_false_belief_not_the_true_one(world):
    _clean(world, "orys")
    propagate(world, "ollivar", "orys", "f_king_poisoned", truthful=False)
    assert world.believes("orys", "f_king_old_age") is not None
    assert world.believes("orys", "f_king_poisoned") is None


def test_a_fact_with_no_contradiction_degrades_to_an_unconvincing_truth(world):
    """The fallback is what lets `contradicts` be added to a seed incrementally
    without anything breaking."""
    _clean(world, "crowe")
    assert spoken_fact(world, "f_king_ill", truthful=False) == "f_king_ill"
    propagate(world, "ferrow", "crowe", "f_king_ill", truthful=False)
    assert world.believes("crowe", "f_king_ill").confidence < 0.6


def test_misinformation_is_traceable_to_who_told_it(world):
    _clean(world, "orys")
    propagate(world, "ollivar", "orys", "f_king_poisoned", truthful=False)
    assert world.believes("orys", "f_king_old_age").source_char_id == "ollivar"


# ── the contest ─────────────────────────────────────────────────────────────

def test_a_contradicting_claim_shakes_what_is_already_held(world):
    _clean(world, "orys")
    world.grant_belief("orys", "f_king_poisoned", Stance.SUSPECTS, 0.3)
    before = world.believes("orys", "f_king_poisoned").confidence

    world.characters["orys"].relationships["ollivar"] = 3.0   # trusted liar
    propagate(world, "ollivar", "orys", "f_king_poisoned", truthful=False)

    assert world.believes("orys", "f_king_poisoned").confidence < before


def test_an_incoming_claim_can_lose_the_argument(world):
    """Telling a man something he holds firmly, from a source he despises, is an
    argument you can lose — and it should land weaker for having been resisted."""
    _clean(world, "orys")
    world.grant_belief("orys", "f_king_poisoned", Stance.KNOWS, 1.0)
    world.characters["orys"].relationships["ollivar"] = -3.0

    propagate(world, "ollivar", "orys", "f_king_poisoned", truthful=False)

    held = world.believes("orys", "f_king_poisoned")
    assert held is not None and held.confidence > 0.3     # still stands
    lie = world.believes("orys", "f_king_old_age")
    assert lie is None or lie.confidence < 0.4            # took poorly, if at all


def test_who_told_you_matters_more_than_how_loudly(world):
    """Same claim, same confidence, different mouths."""
    trusted = revision.credibility(world, "orys", "king")      # relationship +3
    world.characters["orys"].relationships["ferrow"] = -3.0
    despised = revision.credibility(world, "orys", "ferrow")
    assert despised < trusted


def test_something_seen_first_hand_outweighs_any_testimony(world):
    """source=None is first-hand. This is what makes evidence beat talk
    structurally rather than by being handed a bigger number."""
    assert revision.credibility(world, "orys", None) >= revision.credibility(
        world, "orys", "king")


def test_a_belief_shaken_far_enough_is_dropped_entirely(world):
    _clean(world, "orys")
    world.grant_belief("orys", "f_king_poisoned", Stance.SUSPECTS, 0.2)
    world.characters["orys"].relationships["ollivar"] = 3.0

    propagate(world, "ollivar", "orys", "f_king_poisoned", truthful=False)

    assert world.believes("orys", "f_king_poisoned") is None
    assert not any(b.char_id == "orys" and b.fact_id == "f_king_poisoned"
                   for b in world.beliefs)


def test_revision_reports_what_changed(world):
    """A mind changing has to be visible in the DM panel, not mysterious."""
    _clean(world, "orys")
    world.grant_belief("orys", "f_king_poisoned", Stance.SUSPECTS, 0.3)
    world.characters["orys"].relationships["ollivar"] = 3.0

    _changed, notes = propagate_verbose(world, "ollivar", "orys",
                                        "f_king_poisoned", truthful=False)
    assert any("shaken" in n for n in notes)


# ── the ceiling survives all of this ────────────────────────────────────────

def test_a_confident_lie_still_cannot_create_certainty(world):
    _clean(world, "orys")
    world.characters["orys"].relationships["ollivar"] = 3.0
    for _ in range(6):
        propagate(world, "ollivar", "orys", "f_king_poisoned", truthful=False)
    belief = world.believes("orys", "f_king_old_age")
    assert belief.stance is Stance.SUSPECTS
    assert belief.confidence <= 0.6


def test_evidence_beats_the_comfortable_lie_it_contradicts(world):
    """The full loop: be lied to, then find the thing itself."""
    from app import evidence as evidence_mod
    from app.resolution import Degree, Outcome, make_rng

    _clean(world, "orys")
    world.characters["orys"].relationships["ollivar"] = 3.0
    propagate(world, "ollivar", "orys", "f_king_poisoned", truthful=False)
    assert world.believes("orys", "f_king_old_age") is not None

    evidence_mod.drop(world, "f_king_poisoned", "small_council",
                      kind="vial", strength=0.95)
    evidence_mod.discover(world, "orys", Outcome(
        degree=Degree.SUCCESS, total=18, difficulty=14, margin=4, roll=12,
        stat_value=4, detail="stub"), make_rng(1))

    assert world.believes("orys", "f_king_poisoned").stance is Stance.KNOWS
    lie = world.believes("orys", "f_king_old_age")
    assert lie is None or lie.confidence < 0.4
