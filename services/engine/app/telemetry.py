"""Structured logging for every model call — the layer that makes "it got worse"
investigable instead of anecdotal.

The whole reason this exists: an LLM-driven simulation degrades *quietly*. Prose
gets blander, an NPC starts saying things it shouldn't, a provider silently
falls back to a weaker model, and the only symptom is a vague sense that it used
to be better. Without a record you cannot tell which of those happened, and you
end up rewriting prompts by superstition.

## What is recorded

One row per model call, in SQLite (same DATABASE_URL as store.py, so it is one
file and one query surface). Rows are grouped by `game_id` and ordered by
`(turn, seq)`, so reading a game back is literally chronological — the calls in
the order they happened, interleaved across NPCs, referee and narrator.

The columns exist to answer specific questions:

- **"how badly did it hallucinate?"** — `violations` counts engine-side rejections
  in that one call: a belief index out of range, an unparseable `hours_elapsed`,
  an unknown action, a missing required key. These are exactly the things the
  engine already silently repairs, and silently repairing them is what makes
  degradation invisible. Now they are counted.
- **"at what length does it break?"** — `prompt_chars` and `response_chars`
  alongside `violations`, so you can bucket by size and see where reliability
  falls off. This is the question that most needs data and is impossible to
  answer from memory.
- **"was it even the model I think?"** — `provider` and `model`, because a
  benched provider falling through the chain to a weaker one is the single most
  common cause of unexplained quality loss.
- **"was it slow or was it wrong?"** — `latency_ms`, `ok`, `error`.

## Why a table and not a log file

You asked for queryable, and the questions above are all aggregations:
violations per provider, violation rate by prompt length, latency percentiles
per call kind. Those are one SELECT each against a table and an afternoon of
grep against a file. Same engine as store.py, same file, no new dependency.

Logging must never break a turn. Every write is wrapped: a telemetry failure
degrades to nothing at all, because a diagnostic system that can take down the
thing it is diagnosing is worse than no diagnostic system.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import (Boolean, Column, DateTime, Float, Integer, MetaData,
                        String, Table, Text, select)

from .store import DATABASE_URL, _connect_args  # one file, one query surface

metadata = MetaData()

llm_calls = Table(
    "llm_calls", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    # Grouping and ordering: (game_id, turn, seq) reads back chronologically.
    Column("game_id", String(64), index=True),
    Column("turn", Integer, index=True),
    Column("seq", Integer),
    Column("created_at", DateTime, nullable=False),

    # What kind of call: referee | intention | narration | scene_report |
    # orchestrate | worldgen. Lets you compare like with like.
    Column("kind", String(32), index=True),
    # Which character this call was voicing, when it was voicing one. The single
    # most useful filter for "why is this NPC acting wrong".
    Column("char_id", String(64), index=True),

    Column("provider", String(32), index=True),
    Column("model", String(120)),

    Column("prompt_chars", Integer),
    Column("response_chars", Integer),
    Column("latency_ms", Integer),
    Column("ok", Boolean),
    Column("error", Text, default=""),

    # Engine-side rejections in this call. See `violations` in the docstring —
    # this is the hallucination counter.
    Column("violations", Integer, default=0),
    Column("violation_kinds", Text, default="[]"),

    # Full text, for the post-mortem where aggregates are not enough. Kept last
    # so a SELECT of the numeric columns never drags them in.
    Column("prompt", Text, default=""),
    Column("response", Text, default=""),
)

_engine = None
_lock = threading.Lock()
_seq = 0

# Set by the API layer for the duration of a turn, so call sites deep in the
# engine do not have to thread a game id through every signature.
_context: dict = {"game_id": "", "turn": 0}


def _get_engine():
    global _engine
    if _engine is None:
        from sqlalchemy import create_engine
        _engine = create_engine(DATABASE_URL, future=True,
                                connect_args=_connect_args())
        metadata.create_all(_engine)
    return _engine


def init_db() -> None:
    """Create the table. Safe to call repeatedly."""
    try:
        _get_engine()
    except Exception:  # pragma: no cover - diagnostics must never break a turn
        pass


@contextmanager
def turn_context(game_id: str, turn: int):
    """Tag every call made inside this block with a game and turn.

    Module-level rather than threaded through signatures because the alternative
    is a `game_id` parameter on every prompt function in llm.py, which would put
    a telemetry concern into the one module whose job is guarding what models
    see. The tradeoff is that concurrent games in one process would interleave;
    the engine serialises a turn anyway, and correctness of the GAME never
    depends on this value.
    """
    global _seq
    previous = dict(_context)
    _context.update({"game_id": game_id, "turn": turn})
    _seq = 0
    try:
        yield
    finally:
        _context.update(previous)


def record(kind: str, prompt: str, response: str, *, char_id: str = "",
           provider: str = "", model: str = "", latency_ms: int = 0,
           ok: bool = True, error: str = "",
           violation_kinds: list[str] | None = None) -> None:
    """Write one row. Never raises."""
    global _seq
    kinds = violation_kinds or []
    try:
        with _lock:
            _seq += 1
            seq = _seq
        engine = _get_engine()
        with engine.begin() as conn:
            conn.execute(llm_calls.insert().values(
                game_id=_context.get("game_id", ""),
                turn=int(_context.get("turn", 0)),
                seq=seq,
                created_at=datetime.now(timezone.utc),
                kind=kind,
                char_id=char_id,
                provider=provider,
                model=model,
                prompt_chars=len(prompt or ""),
                response_chars=len(response or ""),
                latency_ms=latency_ms,
                ok=ok,
                error=(error or "")[:2000],
                violations=len(kinds),
                violation_kinds=json.dumps(kinds),
                prompt=prompt or "",
                response=response or "",
            ))
    except Exception:  # pragma: no cover - see module docstring
        pass


@contextmanager
def timed(kind: str, prompt: str, char_id: str = ""):
    """Time a call and record it however it ends.

    Usage keeps the call site honest — the row is written on the failure path
    too, which matters because an outage is exactly the kind of degradation this
    module exists to catch:

        with telemetry.timed("intention", prompt, char_id) as slot:
            reply = complete_json(...)
            slot.finish(reply, provider=..., violations=[...])
    """
    started = time.monotonic()
    slot = _Slot()
    try:
        yield slot
    except Exception as exc:
        record(kind, prompt, "", char_id=char_id, ok=False, error=repr(exc),
               latency_ms=int((time.monotonic() - started) * 1000))
        raise
    else:
        record(kind, prompt, slot.response, char_id=char_id,
               provider=slot.provider, model=slot.model,
               latency_ms=int((time.monotonic() - started) * 1000),
               ok=slot.ok, error=slot.error, violation_kinds=slot.violations)


class _Slot:
    """Mutable handle the caller fills in before the context closes."""

    def __init__(self) -> None:
        self.response = ""
        self.provider = ""
        self.model = ""
        self.ok = True
        self.error = ""
        self.violations: list[str] = []

    def finish(self, response: str = "", *, provider: str = "", model: str = "",
               ok: bool = True, error: str = "",
               violations: list[str] | None = None) -> None:
        self.response = response or ""
        self.provider = provider
        self.model = model
        self.ok = ok
        self.error = error
        self.violations = violations or []

    def violation(self, kind: str) -> None:
        """Flag one engine-side rejection. Call this wherever the engine already
        silently repairs a bad reply — silent repair is what makes degradation
        invisible."""
        self.violations.append(kind)


# ── reading it back ─────────────────────────────────────────────────────────

def game_log(game_id: str, limit: int = 500) -> list[dict]:
    """Every call in one game, in the order it happened.

    This is the "group by chat, then chronological" view: ordering by
    (turn, seq) reconstructs the turn exactly as it ran, interleaved across
    NPCs, referee and narrator.
    """
    try:
        engine = _get_engine()
        with engine.begin() as conn:
            rows = conn.execute(
                select(llm_calls)
                .where(llm_calls.c.game_id == game_id)
                .order_by(llm_calls.c.turn, llm_calls.c.seq)
                .limit(limit)).mappings().all()
        return [dict(r) for r in rows]
    except Exception:  # pragma: no cover
        return []


def health(game_id: str | None = None) -> dict:
    """Aggregates that answer 'has it got worse, and where'.

    Deliberately returns the breakdowns rather than a single score: a drop in
    quality is almost always localised to one provider, one call kind, or one
    length band, and an average hides exactly that.
    """
    try:
        engine = _get_engine()
        with engine.begin() as conn:
            query = select(llm_calls)
            if game_id:
                query = query.where(llm_calls.c.game_id == game_id)
            rows = conn.execute(query).mappings().all()
    except Exception:  # pragma: no cover
        return {}

    if not rows:
        return {"calls": 0}

    def bucket(n: int) -> str:
        for edge in (1000, 2000, 4000, 8000, 16000):
            if n < edge:
                return f"<{edge}"
        return ">=16000"

    by_provider: dict[str, dict] = {}
    by_kind: dict[str, dict] = {}
    by_length: dict[str, dict] = {}

    for row in rows:
        for key, table in ((row["provider"] or "?", by_provider),
                           (row["kind"] or "?", by_kind),
                           (bucket(row["prompt_chars"] or 0), by_length)):
            slot = table.setdefault(key, {"calls": 0, "violations": 0,
                                          "failures": 0, "latency_ms": 0})
            slot["calls"] += 1
            slot["violations"] += row["violations"] or 0
            slot["failures"] += 0 if row["ok"] else 1
            slot["latency_ms"] += row["latency_ms"] or 0

    for table in (by_provider, by_kind, by_length):
        for slot in table.values():
            calls = max(1, slot["calls"])
            slot["violation_rate"] = round(slot["violations"] / calls, 3)
            slot["failure_rate"] = round(slot["failures"] / calls, 3)
            slot["avg_latency_ms"] = round(slot["latency_ms"] / calls)
            del slot["latency_ms"]

    return {
        "calls": len(rows),
        "violations": sum(r["violations"] or 0 for r in rows),
        "failures": sum(0 if r["ok"] else 1 for r in rows),
        "by_provider": by_provider,
        "by_kind": by_kind,
        # The band that most needs data: where does reliability fall off?
        "by_prompt_length": by_length,
    }
