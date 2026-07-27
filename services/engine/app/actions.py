"""The action vocabulary, loaded from data.

This module exists because three small hardcoded dicts were the entire reason
"court intrigue" and "resolve a battle" looked like they needed different
codebases:

    gametime.ARCHETYPE_HOURS      how long each kind of act takes
    turn._contest.stat_by_action  what an NPC rolls to do it
    turn._actor_stat_for          what the player rolls against a resisting stat

Everything else in the engine — projection, belief edges, meters, dice, the
scene relay — never inspects what an action *is*. Those three dicts did, and so
they were the lock-in. They are now one table, per scenario, on disk.

Deliberately NOT an abstraction over actions. There is no effect system, no
handler registry, no plugin protocol. An action is a row: how long, who rolls
what, and some free-form tags that optional subsystems may notice. Tags are
ignored if nothing reads them, which is what lets a new scenario add
`"tags": ["engagement"]` without any existing scenario caring.

The fallback table is the court vocabulary, so a world shipped without an
actions.json behaves exactly as the engine did before this module existed.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

# Hard band on elapsed time, enforced regardless of what a table claims. Below
# the floor a turn would leave the clock frozen; above the ceiling one
# hallucinated number skips the whole game.
MIN_HOURS = 0.05
MAX_HOURS = 72.0

DEFAULT_ACTION = "speak"


class Action(BaseModel):
    id: str
    hours: float = 1.0
    actor_stat: str = "wits"
    opposing_stat: str = "resolve"
    tags: list[str] = Field(default_factory=list)


class ActionTable(BaseModel):
    """The vocabulary one scenario speaks. Lookups never raise: an unknown
    action name is a model hallucinating, and a hallucination should cost the
    turn a little accuracy rather than halt it."""

    actions: dict[str, Action] = Field(default_factory=dict)

    def get(self, action_id: str) -> Action:
        key = (action_id or "").strip().lower()
        if key in self.actions:
            return self.actions[key]
        if DEFAULT_ACTION in self.actions:
            return self.actions[DEFAULT_ACTION]
        return Action(id=key or DEFAULT_ACTION)

    def hours_for(self, action_id: str) -> float:
        return max(MIN_HOURS, min(MAX_HOURS, self.get(action_id).hours))

    def has_tag(self, action_id: str, tag: str) -> bool:
        return tag in self.get(action_id).tags

    def names(self) -> list[str]:
        return list(self.actions)

    def prompt_hint(self) -> str:
        """The line the referee and intention prompts quote, so a model's
        estimates land in a familiar range instead of being invented."""
        return ", ".join(f"{a.id} (~{a.hours}h)" for a in self.actions.values())


def load(path: str | Path) -> ActionTable:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = raw.get("actions", []) if isinstance(raw, dict) else raw
    table = ActionTable()
    for row in rows:
        action = Action.model_validate(row)
        table.actions[action.id.strip().lower()] = action
    return table


def from_rows(rows: list[dict]) -> ActionTable:
    """Build a table from rows already in memory — used by worldgen, which
    produces the vocabulary alongside the world rather than reading it back."""
    table = ActionTable()
    for row in rows:
        action = Action.model_validate(row)
        table.actions[action.id.strip().lower()] = action
    return table


# The court vocabulary, used when a scenario ships no table of its own. Keeping
# a built-in default is what makes this module a refactor rather than a
# migration: nothing that worked before needs a new file to keep working.
_FALLBACK_ROWS = [
    {"id": "speak", "hours": 0.25, "actor_stat": "presence", "opposing_stat": "resolve"},
    {"id": "converse", "hours": 0.75, "actor_stat": "presence", "opposing_stat": "resolve"},
    {"id": "observe", "hours": 0.5, "actor_stat": "wits", "opposing_stat": "guile"},
    {"id": "search", "hours": 2.0, "actor_stat": "wits", "opposing_stat": "guile",
     "tags": ["discovery"]},
    {"id": "move", "hours": 1.0, "actor_stat": "might", "opposing_stat": "resolve"},
    {"id": "travel", "hours": 12.0, "actor_stat": "might", "opposing_stat": "resolve"},
    {"id": "scheme", "hours": 3.0, "actor_stat": "guile", "opposing_stat": "wits"},
    {"id": "confront", "hours": 0.5, "actor_stat": "presence", "opposing_stat": "resolve"},
    {"id": "rest", "hours": 8.0, "actor_stat": "resolve", "opposing_stat": "resolve"},
    {"id": "wait", "hours": 4.0, "actor_stat": "wits", "opposing_stat": "wits"},
]

FALLBACK = from_rows(_FALLBACK_ROWS)


def default_table() -> ActionTable:
    """The table used when a world does not carry one."""
    here = Path(__file__).resolve().parent / "world" / "actions.json"
    if here.exists():
        try:
            return load(here)
        except (OSError, ValueError):
            pass
    return FALLBACK
