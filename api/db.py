"""SQLite database for users + persisted profiles + chat event log.

Schema kept intentionally minimal — single-file SQLite, no migrations
framework. Tables:

  users          — auth credentials
  user_profiles  — latest serialized UserProfile per user
  chat_events    — append-only training log: every /chat turn + extracted
                   profile diff + ranked zpids; later /events/click and
                   /events/save calls write back which listing got the
                   user's attention.

All write ops are wrapped in short connections (sqlite is fine with that;
the file lives in api/data/users.db).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

DB_PATH = Path(os.environ.get("RENTWISE_DB_PATH") or (
    Path(__file__).resolve().parent / "data" / "users.db"
))


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


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create schema if not exists. Idempotent."""
    with _connect() as conn:
        conn.executescript(_SCHEMA)
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

def save_profile(user_id: str, profile_dict: dict) -> None:
    payload = json.dumps(profile_dict, default=str, ensure_ascii=False)
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO user_profiles(user_id, profile_json, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET profile_json=excluded.profile_json, "
            "updated_at=excluded.updated_at",
            (user_id, payload, now),
        )
        conn.commit()


def load_profile(user_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT profile_json FROM user_profiles WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["profile_json"])
    except (json.JSONDecodeError, KeyError):
        return None


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
