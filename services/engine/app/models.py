"""The vocabulary every other module speaks.

Three rules are enforced structurally here rather than by prompt, because a
prompt-enforced secret is not a secret:

1. `Fact.is_true` is ENGINE-ONLY. It is never copied into a projection and never
   reaches any model — not even the narrator. See projection.py for why: telling
   the narrator a belief is false makes it hedge ("you *think* he died of old
   age"), which tells the player there is something to doubt without revealing a
   single hidden fact. Leak by tone. Only the engine and the post-game truth
   report may read it.

2. Who-knows-what is a join table (`Belief`), not `known_by[]` on the fact. The
   edge carries data — stance, confidence, and crucially `source_char_id` — so
   "Orys believes X because Varys told him" is representable, and a lie can be
   falsified later by invalidating one edge instead of rewriting arrays.

3. Mutable world attributes (alive, location, meters) are NOT facts. A fact is a
   discoverable proposition with a truth value; `alive` is a field that flips.
   Conflating them means writing fact-invalidation logic forever.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


# ── knowledge ───────────────────────────────────────────────────────────────

class Stance(str, Enum):
    KNOWS = "knows"        # held as certain — the narrator states it flatly
    SUSPECTS = "suspects"  # held as doubt — the narrator may hedge, honestly


class Fact(BaseModel):
    """A discoverable proposition. `content` is phrased NEUTRALLY, as the world
    would state it ("Aerion died of poison"), never as a belief ("X thinks..."),
    because the same fact is shared by every character who holds it and each
    holds it with their own confidence."""

    id: str
    content: str
    # ENGINE ONLY — never projected, never prompted. See module docstring.
    is_true: bool = True
    # Free-form tags the plot machinery keys off ("dragonstone", "poison").
    tags: list[str] = Field(default_factory=list)
    # Facts that cannot both be so. This is what makes DELIBERATE MISINFORMATION
    # expressible without ever letting a model author content: a liar who holds
    # `f_king_poisoned` asserts its contradiction `f_king_old_age` instead, and
    # the listener acquires a specific false belief rather than a weaker true
    # one. The pairing is authored data, so "invent a convincing lie" stays
    # inexpressible — the same absence principle as the belief-index protocol.
    #
    # Declared one-way in the seed and symmetrised at load; see
    # revision.contradictions_of.
    contradicts: list[str] = Field(default_factory=list)

    # ── amendment ────────────────────────────────────────────────────────
    # Most facts are propositions about what happened and can never change:
    # "the king was poisoned" is true forever or was never true. A few are
    # STANDING QUANTITIES — "the Watch numbers 2,100 men" — which are true today
    # and which events should be able to move.
    #
    # Immutable is the DEFAULT and the lock is opt-in-only. An LLM has no route
    # to set this flag: it is authored in the seed, or set by worldgen/lore at
    # creation and never afterwards. See lore.amend for why that matters — a
    # model able to mark a fact mutable could rewrite the murder it committed.
    mutable: bool = False
    # Hard lock. Even a mutable fact refuses amendment while this is set, which
    # is what lets a scenario pin load-bearing numbers that must not drift.
    locked: bool = False
    # Bumped on every amendment. Beliefs acquired before the current revision
    # are STALE — the holder knows an old number — which is how amendment stays
    # honest instead of silently updating everybody's mind at once.
    revision: int = 0


class Belief(BaseModel):
    """One edge of the character×fact join table."""

    char_id: str
    fact_id: str
    stance: Stance = Stance.KNOWS
    confidence: float = 1.0
    # Who this came from. None = first-hand or authored at world seed. This is
    # what makes deception recoverable: discover Varys lied, and every belief
    # sourced to Varys becomes suspect.
    source_char_id: str | None = None
    turn_acquired: int = 0


class ActionRow(BaseModel):
    """One entry of a scenario's action vocabulary, carried on the world itself.

    Mirrors actions.Action but lives here so WorldState can be validated without
    importing the loader. See actions.py for why this is data and not code.
    """

    id: str
    hours: float = 1.0
    actor_stat: str = "wits"
    opposing_stat: str = "resolve"
    tags: list[str] = Field(default_factory=list)


class Event(BaseModel):
    """A thing that happened, tagged with where — so perception can filter it.

    Lives here rather than in turn.py because both the turn loop and the scene
    relay produce them, and neither should have to import the other.
    """

    location: str
    text: str
    # Characters who directly took part; they always perceive it even if the
    # location bookkeeping is imperfect.
    actors: list[str] = Field(default_factory=list)
    # Engine-side detail (dice arithmetic) — DM panel only, never narrated.
    detail: str = ""


class Evidence(BaseModel):
    """A physical trace of a fact, sitting at a location until someone finds it.

    Evidence exists because ASSERTION IS CHEAP AND CERTAINTY SHOULD NOT BE. Talk
    is capped (see plots.propagate) — hearing a thing said, however sincerely,
    can only ever make you suspect it. Only evidence promotes a suspicion to
    knowledge. That is what stops a court collapsing into omniscient opponents
    after four turns of gossip, and it means clues can be *findable* rather than
    punishingly rare: the leak is not the problem, the leak is the trail.

    Evidence is NEVER projected. It is not something you perceive by standing in
    the room — it is something you find by looking. Discovery goes through
    `evidence.discover()` and costs a roll, which is why this object can carry
    `fact_id` safely: nothing serializes it into a prompt.
    """

    id: str
    fact_id: str
    # Free text, authored or orchestrator-invented: "letter", "vial", "ledger
    # line", "boot-print in the ash". The engine never branches on it.
    kind: str = "trace"
    location: str = ""
    # If someone is carrying it rather than it lying about, finding it means
    # getting at them. None = it is simply *there*.
    holder_char_id: str | None = None
    # How much certainty it confers when found, 0..1.
    strength: float = 0.8
    # What it LOOKS like to someone who finds it. Safe to narrate.
    description: str = ""
    found_by: list[str] = Field(default_factory=list)


# ── numeric state ───────────────────────────────────────────────────────────

class ModifierKind(str, Enum):
    FLAT = "flat"          # one-shot add to value
    PCT = "pct"            # one-shot multiply of value (magnitude 0.10 = +10%)
    RATE_PCT = "rate_pct"  # scales the meter's per-hour rate while active


class Modifier(BaseModel):
    """A buff/debuff. This is the LLM's ONLY write channel into numeric state —
    it emits a modifier as data and the engine applies it every tick. The model
    never does arithmetic, which is precisely where models fall apart."""

    id: str
    meter_id: str
    kind: ModifierKind
    magnitude: float
    # None = permanent until removed. Otherwise the absolute turn it stops applying.
    expires_at_turn: int | None = None
    source: str = ""   # human-readable, shown in the DM panel
    applied: bool = False  # FLAT/PCT are one-shot; this stops them re-firing


class Meter(BaseModel):
    """A named number that evolves with elapsed time.

    `rate_formula` is an expression in the safe evaluator's grammar (see
    formula.py) over: value, hours, day, turn, and any other meter by id. It
    yields the change PER HOUR, so advancing is `value += rate * hours` — driven
    by elapsed time rather than turn count, so a turn that skips half a day moves
    the meter by half a day instead of by one turn.
    """

    id: str
    label: str
    value: float
    rate_formula: str = "0"
    min: float | None = None
    max: float | None = None
    # Absolute hours-since-epoch of the last advance, so a meter can never be
    # double-advanced or silently skipped when turns are replayed or rewound.
    last_advanced_at_hour: float = 0.0
    # Meters the player can see even before learning anything (gold, health).
    # Hidden ones (a conspiracy's readiness) only surface in the DM panel.
    visible_to_player: bool = True


# ── world ───────────────────────────────────────────────────────────────────

class Stats(BaseModel):
    """1-5. Opposed rolls read these; see resolution.py."""

    might: int = 2
    guile: int = 2
    presence: int = 2
    wits: int = 2
    resolve: int = 2


class Character(BaseModel):
    id: str
    name: str
    title: str = ""
    stats: Stats = Field(default_factory=Stats)
    wants: list[str] = Field(default_factory=list)
    fears: list[str] = Field(default_factory=list)
    # Fact ids this character actively conceals — feeds the intention prompt so
    # they steer conversation away, rather than being told "don't mention X".
    hides: list[str] = Field(default_factory=list)
    # char_id -> -3.0 (hatred) .. +3.0 (devotion). FLOAT, not int: trust has to
    # move by fractions as a relationship is earned or spent, and an int would
    # quantise a whole playthrough's worth of small betrayals into nothing.
    relationships: dict[str, float] = Field(default_factory=dict)
    # 0.0 (close-mouthed) .. 1.0 (cannot keep anything in). The disposition floor
    # of `P(disclose)` — see disclosure.py. Everything else adjusts around it.
    candor: float = 0.5
    location: str = ""
    alive: bool = True


class Plot(BaseModel):
    """A scheme that advances whether or not the player acts. Stages are a simple
    ladder rather than a general graph — enough to create pressure today."""

    id: str
    name: str
    stage: int = 0
    stages: list[str] = Field(default_factory=list)
    members: list[str] = Field(default_factory=list)
    # Per-tick probability the scheme moves a stage forward.
    advance_chance: float = 0.25
    # Per-advance probability someone adjacent catches wind — this is how the
    # player ever gets a thread to pull on.
    exposure_chance: float = 0.30
    # Facts that become discoverable as the plot advances, one per stage.
    stage_facts: list[str] = Field(default_factory=list)
    active: bool = True

    @property
    def complete(self) -> bool:
        return self.stage >= len(self.stages) - 1 if self.stages else False


class Clock(BaseModel):
    day: int = 0
    hour: float = 8.0
    turn: int = 0

    @property
    def absolute_hour(self) -> float:
        """Hours since world epoch — the monotonic axis meters advance along."""
        return self.day * 24.0 + self.hour


class WorldState(BaseModel):
    """Everything. The full, unfiltered truth — only ever handed to the engine
    and the DM panel. Models receive a ProjectedWorld (see projection.py)."""

    player_id: str
    clock: Clock = Field(default_factory=Clock)
    characters: dict[str, Character] = Field(default_factory=dict)
    facts: dict[str, Fact] = Field(default_factory=dict)
    beliefs: list[Belief] = Field(default_factory=list)
    plots: dict[str, Plot] = Field(default_factory=dict)
    meters: dict[str, Meter] = Field(default_factory=dict)
    modifiers: list[Modifier] = Field(default_factory=list)
    # Physical traces lying around the world. Never projected — found, not seen.
    evidence: dict[str, Evidence] = Field(default_factory=dict)
    # This scenario's action vocabulary. Empty = use the default court table.
    # A battle scenario ships a different list here and needs no engine change;
    # see actions.py for why these rows were the only scenario-specific code.
    actions: list[ActionRow] = Field(default_factory=list)
    # Free text handed to the orchestrator every turn: tone, genre logic, what is
    # impossible in this world. Worldgen writes it; play-time planning reads it,
    # so the planner references an established world instead of inventing one.
    canon: str = ""
    # Scenario identity, so the UI never has to hardcode one world's copy.
    title: str = ""
    blurb: str = ""
    # An authored opening scene. Optional: a scenario that ships one gets a
    # reliable first screen with no LLM call; anything else has one written
    # through the player's projection at game start.
    opening: str = ""
    # Normalised question -> fact id, for truths established mid-game (lore.py).
    # This is what makes just-in-time truth IDEMPOTENT: asking three characters
    # the same thing interrogates one fact instead of minting three. Without it,
    # on-demand generation is just a slower way to be inconsistent.
    lore_index: dict[str, str] = Field(default_factory=dict)
    # Append-only record of what actually happened, for the truth report.
    chronicle: list[str] = Field(default_factory=list)

    # ── belief helpers (the join table's query surface) ──────────────────
    def beliefs_of(self, char_id: str) -> list[Belief]:
        return [b for b in self.beliefs if b.char_id == char_id]

    def holders_of(self, fact_id: str) -> list[Belief]:
        return [b for b in self.beliefs if b.fact_id == fact_id]

    def believes(self, char_id: str, fact_id: str) -> Belief | None:
        for b in self.beliefs:
            if b.char_id == char_id and b.fact_id == fact_id:
                return b
        return None

    def grant_belief(self, char_id: str, fact_id: str, stance: Stance,
                     confidence: float = 1.0, source: str | None = None) -> bool:
        """Add or strengthen a belief. Returns True if anything changed.

        Upgrading beats duplicating: hearing a rumour twice shouldn't create two
        edges, and hearing it CONFIRMED should promote suspicion to knowledge.
        """
        existing = self.believes(char_id, fact_id)
        if existing is None:
            self.beliefs.append(Belief(
                char_id=char_id, fact_id=fact_id, stance=stance,
                confidence=confidence, source_char_id=source,
                turn_acquired=self.clock.turn))
            return True
        if existing.stance is Stance.SUSPECTS and stance is Stance.KNOWS:
            existing.stance = Stance.KNOWS
            existing.confidence = max(existing.confidence, confidence)
            return True
        if confidence > existing.confidence:
            existing.confidence = confidence
            return True
        return False
