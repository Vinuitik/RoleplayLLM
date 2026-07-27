"""Tests for the telemetry layer.

The property that matters most is the boring one: **telemetry can never break a
turn**. A diagnostic system that can take down the thing it is diagnosing is
worse than no diagnostic system, so every failure path is checked.

After that, the point is that engine-side repairs are COUNTED. The engine
already forgives an out-of-range belief index, an unparseable hours value, an
unknown action — and forgiving them silently is exactly what makes a model's
degradation invisible.
"""

import json
from pathlib import Path

import pytest

from app.models import WorldState

SEED = Path(__file__).resolve().parents[1] / "app" / "world" / "seed.json"


@pytest.fixture
def telemetry(tmp_path, monkeypatch):
    """A fresh database per test, so counts are exact."""
    import importlib

    from app import store
    monkeypatch.setattr(store, "DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    module = importlib.import_module("app.telemetry")
    importlib.reload(module)
    monkeypatch.setattr(module, "DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    module._engine = None
    module.init_db()
    return module


@pytest.fixture
def world() -> WorldState:
    raw = json.loads(SEED.read_text(encoding="utf-8"))
    return WorldState.model_validate(
        {k: v for k, v in raw.items() if not k.startswith("_comment")})


# ── it must never break a turn ──────────────────────────────────────────────

def test_a_broken_database_does_not_raise(telemetry, monkeypatch):
    """THE load-bearing property."""
    monkeypatch.setattr(telemetry, "_get_engine",
                        lambda: (_ for _ in ()).throw(RuntimeError("disk on fire")))
    telemetry.record("intention", "p", "r")            # must not raise
    assert telemetry.game_log("anything") == []
    assert telemetry.health() == {}


def test_an_exception_inside_a_timed_block_is_still_recorded(telemetry):
    """An outage is exactly the kind of degradation this exists to catch, so the
    row has to be written on the failure path too."""
    with telemetry.turn_context("g", 1):
        with pytest.raises(ValueError):
            with telemetry.timed("intention", "prompt"):
                raise ValueError("provider exploded")

    rows = telemetry.game_log("g")
    assert len(rows) == 1
    assert rows[0]["ok"] is False
    assert "provider exploded" in rows[0]["error"]


# ── grouping and chronology ─────────────────────────────────────────────────

def test_calls_read_back_grouped_by_game_in_chronological_order(telemetry):
    with telemetry.turn_context("game_a", 1):
        telemetry.record("referee", "p", "r")
        telemetry.record("intention", "p", "r", char_id="ferrow")
        telemetry.record("narration", "p", "r")
    with telemetry.turn_context("game_a", 2):
        telemetry.record("referee", "p", "r")
    with telemetry.turn_context("game_b", 1):
        telemetry.record("referee", "p", "r")

    rows = telemetry.game_log("game_a")
    assert len(rows) == 4
    assert [r["kind"] for r in rows] == ["referee", "intention", "narration",
                                         "referee"]
    assert [(r["turn"], r["seq"]) for r in rows] == [(1, 1), (1, 2), (1, 3), (2, 1)]


def test_one_games_log_never_contains_another(telemetry):
    with telemetry.turn_context("game_a", 1):
        telemetry.record("referee", "p", "r")
    with telemetry.turn_context("game_b", 1):
        telemetry.record("referee", "p", "r")
    assert len(telemetry.game_log("game_b")) == 1


# ── the hallucination counter ───────────────────────────────────────────────

def test_engine_side_repairs_are_counted_not_just_forgiven(telemetry, world,
                                                           monkeypatch):
    """The turn survives a hallucinated belief index and a garbled hours value.
    Both must show up as violations, or a model losing the plot looks exactly
    like a quiet NPC."""
    from app import llm, turn as turn_mod
    monkeypatch.setattr(llm, "telemetry", telemetry)

    def fake_json(prompt, system, required_keys=(), capability="text",
                  priority="medium"):
        if "rules referee" in system.lower():
            return {"difficulty": "moderate", "opposing_stat": "guile",
                    "archetype": "search", "hours_elapsed": 2}
        return {"action": "speak", "target": "Byren Stagg",
                "reveals_belief": 999, "truthful": True,
                "hours_elapsed": "sometime next week"}

    monkeypatch.setattr(llm, "complete_json", fake_json)
    monkeypatch.setattr(llm, "complete_text",
                        lambda p, s, capability="narrate", priority="high": "prose")

    with telemetry.turn_context("g", 1):
        turn_mod.play_turn(world, "search the study", seed=1, scene_passes=1)

    rows = telemetry.game_log("g")
    intentions = [r for r in rows if r["kind"] == "intention"]
    assert intentions, "no NPC intention was recorded"
    kinds = json.loads(intentions[0]["violation_kinds"])
    assert "belief_index_out_of_range" in kinds
    assert "bad_hours" in kinds


def test_a_clean_reply_records_no_violations(telemetry, world, monkeypatch):
    from app import llm
    from app.projection import project
    monkeypatch.setattr(llm, "telemetry", telemetry)
    monkeypatch.setattr(llm, "complete_json",
                        lambda *a, **k: {"action": "observe",
                                         "reveals_belief": -1,
                                         "hours_elapsed": 0.5})
    with telemetry.turn_context("g", 1):
        llm.get_intention(project(world, "ferrow"))
    assert telemetry.game_log("g")[0]["violations"] == 0


def test_a_provider_outage_is_recorded_as_a_violation(telemetry, world,
                                                      monkeypatch):
    """A benched provider falling through to a weaker model is the commonest
    cause of unexplained quality loss, so it must be visible."""
    from app import llm
    from app.projection import project
    monkeypatch.setattr(llm, "telemetry", telemetry)

    def dead(*args, **kwargs):
        raise llm.LLMUnavailable("all providers exhausted")

    monkeypatch.setattr(llm, "complete_json", dead)
    with telemetry.turn_context("g", 1):
        intention = llm.get_intention(project(world, "ferrow"))

    assert intention.action == "observe"          # turn survived
    row = telemetry.game_log("g")[0]
    assert row["ok"] is False
    assert "unavailable" in row["violation_kinds"]


# ── the aggregates ──────────────────────────────────────────────────────────

def test_health_breaks_down_by_provider_kind_and_prompt_length(telemetry):
    """Breakdowns rather than one score: a quality drop is almost always
    localised, and an average is exactly what hides that."""
    with telemetry.turn_context("g", 1):
        telemetry.record("intention", "x" * 500, "r", provider="groq")
        telemetry.record("intention", "x" * 5000, "r", provider="groq",
                         violation_kinds=["belief_index_out_of_range"])
        telemetry.record("narration", "x" * 500, "r", provider="claude-cli")

    health = telemetry.health("g")
    assert health["calls"] == 3
    assert health["violations"] == 1
    assert health["by_provider"]["groq"]["calls"] == 2
    assert health["by_kind"]["narration"]["violations"] == 0
    # The band that most needs data: where does reliability fall off?
    assert health["by_prompt_length"]["<8000"]["violation_rate"] == 1.0
    assert health["by_prompt_length"]["<1000"]["violation_rate"] == 0.0


def test_health_on_an_empty_database_is_harmless(telemetry):
    assert telemetry.health("nobody")["calls"] == 0
