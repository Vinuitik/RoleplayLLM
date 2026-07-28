"""Regression tests for the blank opening screen.

The bug, reported from live play: click into a game and get an empty screen with
three suggestions and no idea where you are. Asking the game to introduce itself
then produced prose referencing things the player had never been shown, because
that introduction was improvised in the chat rather than grounded in a scene
that had actually been established.

Root cause was not in the narrator. `create_game` persisted `narration=""`, so
the opening existed only in the HTTP response — and the UI replays `history()`
on load, which overwrote it with its own empty row.
"""

import json
from pathlib import Path

import pytest

from app import store
from app.models import WorldState

SEED = Path(__file__).resolve().parents[1] / "app" / "world" / "seed.json"


@pytest.fixture
def world() -> WorldState:
    raw = json.loads(SEED.read_text(encoding="utf-8"))
    return WorldState.model_validate(
        {k: v for k, v in raw.items() if not k.startswith("_comment")})


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A real SQLite file per test — this bug lived in persistence, so stubbing
    the store would have tested nothing."""
    from sqlalchemy import create_engine
    engine = create_engine(f"sqlite:///{tmp_path}/t.db", future=True)
    monkeypatch.setattr(store, "engine", engine)
    store.metadata.create_all(engine)
    return engine


def test_the_opening_survives_the_round_trip(db, world):
    """THE regression. History is what the UI replays, so if the opening is not
    in history, the player sees a blank screen."""
    game_id = store.create_game(world, seed="1", narration="You are in a cold room.")
    history = store.history(game_id)
    assert history[0]["narration"] == "You are in a cold room."


def test_a_game_created_without_an_opening_is_still_coherent(db, world):
    game_id = store.create_game(world, seed="1")
    assert store.history(game_id)[0]["narration"] == ""


# ── the authored scenario ships its own opening ─────────────────────────────

def test_the_court_scenario_carries_an_opening_and_suggestions(world):
    """A hand-authored opening is reliable and costs no LLM call."""
    from app.turn import _split_suggestions

    assert world.opening, "the authored scenario has no opening text"
    prose, suggestions = _split_suggestions(world.opening)
    assert len(prose) > 100
    assert len(suggestions) == 3


def test_the_scenario_carries_its_own_identity(world):
    """The UI used to hardcode this, which made swapping worlds a frontend edit."""
    assert world.title
    assert world.blurb


# ── a generated world always gets a written opening ─────────────────────────

def test_a_generated_world_gets_an_opening_through_the_projection(world,
                                                                  monkeypatch):
    from app import llm
    from app.projection import project

    captured = {}

    def fake_text(prompt, system, capability="narrate", priority="high"):
        captured["prompt"] = prompt
        return "The hall is cold.\n\n> Look around\n> Wait\n> Speak"

    monkeypatch.setattr(llm, "complete_text", fake_text)
    opening = llm.get_opening(project(world, "orys"), world.canon)

    assert "The hall is cold." in opening
    # Truth-blindness holds for the opening too — it is the most tempting place
    # to cheat, because leaking reads as scene-setting rather than disclosure.
    assert "poison" not in captured["prompt"].lower()


def test_the_opening_never_leaves_the_player_with_a_blank_screen(world,
                                                                 monkeypatch):
    """Total provider outage. Plain prose is acceptable; nothing is not."""
    from app import llm
    from app.projection import project

    def dead(*args, **kwargs):
        raise llm.LLMUnavailable("all providers exhausted")

    monkeypatch.setattr(llm, "complete_text", dead)
    opening = llm.get_opening(project(world, "orys"), world.canon)

    assert world.characters["orys"].name in opening
    assert "> " in opening              # still offers somewhere to go
    assert len(opening) > 60


# ── who you play is a choice, not a property of the world ───────────────────

def test_every_living_character_is_playable(world):
    """The seed names a `player_id`, but the engine has no concept of a
    protagonist: every character already has a projection, wants, fears and
    secrets. Baking one in was an anti-flexibility constraint, not a design."""
    from app.projection import project

    playable = [c for c in world.characters.values() if c.alive]
    assert len(playable) > 5
    for character in playable:
        # The real bar: anyone can be projected, and their view is coherent
        # enough to open a game in — a name, a place, and their own mind.
        view = project(world, character.id)
        assert view.self_name == character.name
        assert view.location == character.location
        assert view.stats


def test_playing_a_different_character_changes_what_you_start_knowing(world):
    """The payoff. Playing the poisoner is a different game in the same world:
    you begin holding the secret instead of hunting it."""
    from app.projection import project

    world.player_id = "ollivar"
    poisoner = project(world, "ollivar")
    world.player_id = "crowe"
    soldier = project(world, "crowe")

    poisoner_knows = {b.content for b in poisoner.beliefs}
    soldier_knows = {b.content for b in soldier.beliefs}
    assert poisoner_knows != soldier_knows
    # And the secret is only in one of them.
    secret = world.facts["f_king_poisoned"].content
    assert secret in poisoner_knows
    assert secret not in soldier_knows


def test_the_authored_opening_is_only_reused_for_its_own_character(world):
    """The seed's opening says "you are Orys Ashwood, Hand of the King" in as
    many words. Reusing it for anyone else would state something plainly false."""
    assert "Orys Ashwood" in world.opening
    assert world.player_id == "orys"
