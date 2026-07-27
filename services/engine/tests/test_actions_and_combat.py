"""Tests for the action vocabulary as data, and for engagement resolution.

The point of the battle module is not that the game needs battles. It is proof
that the vocabulary refactor worked: a completely different kind of scenario
plugs in as a table plus a resolver, and touches neither projection, nor
beliefs, nor scenes, nor a single prompt.
"""

import random

import pytest

from app import actions as actions_mod
from app import combat
from app.combat import Force, resolve_engagement
from app.resolution import make_rng

COURT = actions_mod.default_table()


# ── the vocabulary is data ──────────────────────────────────────────────────

def test_the_default_table_loads_the_court_vocabulary():
    assert "scheme" in COURT.names()
    assert COURT.hours_for("travel") > COURT.hours_for("speak")


def test_an_unknown_action_degrades_instead_of_raising():
    """A hallucinated action name should cost the turn a little accuracy, never
    halt it — the same contract every other model-facing lookup honours."""
    row = COURT.get("perform_interpretive_dance")
    assert row.actor_stat and row.opposing_stat
    assert actions_mod.MIN_HOURS <= COURT.hours_for("nonsense") <= actions_mod.MAX_HOURS


def test_hours_are_clamped_however_absurd_the_table_is():
    table = actions_mod.from_rows([
        {"id": "blink", "hours": 0.0},
        {"id": "aeon", "hours": 99999.0},
    ])
    assert table.hours_for("blink") >= actions_mod.MIN_HOURS
    assert table.hours_for("aeon") <= actions_mod.MAX_HOURS


def test_a_scenario_can_replace_the_vocabulary_entirely():
    """This is the whole point. A battle table shares no action ids with the
    court table, and the engine does not care."""
    battle = actions_mod.from_rows([
        {"id": "charge", "hours": 0.2, "actor_stat": "might",
         "opposing_stat": "resolve", "tags": ["engagement"]},
        {"id": "hold", "hours": 1.0, "actor_stat": "resolve",
         "opposing_stat": "might", "tags": ["engagement"]},
        {"id": "scout", "hours": 2.0, "actor_stat": "wits",
         "opposing_stat": "guile", "tags": ["discovery"]},
    ])
    assert battle.get("charge").actor_stat == "might"
    assert battle.has_tag("scout", "discovery")
    assert not battle.has_tag("charge", "discovery")
    assert "scheme" not in battle.names()


def test_tags_drive_subsystems_so_new_ones_break_nothing():
    table = actions_mod.from_rows([{"id": "x", "tags": ["not_a_real_subsystem"]}])
    assert table.has_tag("x", "not_a_real_subsystem")
    assert not table.has_tag("x", "discovery")


# ── engagements ─────────────────────────────────────────────────────────────

def _force(**kw) -> Force:
    base = dict(id="a", name="Force", men=1000, quality=2, morale=1.0,
                posture="open", commander_stat=2)
    base.update(kw)
    return Force(**base)


def test_fortification_is_worth_more_than_numbers():
    """The entire reason anyone builds a wall. A dug-in defender should beat a
    materially larger force in the open."""
    dug_in = _force(id="d", men=1000, posture="fortified")
    open_field = _force(id="a", men=2000, posture="open")
    assert dug_in.effective() > open_field.effective()


def test_quality_and_morale_multiply_rather_than_add():
    levies = _force(men=2000, quality=1, morale=0.5)
    knights = _force(men=1000, quality=5, morale=1.0)
    assert knights.effective() > levies.effective()


def test_a_broken_force_is_worth_nothing_regardless_of_headcount():
    assert _force(men=5000, morale=0.0).effective() == 0.0


def test_medieval_attrition_is_linear_not_square():
    """Lanchester's LINEAR law is the pre-gunpowder case: melee is
    frontage-limited, so a numerical edge does not compound. Under the square
    law doubling your army would roughly quadruple the enemy's losses; under the
    linear law it should stay roughly proportional."""
    assert combat.ATTRITION_EXPONENT == 1.0

    even = resolve_engagement(_force(id="a", men=1000, commander_stat=3),
                              _force(id="d", men=1000), make_rng(4))
    double = resolve_engagement(_force(id="a", men=2000, commander_stat=3),
                                _force(id="d", men=1000), make_rng(4))
    # Doubling helps, but nowhere near quadratically.
    assert double.defender_losses > even.defender_losses
    assert double.defender_losses < even.defender_losses * 4


def test_numbers_alone_do_not_decide_a_medieval_battle():
    """The medieval result: ground and quality matter more than headcount, which
    is exactly what the linear law is supposed to produce."""
    wins = 0
    for seed in range(60):
        result = resolve_engagement(
            _force(id="a", men=3000, quality=2, posture="open", commander_stat=2),
            _force(id="d", men=1000, quality=4, posture="fortified",
                   commander_stat=4),
            make_rng(seed))
        wins += result.attacker_won
    assert 0 < wins < 55, f"three-to-one in the open swept a fortified elite: {wins}/60"


def test_dice_still_decide_the_day():
    """Same forces, different seeds, different outcomes. The ratio sets the
    problem; resolution.py returns the verdict — as everywhere else."""
    outcomes = {resolve_engagement(_force(id="a"), _force(id="d"),
                                   make_rng(s)).attacker_won
                for s in range(40)}
    assert outcomes == {True, False}


def test_an_engagement_is_deterministic_for_a_given_seed():
    """Rewind has to reproduce a battle exactly, same as any other turn."""
    a = resolve_engagement(_force(id="a"), _force(id="d"), make_rng("x"))
    b = resolve_engagement(_force(id="a"), _force(id="d"), make_rng("x"))
    assert a.model_dump() == b.model_dump()


def test_the_winner_still_bleeds():
    """A battle where the victor loses nothing is a cutscene."""
    for seed in range(30):
        result = resolve_engagement(
            _force(id="a", men=4000, quality=4, commander_stat=5),
            _force(id="d", men=500, quality=1, morale=0.6), make_rng(seed))
        if result.attacker_won:
            assert result.attacker_losses > 0
            return
    pytest.fail("the attacker never won a lopsided fight")


def test_losses_never_exceed_the_men_present():
    for seed in range(50):
        result = resolve_engagement(_force(id="a", men=100),
                                    _force(id="d", men=100), make_rng(seed))
        assert 0 <= result.attacker_losses <= 100
        assert 0 <= result.defender_losses <= 100


def test_applying_a_result_routs_a_broken_force():
    attacker = _force(id="a", men=5000, quality=5, commander_stat=5)
    defender = _force(id="d", men=200, quality=1, morale=0.2)
    result = resolve_engagement(attacker, defender, make_rng(2))
    combat.apply_result(attacker, defender, result)
    assert defender.men <= 200
    assert 0.0 <= defender.morale <= 1.0


def test_resolution_and_application_are_separate():
    """So a battle can be previewed, logged or rewound without being committed."""
    attacker, defender = _force(id="a"), _force(id="d")
    before = (attacker.men, defender.men)
    resolve_engagement(attacker, defender, make_rng(1))
    assert (attacker.men, defender.men) == before
