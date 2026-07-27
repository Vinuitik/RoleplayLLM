"""FastAPI surface. Thin on purpose — all the thinking lives in the engine modules.

Note what is NOT here: no endpoint returns a raw WorldState to the browser except
/dm, and /dm exists precisely so you can audit the engine. Everything the player
sees goes through projection first, so the UI cannot leak what the engine kept.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import llm, store, telemetry, worldgen
from .models import WorldState
from .projection import project
from .turn import play_turn

SEED_FILE = Path(__file__).resolve().parent / "world" / "seed.json"

app = FastAPI(title="RoleplayLLM engine", version="0.1.0")

# nginx serves the UI same-origin in production, so this only matters for `vite
# dev` on another port.
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    store.init_db()
    telemetry.init_db()


def load_seed_world() -> WorldState:
    raw = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    # _comment_* keys are authoring notes for whoever edits the world by hand.
    return WorldState.model_validate(
        {k: v for k, v in raw.items() if not k.startswith("_comment")})


# ── requests ────────────────────────────────────────────────────────────────

class NewGame(BaseModel):
    title: str = ""
    # Blank = random. Fixed = the same dice every playthrough, for debugging.
    seed: str = ""


class Action(BaseModel):
    text: str
    # Offstage scenes cost ZERO tokens, so this is no longer a budget switch —
    # it exists to freeze the wider world while debugging one room.
    conversations: bool = True
    # The one real cost dial: the onstage group scene runs
    # `people_present x scene_passes` cheap calls. 1 for a fast turn, 0 to fall
    # back to one-line-each NPC reactions.
    scene_passes: int = 3


# ── health ──────────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/providers")
def providers() -> dict:
    """Proxied from the wrapper so the DM panel can explain a stalled turn
    ('everything is on a 429 bench') instead of it looking like an engine bug."""
    return llm.providers_status()


# ── games ───────────────────────────────────────────────────────────────────

@app.post("/games")
def new_game(request: NewGame) -> dict:
    world = load_seed_world()
    seed = request.seed or uuid.uuid4().hex[:8]
    game_id = store.create_game(world, seed=seed, title=request.title)
    view = project(world, world.player_id)
    return {"game_id": game_id, "seed": seed,
            "turn": 0, "time_of_day": view.time_of_day,
            "narration": _opening_scene(view),
            "suggested_actions": [
                "Ask Grand Maester Ollivar about the king's illness",
                "Press Lord Stagg on the treasury ledgers",
                "Visit the king in his chambers",
            ]}


def _opening_scene(view) -> str:
    """Hand-written, not generated: the first thing the player reads should be
    reliable, and it costs an LLM call we don't need to spend."""
    return (
        f"{view.time_of_day}. The small council chamber smells of cold wax.\n\n"
        f"You are {view.self_name}, {view.self_title} — nine days in the office "
        f"and already the realm feels like something held together by hand. The "
        f"king has not left his bed in eleven days. Grand Maester Ollivar says "
        f"it is age and a hard winter.\n\n"
        f"Across the table, Byren Stagg has not opened the ledgers he brought. "
        f"Mycella Ferrow is watching you, and has been for some time.")


class GeneratedGame(BaseModel):
    premise: str
    title: str = ""
    seed: str = ""
    characters: int = 7
    facts: int = 14
    # Write the generated world out as a seed file too, so it can be edited,
    # diffed and replayed like a hand-authored scenario.
    save_as: str = ""


@app.post("/games/generate")
def generate_game(request: GeneratedGame) -> dict:
    """Session Zero. Build a whole world from a premise, then start a game in it.

    Slow and expensive by design — it runs once, on the `orchestrate` chain, and
    the player is not waiting on a turn while it happens. Everything the
    orchestrator later references was written here, which is what stops it
    inventing entities mid-game.

    `report` lists every repair made to the generated spec: dangling ids that
    were dropped, fields that had to fall back. A scenario that generated badly
    should be visible rather than silently thin.
    """
    with telemetry.turn_context("worldgen", 0):
        try:
            world, report = worldgen.generate(
                request.premise, request.characters, request.facts)
        except (ValueError, llm.LLMUnavailable) as exc:
            raise HTTPException(502, f"worldgen failed: {exc}") from None

    saved = ""
    if request.save_as:
        safe = "".join(c for c in request.save_as if c.isalnum() or c in "-_")
        saved = worldgen.save(world, SEED_FILE.parent / f"{safe}.json")

    seed = request.seed or uuid.uuid4().hex[:8]
    game_id = store.create_game(world, seed=seed,
                                title=request.title or request.premise[:60])
    view = project(world, world.player_id)
    return {
        "game_id": game_id,
        "seed": seed,
        "turn": 0,
        "time_of_day": view.time_of_day,
        "canon": world.canon,
        "report": report,
        "saved_to": saved,
        "narration": f"{view.time_of_day}. You are {view.self_name}"
                     + (f", {view.self_title}." if view.self_title else "."),
        "suggested_actions": ["Look around", "Speak to whoever is here", "Wait"],
    }


@app.get("/games")
def games() -> list[dict]:
    return store.list_games()


@app.delete("/games/{game_id}")
def remove_game(game_id: str) -> dict:
    store.delete_game(game_id)
    return {"deleted": game_id}


@app.get("/games/{game_id}/history")
def game_history(game_id: str) -> list[dict]:
    return store.history(game_id)


@app.post("/games/{game_id}/turn")
def take_turn(game_id: str, action: Action) -> dict:
    world = store.load_world(game_id)
    if world is None:
        raise HTTPException(404, "no such game")

    # Every model call inside this block is tagged with (game, turn) and given a
    # sequence number, so the log reads back as the turn actually ran.
    with telemetry.turn_context(game_id, world.clock.turn + 1):
        result = play_turn(world, action.text, seed=store.get_seed(game_id),
                           enable_conversations=action.conversations,
                           scene_passes=action.scene_passes)
    store.save_snapshot(game_id, world, result.narration, result.dm_log,
                        action.text)
    return result.model_dump()


@app.get("/games/{game_id}/telemetry")
def game_telemetry(game_id: str, limit: int = 500) -> dict:
    """Every model call in this game, in the order it happened.

    Grouped by game and ordered by (turn, seq), so a session reads back
    chronologically — interleaved across NPCs, referee and narrator, exactly as
    it ran. This is the view for "something broke on turn 12, what did the
    models actually see and say".
    """
    return {"game_id": game_id, "calls": telemetry.game_log(game_id, limit)}


@app.get("/telemetry/health")
def telemetry_health(game_id: str = "") -> dict:
    """Aggregates for 'has it got worse, and where'.

    Breakdowns rather than a single score, because a quality drop is almost
    always localised to one provider, one call kind, or one prompt-length band —
    and an average is exactly what hides that.
    """
    return telemetry.health(game_id or None)


@app.post("/games/{game_id}/rewind/{to_turn}")
def rewind_game(game_id: str, to_turn: int) -> dict:
    world = store.rewind(game_id, to_turn)
    if world is None:
        raise HTTPException(404, "no snapshot at that turn")
    view = project(world, world.player_id)
    return {"turn": world.clock.turn, "time_of_day": view.time_of_day,
            "history": store.history(game_id)}


# ── the DM view ─────────────────────────────────────────────────────────────

@app.get("/games/{game_id}/dm")
def dm_view(game_id: str) -> dict:
    """Full unfiltered state beside every character's projection.

    The single best tool for proving to yourself the hidden state never leaks:
    if a secret shows up in the wrong projection here, you have found the bug
    before the player found the exploit.
    """
    world = store.load_world(game_id)
    if world is None:
        raise HTTPException(404, "no such game")

    projections = {}
    for char_id, character in world.characters.items():
        view = project(world, char_id)
        projections[char_id] = {
            "name": character.name,
            "location": character.location,
            "alive": character.alive,
            "knows": [{"content": b.content, "stance": b.stance.value,
                       "confidence": b.confidence, "source": b.source}
                      for b in view.beliefs],
        }

    return {
        "turn": world.clock.turn,
        "time_of_day": project(world, world.player_id).time_of_day,
        # The unfiltered truth — this endpoint is the ONLY place it is served,
        # and it is what the DM panel exists to show.
        "truth": {
            "facts": [{"id": f.id, "content": f.content, "is_true": f.is_true}
                      for f in world.facts.values()],
            "plots": [{"name": p.name, "stage": p.stage,
                       "stage_name": (p.stages[p.stage] if p.stage < len(p.stages)
                                      else "done"),
                       "members": [world.characters[m].name for m in p.members
                                   if m in world.characters],
                       "active": p.active}
                      for p in world.plots.values()],
            "meters": [{"id": m.id, "label": m.label, "value": round(m.value, 2),
                        "formula": m.rate_formula,
                        "hidden": not m.visible_to_player}
                       for m in world.meters.values()],
            "modifiers": [m.model_dump() for m in world.modifiers],
        },
        "projections": projections,
        "chronicle": world.chronicle[-60:],
    }


@app.get("/games/{game_id}/truth")
def truth(game_id: str) -> dict:
    """Post-game report: what was true beside what you believed, and who lied.

    Deliberately a separate endpoint from /dm — this is the payoff you read after
    a run, and the thing that makes the whole hidden-state machinery visible.
    """
    world = store.load_world(game_id)
    if world is None:
        raise HTTPException(404, "no such game")
    return store.truth_report(world)
