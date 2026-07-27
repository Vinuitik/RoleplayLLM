"""Establishing truth on demand — and exactly once.

Worldgen cannot enumerate everything. The player will ask how many guards hold
King's Landing, and no fact covers it. There are three ways that can go and only
one of them is acceptable:

1. **The NPC waffles.** The world feels thin and the player learns not to ask.
2. **The NPC makes a number up in its prose.** Catastrophic: the "truth" now
   exists only in a transcript, nobody else in the world knows it, and the next
   character asked will invent a different one. This is the failure that makes
   LLM roleplay feel like sand.
3. **The engine establishes a real fact, right then, and remembers it forever.**

This module is (3). When a question lands on a gap, the truth is generated ONCE,
written into `world.facts` with a real `is_true`, and granted to the characters
who would plausibly know it. From that instant it behaves like any fact authored
at worldgen: it propagates, it can be lied about, it can be discovered, it can be
contradicted, and every projection filters it the same way.

## Why the lie must be generated with the truth

If a character chooses to deceive about a gap, a plausible FALSE fact has to
exist too — otherwise `truthful: false` has nothing to assert and the deception
silently degrades into an unconvincing truth. So `establish` creates the pair
together and wires `contradicts` between them, which is exactly the shape
worldgen produces by hand.

The consequence is the good one: the player asks the Commander how many guards
there are, he lies, and *the real number still exists*, held by someone else,
findable later. The lie has something to be caught against.

## Idempotency is the whole game

A topic resolves to the same fact forever. `world.lore_index` maps a normalised
topic key to the fact id it created, so asking three characters the same
question interrogates one truth rather than minting three. Without this the
module would be a more expensive version of failure (2).

## What it is NOT allowed to do

It never invents characters, locations or plots — only facts, and only about
what was asked. It never decides an outcome. And it is given the existing canon
and related facts, so a newly established truth cannot contradict one already
standing.
"""

from __future__ import annotations

import random
import re

from pydantic import BaseModel, Field, ValidationError

from . import llm, telemetry
from .models import Fact, Stance, WorldState

# Guardrail on runaway generation: a session that establishes hundreds of facts
# has stopped being a scenario and started being a chatbot with extra steps.
MAX_ESTABLISHED_PER_GAME = 60

# Established truth is held firmly by those who would know it, but it is not
# sacred: it is a normal belief and can be shaken like any other.
KNOWER_CONFIDENCE = 1.0


class LoreProposal(BaseModel):
    """What the establishing call is allowed to return. Note what is absent:
    no character creation, no location, no outcome, no plot."""

    # The true state of affairs, phrased neutrally as the world would state it.
    truth: str = ""
    # A plausible false version someone might assert instead. Optional but
    # strongly wanted — without it, deception about this topic is impossible.
    lie: str = ""
    tags: list[str] = Field(default_factory=list)
    # Ids of existing characters who would plausibly know the truth first-hand.
    knowers: list[str] = Field(default_factory=list)
    # Ids of characters who would plausibly believe the FALSE version.
    misled: list[str] = Field(default_factory=list)
    # Whether this is a STANDING QUANTITY that events should be able to move
    # ("the Watch numbers 2,100 men") rather than a settled proposition ("the
    # king was poisoned"). Decided here, AT CREATION, and never again — see
    # `amend` for why that one-way door is the entire safety property.
    standing: bool = False
    reasoning: str = ""


LORE_SYSTEM = (
    "You establish a single missing fact about an existing world. You are a "
    "record-keeper, not a storyteller: you state what is so, plainly and "
    "specifically, and you never describe events, outcomes, or what anyone does "
    "about it. You never invent people or places that do not already exist. "
    "Reply with one JSON object and nothing else."
)


def topic_key(question: str) -> str:
    """Normalise a question into a stable key.

    Crude on purpose — stemming and synonyms would be a research project, and
    the failure mode of a too-narrow key (occasionally establishing two related
    facts) is far cheaper than a too-broad one (one fact answering questions it
    should not).
    """
    words = re.findall(r"[a-z0-9]+", (question or "").lower())
    stop = {"the", "a", "an", "of", "in", "is", "are", "how", "what", "many",
            "much", "there", "do", "does", "did", "who", "whom", "and", "to",
            "for", "on", "at", "it", "they", "we", "you", "i", "me", "my"}
    keep = sorted(w for w in words if w not in stop)
    return "-".join(keep[:8])


def _lore_prompt(world: WorldState, question: str) -> str:
    cast = "\n".join(
        f"  - {c.id}: {c.name}, {c.title or 'no title'} (at {c.location})"
        for c in world.characters.values() if c.alive)
    # Related standing facts, so a new truth cannot contradict an old one.
    key_words = set(topic_key(question).split("-"))
    related = [f.content for f in world.facts.values()
               if key_words & set(re.findall(r"[a-z0-9]+", f.content.lower()))]
    established = "\n".join(f"  - {c}" for c in related[:12]) or "  (none)"

    return f"""WORLD CANON:
{world.canon or "(none recorded)"}

PEOPLE WHO EXIST (you may not invent others):
{cast}

FACTS ALREADY ESTABLISHED ABOUT THIS SUBJECT — your answer must not contradict
these:
{established}

SOMEONE HAS ASKED SOMETHING THE RECORD DOES NOT COVER:
"{question}"

Establish the truth. Be SPECIFIC — a number, a name, a quantity — because vague
truths cannot be lied about or discovered later. Then supply the plausible false
version someone with a reason to deceive would assert instead.

{{
  "truth":     "the true state of affairs, stated neutrally",
  "lie":       "a plausible false version, stated neutrally",
  "tags":      ["one or two topic words"],
  "knowers":   ["ids of existing characters who would know this first-hand"],
  "misled":    ["ids of existing characters who would believe the false version"],
  "standing":  true if this is a QUANTITY that events could change later (a
               headcount, a stockpile, a distance), false if it is a settled
               matter of fact that either is or is not so,
  "reasoning": "one clause, for the debug view"
}}"""


def establish(world: WorldState, question: str,
              rng: random.Random | None = None) -> tuple[str | None, str | None, list[str]]:
    """Create the truth about `question`, once. Returns (true_id, false_id, log).

    Idempotent by topic: asking the same thing again returns the fact already
    established rather than minting a second one. This is the property that
    separates the module from an expensive version of the failure it prevents.
    """
    log: list[str] = []
    key = topic_key(question)
    if not key:
        return None, None, log

    existing = world.lore_index.get(key)
    if existing and existing in world.facts:
        false_id = next((f for f in world.facts[existing].contradicts
                         if f in world.facts), None)
        log.append(f"[lore] '{key}' already established; reusing")
        return existing, false_id, log

    if len(world.lore_index) >= MAX_ESTABLISHED_PER_GAME:
        log.append(f"[lore] refused '{key}': established-fact cap reached")
        return None, None, log

    prompt = _lore_prompt(world, question)
    with telemetry.timed("lore", prompt) as slot:
        try:
            raw = llm.complete_json(prompt, LORE_SYSTEM, required_keys=("truth",),
                                    capability="orchestrate", priority="high")
            proposal = LoreProposal.model_validate(raw)
            slot.finish(str(raw)[:4000])
        except (llm.LLMUnavailable, ValidationError) as exc:
            slot.finish("", ok=False, error=str(exc)[:300],
                        violations=["lore_unavailable"])
            log.append(f"[lore] could not establish '{key}': {exc}")
            return None, None, log

    if not proposal.truth.strip():
        log.append(f"[lore] empty truth for '{key}'")
        return None, None, log

    index = len(world.lore_index)
    true_id = f"f_lore_{index:03d}"
    false_id = f"f_lore_{index:03d}_false" if proposal.lie.strip() else None

    world.facts[true_id] = Fact(
        id=true_id, content=proposal.truth.strip(), is_true=True,
        tags=list(proposal.tags) + ["established"],
        contradicts=[false_id] if false_id else [],
        # The only moment `mutable` is ever decided. A quantity can be moved by
        # later events; a settled matter of fact never can.
        mutable=bool(proposal.standing))
    log.append(f"[lore] established {true_id}: {proposal.truth.strip()}")

    if false_id:
        world.facts[false_id] = Fact(
            id=false_id, content=proposal.lie.strip(), is_true=False,
            tags=list(proposal.tags) + ["established", "comfortable-lie"],
            contradicts=[true_id])
        log.append(f"[lore] established {false_id} (false): {proposal.lie.strip()}")

    # Grant to plausible knowers. A truth nobody holds is not yet part of the
    # world — it is just a row waiting for someone to be able to say it.
    knowers = [c for c in proposal.knowers if c in world.characters]
    if not knowers:
        knowers = _guess_knowers(world, proposal.truth, rng)
        log.append(f"[lore] no valid knowers proposed; inferred {knowers}")
    for char_id in knowers:
        world.grant_belief(char_id, true_id, Stance.KNOWS, KNOWER_CONFIDENCE)

    for char_id in proposal.misled:
        if char_id in world.characters and char_id not in knowers and false_id:
            world.grant_belief(char_id, false_id, Stance.SUSPECTS, 0.6)

    world.lore_index[key] = true_id
    return true_id, false_id, log


def _guess_knowers(world: WorldState, truth: str,
                   rng: random.Random | None) -> list[str]:
    """Fallback when the model names nobody real.

    Scores characters by word overlap between their title and the fact, which is
    crude but catches the case that matters: the Commander of the City Watch
    knows the guard count. A truth with no holder at all would be undiscoverable
    and unsayable, so this always returns someone.
    """
    words = set(re.findall(r"[a-z]+", truth.lower()))
    scored = []
    for character in world.characters.values():
        if not character.alive:
            continue
        title_words = set(re.findall(r"[a-z]+", (character.title or "").lower()))
        scored.append((len(words & title_words), character.id))
    if not scored:
        return []
    scored.sort(key=lambda s: (-s[0], s[1]))
    best = scored[0][0]
    if best > 0:
        return [c for score, c in scored if score == best][:2]
    pool = [c for _s, c in scored]
    picker = rng or random.Random(truth)
    return [picker.choice(pool)]


# ── amendment ───────────────────────────────────────────────────────────────

# How far a belief's confidence falls when the fact underneath it moves. Not to
# zero: someone who knew the garrison was 2,100 last week is not now ignorant,
# they are out of date, and "out of date" is a doubt rather than a blank.
STALE_PENALTY = 0.5


class AmendmentRefused(RuntimeError):
    """Raised when something tries to change a fact it may not change."""


def amend(world: WorldState, fact_id: str, new_content: str, cause: str,
          *, force: bool = False) -> list[str]:
    """Change what a standing quantity says, because an event moved it.

    THE LOCK, and why it is shaped like this:

    An LLM must never be able to rewrite the past. If a model could amend
    arbitrary facts it would eventually amend the murder it committed, the debt
    it owes, or the evidence against it — and every one of those would look like
    a legitimate state update. So amendment is refused unless the fact was
    marked `mutable` AT CREATION, and nothing a model returns can set that flag:
    it is authored in the seed, or decided by worldgen/lore when the fact is
    born, and never afterwards.

    `locked` is the second gate, for a scenario that wants to pin a number even
    though it is nominally a quantity. `force` exists for engine-internal
    callers (combat applying its own casualties) and is never reachable from a
    model-supplied path.

    The interesting part is what happens to everyone who believed it. Their
    beliefs are NOT silently updated — that would hand the whole world free
    omniscience every time a number moved. They are marked stale: confidence
    halves and stance drops to SUSPECTS, because a man who knew the garrison
    last month now merely thinks he knows it. Learning the new number is a
    normal act of communication or discovery, like anything else here.
    """
    fact = world.facts.get(fact_id)
    if fact is None:
        raise AmendmentRefused(f"no such fact: {fact_id}")
    if not new_content.strip():
        raise AmendmentRefused("amendment with empty content")
    if fact.locked and not force:
        raise AmendmentRefused(f"{fact_id} is locked")
    if not fact.mutable and not force:
        raise AmendmentRefused(
            f"{fact_id} is not a standing quantity and cannot be amended")

    old = fact.content
    fact.content = new_content.strip()
    fact.revision += 1

    log = [f"[amend] {fact_id} r{fact.revision} ({cause}): "
           f"{old!r} -> {fact.content!r}"]

    # Everyone who held the old value now holds an out-of-date one.
    for belief in world.beliefs:
        if belief.fact_id != fact_id:
            continue
        belief.stance = Stance.SUSPECTS
        belief.confidence = round(belief.confidence * STALE_PENALTY, 3)
        log.append(f"[amend] {world.characters[belief.char_id].name}'s knowledge "
                   f"is now stale ({belief.confidence})")
    return log


def mark_mutable(world: WorldState, fact_id: str, mutable: bool = True) -> bool:
    """Authoring helper. Deliberately NOT exposed to any model-facing path — it
    exists for seeds, worldgen and tests, and is the single place the mutable
    flag can be turned on after creation."""
    fact = world.facts.get(fact_id)
    if fact is None:
        return False
    fact.mutable = mutable
    return True


def has_coverage(world: WorldState, question: str) -> bool:
    """Whether the record already says something about this.

    Checked before spending a call: most questions land on facts that already
    exist, and establishing lore should be the exception rather than the reflex.
    """
    key_words = set(topic_key(question).split("-")) - {""}
    if not key_words:
        return True
    if world.lore_index.get(topic_key(question)):
        return True
    for fact in world.facts.values():
        content_words = set(re.findall(r"[a-z0-9]+", fact.content.lower()))
        # Two content words in common is a low bar deliberately: a false negative
        # costs one LLM call, a false positive means the question goes unanswered.
        if len(key_words & content_words) >= 2:
            return True
    return False
