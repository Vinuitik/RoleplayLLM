"""Tests for Session Zero.

The whole point of a pre-game pass is that the orchestrator stops inventing at
play time — so these tests are almost entirely about REFERENTIAL INTEGRITY. A
generated world that references entities it never created is exactly the failure
worldgen exists to prevent, and it must be caught at build time rather than
surfacing as a mystery on turn nine.

The generator itself is never called here; `build_world` is pure.
"""

import pytest

from app.worldgen import build_world


def _spec(**overrides) -> dict:
    spec = {
        "canon": "A besieged city. No magic. Winter is the real enemy.",
        "player_id": "captain",
        "locations": ["gatehouse", "keep"],
        "actions": [{"id": "hold", "hours": 1.0, "actor_stat": "resolve",
                     "opposing_stat": "might", "tags": ["engagement"]}],
        "facts": [
            {"id": "f_walls_cracked", "content": "The east wall is failing.",
             "is_true": True, "tags": ["siege"], "contradicts": ["f_walls_sound"]},
            {"id": "f_walls_sound", "content": "The walls will hold the winter.",
             "is_true": False, "tags": ["comfortable-lie"]},
        ],
        "characters": [
            {"id": "captain", "name": "Captain", "location": "gatehouse",
             "hides": [], "relationships": {"mason": 1}, "candor": 0.6},
            {"id": "mason", "name": "Mason", "location": "keep",
             "hides": ["f_walls_cracked"], "relationships": {"captain": 0}},
        ],
        "beliefs": [
            {"char_id": "mason", "fact_id": "f_walls_cracked",
             "stance": "knows", "confidence": 1.0},
            {"char_id": "captain", "fact_id": "f_walls_sound",
             "stance": "knows", "confidence": 1.0, "source_char_id": "mason"},
        ],
        "plots": [{"id": "collapse", "name": "The wall gives",
                   "stages": ["cracks", "breach"], "members": ["mason"],
                   "stage_facts": ["f_walls_cracked"]}],
        "meters": [{"id": "grain", "label": "Grain", "value": 80,
                    "rate_formula": "-0.2", "min": 0, "max": 100}],
    }
    spec.update(overrides)
    return spec


# ── the happy path ──────────────────────────────────────────────────────────

def test_a_well_formed_spec_builds_cleanly():
    world, report = build_world(_spec())
    assert world.player_id == "captain"
    assert len(world.characters) == 2
    assert world.canon
    assert not [r for r in report if not r.startswith("WARNING")]


def test_the_generated_action_vocabulary_reaches_the_world():
    """A battle scenario ships `hold`, not `scheme`, and needs no engine change."""
    world, _ = build_world(_spec())
    assert [a.id for a in world.actions] == ["hold"]


def test_canon_is_carried_for_the_orchestrator():
    world, _ = build_world(_spec())
    assert "No magic" in world.canon


# ── referential integrity: inventing by reference does not work ─────────────

def test_a_belief_in_a_nonexistent_fact_is_dropped():
    spec = _spec()
    spec["beliefs"].append({"char_id": "mason", "fact_id": "f_never_defined",
                            "stance": "knows", "confidence": 1.0})
    world, report = build_world(spec)
    assert all(b.fact_id != "f_never_defined" for b in world.beliefs)
    assert any("dangling" in r for r in report)


def test_a_belief_held_by_a_nonexistent_character_is_dropped():
    spec = _spec()
    spec["beliefs"].append({"char_id": "ghost", "fact_id": "f_walls_sound",
                            "stance": "knows", "confidence": 1.0})
    world, _ = build_world(spec)
    assert all(b.char_id != "ghost" for b in world.beliefs)


def test_a_dangling_contradiction_edge_is_dropped():
    spec = _spec()
    spec["facts"][0]["contradicts"] = ["f_walls_sound", "f_imaginary"]
    world, report = build_world(spec)
    assert world.facts["f_walls_cracked"].contradicts == ["f_walls_sound"]
    assert any("contradicts" in r for r in report)


def test_hiding_a_fact_that_does_not_exist_is_dropped():
    """This one already bit the project once through `hides`, from the other
    direction — an authored id that leaked content into a prompt."""
    spec = _spec()
    spec["characters"][1]["hides"] = ["f_walls_cracked", "f_imaginary"]
    world, _ = build_world(spec)
    assert world.characters["mason"].hides == ["f_walls_cracked"]


def test_a_relationship_with_a_stranger_is_dropped():
    spec = _spec()
    spec["characters"][0]["relationships"] = {"mason": 1, "nobody": -3}
    world, _ = build_world(spec)
    assert "nobody" not in world.characters["captain"].relationships


def test_a_plot_with_no_real_members_is_dropped():
    spec = _spec()
    spec["plots"] = [{"id": "phantom", "name": "Nothing", "stages": ["a"],
                      "members": ["ghost"], "stage_facts": []}]
    world, report = build_world(spec)
    assert "phantom" not in world.plots
    assert any("no valid members" in r for r in report)


def test_a_plot_keeps_only_stage_facts_that_exist():
    spec = _spec()
    spec["plots"][0]["stage_facts"] = ["f_walls_cracked", "f_imaginary"]
    world, _ = build_world(spec)
    assert world.plots["collapse"].stage_facts == ["f_walls_cracked"]


def test_an_unknown_location_is_repaired_not_rejected():
    """One bad field must not cost the whole generated world."""
    spec = _spec()
    spec["characters"][0]["location"] = "atlantis"
    world, report = build_world(spec)
    assert world.characters["captain"].location in ("gatehouse", "keep")
    assert any("unknown location" in r for r in report)


def test_a_bogus_player_id_falls_back_to_a_real_character():
    spec = _spec()
    spec["player_id"] = "nobody_at_all"
    world, report = build_world(spec)
    assert world.player_id in world.characters
    assert any("player_id" in r for r in report)


def test_a_world_with_no_characters_is_refused():
    """Repair has a floor. There is nothing to salvage here."""
    with pytest.raises(ValueError):
        build_world(_spec(characters=[]))


# ── the warnings that matter for play ───────────────────────────────────────

def test_a_world_where_nothing_can_be_a_lie_is_flagged():
    spec = _spec()
    for fact in spec["facts"]:
        fact["is_true"] = True
        fact["contradicts"] = []
    _world, report = build_world(spec)
    assert any("nothing in this world can be a lie" in r for r in report)


def test_a_world_with_no_contradiction_pairs_is_flagged():
    spec = _spec()
    spec["facts"][0]["contradicts"] = []
    _world, report = build_world(spec)
    assert any("misinformation is impossible" in r for r in report)


# ── the generated world is a real world ─────────────────────────────────────

def test_a_generated_world_survives_projection():
    """The acid test: everything downstream must work on it unchanged."""
    from app.projection import project

    world, _ = build_world(_spec())
    view = project(world, "captain")
    assert view.self_name == "Captain"
    # The captain believes the comfortable lie and does not know the truth.
    contents = [b.content for b in view.beliefs]
    assert "The walls will hold the winter." in contents
    assert "The east wall is failing." not in contents


def test_a_generated_world_supports_misinformation_end_to_end():
    from app.plots import propagate

    world, _ = build_world(_spec())
    # The mason, who knows the wall is failing, tells the captain otherwise.
    propagate(world, "mason", "captain", "f_walls_cracked", truthful=False)
    assert world.believes("captain", "f_walls_sound") is not None
