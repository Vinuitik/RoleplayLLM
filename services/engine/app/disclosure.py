"""Whether a character actually says the thing they meant to say.

The failure this exists to fix was reproduced on the very first live turn:
Mycella Ferrow, a *conspirator*, volunteered the entire Dragonstone conspiracy to
a man she had just met, at confidence 0.9, unprompted. Nothing in the model made
her reluctant. A character with a secret and no hesitation is not a character
with a secret.

The division of labour matches every other LLM boundary in this codebase:

    the model proposes INTENT to disclose; the engine gates it.

Asking a model to decide "would she say this?" fails the same way asking it to
adjudicate its own dice fails — it says yes, because saying yes is the more
interesting sentence to write. So the model still picks *which* belief it reaches
for, and this module decides whether it comes out.

    P(disclose) = candor(speaker) + trust(speaker -> room) - risk(fact, speaker)

Three properties worth preserving under any redesign:

1. **No player special case.** The same roll runs NPC->NPC in an empty corridor
   and NPC->player mid-scene. The player is not privileged, which is why walking
   in on a conversation feels like walking in on a conversation.

2. **Trust is read against the WHOLE room, at its minimum.** You do not tell your
   fellow conspirator anything while the man who would hang you both is standing
   there. This is what makes the composition of a room matter — get the wrong
   person to leave and the scene opens up — and it falls straight out of speech
   propagating to every witness rather than to one addressee.

3. **The floor is not zero.** `MIN_CHANCE` leaves a couple of percent even for a
   secret held by a close-mouthed conspirator among enemies. Secrets get out by
   slip far more often than by decision, and a hard zero makes a conspiracy
   unlosable rather than difficult.
"""

from __future__ import annotations

import random

from .models import WorldState

# A secret held under a floor of zero is a secret that can only ever be taken,
# never dropped. Two percent is the slip of the tongue.
MIN_CHANCE = 0.02
MAX_CHANCE = 0.95

# relationships run -3..+3; a devoted listener is worth +0.5 to the odds, a
# hated one -0.5. Deliberately smaller than `candor` can swing: who you are
# matters more than who you are talking to.
TRUST_SCALE = 6.0

# risk weights. `hides` is the strongest signal because it is the author (or the
# orchestrator) saying outright "this one is dear to them".
RISK_HIDES = 0.60
RISK_TAGGED_SECRET = 0.30
RISK_PLOT_MEMBER = 0.40
SECRET_TAGS = ("secret",)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def risk(world: WorldState, speaker_id: str, fact_id: str) -> float:
    """How much this particular speaker stands to lose by saying this fact.

    Additive and uncapped on purpose: a conspirator's own plot fact that they
    also actively conceal and that is tagged secret should be effectively
    unsayable, not merely unlikely.
    """
    speaker = world.characters.get(speaker_id)
    if speaker is None:
        return 0.0

    score = 0.0
    if fact_id in speaker.hides:
        score += RISK_HIDES

    fact = world.facts.get(fact_id)
    if fact is not None and any(t in SECRET_TAGS for t in fact.tags):
        score += RISK_TAGGED_SECRET

    # Being in the scheme the fact belongs to is its own exposure: this is the
    # thing that hangs you, and you know it does.
    for plot in world.plots.values():
        if speaker_id in plot.members and fact_id in plot.stage_facts:
            score += RISK_PLOT_MEMBER
            break

    return score


def trust(world: WorldState, speaker_id: str, listener_ids: list[str]) -> float:
    """Trust in the ROOM, which is the minimum trust in anyone standing in it.

    Not an average: one enemy present is not offset by two friends. That is the
    whole reason a private word is worth arranging.
    """
    speaker = world.characters.get(speaker_id)
    if speaker is None or not listener_ids:
        return 0.0
    worst = min(speaker.relationships.get(lid, 0.0) for lid in listener_ids)
    return _clamp(worst / TRUST_SCALE, -0.5, 0.5)


def disclosure_chance(world: WorldState, speaker_id: str,
                      listener_ids: list[str], fact_id: str) -> float:
    """P(the speaker actually says it). Pure — no rng, so it can be tested and
    displayed in the DM panel exactly as the engine will use it."""
    speaker = world.characters.get(speaker_id)
    if speaker is None:
        return 0.0
    raw = (speaker.candor
           + trust(world, speaker_id, listener_ids)
           - risk(world, speaker_id, fact_id))
    return _clamp(raw, MIN_CHANCE, MAX_CHANCE)


def will_disclose(world: WorldState, speaker_id: str, listener_ids: list[str],
                  fact_id: str, rng: random.Random) -> tuple[bool, float]:
    """Roll it. Returns (disclosed, chance) — the chance comes back so the DM
    panel can show *why* a scene went quiet instead of it looking like a bug."""
    chance = disclosure_chance(world, speaker_id, listener_ids, fact_id)
    return rng.random() < chance, chance
