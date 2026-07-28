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

import json
import os

import requests
from pydantic import BaseModel, Field, ValidationError

from . import telemetry
from .gametime import ARCHETYPE_HOURS, coerce_hours
from .projection import ProjectedWorld
from .resolution import DIFFICULTY

DIFFICULTY_LABELS = set(DIFFICULTY)

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
    a provider outage should cost the world some colour, not end the session.

    Every way this call can go wrong is COUNTED, not just repaired. Silent
    repair is precisely what makes model degradation invisible: an out-of-range
    belief index is dropped and the turn carries on, so a model that has started
    hallucinating indices looks exactly like a quiet NPC. See telemetry.py.
    """
    prompt = npc_intention_prompt(view, situation_note)
    with telemetry.timed("intention", prompt, char_id=view.self_name) as slot:
        try:
            raw = complete_json(prompt, NPC_SYSTEM, required_keys=("action",),
                                capability="text", priority="medium")
        except LLMUnavailable as exc:
            slot.finish("", ok=False, error=str(exc), violations=["unavailable"])
            return Intention(action="observe", rationale="(no model available)")
        except ValidationError as exc:
            slot.finish("", ok=False, error=str(exc)[:500],
                        violations=["schema"])
            return Intention(action="observe", rationale="(malformed reply)")

        violations = _intention_violations(raw, view)
        slot.finish(json.dumps(raw)[:8000],
                    provider=raw.get("_provider", ""),
                    model=raw.get("_model", ""),
                    violations=violations)
        try:
            return Intention.model_validate(raw)
        except ValidationError:
            return Intention(action="observe", rationale="(malformed reply)")


def _intention_violations(raw: dict, view: ProjectedWorld) -> list[str]:
    """Everything the engine is about to silently forgive, named.

    These are not errors — the turn survives all of them. They are the signal
    that a model has drifted, and they are invisible without being counted.
    """
    found: list[str] = []
    index = raw.get("reveals_belief", NO_FACT)
    if isinstance(index, int) and index != NO_FACT:
        if not (0 <= index < len(view.beliefs)):
            # The belief-index protocol working as designed — and worth knowing
            # about, because a rising rate here means a model losing the plot.
            found.append("belief_index_out_of_range")
    elif index != NO_FACT:
        found.append("belief_index_not_an_int")

    action = str(raw.get("action", "")).strip().lower()
    if action and action not in ARCHETYPE_HOURS:
        found.append("unknown_action")

    hours = raw.get("hours_elapsed")
    if hours is not None:
        _value, note = coerce_hours(hours, action or "speak")
        if note:
            found.append("bad_hours")
    return found


def get_referee(view: ProjectedWorld, action_text: str) -> RefereeCall:
    prompt = referee_prompt(view, action_text)
    with telemetry.timed("referee", prompt) as slot:
        try:
            raw = complete_json(prompt, REFEREE_SYSTEM,
                                required_keys=("difficulty",),
                                capability="text", priority="high")
        except LLMUnavailable as exc:
            slot.finish("", ok=False, error=str(exc), violations=["unavailable"])
            return RefereeCall(reasoning="(no model available — default difficulty)")
        except ValidationError as exc:
            slot.finish("", ok=False, error=str(exc)[:500], violations=["schema"])
            return RefereeCall(reasoning="(malformed reply — default difficulty)")

        violations = []
        if str(raw.get("difficulty", "")).strip().lower() not in DIFFICULTY_LABELS:
            if not isinstance(raw.get("difficulty"), (int, float)):
                violations.append("unknown_difficulty")
        if raw.get("hours_elapsed") is not None:
            _v, note = coerce_hours(raw.get("hours_elapsed"),
                                    str(raw.get("archetype", "speak")))
            if note:
                violations.append("bad_hours")
        slot.finish(json.dumps(raw)[:8000], violations=violations)
        try:
            return RefereeCall.model_validate(raw)
        except ValidationError:
            return RefereeCall(reasoning="(malformed reply — default difficulty)")


OPENING_SYSTEM = (
    "You open a scene. The player has just arrived and knows nothing except what "
    "they are told here. Establish where they are, who they are, and who is in "
    "the room — concretely, in second person, present tense, under 200 words. "
    "You may only use what you are given: never invent a person, place, event or "
    "fact that is not listed. Then a blank line, then exactly three suggested "
    "actions, one per line, each starting with '> '."
)


def get_opening(view: ProjectedWorld, canon: str = "") -> str:
    """Write the first thing the player reads in a generated world.

    Goes through the narrator's projection like everything else, so the opening
    cannot leak a secret to establish atmosphere — which is exactly the sort of
    place a hand-written intro would cheat, because it feels like scene-setting
    rather than disclosure.

    `canon` is world-level tone and genre logic, not hidden state, so it is safe
    to pass and it is what stops a generated world opening in the wrong register.
    """
    people = _people(view)
    beliefs = "\n".join(f"  - {b.content}" for b in view.beliefs[:8]) or "  - very little"
    prompt = f"""TIME: {view.time_of_day}
PLACE: {view.location}
YOU ARE NARRATING TO: {view.self_name}, {view.self_title}

WORLD:
{canon or "(no canon recorded)"}

THEY WANT: {'; '.join(view.wants) or 'nothing in particular'}
THEY FEAR: {'; '.join(view.fears) or 'nothing'}

PEOPLE PRESENT:
{people}

WHAT THEY ALREADY KNOW:
{beliefs}

Open the scene."""
    with telemetry.timed("opening", prompt) as slot:
        try:
            text = complete_text(prompt, OPENING_SYSTEM, capability="narrate",
                                 priority="high")
        except LLMUnavailable as exc:
            slot.finish("", ok=False, error=str(exc), violations=["unavailable"])
            return _fallback_opening(view)
        slot.finish(text, violations=[] if "> " in text else ["no_suggestions"])
        return text


def _fallback_opening(view: ProjectedWorld) -> str:
    """Every provider is down and the player still needs to know where they are.

    Plain, but never blank: a player dropped onto an empty screen with three
    suggestions has no idea what game they are in.
    """
    here = ", ".join(f"{p.name} ({p.title})" for p in view.present) or "no one"
    return (f"{view.time_of_day}. You are {view.self_name}"
            + (f", {view.self_title}" if view.self_title else "")
            + f", at {view.location or 'an unremarked place'}.\n\n"
            f"Present: {here}.\n\n"
            "> Look around\n> Speak to whoever is here\n> Wait and listen")


SCENE_REPORT_SYSTEM = (
    "You report, secondhand, a conversation the listener was not present for. "
    "You never invent what was said. Lines you are told were unintelligible stay "
    "unintelligible — do not guess at them. Two or three sentences, past tense, "
    "the tone of something pieced together after the fact."
)


def get_scene_report(view: ProjectedWorld, location: str,
                     lines: list[str]) -> str:
    """Write up an offstage scene for someone who has since earned knowledge of it.

    This is the ONLY place an offstage conversation ever costs a token, and it is
    paid once, long after the fact, for the small fraction of scenes that ever
    surface. Lines the recaller cannot account for arrive already redacted (see
    scene.render_recalled) — so the redaction is structural, and this prompt is
    merely asked not to paper over it.
    """
    body = "\n".join(f"  - {line}" for line in lines) or "  - nothing of note"
    try:
        return complete_text(
            f"""TIME: {view.time_of_day}
YOU ARE WRITING FOR: {view.self_name}, {view.self_title}
THEY HAVE JUST LEARNED OF A CONVERSATION AT: {location}

WHAT THEY CAN ACCOUNT FOR:
{body}

Report what they now understand of that meeting. Do not invent participants,
words, or conclusions beyond the list above.""",
            SCENE_REPORT_SYSTEM, capability="narrate", priority="medium")
    except LLMUnavailable:
        return "\n".join(f"- {line}" for line in lines)


def get_narration(view: ProjectedWorld, resolved_events: list[str],
                  player_action: str = "") -> str:
    """Narration is the one call the player actually reads, so it runs on the
    quality-first chain. If it fails we fall back to the raw event list — ugly,
    but the game continues and nothing is fabricated."""
    prompt = narrator_prompt(view, resolved_events, player_action)
    with telemetry.timed("narration", prompt) as slot:
        try:
            text = complete_text(prompt, NARRATOR_SYSTEM,
                                 capability="narrate", priority="high")
        except LLMUnavailable as exc:
            slot.finish("", ok=False, error=str(exc), violations=["unavailable"])
            lines = "\n".join(f"- {e}" for e in resolved_events)
            return f"[narrator unavailable — raw events]\n{lines}"

        # The narrator's two failure modes, both silent and both worth counting:
        # ignoring the suggestion format, and padding well past the word limit
        # (the reliable early symptom of a fallback to a weaker model).
        violations = []
        if "> " not in text:
            violations.append("no_suggestions")
        if len(text) > 3000:
            violations.append("overlong_narration")
        slot.finish(text, violations=violations)
        return text
