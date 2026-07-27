"""Meters: named numbers that evolve with elapsed time.

The division of labour, which is the whole point of this module:

    the LLM emits a formula or a modifier ONCE, as data
    the engine applies it EVERY tick, deterministically

Models are unreliable at carrying arithmetic across many steps — ask one to
compound interest over twelve turns and it will drift. Ask it instead for
`"gold * 0.02 - upkeep"` and the engine can run that exactly, forever. So the
model does the part it is good at (deciding that a bribe should cost 15% more
while the city is in unrest) and never the part it is bad at (the running total).

Advance is driven by ELAPSED HOURS, not turn count. A turn that skips half a day
moves meters by twelve hours; a turn spent in one conversation moves them barely
at all. Tracking `last_advanced_at_hour` per meter makes the step idempotent —
re-running a tick, or rewinding and replaying, can never double-count.
"""

from __future__ import annotations

from .formula import FormulaError, evaluate
from .models import Meter, Modifier, ModifierKind, WorldState


def meter_variables(world: WorldState, meter: Meter | None = None) -> dict[str, float]:
    """The namespace a rate formula is evaluated in.

    Every meter is exposed by id so formulas can couple ("unrest rises while
    treasury is empty"), plus the clock. `value` is the current meter's own value,
    which is what makes decay curves (`-value * 0.05`) expressible.
    """
    variables: dict[str, float] = {m.id: m.value for m in world.meters.values()}
    variables.update({
        "day": float(world.clock.day),
        "hour": float(world.clock.hour),
        "turn": float(world.clock.turn),
    })
    if meter is not None:
        variables["value"] = meter.value
    return variables


def active_modifiers(world: WorldState, meter_id: str) -> list[Modifier]:
    return [m for m in world.modifiers
            if m.meter_id == meter_id
            and (m.expires_at_turn is None or m.expires_at_turn > world.clock.turn)]


def _rate_multiplier(world: WorldState, meter_id: str) -> float:
    """Product of every active RATE_PCT modifier. magnitude 0.10 = +10% rate.

    Multiplicative rather than additive so two -50% debuffs quarter the rate
    instead of stopping it dead at zero (and three don't reverse its sign).
    """
    multiplier = 1.0
    for mod in active_modifiers(world, meter_id):
        if mod.kind is ModifierKind.RATE_PCT:
            multiplier *= max(0.0, 1.0 + mod.magnitude)
    return multiplier


def _clamp(meter: Meter, value: float) -> float:
    if meter.min is not None:
        value = max(meter.min, value)
    if meter.max is not None:
        value = min(meter.max, value)
    return value


def apply_pending_modifiers(world: WorldState) -> list[str]:
    """Fire the one-shot modifiers (FLAT, PCT) that haven't fired yet.

    RATE_PCT is not one-shot — it's read continuously by _rate_multiplier — so it
    is skipped here. `applied` is what stops a one-shot re-firing every tick for
    the rest of the game.
    """
    events: list[str] = []
    for mod in world.modifiers:
        if mod.applied or mod.kind is ModifierKind.RATE_PCT:
            continue
        meter = world.meters.get(mod.meter_id)
        if meter is None:
            mod.applied = True  # meter went away; retire the orphan quietly
            continue
        before = meter.value
        if mod.kind is ModifierKind.FLAT:
            meter.value = _clamp(meter, meter.value + mod.magnitude)
        elif mod.kind is ModifierKind.PCT:
            meter.value = _clamp(meter, meter.value * (1.0 + mod.magnitude))
        mod.applied = True
        events.append(f"{meter.label}: {before:.2f} -> {meter.value:.2f} ({mod.source})")
    return events


def advance_meters(world: WorldState, to_absolute_hour: float) -> list[str]:
    """Advance every meter from its own last-advanced mark up to `to_absolute_hour`.

    All rates are computed from a SNAPSHOT of the pre-step values, then applied
    together. Without that, a formula referencing another meter would silently
    depend on dict iteration order — meter A would see B's new value while B saw
    A's old one, and the same world would evolve differently after a reload.
    Simultaneous update keeps the step order-independent and reproducible.
    """
    events: list[str] = []
    snapshot = meter_variables(world)
    deltas: dict[str, float] = {}

    for meter in world.meters.values():
        hours = to_absolute_hour - meter.last_advanced_at_hour
        if hours <= 0:
            continue  # already at or past this mark — idempotent by construction
        variables = dict(snapshot)
        variables["value"] = meter.value
        variables["hours"] = hours
        try:
            rate = evaluate(meter.rate_formula, variables)
        except FormulaError as e:
            # A bad formula must never halt the game. Freeze that one meter,
            # surface it in the DM panel, and let the turn finish.
            events.append(f"!! {meter.label} formula error, meter frozen: {e}")
            meter.last_advanced_at_hour = to_absolute_hour
            continue
        deltas[meter.id] = rate * hours * _rate_multiplier(world, meter.id)

    for meter_id, delta in deltas.items():
        meter = world.meters[meter_id]
        before = meter.value
        meter.value = _clamp(meter, meter.value + delta)
        meter.last_advanced_at_hour = to_absolute_hour
        if abs(meter.value - before) > 1e-9:
            events.append(f"{meter.label}: {before:.2f} -> {meter.value:.2f}")

    return events


def prune_expired_modifiers(world: WorldState) -> list[str]:
    """Drop modifiers that have run out. One-shot modifiers are kept once applied
    only until expiry, so the list can't grow without bound over a long game."""
    turn = world.clock.turn
    keep, dropped = [], []
    for mod in world.modifiers:
        if mod.expires_at_turn is not None and mod.expires_at_turn <= turn:
            dropped.append(mod)
        else:
            keep.append(mod)
    world.modifiers = keep
    return [f"expired: {m.source or m.id}" for m in dropped]


def add_modifier(world: WorldState, meter_id: str, kind: ModifierKind,
                 magnitude: float, duration_turns: int | None = None,
                 source: str = "") -> Modifier | None:
    """Attach a buff/debuff. Returns None if the meter doesn't exist.

    This is the function the LLM's structured output lands in, so it validates
    rather than trusts: an unknown meter id is refused instead of creating one,
    which stops a hallucinated meter name from quietly spawning state nothing
    else in the world knows about.
    """
    if meter_id not in world.meters:
        return None
    modifier = Modifier(
        id=f"mod_{len(world.modifiers)}_{meter_id}",
        meter_id=meter_id,
        kind=kind,
        magnitude=float(magnitude),
        expires_at_turn=(world.clock.turn + duration_turns
                         if duration_turns is not None else None),
        source=source,
    )
    world.modifiers.append(modifier)
    return modifier


def set_rate_formula(world: WorldState, meter_id: str, expression: str) -> str | None:
    """Replace a meter's rate formula, validating it first.

    Returns None on success or an error string to feed back to the model for a
    repair attempt — a formula is rejected at authoring time, never mid-tick.
    """
    meter = world.meters.get(meter_id)
    if meter is None:
        return f"unknown meter {meter_id!r}"
    variables = meter_variables(world, meter)
    variables["hours"] = 1.0
    try:
        evaluate(expression, variables)
    except FormulaError as e:
        return str(e)
    meter.rate_formula = expression
    return None
