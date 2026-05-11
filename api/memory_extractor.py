"""Long-term memory extractor.

Runs opportunistically after each /chat turn: looks at the user's latest
message + the profile diff and decides whether anything *durable* (lasts
beyond this search session) should be captured.

Durable facts vs ephemeral preferences:
  • DURABLE: family makeup, vehicle type, contract length, schools,
    chronic conditions, professional context ("travel nurse", "single
    mom of two", "drives an F-250")
  • EPHEMERAL: budget for this search, neighborhoods today, must_haves
    for this listing trip — these already live in UserProfile

We keep memory cheap: only a handful of keys (max ~20), each a short
free-text fact. The whole thing is read into prompts as JSON.
"""
from __future__ import annotations

import json
import logging
import os

from anthropic import Anthropic

LOG = logging.getLogger(__name__)

EXTRACT_PROMPT = """You are a memory curator for RentWise. The user just
sent a message. Decide if any DURABLE personal fact about them should
be saved to long-term memory.

A durable fact:
  • Persists beyond this rental search ("ages of children", "drives a
    pickup truck", "13-week travel nurse contract", "celiac disease")
  • Will still be true 6 months from now if mentioned
  • Is something a friend would remember and bring up later

NOT a durable fact:
  • Today's budget / bed count / commute (already in UserProfile)
  • Today's neighborhood preferences (already in UserProfile)
  • Decisions about specific listings ("I like Modera Rincon Hill") —
    those are interaction history, not personal facts

CURRENT MEMORY (don't duplicate):
{current_memory}

USER MESSAGE:
"{message}"

Output a JSON object with new memory keys, OR an empty object {{}} if
nothing durable was revealed.

Example:
  {{"vehicle": "F-250 pickup", "kids": "two, ages 5 and 8"}}

Output ONLY the JSON object — no prose, no fences."""


def extract_durable_facts(message: str, current_memory: dict, client: Anthropic | None = None) -> dict:
    """Return new fact dict (may be empty) to merge into long-term memory."""
    if not message or len(message.strip()) < 10:
        return {}
    if not client:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return {}
        client = Anthropic()
    prompt = EXTRACT_PROMPT.format(
        current_memory=json.dumps(current_memory or {}, indent=2, ensure_ascii=False),
        message=message[:2000],
    )
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:].strip()
        data = json.loads(raw)
        if isinstance(data, dict):
            # Filter to short string values only — memory should stay compact
            cleaned = {}
            for k, v in data.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    continue
                k_clean = k.strip().lower().replace(" ", "_")[:32]
                v_clean = v.strip()[:200]
                if k_clean and v_clean and k_clean not in current_memory:
                    cleaned[k_clean] = v_clean
            return cleaned
    except (json.JSONDecodeError, Exception) as e:
        LOG.debug("memory extraction failed: %s", e)
    return {}
