"""Session Zero — building a world before anyone plays in it.

The reason an orchestrator hallucinates is that it is asked to invent *and* to
remember at the same time, under latency pressure, from a partial view. So it is
asked to do neither at play time. Worldgen is a cold, unhurried pass that runs
once per new game and writes the whole world out as DATA.

    worldgen (once, slow, Opus)  ->  world.json
    play     (every turn, fast)  ->  references what worldgen wrote

At play time the contract is **reference, don't invent**, and it is enforced
structurally rather than by instruction — the same absence principle that
governs everything else here:

1. **Every id is validated against what exists.** A plot naming a member who was
   never created, a belief in a fact that does not exist, a `contradicts` edge
   pointing into the void — all dropped at load, and reported. Inventing an
   entity by referencing it is not forbidden, it simply does not happen.

2. **`canon` goes into every orchestrator prompt.** A world where dragons are a
   folk story and one where they are artillery need different plans, and a
   planner should never have to guess which it is in.

The generator is asked for a scenario, not a story: entities, facts with truth
values, who believes what, plots, meters, and an action vocabulary. What happens
is then the engine's business — worldgen has no way to express an outcome, which
is the same reason the orchestrator has no tool that returns one.

## On repair rather than rejection

A model producing a 200-line world will get some of it wrong. Rejecting the
whole payload for one dangling id would make generation a lottery. Every problem
found is repaired to the nearest safe state and *recorded*, so a scenario that
generated badly is visible in the report rather than silently thin.
"""

from __future__ import annotations

import json
import uuid

from pydantic import ValidationError

from . import llm, telemetry
from .models import WorldState

# Small enough that a model can hold the whole world in its head while writing
# it, which is what keeps the parts consistent with each other.
DEFAULT_CHARACTERS = 7
DEFAULT_FACTS = 14

WORLDGEN_SYSTEM = (
    "You are building the starting state of a simulation, not writing a story. "
    "You produce entities, facts, beliefs and schemes as structured data. You "
    "never decide what happens — a dice engine and a turn loop do that. Output "
    "exactly one JSON object and nothing else."
)


def worldgen_prompt(premise: str, characters: int = DEFAULT_CHARACTERS,
                    facts: int = DEFAULT_FACTS) -> str:
    return f"""Build the opening state of a simulation from this premise:

"{premise}"

Rules that make this work mechanically. Read them as constraints, not style notes.

FACTS are propositions with a hidden truth value. Phrase `content` NEUTRALLY, as
the world would state it ("the king was poisoned"), never as someone's opinion
("X thinks the king was poisoned") — the same fact is held by many characters,
each at their own confidence.

Include COMFORTABLE LIES: false facts (`is_true: false`) that contradict true
ones. Name the pairing in `contradicts`. These are what let characters deceive
each other — a liar asserts the contradiction of what they know. A world with no
false facts is a world where nobody can lie.

BELIEFS are edges: who holds which fact, how firmly. Most characters should be
WRONG about something important. Give at least one character a confident false
belief sourced to whoever told them.

Not every fact needs a holder. A fact nobody believes yet is a discovery waiting
to happen, and those are the good ones.

CHARACTERS need conflicting goals. `hides` lists fact ids they actively conceal;
`candor` (0..1) is how freely they talk at all; `relationships` runs -3..+3.

PLOTS advance on their own whether or not the player acts. `stage_facts` lists
one fact id per stage that becomes true as it progresses.

METERS are numbers that evolve by formula. `rate_formula` may use: value, hours,
day, turn, and other meter ids. Arithmetic only.

ACTIONS are the verbs this scenario needs. A court needs `scheme` and `confront`;
a battle needs `charge` and `hold`. `actor_stat` and `opposing_stat` must each be
one of: might, guile, presence, wits, resolve.

Reply with ONE JSON object:

{{
  "canon": "2-3 sentences: tone, what is possible in this world, what is not",
  "player_id": "id of the character the player controls",
  "locations": ["snake_case_place", ...],
  "actions": [
    {{"id":"verb","hours":1.0,"actor_stat":"wits","opposing_stat":"resolve","tags":[]}}
  ],
  "facts": [
    {{"id":"f_snake_case","content":"neutral statement","is_true":true,
      "tags":["theme"],"contradicts":[]}}
  ],
  "characters": [
    {{"id":"snake_case","name":"Name","title":"Role",
      "stats":{{"might":2,"guile":3,"presence":4,"wits":4,"resolve":3}},
      "wants":["..."],"fears":["..."],"hides":["f_..."],"candor":0.4,
      "relationships":{{"other_id":2}},"location":"snake_case_place"}}
  ],
  "beliefs": [
    {{"char_id":"...","fact_id":"f_...","stance":"knows|suspects",
      "confidence":0.9,"source_char_id":null}}
  ],
  "plots": [
    {{"id":"...","name":"...","stages":["...","..."],"members":["..."],
      "advance_chance":0.25,"exposure_chance":0.3,"stage_facts":["f_..."]}}
  ],
  "meters": [
    {{"id":"...","label":"...","value":50,"rate_formula":"-0.1",
      "min":0,"max":100,"visible_to_player":true}}
  ]
}}

Aim for about {characters} characters and {facts} facts. Every id referenced
anywhere must be defined above — a dangling id is dropped, and the world is
poorer for it."""


def _index(rows: list, key: str = "id") -> dict:
    out = {}
    for row in rows or []:
        if isinstance(row, dict) and row.get(key):
            out[str(row[key])] = row
    return out


def build_world(spec: dict) -> tuple[WorldState, list[str]]:
    """Turn a generated spec into a validated WorldState.

    Returns (world, report). The report names every repair — a scenario that
    generated badly should be visible, not silently thin.
    """
    report: list[str] = []

    facts = _index(spec.get("facts", []))
    characters = _index(spec.get("characters", []))
    if not characters:
        raise ValueError("worldgen produced no characters")

    # Referential integrity, in dependency order. Each pass drops what points at
    # something that does not exist — inventing by reference simply does not work.
    for fact_id, fact in facts.items():
        kept = [c for c in fact.get("contradicts", []) if c in facts]
        if len(kept) != len(fact.get("contradicts", [])):
            report.append(f"fact {fact_id}: dropped dangling contradicts")
        fact["contradicts"] = kept

    locations = set(spec.get("locations") or [])
    for char_id, character in characters.items():
        hides = [f for f in character.get("hides", []) if f in facts]
        if len(hides) != len(character.get("hides", [])):
            report.append(f"character {char_id}: dropped hides for unknown facts")
        character["hides"] = hides

        character["relationships"] = {
            other: value for other, value in (character.get("relationships") or {}).items()
            if other in characters}

        if character.get("location") not in locations:
            fallback = sorted(locations)[0] if locations else "start"
            report.append(f"character {char_id}: unknown location, moved to {fallback}")
            character["location"] = fallback
        character.setdefault("candor", 0.5)

    beliefs = []
    for belief in spec.get("beliefs", []) or []:
        if not isinstance(belief, dict):
            continue
        if belief.get("char_id") not in characters or belief.get("fact_id") not in facts:
            report.append(f"belief {belief.get('char_id')}/{belief.get('fact_id')}: "
                          f"dangling, dropped")
            continue
        if belief.get("source_char_id") not in characters:
            belief["source_char_id"] = None
        beliefs.append(belief)

    plots = {}
    for plot_id, plot in _index(spec.get("plots", [])).items():
        plot["members"] = [m for m in plot.get("members", []) if m in characters]
        plot["stage_facts"] = [f for f in plot.get("stage_facts", []) if f in facts]
        if not plot["members"]:
            report.append(f"plot {plot_id}: no valid members, dropped")
            continue
        plots[plot_id] = plot

    player_id = spec.get("player_id")
    if player_id not in characters:
        player_id = sorted(characters)[0]
        report.append(f"player_id was not a real character; using {player_id}")

    world = WorldState.model_validate({
        "player_id": player_id,
        "canon": (spec.get("canon") or "")[:2000],
        "facts": facts,
        "characters": characters,
        "beliefs": beliefs,
        "plots": plots,
        "meters": _index(spec.get("meters", [])),
        "actions": [a for a in (spec.get("actions") or [])
                    if isinstance(a, dict) and a.get("id")],
    })

    # A world where nobody can lie is a world with no intrigue in it at all.
    if not any(f.get("is_true") is False for f in facts.values()):
        report.append("WARNING: no false facts — nothing in this world can be a lie")
    if not any(f.get("contradicts") for f in facts.values()):
        report.append("WARNING: no contradiction pairs — deliberate "
                      "misinformation is impossible in this world")
    return world, report


def generate(premise: str, characters: int = DEFAULT_CHARACTERS,
             facts: int = DEFAULT_FACTS) -> tuple[WorldState, list[str]]:
    """Run the pre-game pass. Slow and expensive by design — it happens once.

    Uses the `orchestrate` capability chain, which leads with claude-cli: this is
    the one call where reasoning quality decides the quality of everything that
    follows, and the player is not waiting on a turn while it runs.
    """
    prompt = worldgen_prompt(premise, characters, facts)
    with telemetry.timed("worldgen", prompt) as slot:
        raw = llm.complete_json(
            prompt, WORLDGEN_SYSTEM,
            required_keys=("characters", "facts"),
            capability="orchestrate", priority="high")
        slot.finish(json.dumps(raw)[:20000])

    try:
        world, report = build_world(raw)
    except (ValidationError, ValueError) as exc:
        raise ValueError(f"worldgen produced an unusable world: {exc}") from None

    if not world.meters:
        report.append("no meters generated; the world has no pressure of its own")
    return world, report


def save(world: WorldState, path) -> str:
    """Freeze a generated world to disk as a seed file.

    This is what makes generated and hand-authored scenarios the same thing: the
    output of worldgen is exactly the format you would write by hand, so it can
    be edited, diffed, replayed and shared.
    """
    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(world.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8")
    return str(target)


def new_scenario_id() -> str:
    return f"gen_{uuid.uuid4().hex[:8]}"
