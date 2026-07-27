"""Knowledge projection — the load-bearing wall of the whole design.

`project(world, char_id)` builds the ONLY view of the world any language model
ever receives. A secret is kept because the model never receives it, not because
it was told to keep quiet. Prompt-enforced secrecy fails the moment the player
asks an unexpected question; absence does not fail.

Two leaks this module exists to prevent, both subtle:

1. **Truth leak.** `Fact.is_true` never crosses this boundary — not to NPCs, and
   not to the narrator either. If the narrator knows a belief is false it hedges:
   "you *think* the Old Hand died of old age." That sentence reveals there is
   something to doubt while disclosing no hidden fact at all. The narrator gets
   the character's own confidence and nothing more, so it hedges exactly when the
   character genuinely doubts — which is honest, not a leak.

2. **Identifier leak.** Fact ids are human-authored and therefore descriptive
   (`f_hand_poisoned`). Shipping the id alongside the content would announce the
   secret in the id even where the content was filtered. Projected beliefs carry
   `fact_id` for the engine's use but mark it `exclude=True`, so it is present on
   the object and absent from every serialization that reaches a model.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .gametime import describe_time, phase_of_day
from .models import Character, Stance, WorldState


class ProjectedBelief(BaseModel):
    """One thing a character holds to be so, as they hold it."""

    content: str
    stance: Stance
    confidence: float
    # Name, never id — "Varys told me" is in-fiction; "varys_01" is a leak of
    # engine internals into the prose.
    source: str | None = None
    turn_acquired: int = 0
    # Engine-only handle. exclude=True keeps it out of model_dump()/JSON, which
    # is what every prompt builder serializes through.
    fact_id: str = Field(exclude=True)


class ProjectedCharacter(BaseModel):
    """Another person as this character perceives them. Deliberately shallow:
    stats, wants, fears and secrets are private to their owner."""

    name: str
    title: str = ""
    location: str = ""
    alive: bool = True
    # How the viewer feels about them, not the reverse. Float since trust became
    # mutable — it has to move by fractions as a relationship is earned or spent.
    disposition: float = 0.0


class ProjectedMeter(BaseModel):
    label: str
    value: float


class ProjectedWorld(BaseModel):
    """Everything one character can perceive. This is what gets serialized into
    a prompt — so if a secret is not in here, no prompt can betray it."""

    self_name: str
    self_title: str = ""
    location: str = ""
    day: int = 0
    hour: float = 0.0
    turn: int = 0
    # Time in words. Models are unreliable at inferring "is it night?" from a raw
    # hour, and every scene depends on knowing. So we tell them, every turn.
    phase: str = "morning"
    time_of_day: str = ""
    # The viewer's own sheet — they know their own mind.
    wants: list[str] = Field(default_factory=list)
    fears: list[str] = Field(default_factory=list)
    stats: dict[str, int] = Field(default_factory=dict)
    # Facts this character is actively concealing, as CONTENT. Feeds the intention
    # prompt so they steer away from the topic in character, instead of being
    # handed a "do not mention X" rule they may narrate around.
    concealing: list[str] = Field(default_factory=list)
    beliefs: list[ProjectedBelief] = Field(default_factory=list)
    present: list[ProjectedCharacter] = Field(default_factory=list)
    known_of: list[ProjectedCharacter] = Field(default_factory=list)
    meters: list[ProjectedMeter] = Field(default_factory=list)

    def prompt_payload(self) -> dict:
        """Serialization used for prompts. Goes through model_dump so every
        `exclude=True` field (fact_id) is dropped by construction — a prompt
        builder cannot accidentally reach past this."""
        return self.model_dump(mode="json")


def project(world: WorldState, char_id: str) -> ProjectedWorld:
    """Build `char_id`'s view of the world. Never returns hidden state."""
    character = world.characters[char_id]

    beliefs: list[ProjectedBelief] = []
    for belief in world.beliefs_of(char_id):
        fact = world.facts.get(belief.fact_id)
        if fact is None:
            continue
        source_name = None
        if belief.source_char_id and belief.source_char_id in world.characters:
            source_name = world.characters[belief.source_char_id].name
        beliefs.append(ProjectedBelief(
            content=fact.content,          # NOTE: fact.is_true is NOT read here.
            stance=belief.stance,
            confidence=belief.confidence,
            source=source_name,
            turn_acquired=belief.turn_acquired,
            fact_id=fact.id,
        ))
    # Strongest convictions first: prompts get truncated, and losing a rumour
    # hurts far less than losing a certainty.
    beliefs.sort(key=lambda b: (b.stance is not Stance.KNOWS, -b.confidence))

    present, known_of = [], []
    for other_id, other in world.characters.items():
        if other_id == char_id:
            continue
        projected = _project_character(other, character)
        if other.alive and other.location == character.location:
            present.append(projected)
        else:
            known_of.append(projected)

    # Hidden meters (a conspiracy's readiness) are DM-panel only. The player's
    # own view is filtered the same way as everyone else's — no special case.
    meters = [ProjectedMeter(label=m.label, value=round(m.value, 2))
              for m in world.meters.values() if m.visible_to_player]

    # `hides` is an authoring convenience, so it is NOT trusted as a source of
    # content: a character can only conceal something they actually hold a belief
    # about. Without this filter an author who lists a fact the character hasn't
    # learned yet leaks its content straight into their own prompt — which is
    # exactly what the projection property test caught here.
    held_fact_ids = {b.fact_id for b in beliefs}
    concealing = [world.facts[f].content for f in character.hides
                  if f in world.facts and f in held_fact_ids]

    return ProjectedWorld(
        self_name=character.name,
        self_title=character.title,
        location=character.location,
        day=world.clock.day,
        hour=world.clock.hour,
        turn=world.clock.turn,
        phase=phase_of_day(world.clock.hour),
        time_of_day=describe_time(world.clock.day, world.clock.hour),
        wants=list(character.wants),
        fears=list(character.fears),
        stats=character.stats.model_dump(),
        concealing=concealing,
        beliefs=beliefs,
        present=present,
        known_of=known_of,
        meters=meters,
    )


def _project_character(other: Character, viewer: Character) -> ProjectedCharacter:
    return ProjectedCharacter(
        name=other.name,
        title=other.title,
        location=other.location,
        alive=other.alive,
        disposition=round(viewer.relationships.get(other.id, 0.0), 2),
    )


def perceives(world: WorldState, char_id: str, location: str) -> bool:
    """Whether `char_id` would witness something happening at `location`.

    The gate between resolution and narration. Without it the narrator receives
    every resolved outcome in the world — including a poisoning three rooms away —
    and dutifully tells the player about it, defeating projection entirely.
    """
    character = world.characters.get(char_id)
    return bool(character and character.alive and character.location == location)
