"""Tests for evidence — the only route from suspicion to certainty.

The pairing matters: `plots.propagate` caps talk at SUSPECTS, and on its own that
cap makes the world unknowable. These tests exist to prove the other half is
real — that certainty is still reachable, and reachable by DOING something.
"""

import json
from pathlib import Path

import pytest

from app import evidence as evidence_mod
from app.models import Stance, WorldState
from app.resolution import Degree, Outcome, make_rng

SEED = Path(__file__).resolve().parents[1] / "app" / "world" / "seed.json"


@pytest.fixture
def world() -> WorldState:
    raw = json.loads(SEED.read_text(encoding="utf-8"))
    return WorldState.model_validate(
        {k: v for k, v in raw.items() if not k.startswith("_comment")})


def _outcome(degree: Degree) -> Outcome:
    return Outcome(degree=degree, total=18, difficulty=14, margin=4, roll=12,
                   stat_value=4, detail="stub")


# ── the wall between talk and knowledge ─────────────────────────────────────

def test_evidence_is_what_promotes_a_suspicion_to_knowledge(world):
    from app.plots import propagate

    propagate(world, "sela", "orys", "f_king_poisoned", truthful=True)
    assert world.believes("orys", "f_king_poisoned").stance is Stance.SUSPECTS

    evidence_mod.drop(world, "f_king_poisoned", "small_council",
                      kind="vial", strength=0.95)
    evidence_mod.discover(world, "orys", _outcome(Degree.SUCCESS), make_rng(1))

    belief = world.believes("orys", "f_king_poisoned")
    assert belief.stance is Stance.KNOWS
    assert belief.confidence >= 0.9


def test_a_thing_seen_first_hand_no_longer_rests_on_who_told_you(world):
    """If Sela is later exposed as a liar, every belief sourced to her becomes
    suspect. This one must survive that, because it no longer rests on her."""
    from app.plots import propagate

    propagate(world, "sela", "orys", "f_king_poisoned", truthful=True)
    assert world.believes("orys", "f_king_poisoned").source_char_id == "sela"

    evidence_mod.drop(world, "f_king_poisoned", "small_council", strength=0.9)
    evidence_mod.discover(world, "orys", _outcome(Degree.SUCCESS), make_rng(1))

    assert world.believes("orys", "f_king_poisoned").source_char_id is None


def test_a_partial_success_yields_doubt_not_certainty(world):
    evidence_mod.drop(world, "f_king_poisoned", "small_council", strength=0.9)
    evidence_mod.discover(world, "orys", _outcome(Degree.PARTIAL), make_rng(1))

    belief = world.believes("orys", "f_king_poisoned")
    assert belief.stance is Stance.SUSPECTS
    assert belief.confidence < 0.9


def test_a_failed_search_finds_nothing_and_leaves_the_trail_intact(world):
    item = evidence_mod.drop(world, "f_king_poisoned", "small_council")
    evidence_mod.discover(world, "orys", _outcome(Degree.FAILURE), make_rng(1))

    assert world.believes("orys", "f_king_poisoned") is None
    assert item.found_by == []          # still there for the next attempt


# ── it stays where it fell ──────────────────────────────────────────────────

def test_evidence_is_found_not_perceived(world):
    """Standing in the room is not enough. This is why the object can carry a
    fact_id at all — nothing serializes it into a prompt."""
    from app.projection import project

    evidence_mod.drop(world, "f_king_poisoned", "small_council", strength=0.9)
    view = project(world, "orys")           # orys IS in small_council
    assert not any(b.fact_id == "f_king_poisoned" for b in view.beliefs)
    assert "poison" not in json.dumps(view.prompt_payload()).lower()


def test_you_cannot_search_the_same_desk_twice_for_the_same_thing(world):
    evidence_mod.drop(world, "f_king_poisoned", "small_council", strength=0.9)
    first = evidence_mod.discover(world, "orys", _outcome(Degree.SUCCESS), make_rng(1))
    second = evidence_mod.discover(world, "orys", _outcome(Degree.SUCCESS), make_rng(1))
    assert first and not second


def test_evidence_elsewhere_is_not_findable_from_here(world):
    evidence_mod.drop(world, "f_king_poisoned", "dragonstone", strength=0.9)
    assert evidence_mod.discover(world, "orys", _outcome(Degree.SUCCESS),
                                 make_rng(1)) == []


def test_evidence_for_an_unknown_fact_is_refused(world):
    assert evidence_mod.drop(world, "f_does_not_exist", "small_council") is None


# ── the plot leaves a physical trail ────────────────────────────────────────

def test_an_advancing_plot_drops_findable_evidence(world):
    """Exposure used to conjure a hunch with no object behind it, discoverable
    only by being adjacent at the right moment. Now a scheme leaves a mark."""
    from app.plots import tick

    rng = make_rng(5)
    for _ in range(40):
        tick(world, rng, hours=2.0)

    assert world.evidence, "an advancing conspiracy left no trail at all"
    assert all(e.fact_id in world.facts for e in world.evidence.values())
    assert all(e.location for e in world.evidence.values())
