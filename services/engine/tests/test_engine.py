"""Tests for the deterministic layers — resolution, meters, tick.

No LLM is involved anywhere below, which is the point: every consequence in the
game is decided by code that can be tested exactly.
"""

import json
from pathlib import Path

import pytest

from app.formula import FormulaError, evaluate, validate
from app.models import ModifierKind, Stance, WorldState
from app.numerics import (add_modifier, advance_meters, apply_pending_modifiers,
                          set_rate_formula)
from app.plots import TALK_CEILING, propagate, tick
from app.resolution import (Degree, difficulty_from_label, make_rng, resolve,
                            resolve_opposed)

SEED = Path(__file__).resolve().parents[1] / "app" / "world" / "seed.json"


@pytest.fixture
def world() -> WorldState:
    raw = json.loads(SEED.read_text(encoding="utf-8"))
    return WorldState.model_validate(
        {k: v for k, v in raw.items() if not k.startswith("_comment")})


# ── resolution ──────────────────────────────────────────────────────────────

def test_same_seed_gives_identical_rolls():
    """Rewind depends on this. If replaying a snapshot produced different rolls,
    save/load would be a lie rather than a feature."""
    a = [resolve(3, 14, make_rng(1234)).roll for _ in range(1)]
    b = [resolve(3, 14, make_rng(1234)).roll for _ in range(1)]
    assert a == b

    rng1, rng2 = make_rng("orys"), make_rng("orys")
    assert [resolve(2, 12, rng1).total for _ in range(20)] == \
           [resolve(2, 12, rng2).total for _ in range(20)]


def test_different_seeds_diverge():
    rng1, rng2 = make_rng(1), make_rng(2)
    assert [resolve(2, 12, rng1).roll for _ in range(20)] != \
           [resolve(2, 12, rng2).roll for _ in range(20)]


def test_resolve_is_pure_no_global_rng_mutation():
    """Two concurrent games must not perturb each other's dice."""
    import random
    random.seed(999)
    before = random.random()
    random.seed(999)
    resolve(3, 10, make_rng(42))
    assert random.random() == before


def test_natural_twenty_always_crits_even_against_impossible_odds():
    """A hopeless attempt can still land — that is what keeps a scene tense."""
    rng = make_rng(0)
    for _ in range(500):
        outcome = resolve(1, 99, rng)
        if outcome.roll == 20:
            assert outcome.degree is Degree.CRITICAL_SUCCESS
            assert outcome.succeeded
            return
    pytest.fail("no natural 20 in 500 rolls — rng is wrong")


def test_natural_one_always_fails_even_with_overwhelming_advantage():
    rng = make_rng(0)
    for _ in range(500):
        outcome = resolve(5, 1, rng)
        if outcome.roll == 1:
            assert outcome.degree is Degree.CRITICAL_FAILURE
            assert not outcome.succeeded
            return
    pytest.fail("no natural 1 in 500 rolls — rng is wrong")


def test_partial_success_band_exists():
    """Four degrees, not two: 'you get the meeting, but Varys knows you wanted
    it' is where the interesting play lives."""
    rng = make_rng(7)
    degrees = {resolve(3, 15, rng).degree for _ in range(300)}
    assert Degree.PARTIAL in degrees
    assert Degree.SUCCESS in degrees
    assert Degree.FAILURE in degrees


def test_opposed_roll_is_a_near_coin_flip_when_evenly_matched():
    rng = make_rng(11)
    wins = sum(resolve_opposed(3, 3, rng).succeeded for _ in range(2000))
    assert 800 < wins < 1200, f"evenly-matched win rate looks wrong: {wins}/2000"


def test_difficulty_label_clamps_absurd_referee_output():
    """An LLM referee that returns DC 900 should produce a very hard check, not
    an unwinnable one."""
    assert difficulty_from_label("hard") == 18
    assert difficulty_from_label("nonsense") == 14      # falls back to moderate
    assert difficulty_from_label(900) == 26             # clamped to the ladder
    assert difficulty_from_label(-50) == 5
    assert difficulty_from_label(None) == 14


# ── formulas ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("attack", [
    "__import__('os').system('rm -rf /')",
    "().__class__.__bases__[0].__subclasses__()",
    "open('/etc/passwd').read()",
    "value.__class__.__mro__",
    "[x for x in range(99)]",
    "(lambda: 1)()",
    "exec('import os')",
    "globals()",
])
def test_formula_refuses_code_execution(attack):
    """LLM-authored formulas are untrusted input executing on the host. eval()
    here would be remote code execution in your own house."""
    with pytest.raises(FormulaError):
        evaluate(attack, {"value": 1.0})


def test_formula_refuses_runaway_exponent():
    """2**10**9 would hang the process — the one whitelisted op that can."""
    with pytest.raises(FormulaError):
        evaluate("2 ** 10 ** 9")


def test_formula_rejects_non_finite_results():
    """NaN/inf would silently poison a meter forever with no way back."""
    with pytest.raises(FormulaError):
        evaluate("1 / 0")


def test_formula_supports_what_rate_expressions_actually_need():
    assert evaluate("-0.25 - unrest * 0.05", {"unrest": 3}) == pytest.approx(-0.40)
    assert evaluate("0.04 if treasury < 60 else -0.01", {"treasury": 20}) == 0.04
    assert evaluate("0.04 if treasury < 60 else -0.01", {"treasury": 90}) == -0.01
    assert evaluate("clamp(value, 0, 10)", {"value": 42}) == 10


def test_validate_returns_message_instead_of_raising():
    """The authoring boundary wants an error string to feed back for repair."""
    assert validate("value * 2", {"value": 1}) is None
    assert "unknown name" in validate("wat * 2", {"value": 1})


# ── meters ──────────────────────────────────────────────────────────────────

def test_meters_advance_by_elapsed_time_not_turn_count(world):
    """A turn that skips half a day must move a meter by twelve hours."""
    start = world.meters["king_health"].value
    advance_meters(world, world.clock.absolute_hour + 12.0)
    assert world.meters["king_health"].value == pytest.approx(start - 0.12 * 12)


def test_advancing_is_idempotent(world):
    """Re-running a tick, or rewinding and replaying, must never double-count."""
    target = world.clock.absolute_hour + 6.0
    advance_meters(world, target)
    once = world.meters["king_health"].value
    advance_meters(world, target)
    advance_meters(world, target)
    assert world.meters["king_health"].value == once


def test_meter_step_is_order_independent(world):
    """Meters reference each other (treasury <-> unrest). If rates were computed
    from partially-updated values, dict order would change the outcome and the
    same save would evolve differently after a reload."""
    import copy
    a = copy.deepcopy(world)
    b = copy.deepcopy(world)
    b.meters = dict(reversed(list(b.meters.items())))

    advance_meters(a, a.clock.absolute_hour + 10.0)
    advance_meters(b, b.clock.absolute_hour + 10.0)
    for meter_id in a.meters:
        assert a.meters[meter_id].value == pytest.approx(b.meters[meter_id].value)


def test_meters_respect_min_and_max(world):
    world.meters["unrest"].value = 9.9
    world.meters["treasury"].value = 0.0   # drives unrest up hard
    advance_meters(world, world.clock.absolute_hour + 500.0)
    assert world.meters["unrest"].value <= 10.0
    assert world.meters["treasury"].value >= 0.0


def test_bad_formula_freezes_one_meter_without_halting_the_game(world):
    """A hallucinated formula must never end the session."""
    world.meters["unrest"].rate_formula = "explode(("
    events = advance_meters(world, world.clock.absolute_hour + 5.0)
    assert any("formula error" in e for e in events)
    assert world.meters["king_health"].value < 42.0   # others still advanced


# ── modifiers ───────────────────────────────────────────────────────────────

def test_flat_modifier_fires_once_not_every_tick(world):
    add_modifier(world, "standing", ModifierKind.FLAT, 10.0, source="saved the watch")
    apply_pending_modifiers(world)
    assert world.meters["standing"].value == 60.0
    for _ in range(5):
        apply_pending_modifiers(world)
    assert world.meters["standing"].value == 60.0


def test_rate_modifiers_stack_multiplicatively(world):
    """Two -50% debuffs should quarter a rate, not zero it — and three must not
    reverse its sign."""
    add_modifier(world, "king_health", ModifierKind.RATE_PCT, -0.5)
    add_modifier(world, "king_health", ModifierKind.RATE_PCT, -0.5)
    start = world.meters["king_health"].value
    advance_meters(world, world.clock.absolute_hour + 10.0)
    assert world.meters["king_health"].value == pytest.approx(start - 0.12 * 10 * 0.25)


def test_modifier_expires(world):
    add_modifier(world, "standing", ModifierKind.RATE_PCT, 1.0, duration_turns=2)
    assert len(world.modifiers) == 1
    world.clock.turn += 5
    from app.numerics import prune_expired_modifiers
    prune_expired_modifiers(world)
    assert world.modifiers == []


def test_modifier_on_unknown_meter_is_refused(world):
    """A hallucinated meter name must not quietly spawn state nothing else knows
    about."""
    assert add_modifier(world, "swagger", ModifierKind.FLAT, 5.0) is None
    assert "swagger" not in world.meters


def test_set_rate_formula_validates_before_accepting(world):
    original = world.meters["unrest"].rate_formula
    error = set_rate_formula(world, "unrest", "__import__('os')")
    assert error is not None
    assert world.meters["unrest"].rate_formula == original   # unchanged

    assert set_rate_formula(world, "unrest", "0.02 * treasury") is None
    assert world.meters["unrest"].rate_formula == "0.02 * treasury"


# ── tick ────────────────────────────────────────────────────────────────────

def test_world_moves_without_the_player_acting(world):
    """The difference between a simulation and a chatbot."""
    rng = make_rng(3)
    before_health = world.meters["king_health"].value
    for _ in range(10):
        tick(world, rng, hours=3.0)
    assert world.clock.turn == 10
    assert world.meters["king_health"].value < before_health
    assert world.clock.day >= 1


def test_plots_advance_and_eventually_expose(world):
    """Exposure is the only reason the player ever gets a thread to pull."""
    rng = make_rng(5)
    for _ in range(40):
        tick(world, rng, hours=2.0)
    assert world.plots["dragonstone"].stage > 0
    assert any("exposure" in line for line in world.chronicle)


def test_exposure_grants_suspicion_never_certainty(world):
    """A leak must hand someone a doubt they can act on, not a certainty that
    ends the mystery."""
    rng = make_rng(5)
    conspirators = set(world.plots["dragonstone"].members)
    for _ in range(40):
        tick(world, rng, hours=2.0)
    for belief in world.beliefs:
        if belief.fact_id == "f_king_poisoned" and belief.char_id not in conspirators:
            if belief.turn_acquired > 0:
                assert belief.stance is Stance.SUSPECTS
                assert belief.confidence < 0.6


def test_clock_rolls_over_days_correctly(world):
    rng = make_rng(1)
    world.clock.hour = 22.0
    tick(world, rng, hours=5.0)
    assert world.clock.day == 1
    assert world.clock.hour == pytest.approx(3.0)


# ── deception ───────────────────────────────────────────────────────────────

def test_a_lie_records_its_source_so_it_can_be_unwound_later(world):
    """This is why beliefs are a join table and not known_by[]: discover the liar
    and every belief sourced to them becomes re-examinable."""
    assert propagate(world, "stagg", "orys", "f_stagg_blames_war", truthful=False)
    belief = world.believes("orys", "f_stagg_blames_war")
    assert belief.source_char_id == "stagg"
    assert belief.stance is Stance.SUSPECTS
    assert world.facts["f_stagg_blames_war"].is_true is False


def test_hearing_a_rumour_twice_does_not_duplicate_the_edge(world):
    propagate(world, "wyl", "orys", "f_queen_lover", truthful=False)
    propagate(world, "ferrow", "orys", "f_queen_lover", truthful=False)
    edges = [b for b in world.beliefs
             if b.char_id == "orys" and b.fact_id == "f_queen_lover"]
    assert len(edges) == 1


def test_talk_alone_can_never_produce_certainty(world):
    """DELIBERATE REVERSAL of the old `confirmation promotes suspicion to
    knowledge` behaviour.

    Being told a thing — plainly, sincerely, by an eyewitness, twice — leaves you
    suspecting it and nothing more. Without this cap every conversation is a step
    toward everyone knowing everything, and the only remaining lever is making
    clues rare, which makes the game unwinnable rather than difficult. Certainty
    is bought with evidence; see test_evidence.py.
    """
    propagate(world, "sela", "orys", "f_king_poisoned", truthful=False)
    assert world.believes("orys", "f_king_poisoned").stance is Stance.SUSPECTS
    propagate(world, "queen", "orys", "f_king_poisoned", truthful=True)

    belief = world.believes("orys", "f_king_poisoned")
    assert belief.stance is Stance.SUSPECTS
    assert belief.confidence <= TALK_CEILING


def test_no_amount_of_repetition_breaks_the_ceiling(world):
    for speaker in ("sela", "queen", "ollivar", "wyl", "ferrow", "stagg"):
        propagate(world, speaker, "orys", "f_king_poisoned", truthful=True)
    belief = world.believes("orys", "f_king_poisoned")
    assert belief.stance is Stance.SUSPECTS
    assert belief.confidence <= TALK_CEILING


def test_a_lie_lands_softer_than_the_truth(world):
    propagate(world, "stagg", "orys", "f_stagg_blames_war", truthful=False)
    propagate(world, "stagg", "crowe", "f_stagg_blames_war", truthful=True)
    assert (world.believes("orys", "f_stagg_blames_war").confidence
            < world.believes("crowe", "f_stagg_blames_war").confidence)
