"""Shared context object passed to ALL agents on every turn.

Centralizes:
  • full UserProfile dict (budget, beds, commute lat/lng, must_haves, …)
  • recent turn history with agent attribution (last 5 turns)
  • long-term memory loaded from db (cross-session facts about this user)
  • listings currently in scope (rich detail for context-aware answers)

The idea: instead of each agent only seeing partial data (LocationAgent
saw listings only, OutreachAgent saw user_name only), every agent
receives the same structured ShareContext and can reference what the
*other* agents recently said. This lets them "collaborate" — Location
agent can say "As Search ranked Mountain View #1 for its proximity to
Apple Park…", Property agent can note "Reviews flagged thin walls here…"
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any


def _profile_to_dict(profile) -> dict:  # noqa: ANN001
    """Convert UserProfile dataclass → JSON-safe dict for LLM prompts.
    Includes commute lat/lng + max_minutes so agents know the user's
    work location.
    """
    try:
        d = asdict(profile)
    except (TypeError, ValueError):
        d = {}
    # Strip empty defaults so prompts stay compact
    return {k: v for k, v in d.items() if v not in (None, "", [], {}, 0)}


def _recent_turns(session, k: int = 5) -> list[dict]:  # noqa: ANN001
    """Last k turns with agent attribution. Most recent first."""
    out = []
    for t in list(session.history)[-(k * 2):]:  # turns include user+agent rows
        out.append({
            "role": t.role,
            "agent": t.agent,
            "text": (t.text or "")[:600],  # truncate for prompt budget
        })
    return out


def _other_agent_responses(session, current_agent: str) -> list[dict]:  # noqa: ANN001
    """Last response from each OTHER agent in this session. Lets the
    current agent reference what its peers recently said.
    """
    seen: dict[str, dict] = {}
    for t in reversed(list(session.history)):
        if t.role == "agent" and t.agent and t.agent != current_agent and t.agent not in seen:
            seen[t.agent] = {
                "agent": t.agent,
                "text": (t.text or "")[:500],
            }
        if len(seen) >= 4:
            break
    return list(seen.values())


def build_shared_context(session, current_agent: str) -> dict:  # noqa: ANN001
    """Construct the shared-context dict every agent should consult.

    Args:
      session: the Session object for this turn
      current_agent: name of the agent calling this (so we can exclude
        its own prior responses from "other_agents")

    Returns a JSON-safe dict that can be serialized into an LLM prompt.
    """
    return {
        "user_profile": _profile_to_dict(session.profile),
        "long_term_memory": session.long_term_memory or {},
        "recent_turns": _recent_turns(session, k=5),
        "other_agents_recent": _other_agent_responses(session, current_agent),
        "listings_in_scope_count": len(session.listings_in_scope),
        "shortlist_count": len(session.shortlist),
    }


def shared_context_prompt_block(ctx: dict) -> str:
    """Format the shared context into a human-readable block for LLM
    prompts. Designed to be cheap (under 600 tokens typical) and
    self-explanatory so the LLM uses it intuitively.
    """
    import json
    lines = ["=== SHARED CONTEXT (everything other agents know about this user) ===\n"]
    prof = ctx.get("user_profile") or {}
    if prof:
        lines.append("USER PROFILE:")
        lines.append(json.dumps(prof, indent=2, default=str))
        lines.append("")
    mem = ctx.get("long_term_memory") or {}
    if mem:
        lines.append("LONG-TERM MEMORY (durable facts across sessions):")
        lines.append(json.dumps(mem, indent=2, default=str))
        lines.append("")
    others = ctx.get("other_agents_recent") or []
    if others:
        lines.append("WHAT OTHER AGENTS RECENTLY TOLD THE USER:")
        for r in others:
            lines.append(f"  @{r['agent']}: {r['text']}")
        lines.append("")
    lines.append(f"(Listings in scope: {ctx.get('listings_in_scope_count', 0)}, "
                 f"shortlist size: {ctx.get('shortlist_count', 0)})")
    lines.append("=== END SHARED CONTEXT ===\n")
    lines.append("Use this context to ground your answer in what's already known. "
                 "Reference other agents by name (e.g. \"as @search just noted…\") "
                 "when relevant. Never ask the user for information that's already "
                 "in user_profile or long_term_memory.")
    return "\n".join(lines)
