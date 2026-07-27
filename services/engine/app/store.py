"""Persistence — every turn is a snapshot, which is what makes rewind free.

SQLAlchemy Core over SQLite. The schema is deliberately ordinary (no SQLite-only
types, no JSON1 functions) so moving to Postgres is a DATABASE_URL change and
nothing else — worth the small ceremony now, given the compose stack is already
microservices.

Storing the FULL world per turn rather than a diff is the right trade here: a
world is a few hundred KB of JSON, a long game is a few hundred turns, and being
able to load any turn directly means rewind is a SELECT rather than a replay.
It also doubles as the anti-cheat inspector — you can diff turn N against N+1 and
see exactly what the engine did, with the hidden state included.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

from sqlalchemy import (Column, DateTime, Integer, MetaData, String, Table, Text,
                        create_engine, delete, desc, select)

from .models import WorldState

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:////data/rp.db")

metadata = MetaData()

games = Table(
    "games", metadata,
    Column("id", String(64), primary_key=True),
    Column("seed", String(64), nullable=False),
    Column("title", String(200), default=""),
    Column("created_at", DateTime, nullable=False),
)

snapshots = Table(
    "snapshots", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("game_id", String(64), nullable=False, index=True),
    Column("turn", Integer, nullable=False, index=True),
    # Text, not a JSON column type — keeps SQLite and Postgres byte-identical.
    Column("state", Text, nullable=False),
    Column("narration", Text, default=""),
    Column("dm_log", Text, default="[]"),
    Column("player_action", Text, default=""),
    Column("created_at", DateTime, nullable=False),
)


def _connect_args() -> dict:
    # FastAPI serves requests on a threadpool; SQLite's default same-thread check
    # would reject those connections.
    return {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}


engine = create_engine(DATABASE_URL, connect_args=_connect_args(), future=True)


def init_db() -> None:
    if DATABASE_URL.startswith("sqlite:///"):
        path = DATABASE_URL.removeprefix("sqlite:///")
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
    metadata.create_all(engine)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── games ───────────────────────────────────────────────────────────────────

def create_game(world: WorldState, seed: str, title: str = "") -> str:
    game_id = uuid.uuid4().hex[:12]
    with engine.begin() as connection:
        connection.execute(games.insert().values(
            id=game_id, seed=seed, title=title, created_at=_now()))
    save_snapshot(game_id, world, narration="", dm_log=[], player_action="")
    return game_id


def list_games() -> list[dict]:
    with engine.begin() as connection:
        rows = connection.execute(
            select(games).order_by(desc(games.c.created_at))).mappings().all()
        out = []
        for row in rows:
            latest = connection.execute(
                select(snapshots.c.turn)
                .where(snapshots.c.game_id == row["id"])
                .order_by(desc(snapshots.c.turn)).limit(1)).scalar()
            out.append({"id": row["id"], "seed": row["seed"],
                        "title": row["title"],
                        "created_at": row["created_at"].isoformat(),
                        "turn": latest or 0})
        return out


def delete_game(game_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(delete(snapshots).where(snapshots.c.game_id == game_id))
        connection.execute(delete(games).where(games.c.id == game_id))


def get_seed(game_id: str) -> str | None:
    with engine.begin() as connection:
        return connection.execute(
            select(games.c.seed).where(games.c.id == game_id)).scalar()


# ── snapshots ───────────────────────────────────────────────────────────────

def save_snapshot(game_id: str, world: WorldState, narration: str,
                  dm_log: list[str], player_action: str = "") -> None:
    with engine.begin() as connection:
        connection.execute(snapshots.insert().values(
            game_id=game_id,
            turn=world.clock.turn,
            state=world.model_dump_json(),
            narration=narration,
            dm_log=json.dumps(dm_log),
            player_action=player_action,
            created_at=_now()))


def load_world(game_id: str, turn: int | None = None) -> WorldState | None:
    """Load the world at `turn`, or the latest if turn is None."""
    query = select(snapshots.c.state).where(snapshots.c.game_id == game_id)
    query = (query.where(snapshots.c.turn == turn) if turn is not None
             else query.order_by(desc(snapshots.c.turn)))
    with engine.begin() as connection:
        raw = connection.execute(query.limit(1)).scalar()
    return WorldState.model_validate_json(raw) if raw else None


def history(game_id: str) -> list[dict]:
    """Every turn's narration — this is what the chat UI replays on load."""
    with engine.begin() as connection:
        rows = connection.execute(
            select(snapshots.c.turn, snapshots.c.narration,
                   snapshots.c.player_action, snapshots.c.dm_log)
            .where(snapshots.c.game_id == game_id)
            .order_by(snapshots.c.turn)).mappings().all()
    return [{"turn": r["turn"], "narration": r["narration"],
             "player_action": r["player_action"],
             "dm_log": json.loads(r["dm_log"] or "[]")} for r in rows]


def rewind(game_id: str, to_turn: int) -> WorldState | None:
    """Restore an earlier turn and discard everything after it.

    Destructive on purpose: branching timelines would need a tree, and this is a
    single-player game where 'that went badly, take it back' is the actual need.
    The snapshot AT to_turn is kept — you rewind TO a turn, not past it.
    """
    world = load_world(game_id, to_turn)
    if world is None:
        return None
    with engine.begin() as connection:
        connection.execute(delete(snapshots).where(
            (snapshots.c.game_id == game_id) & (snapshots.c.turn > to_turn)))
    return world


def truth_report(world: WorldState) -> dict:
    """The post-game payoff: what was actually true beside what the player believed.

    This is the ONLY place `is_true` is allowed out of the engine, and only for a
    finished game — during play it would collapse the entire mystery.
    """
    player_beliefs = {b.fact_id: b for b in world.beliefs_of(world.player_id)}
    rows = []
    for fact in world.facts.values():
        belief = player_beliefs.get(fact.id)
        rows.append({
            "content": fact.content,
            "was_true": fact.is_true,
            "you_believed": belief.stance.value if belief else "never learned",
            "confidence": belief.confidence if belief else 0.0,
            "told_by": (world.characters[belief.source_char_id].name
                        if belief and belief.source_char_id
                        and belief.source_char_id in world.characters else None),
            "deceived": bool(belief and not fact.is_true),
        })
    rows.sort(key=lambda r: (not r["deceived"], r["you_believed"] == "never learned"))
    return {"facts": rows,
            "deceptions": sum(1 for r in rows if r["deceived"]),
            "never_learned": sum(1 for r in rows
                                 if r["you_believed"] == "never learned"),
            "chronicle": world.chronicle}
