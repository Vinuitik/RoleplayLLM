"""Tests for the disclosure gate.

The bug being fixed here was reproduced live on turn one: a conspirator
volunteered the whole conspiracy to a stranger, unprompted, at 0.9 confidence.
Nothing made her reluctant.

These are properties rather than numbers wherever possible. The exact weights in
disclosure.py are tuning and will move; what must not move is the ORDERING —
secrets are harder to say than gossip, enemies in the room shut people up, and
nothing is ever quite impossible to let slip.
"""

import json
from pathlib import Path

import pytest

from app import disclosure
from app.models import WorldState

SEED = Path(__file__).resolve().parents[1] / "app" / "world" / "seed.json"


@pytest.fixture
def world() -> WorldState:
    raw = json.loads(SEED.read_text(encoding="utf-8"))
    return WorldState.model_validate(
        {k: v for k, v in raw.items() if not k.startswith("_comment")})


class _Rng:
    """A dice-free rng. Disclosure is a probability; testing it against a real
    generator would test the generator."""

    def __init__(self, value: float):
        self.value = value

    def random(self) -> float:
        return self.value


# ── the ordering that must hold ─────────────────────────────────────────────

def test_a_concealed_fact_is_harder_to_say_than_an_open_one(world):
    """Ollivar hides the poisoning; he holds public facts too."""
    hidden = disclosure.disclosure_chance(world, "ollivar", ["orys"], "f_king_poisoned")
    open_ = disclosure.disclosure_chance(world, "ollivar", ["orys"], "f_king_ill")
    assert hidden < open_


def test_a_conspirator_guards_their_own_plot_fact_hardest(world):
    """Membership is its own exposure: this is the thing that hangs you."""
    member = disclosure.disclosure_chance(world, "ollivar", ["orys"], "f_king_poisoned")
    world.characters["outsider"] = world.characters["crowe"].model_copy(
        update={"id": "outsider", "hides": [], "candor": 0.3})
    outsider = disclosure.disclosure_chance(world, "outsider", ["orys"], "f_king_poisoned")
    assert member < outsider


def test_trust_moves_the_odds_in_the_right_direction(world):
    speaker = world.characters["ferrow"]
    speaker.relationships["orys"] = 3.0
    friendly = disclosure.disclosure_chance(world, "ferrow", ["orys"], "f_king_ill")
    speaker.relationships["orys"] = -3.0
    hostile = disclosure.disclosure_chance(world, "ferrow", ["orys"], "f_king_ill")
    assert hostile < friendly


def test_one_enemy_in_the_room_silences_a_friendly_confidence(world):
    """Trust is read at the room's MINIMUM, not its average. This is the whole
    reason a private word is worth arranging — and it is what makes the
    composition of a scene something the player can manipulate."""
    speaker = world.characters["ferrow"]
    speaker.relationships.update({"orys": 3.0, "stagg": -3.0})

    alone = disclosure.disclosure_chance(world, "ferrow", ["orys"], "f_king_ill")
    crowded = disclosure.disclosure_chance(world, "ferrow", ["orys", "stagg"],
                                           "f_king_ill")
    assert crowded < alone


def test_candor_is_the_floor_everything_else_moves_around(world):
    world.characters["ferrow"].candor = 0.1
    tight = disclosure.disclosure_chance(world, "ferrow", ["orys"], "f_king_ill")
    world.characters["ferrow"].candor = 0.9
    loose = disclosure.disclosure_chance(world, "ferrow", ["orys"], "f_king_ill")
    assert tight < loose


# ── the bounds ──────────────────────────────────────────────────────────────

def test_nothing_is_ever_perfectly_unsayable(world):
    """A hard zero makes a conspiracy unlosable rather than difficult. Secrets
    get out by slip far more often than by decision."""
    world.characters["ollivar"].candor = 0.0
    world.characters["ollivar"].relationships["orys"] = -3.0
    chance = disclosure.disclosure_chance(world, "ollivar", ["orys"], "f_king_poisoned")
    assert chance >= disclosure.MIN_CHANCE > 0


def test_nothing_is_ever_certain_to_be_said(world):
    world.characters["crowe"].candor = 1.0
    world.characters["crowe"].relationships["orys"] = 3.0
    chance = disclosure.disclosure_chance(world, "crowe", ["orys"], "f_king_ill")
    assert chance <= disclosure.MAX_CHANCE < 1.0


def test_an_empty_room_carries_no_trust_term(world):
    assert disclosure.trust(world, "ferrow", []) == 0.0


# ── the roll ────────────────────────────────────────────────────────────────

def test_the_roll_reports_the_chance_it_used(world):
    disclosed, chance = disclosure.will_disclose(
        world, "ferrow", ["orys"], "f_king_ill", _Rng(0.0))
    assert disclosed is True
    assert 0.0 < chance <= disclosure.MAX_CHANCE


def test_a_high_roll_withholds(world):
    disclosed, _ = disclosure.will_disclose(
        world, "ollivar", ["orys"], "f_king_poisoned", _Rng(0.99))
    assert disclosed is False


def test_the_player_gets_no_special_case(world):
    """The same gate runs NPC->NPC in an empty corridor and NPC->player mid-scene.
    Identical relationship, identical fact, identical odds."""
    world.characters["ferrow"].relationships["orys"] = 1.0
    world.characters["ferrow"].relationships["stagg"] = 1.0
    to_player = disclosure.disclosure_chance(world, "ferrow", ["orys"], "f_king_ill")
    to_npc = disclosure.disclosure_chance(world, "ferrow", ["stagg"], "f_king_ill")
    assert to_player == to_npc
