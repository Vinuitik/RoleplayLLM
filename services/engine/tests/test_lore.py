"""Tests for just-in-time truth.

The failure being prevented is the one that makes LLM roleplay feel like sand:
the player asks how many guards hold the city, a model invents "two thousand"
inside its prose, and that number now exists only in a transcript. Nobody else
in the world knows it, nothing can contradict it, and the next character asked
says three hundred.

So the engine establishes a REAL fact instead — with a real `is_true`, held by
whoever would plausibly know it — and from that instant it behaves like anything
worldgen authored.
"""

import json
from pathlib import Path

import pytest

from app import llm, lore
from app.models import Stance, WorldState

SEED = Path(__file__).resolve().parents[1] / "app" / "world" / "seed.json"


@pytest.fixture
def world() -> WorldState:
    raw = json.loads(SEED.read_text(encoding="utf-8"))
    return WorldState.model_validate(
        {k: v for k, v in raw.items() if not k.startswith("_comment")})


@pytest.fixture
def establishes(monkeypatch):
    """Stub the establishing call, counting how often it actually fires."""
    calls = []

    def fake(prompt, system, required_keys=(), capability="text",
             priority="medium"):
        calls.append(prompt)
        return {
            "truth": "The City Watch numbers two thousand one hundred men.",
            "lie": "The City Watch numbers six thousand men.",
            "tags": ["watch"],
            "knowers": ["crowe"],
            "misled": ["orys"],
            "reasoning": "the Commander would know his own strength",
        }

    monkeypatch.setattr(llm, "complete_json", fake)
    return calls


# ── the truth becomes real ──────────────────────────────────────────────────

def test_a_gap_becomes_a_real_fact_with_a_truth_value(world, establishes):
    true_id, _false_id, _log = lore.establish(world, "How many guards are there?")
    assert true_id in world.facts
    fact = world.facts[true_id]
    assert fact.is_true is True
    assert "two thousand one hundred" in fact.content


def test_the_lie_is_created_alongside_the_truth(world, establishes):
    """Without a false counterpart, `truthful: false` has nothing to assert and
    deception silently degrades into an unconvincing truth."""
    true_id, false_id, _log = lore.establish(world, "How many guards are there?")
    assert false_id in world.facts
    assert world.facts[false_id].is_true is False
    assert false_id in world.facts[true_id].contradicts
    assert true_id in world.facts[false_id].contradicts


def test_the_truth_is_granted_to_someone_who_would_know_it(world, establishes):
    """A truth nobody holds is not part of the world — it is a row waiting for
    someone to be able to say it."""
    true_id, _f, _log = lore.establish(world, "How many guards are there?")
    held = world.believes("crowe", true_id)
    assert held is not None
    assert held.stance is Stance.KNOWS


def test_someone_can_be_misled_from_the_start(world, establishes):
    _t, false_id, _log = lore.establish(world, "How many guards are there?")
    assert world.believes("orys", false_id) is not None


def test_an_established_fact_can_immediately_be_lied_about(world, establishes):
    """The payoff: the Commander lies about the number, and the real one still
    exists, held by someone, findable later. The lie has something to be caught
    against."""
    from app.plots import propagate

    true_id, false_id, _log = lore.establish(world, "How many guards are there?")
    world.beliefs = [b for b in world.beliefs if b.char_id != "stagg"]
    propagate(world, "crowe", "stagg", true_id, truthful=False)

    assert world.believes("stagg", false_id) is not None
    assert world.believes("stagg", true_id) is None
    assert world.facts[true_id].is_true is True      # the truth is untouched


def test_an_established_fact_is_projected_like_any_other(world, establishes):
    from app.projection import project

    true_id, _f, _log = lore.establish(world, "How many guards are there?")
    knower = project(world, "crowe")
    assert any("two thousand one hundred" in b.content for b in knower.beliefs)
    # And it does not leak to someone who was never told.
    assert not any(b.fact_id == true_id for b in project(world, "sela").beliefs)


# ── idempotency: the whole game ─────────────────────────────────────────────

def test_the_same_question_never_mints_a_second_truth(world, establishes):
    first, _f1, _l1 = lore.establish(world, "How many guards are there?")
    second, _f2, _l2 = lore.establish(world, "How many guards are there?")
    assert first == second
    assert len(establishes) == 1, "the model was asked twice about one truth"


def test_asking_three_characters_interrogates_one_truth(world, establishes):
    """Without this the module is just a more expensive way to be inconsistent."""
    ids = {lore.establish(world, q)[0] for q in (
        "How many guards are there?",
        "how many guards are there",
        "There are how many guards?",
    )}
    assert len(ids) == 1
    assert len(establishes) == 1


def test_topic_keys_ignore_word_order_and_filler():
    assert lore.topic_key("How many guards are there?") == \
           lore.topic_key("there are how many guards")


def test_different_questions_get_different_truths(world, establishes):
    first, _f, _l = lore.establish(world, "How many guards are there?")
    second, _f2, _l2 = lore.establish(world, "How deep is the harbour?")
    assert first != second
    assert len(establishes) == 2


# ── restraint ───────────────────────────────────────────────────────────────

def test_a_question_the_record_already_covers_is_not_re_established(world):
    """Establishing lore should be the exception, not the reflex."""
    assert lore.has_coverage(world, "Is the king ill? He has been bedridden.")


def test_an_uncovered_question_is_detected(world):
    assert not lore.has_coverage(world, "How deep is the harbour at Gulltown?")


def test_generation_is_capped(world, establishes, monkeypatch):
    """A session establishing hundreds of facts has stopped being a scenario."""
    monkeypatch.setattr(lore, "MAX_ESTABLISHED_PER_GAME", 2)
    lore.establish(world, "question one about ships")
    lore.establish(world, "question two about horses")
    third, _f, log = lore.establish(world, "question three about grain")
    assert third is None
    assert any("cap reached" in line for line in log)


def test_an_outage_degrades_to_no_fact_rather_than_a_bad_one(world, monkeypatch):
    def dead(*args, **kwargs):
        raise llm.LLMUnavailable("all providers exhausted")

    monkeypatch.setattr(llm, "complete_json", dead)
    true_id, false_id, log = lore.establish(world, "How many guards are there?")
    assert true_id is None and false_id is None
    assert any("could not establish" in line for line in log)
    assert world.lore_index == {}          # nothing half-written


def test_a_model_naming_nobody_real_still_produces_a_holder(world, monkeypatch):
    """A truth with no holder would be unsayable and undiscoverable."""
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {
        "truth": "The City Watch numbers two thousand men.",
        "lie": "The City Watch numbers six thousand men.",
        "knowers": ["a_character_that_does_not_exist"], "misled": [],
    })
    true_id, _f, log = lore.establish(world, "How many guards are there?")
    assert world.holders_of(true_id), "nobody holds the established truth"
    assert any("inferred" in line for line in log)


def test_the_inferred_holder_is_the_plausible_one(world, monkeypatch):
    """Crude title matching, but it catches the case that matters: the
    Commander of the City Watch knows the strength of the City Watch."""
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {
        "truth": "The City Watch commander keeps a reserve of horse.",
        "lie": "There is no reserve.", "knowers": [], "misled": [],
    })
    true_id, _f, _log = lore.establish(world, "Is there a mounted reserve?")
    holders = [b.char_id for b in world.holders_of(true_id)]
    assert "crowe" in holders          # Commander of the City Watch


def test_an_empty_question_establishes_nothing(world, establishes):
    assert lore.establish(world, "   ")[0] is None
    assert not establishes


# ── amendment: facts that events can move, and the lock that stops the rest ──

def test_a_settled_proposition_cannot_be_amended(world):
    """The lock. An LLM able to amend arbitrary facts would eventually amend the
    murder it committed, and it would look like a legitimate state update."""
    with pytest.raises(lore.AmendmentRefused):
        lore.amend(world, "f_king_poisoned",
                   "The king died peacefully of old age.", cause="a lie")
    assert world.facts["f_king_poisoned"].content.startswith("The king's illness is poison")


def test_immutability_is_the_default(world):
    assert all(not f.mutable for f in world.facts.values())


def test_a_standing_quantity_is_marked_mutable_at_creation(world, monkeypatch):
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {
        "truth": "The City Watch numbers two thousand one hundred men.",
        "lie": "The City Watch numbers six thousand men.",
        "knowers": ["crowe"], "standing": True})
    true_id, false_id, _log = lore.establish(world, "How many guards are there?")
    assert world.facts[true_id].mutable is True
    # The LIE is not a standing quantity — nothing should be able to amend it.
    assert world.facts[false_id].mutable is False


def test_a_settled_answer_is_not_marked_mutable(world, monkeypatch):
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {
        "truth": "The harbour chain was forged in Braavos.",
        "lie": "The harbour chain was forged here.",
        "knowers": ["crowe"], "standing": False})
    true_id, _f, _log = lore.establish(world, "Where was the chain forged?")
    assert world.facts[true_id].mutable is False


def test_an_event_can_move_a_standing_quantity(world, monkeypatch):
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {
        "truth": "The City Watch numbers two thousand one hundred men.",
        "lie": "six thousand", "knowers": ["crowe"], "standing": True})
    true_id, _f, _log = lore.establish(world, "How many guards are there?")

    log = lore.amend(world, true_id,
                     "The City Watch numbers one thousand four hundred men.",
                     cause="losses at the Mud Gate")
    assert "one thousand four hundred" in world.facts[true_id].content
    assert world.facts[true_id].revision == 1
    assert any("amend" in line for line in log)


def test_amendment_makes_existing_knowledge_STALE_not_updated(world, monkeypatch):
    """The important half. Silently updating every holder would hand the whole
    world free omniscience every time a number moved — the Commander two hundred
    miles away should not learn his own casualties by magic."""
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {
        "truth": "The City Watch numbers two thousand one hundred men.",
        "lie": "six thousand", "knowers": ["crowe"], "standing": True})
    true_id, _f, _log = lore.establish(world, "How many guards are there?")
    assert world.believes("crowe", true_id).stance is Stance.KNOWS

    lore.amend(world, true_id, "The City Watch numbers one thousand men.",
               cause="desertion")

    held = world.believes("crowe", true_id)
    assert held is not None                      # not erased
    assert held.stance is Stance.SUSPECTS        # but no longer certain
    assert held.confidence < 1.0


def test_a_locked_fact_refuses_amendment_even_when_mutable(world, monkeypatch):
    """For a scenario that wants to pin a load-bearing number."""
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {
        "truth": "The wall is three hundred feet high.", "lie": "two hundred",
        "knowers": ["crowe"], "standing": True})
    true_id, _f, _log = lore.establish(world, "How high is the wall?")
    world.facts[true_id].locked = True

    with pytest.raises(lore.AmendmentRefused):
        lore.amend(world, true_id, "The wall is ten feet high.", cause="nonsense")


def test_the_engine_can_force_past_the_lock(world):
    """`force` is for engine-internal callers — combat applying its own
    casualties — and is unreachable from any model-supplied path."""
    lore.amend(world, "f_watch_unpaid",
               "The City Watch has gone three months without pay.",
               cause="another month", force=True)
    assert "three months" in world.facts["f_watch_unpaid"].content


def test_amending_an_unknown_fact_is_refused(world):
    with pytest.raises(lore.AmendmentRefused):
        lore.amend(world, "f_nope", "anything", cause="x", force=True)


def test_amending_to_nothing_is_refused(world):
    with pytest.raises(lore.AmendmentRefused):
        lore.amend(world, "f_watch_unpaid", "   ", cause="x", force=True)
