"""Client for the host-wrapper, plus every prompt the game sends.

The wrapper owns provider routing and credentials; this module owns *what we ask
for* and *what we refuse to accept back*. Three rules run through all of it:

1. **The model only ever sees a ProjectedWorld.** Never `WorldState`. Enforced by
   the type signatures below — nothing here takes a WorldState at all.

2. **Facts are referenced by INDEX, never by content.** A speaker gets a numbered
   list of their own beliefs and returns the number of the one they act on. They
   cannot name a fact they do not hold, because the protocol gives them no way to
   express one. This is the same absence principle as projection, applied to
   output instead of input.

3. **Every reply is a proposal.** It is validated against a Pydantic model here,
   then adjudicated by dice in resolution.py. A malformed reply degrades to a
   safe default; it never halts the turn.
"""

from __future__ import annotations

import os

import requests
from pydantic import BaseModel, Field, ValidationError

from .gametime import ARCHETYPE_HOURS
from .projection import ProjectedWorld

WRAPPER_URL = os.environ.get("WRAPPER_URL", "http://host.docker.internal:5501")
REQUEST_TIMEOUT_S = int(os.environ.get("LLM_REQUEST_TIMEOUT_S", "180"))

# Sentinel for "the speaker chose to reveal nothing" — the model must have a way
# to decline, or it will invent something to fill the slot.
NO_FACT = -1


class LLMUnavailable(RuntimeError):
    """Every provider failed. The caller degrades; it never crashes the turn."""


# ── schemas we accept back ──────────────────────────────────────────────────

class Intention(BaseModel):
    """What an NPC proposes to do. Proposal only — dice decide the outcome."""

    action: str = Field(default="observe")
    target: str = ""
    # Index into the numbered belief list the prompt supplied, or NO_FACT.
    reveals_belief: int = NO_FACT
    truthful: bool = True
    rationale: str = ""
    hours_elapsed: float | str | None = None
    speech: str = ""


class RefereeCall(BaseModel):
    """The LLM as rules-lawyer for a novel player action: it sets the problem,
    the dice give the verdict."""

    difficulty: str | int = "moderate"
    opposing_stat: str = "wits"
    archetype: str = "speak"
    hours_elapsed: float | str | None = None
    reasoning: str = ""


class MeterProposal(BaseModel):
    """A buff the fiction implies — 'the watch is paid, unrest should ease'."""

    meter_id: str
    kind: str = "rate_pct"
    magnitude: float = 0.0
    duration_turns: int | None = None
    source: str = ""


# ── transport ───────────────────────────────────────────────────────────────

def _post(path: str, payload: dict) -> dict:
    try:
        response = requests.post(f"{WRAPPER_URL}{path}", json=payload,
                                 timeout=REQUEST_TIMEOUT_S)
    except requests.RequestException as e:
        raise LLMUnavailable(f"wrapper unreachable at {WRAPPER_URL}: {e}") from None
    if response.status_code == 503:
        raise LLMUnavailable(response.json().get("error", "all providers exhausted"))
    if response.status_code >= 400:
        raise LLMUnavailable(f"wrapper HTTP {response.status_code}: {response.text[:200]}")
    return response.json()


def complete_json(prompt: str, system: str, required_keys: tuple[str, ...] = (),
                  capability: str = "text", priority: str = "medium") -> dict:
    return _post("/complete-json", {
        "prompt": prompt, "system": system, "required_keys": list(required_keys),
        "capability": capability, "priority": priority,
    })["data"]


def complete_text(prompt: str, system: str, capability: str = "narrate",
                  priority: str = "high") -> str:
    return _post("/complete", {
        "prompt": prompt, "system": system,
        "capability": capability, "priority": priority,
    })["text"]


def providers_status() -> dict:
    try:
        return requests.get(f"{WRAPPER_URL}/providers", timeout=10).json()
    except requests.RequestException as e:
        return {"error": str(e)}


# ── prompt construction ─────────────────────────────────────────────────────

def _belief_menu(view: ProjectedWorld) -> str:
    """The numbered list. This is the core trick.

    By giving the speaker indices into their OWN beliefs, "say something you
    don't know" becomes inexpressible rather than merely forbidden. Confidence is
    shown so they hedge a rumour and assert a certainty — and note there is no
    truth marker anywhere, because they don't have one either.
    """
    if not view.beliefs:
        return "  (you know nothing worth saying)"
    lines = []
    for i, belief in enumerate(view.beliefs):
        held = ("you are certain of this" if belief.stance.value == "knows"
                else f"you only suspect this (confidence {belief.confidence:.1f})")
        heard = f", heard from {belief.source}" if belief.source else ""
        lines.append(f"  [{i}] {belief.content} — {held}{heard}")
    return "\n".join(lines)


def _people(view: ProjectedWorld) -> str:
    if not view.present:
        return "  (you are alone)"
    return "\n".join(
        f"  - {p.name}, {p.title} (you regard them: {p.disposition:+.1f})"
        for p in view.present)


def _situation(view: ProjectedWorld) -> str:
    meters = "\n".join(f"  - {m.label}: {m.value}" for m in view.meters) or "  (nothing)"
    return (
        f"TIME: {view.time_of_day}\n"
        f"PLACE: {view.location}\n\n"
        f"YOU ARE: {view.self_name}, {view.self_title}\n"
        f"YOU WANT: {'; '.join(view.wants) or 'nothing in particular'}\n"
        f"YOU FEAR: {'; '.join(view.fears) or 'nothing'}\n"
        f"YOUR STATS (1-5): {view.stats}\n\n"
        f"PEOPLE HERE:\n{_people(view)}\n\n"
        f"WHAT YOU KNOW:\n{_belief_menu(view)}\n\n"
        f"YOU ARE HIDING: {'; '.join(view.concealing) or 'nothing'}\n\n"
        f"THE WORLD AS YOU SEE IT:\n{meters}"
    )


NPC_SYSTEM = (
    "You play one character in a political intrigue simulation. You decide only "
    "what your character INTENDS; a dice engine decides whether it works, and a "
    "separate narrator describes it. Never narrate outcomes, never invent facts, "
    "never mention anything not listed in WHAT YOU KNOW. Reply with one JSON "
    "object and nothing else."
)


def npc_intention_prompt(view: ProjectedWorld, situation_note: str = "") -> str:
    archetypes = ", ".join(f"{k} (~{v}h)" for k, v in ARCHETYPE_HOURS.items())
    return f"""{_situation(view)}

{f'JUST HAPPENED: {situation_note}' if situation_note else ''}

Decide what {view.self_name} does next, in character, driven by what they want
and fear. Reply with ONE JSON object:

{{
  "action":         one of: {archetypes.replace(' (~', ' (')},
  "target":         the NAME of the person you address, or "" for the room,
  "reveals_belief": the NUMBER in brackets of something from WHAT YOU KNOW that
                    you say aloud, or {NO_FACT} to reveal nothing. You may ONLY
                    use a number that appears above. EVERYONE listed in PEOPLE
                    HERE hears whatever you say — "target" is only who you look
                    at while saying it. Choosing to reveal nothing is a real and
                    often correct choice.
  "truthful":       true if you state it plainly, false if you twist it,
  "speech":         one short line of dialogue, or "",
  "hours_elapsed":  realistic hours this takes (see the archetype hints above),
  "rationale":      one clause on why — for the debug view, not the player
}}"""


REFEREE_SYSTEM = (
    "You are the rules referee for a tabletop simulation. You never decide "
    "whether an action succeeds — you only state how hard it is and what it "
    "costs in time. Dice do the rest. Reply with one JSON object and nothing else."
)


def referee_prompt(view: ProjectedWorld, action_text: str) -> str:
    return f"""{_situation(view)}

The player attempts: "{action_text}"

Rate the attempt. Reply with ONE JSON object:

{{
  "difficulty":    one of trivial, easy, moderate, hard, severe, near_impossible,
  "opposing_stat": which of might, guile, presence, wits, resolve resists it,
  "archetype":     one of {', '.join(ARCHETYPE_HOURS)},
  "hours_elapsed": realistic hours this attempt takes,
  "reasoning":     one clause, for the debug view
}}

Judge difficulty by the fiction, not by whether it would be a good story. An
attempt to do something impossible in this situation is near_impossible even if
it would be dramatic."""


NARRATOR_SYSTEM = (
    "You are the narrator of a political intrigue simulation. Everything you are "
    "given has ALREADY been decided by the engine. You describe what happened; "
    "you never decide it, never add events, and never introduce information not "
    "present in your input. Second person, present tense, under 180 words. Then "
    "offer three short suggested actions — hints, never limits."
)


def narrator_prompt(view: ProjectedWorld, resolved_events: list[str],
                    player_action: str = "") -> str:
    """The narrator's input is deliberately thin.

    It gets resolved events (true by construction, already filtered through the
    player's perception) and the player's own beliefs WITHOUT truth values. So it
    hedges exactly when the player genuinely doubts — never because it privately
    knows a belief is false. A narrator that knew would write "you *think* he
    died of old age", and that hedge tells the player there is something to doubt
    while disclosing no hidden fact at all.
    """
    events = "\n".join(f"  - {e}" for e in resolved_events) or "  - nothing of note"
    beliefs = "\n".join(
        f"  - {b.content}"
        + ("" if b.stance.value == "knows" else "  (they are unsure of this)")
        for b in view.beliefs[:12]) or "  - very little"

    return f"""TIME: {view.time_of_day}
PLACE: {view.location}
YOU ARE NARRATING TO: {view.self_name}, {view.self_title}

{f'THEY ATTEMPTED: {player_action}' if player_action else ''}

WHAT HAPPENED (already resolved — narrate ONLY these, add nothing):
{events}

PEOPLE PRESENT:
{_people(view)}

WHAT THEY BELIEVE (state plainly what they are sure of; you may hedge only where
marked unsure):
{beliefs}

Write the scene. Then a blank line, then exactly three suggested actions, one per
line, each starting with "> ". Do not invent events, people, or information."""


# ── validated calls ─────────────────────────────────────────────────────────

def get_intention(view: ProjectedWorld, situation_note: str = "") -> Intention:
    """Ask an NPC what it intends. Degrades to observing rather than failing —
    a provider outage should cost the world some colour, not end the session."""
    try:
        raw = complete_json(
            npc_intention_prompt(view, situation_note), NPC_SYSTEM,
            required_keys=("action",), capability="text", priority="medium")
        return Intention.model_validate(raw)
    except (LLMUnavailable, ValidationError):
        return Intention(action="observe", rationale="(no model available)")


def get_referee(view: ProjectedWorld, action_text: str) -> RefereeCall:
    try:
        raw = complete_json(
            referee_prompt(view, action_text), REFEREE_SYSTEM,
            required_keys=("difficulty",), capability="text", priority="high")
        return RefereeCall.model_validate(raw)
    except (LLMUnavailable, ValidationError):
        return RefereeCall(reasoning="(no model available — default difficulty)")


def get_narration(view: ProjectedWorld, resolved_events: list[str],
                  player_action: str = "") -> str:
    """Narration is the one call the player actually reads, so it runs on the
    quality-first chain. If it fails we fall back to the raw event list — ugly,
    but the game continues and nothing is fabricated."""
    try:
        return complete_text(narrator_prompt(view, resolved_events, player_action),
                             NARRATOR_SYSTEM, capability="narrate", priority="high")
    except LLMUnavailable:
        lines = "\n".join(f"- {e}" for e in resolved_events)
        return f"[narrator unavailable — raw events]\n{lines}"
