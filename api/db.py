"""Database backend for users + persisted profiles + chat event log.

Backend selection:
  • DATABASE_URL starts with "postgres://" or "postgresql://" → psycopg 3
  • Otherwise (or unset) → SQLite at RENTWISE_DB_PATH or api/data/users.db

All public functions in this module work identically against both
backends. Internally we keep SQL written with `?` placeholders (SQLite
style); for psycopg we translate them to `%s` on the fly. Both backends
support `ON CONFLICT` upsert (Postgres 9.5+ / SQLite 3.24+).

Tables:
  users          — auth credentials
  user_profiles  — latest UserProfile + long-term memory per user
  chat_events    — append-only training log
  interaction_events — click/save/remove events
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

DB_PATH = Path(os.environ.get("RENTWISE_DB_PATH") or (
    Path(__file__).resolve().parent / "data" / "users.db"
))

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
# Render gives postgres://; psycopg prefers postgresql://. Normalize.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

IS_POSTGRES = DATABASE_URL.startswith("postgresql://")

if IS_POSTGRES:
    import psycopg  # type: ignore
    from psycopg.rows import dict_row  # type: ignore


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS user_profiles (
    user_id      TEXT PRIMARY KEY,
    profile_json TEXT NOT NULL,
    memory_json  TEXT,           -- long-term cross-session memory
    updated_at   REAL NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS chat_events (
    id                       TEXT PRIMARY KEY,
    user_id                  TEXT NOT NULL,
    session_id               TEXT NOT NULL,
    timestamp                REAL NOT NULL,
    user_message             TEXT NOT NULL,
    agent_id                 TEXT,
    router_reason            TEXT,
    profile_before_json      TEXT,
    profile_after_json       TEXT,
    ranked_zpids_json        TEXT,
    reply_text               TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_chat_events_user_ts ON chat_events(user_id, timestamp);

CREATE TABLE IF NOT EXISTS interaction_events (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    chat_event_id   TEXT,
    timestamp       REAL NOT NULL,
    event_type      TEXT NOT NULL,  -- 'click' | 'save' | 'remove' | 'show_more'
    zpid            TEXT,
    rank_position   INTEGER,
    extra_json      TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_interaction_events_user_ts ON interaction_events(user_id, timestamp);
"""


class _ConnWrap:
    """Thin context-manager wrapper so the rest of db.py can `with _connect() as c:`
    against either sqlite3.Connection or psycopg.Connection. Translates `?` →
    `%s` placeholders for psycopg on the fly. Row factory returns dict-like
    objects for both backends so `row["email"]` works uniformly.
    """

    def __init__(self):
        if IS_POSTGRES:
            self._conn = psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=False)
        else:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
            self._conn.row_factory = sqlite3.Row
        self._is_pg = IS_POSTGRES

    def execute(self, sql: str, params: tuple = ()):
        if self._is_pg:
            sql = sql.replace("?", "%s")
            cur = self._conn.cursor()
            cur.execute(sql, params)
            return cur
        return self._conn.execute(sql, params)

    def executescript(self, sql: str):
        if self._is_pg:
            # Postgres has no executescript — split on `;` and run each.
            cur = self._conn.cursor()
            for stmt in sql.split(";"):
                if stmt.strip():
                    cur.execute(stmt)
            return cur
        return self._conn.executescript(sql)

    def commit(self):
        self._conn.commit()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            try:
                self._conn.commit()
            except Exception:
                pass
        else:
            try:
                self._conn.rollback()
            except Exception:
                pass
        self._conn.close()


def _connect() -> _ConnWrap:
    return _ConnWrap()


def init_db() -> None:
    """Create schema if not exists. Idempotent. Includes a tiny migration
    that adds memory_json column to user_profiles if pre-existing rows
    didn't have it (so the long-term-memory upgrade is non-breaking).

    Schema is written to work on both SQLite and Postgres. `REAL` is
    valid on SQLite and is a 32-bit float on Postgres — we use it for
    Unix epoch seconds where 32-bit precision is fine.
    """
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        # Check whether memory_json column exists, then add if missing.
        if IS_POSTGRES:
            cur = conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = ? AND column_name = ?",
                ("user_profiles", "memory_json"),
            )
            exists = bool(cur.fetchone())
        else:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(user_profiles)").fetchall()]
            exists = "memory_json" in cols
        if not exists:
            try:
                conn.execute("ALTER TABLE user_profiles ADD COLUMN memory_json TEXT")
            except Exception:
                pass
        conn.commit()


@dataclass
class UserRow:
    id: str
    email: str
    created_at: float


# --- users / auth ----------------------------------------------------------

def create_user(email: str, password_hash: str) -> UserRow:
    uid = str(uuid.uuid4())
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO users(id, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (uid, email.lower().strip(), password_hash, now),
        )
        conn.commit()
    return UserRow(id=uid, email=email.lower().strip(), created_at=now)


def get_user_by_email(email: str) -> tuple[UserRow, str] | None:
    """Return (UserRow, password_hash) or None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, email, password_hash, created_at FROM users WHERE email = ?",
            (email.lower().strip(),),
        ).fetchone()
    if not row:
        return None
    return (
        UserRow(id=row["id"], email=row["email"], created_at=row["created_at"]),
        row["password_hash"],
    )


def get_user_by_id(user_id: str) -> UserRow | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, email, created_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    return UserRow(id=row["id"], email=row["email"], created_at=row["created_at"])


# --- profile snapshots -----------------------------------------------------

def save_profile(user_id: str, profile_dict: dict, memory_dict: dict | None = None) -> None:
    payload = json.dumps(profile_dict, default=str, ensure_ascii=False)
    mem_payload = json.dumps(memory_dict or {}, default=str, ensure_ascii=False) if memory_dict is not None else None
    now = time.time()
    with _connect() as conn:
        # First ensure the row exists with both columns; ON CONFLICT to update
        # selectively so a save_profile call doesn't clobber existing memory.
        if mem_payload is not None:
            conn.execute(
                "INSERT INTO user_profiles(user_id, profile_json, memory_json, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "profile_json=excluded.profile_json, "
                "memory_json=excluded.memory_json, "
                "updated_at=excluded.updated_at",
                (user_id, payload, mem_payload, now),
            )
        else:
            conn.execute(
                "INSERT INTO user_profiles(user_id, profile_json, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "profile_json=excluded.profile_json, "
                "updated_at=excluded.updated_at",
                (user_id, payload, now),
            )
        conn.commit()


def load_profile(user_id: str) -> tuple[dict | None, dict]:
    """Return (profile_dict, memory_dict). memory is always a dict (empty
    if none stored). profile is None when no row exists.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT profile_json, memory_json FROM user_profiles WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return None, {}
    try:
        prof = json.loads(row["profile_json"])
    except (json.JSONDecodeError, KeyError):
        prof = None
    mem = {}
    raw_mem = row["memory_json"] if "memory_json" in row.keys() else None
    if raw_mem:
        try:
            mem = json.loads(raw_mem) or {}
        except json.JSONDecodeError:
            pass
    return prof, mem


def save_memory(user_id: str, memory_dict: dict) -> None:
    """Persist long-term memory dict; merges with existing (not replace)."""
    _, existing = load_profile(user_id)
    merged = {**existing, **memory_dict}
    payload = json.dumps(merged, default=str, ensure_ascii=False)
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO user_profiles(user_id, profile_json, memory_json, updated_at) "
            "VALUES (?, '{}', ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "memory_json=excluded.memory_json, updated_at=excluded.updated_at",
            (user_id, payload, now),
        )
        conn.commit()


def replace_memory(user_id: str, memory_dict: dict) -> None:
    """Replace entire long-term memory (used by user-facing PUT /memory)."""
    payload = json.dumps(memory_dict, default=str, ensure_ascii=False)
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO user_profiles(user_id, profile_json, memory_json, updated_at) "
            "VALUES (?, '{}', ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "memory_json=excluded.memory_json, updated_at=excluded.updated_at",
            (user_id, payload, now),
        )
        conn.commit()


def delete_memory_key(user_id: str, key: str) -> bool:
    """Remove a single key from memory. Returns True if the key existed."""
    _, existing = load_profile(user_id)
    if key not in existing:
        return False
    del existing[key]
    replace_memory(user_id, existing)
    return True


# --- chat event log (training data) ---------------------------------------

def log_chat_event(
    user_id: str,
    session_id: str,
    user_message: str,
    agent_id: str | None,
    router_reason: str | None,
    profile_before: dict | None,
    profile_after: dict | None,
    ranked_zpids: list[str] | None,
    reply_text: str | None,
) -> str:
    """Append a row to chat_events. Returns the event id (UUID)."""
    eid = str(uuid.uuid4())
    with _connect() as conn:
        conn.execute(
            "INSERT INTO chat_events(id, user_id, session_id, timestamp, user_message, "
            "agent_id, router_reason, profile_before_json, profile_after_json, "
            "ranked_zpids_json, reply_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                eid, user_id, session_id, time.time(), user_message,
                agent_id, router_reason,
                json.dumps(profile_before, default=str) if profile_before else None,
                json.dumps(profile_after, default=str) if profile_after else None,
                json.dumps(ranked_zpids) if ranked_zpids else None,
                reply_text,
            ),
        )
        conn.commit()
    return eid


def export_user_data(user_id: str) -> dict:
    """Return all rows for this user across the 4 tables, suitable for
    GDPR data-portability requests. Returns a dict ready to serialize.
    """
    with _connect() as conn:
        u = conn.execute(
            "SELECT id, email, created_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        prof = conn.execute(
            "SELECT profile_json, updated_at FROM user_profiles WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        chats = conn.execute(
            "SELECT id, session_id, timestamp, user_message, agent_id, "
            "       router_reason, profile_before_json, profile_after_json, "
            "       ranked_zpids_json, reply_text "
            "FROM chat_events WHERE user_id = ? ORDER BY timestamp ASC",
            (user_id,),
        ).fetchall()
        interactions = conn.execute(
            "SELECT id, session_id, chat_event_id, timestamp, event_type, "
            "       zpid, rank_position, extra_json "
            "FROM interaction_events WHERE user_id = ? ORDER BY timestamp ASC",
            (user_id,),
        ).fetchall()
    if not u:
        return {}
    return {
        "user": {"id": u["id"], "email": u["email"], "created_at": u["created_at"]},
        "profile": {
            "profile_json": (json.loads(prof["profile_json"]) if prof else None),
            "updated_at": (prof["updated_at"] if prof else None),
        },
        "chat_events": [
            {
                "id": r["id"], "session_id": r["session_id"],
                "timestamp": r["timestamp"], "user_message": r["user_message"],
                "agent_id": r["agent_id"], "router_reason": r["router_reason"],
                "profile_before": (
                    json.loads(r["profile_before_json"]) if r["profile_before_json"] else None
                ),
                "profile_after": (
                    json.loads(r["profile_after_json"]) if r["profile_after_json"] else None
                ),
                "ranked_zpids": (
                    json.loads(r["ranked_zpids_json"]) if r["ranked_zpids_json"] else []
                ),
                "reply_text": r["reply_text"],
            }
            for r in chats
        ],
        "interactions": [
            {
                "id": r["id"], "session_id": r["session_id"],
                "chat_event_id": r["chat_event_id"], "timestamp": r["timestamp"],
                "event_type": r["event_type"], "zpid": r["zpid"],
                "rank_position": r["rank_position"],
                "extra": (json.loads(r["extra_json"]) if r["extra_json"] else None),
            }
            for r in interactions
        ],
    }


def delete_user_cascade(user_id: str) -> dict:
    """GDPR: irreversibly delete this user's row + all dependent rows
    across the 4 tables. Returns row counts deleted.
    """
    counts = {}
    with _connect() as conn:
        cur = conn.execute("DELETE FROM interaction_events WHERE user_id = ?", (user_id,))
        counts["interaction_events"] = cur.rowcount
        cur = conn.execute("DELETE FROM chat_events WHERE user_id = ?", (user_id,))
        counts["chat_events"] = cur.rowcount
        cur = conn.execute("DELETE FROM user_profiles WHERE user_id = ?", (user_id,))
        counts["user_profiles"] = cur.rowcount
        cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        counts["users"] = cur.rowcount
        conn.commit()
    return counts


def log_interaction(
    user_id: str,
    session_id: str,
    event_type: str,
    zpid: str | None = None,
    rank_position: int | None = None,
    chat_event_id: str | None = None,
    extra: dict | None = None,
) -> str:
    iid = str(uuid.uuid4())
    with _connect() as conn:
        conn.execute(
            "INSERT INTO interaction_events(id, user_id, session_id, chat_event_id, "
            "timestamp, event_type, zpid, rank_position, extra_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                iid, user_id, session_id, chat_event_id, time.time(),
                event_type, zpid, rank_position,
                json.dumps(extra) if extra else None,
            ),
        )
        conn.commit()
    return iid
