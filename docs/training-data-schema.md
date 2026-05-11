# Training Data Schema

This document describes the SQLite tables RentWise writes per-user to
`api/data/users.db` (path overridable via `RENTWISE_DB_PATH`). All tables
are append-only except `user_profiles` (which upserts the latest snapshot
per user). The schema is designed so the data can later be exported as
JSONL for fine-tuning preference models, evaluating recommendation
quality, or training a ranker reward signal.

## Tables

### `users` — auth credentials

| Column          | Type | Notes                                  |
|-----------------|------|----------------------------------------|
| `id`            | TEXT | UUID v4. Primary key.                  |
| `email`         | TEXT | Lowercased, unique.                    |
| `password_hash` | TEXT | bcrypt over SHA256(password). See `api/auth.py:_hash_password`. |
| `created_at`    | REAL | Unix epoch seconds.                    |

### `user_profiles` — latest persisted UserProfile

One row per user, upserted each `/chat` turn.

| Column         | Type | Notes                                                            |
|----------------|------|------------------------------------------------------------------|
| `user_id`      | TEXT | PK; FK → users.id                                                |
| `profile_json` | TEXT | JSON dump of `UserProfile` dataclass (budget, beds, commute, must_haves, nice_to_haves, avoid, constraints, weights, …). |
| `updated_at`   | REAL | Unix epoch seconds.                                              |

### `chat_events` — per-turn append log

One row per authenticated `/chat` call.

| Column                  | Type | Notes |
|-------------------------|------|-------|
| `id`                    | TEXT | UUID v4. Primary key. Echoed to frontend in `response.metadata.chat_event_id`. |
| `user_id`               | TEXT | FK → users.id                                                                  |
| `session_id`            | TEXT | Frontend-generated session id (per browser tab).                               |
| `timestamp`             | REAL | Unix epoch seconds, server time.                                               |
| `user_message`          | TEXT | Raw user input.                                                                |
| `agent_id`              | TEXT | Which agent handled the turn: `search` / `property` / `location` / `outreach` / `reviews`. |
| `router_reason`         | TEXT | One-line rationale from AgentRouter for why this agent.                        |
| `profile_before_json`   | TEXT | JSON snapshot of UserProfile *before* this turn.                               |
| `profile_after_json`    | TEXT | JSON snapshot *after* ProfileUpdater applied the patch.                        |
| `ranked_zpids_json`     | TEXT | JSON array of zpids in the top-5 / top-N the user saw, in display order.       |
| `reply_text`            | TEXT | Markdown response shown to user.                                               |

### `interaction_events` — user actions

Append-only; rows added when frontend POSTs to `/events/track` (also
auto-emitted by `/shortlist/remove`).

| Column            | Type    | Notes                                                              |
|-------------------|---------|--------------------------------------------------------------------|
| `id`              | TEXT    | UUID v4. PK.                                                       |
| `user_id`         | TEXT    | FK → users.id                                                      |
| `session_id`      | TEXT    | Same session id as the chat_event that surfaced this listing.      |
| `chat_event_id`   | TEXT    | Optional FK → chat_events.id. Links the action to the turn that surfaced the listing. |
| `timestamp`       | REAL    | Unix epoch seconds.                                                |
| `event_type`      | TEXT    | `click` / `save` / `remove` / `show_more` / `external_link`.       |
| `zpid`            | TEXT    | Affected listing.                                                  |
| `rank_position`   | INTEGER | 1-based rank in the list when this event fired (1 = top recommendation). |
| `extra_json`      | TEXT    | Optional free-form JSON (e.g. dwell time on listing card, scroll depth). |

## Derived training signals

These tables let downstream training jobs compute, per chat turn:

| Signal | Computation |
|--------|-------------|
| **Implicit positive** | `chat_event` followed within session by `interaction_event(event_type IN ('click','save'))` referencing one of `ranked_zpids` |
| **Implicit negative** | `chat_event` ranked_zpids that received no interaction *and* the session continued with more chat turns (user kept searching → these didn't satisfy them) |
| **Strong negative**   | `interaction_event(event_type='remove')` on a zpid that was in `ranked_zpids` |
| **Preference drift**  | Diff `profile_before_json` vs `profile_after_json` over chat_events — captures how user preferences evolve across the conversation |
| **Ranking miss**      | Cases where `rank_position` of clicked listing is > 3 → ranker should have surfaced it higher |

## Privacy / retention

- All data is per-user; no cross-user leakage.
- Passwords are stored only as bcrypt(SHA256(password)) — irreversible.
- JWT tokens are 30-day; signed with `JWT_SECRET` (env var; randomly
  generated per process if unset — restarts invalidate all tokens).
- Email is the only PII stored. If the user requests deletion, cascade-
  delete by `user_id` across all 4 tables.

## Export for training

```bash
sqlite3 -json api/data/users.db "
SELECT
  ce.timestamp,
  ce.user_id,
  ce.session_id,
  ce.user_message,
  ce.agent_id,
  ce.profile_before_json,
  ce.profile_after_json,
  ce.ranked_zpids_json,
  (
    SELECT json_group_array(json_object(
      'type', ie.event_type,
      'zpid', ie.zpid,
      'rank', ie.rank_position,
      'ts', ie.timestamp
    ))
    FROM interaction_events ie
    WHERE ie.chat_event_id = ce.id
  ) AS interactions
FROM chat_events ce
ORDER BY ce.timestamp ASC
" > training_export.jsonl
```

Each line is a self-contained sample: the user turn (input), the
extracted profile delta (state change), the listings shown (action), and
the user's subsequent reactions (reward signal).
