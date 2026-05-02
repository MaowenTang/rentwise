"""Agent Router — LLM-driven dispatch.

Reads the latest user message + recent chat history + current listings
in scope, then picks ONE agent to handle the turn. Multi-agent fan-out
is v1 per the design spec §3.A1.

Explicit @mentions override the router:
  @search ...    → SearchAgent
  @property ...  → PropertyAnalystAgent
  @location ...  → LocationCommuteAgent
  @outreach ...  → OutreachAgent
"""
from __future__ import annotations

import json
import re

from .base import BaseAgent

ROUTE_PROMPT = """You are the Agent Router for RentWise, a multi-agent
apartment-hunting chat. Decide which ONE agent should respond.

Available agents:

  "search" — Find NEW listings or re-search. Use when the user is
    describing what kind of place they want and expects a fresh search:
      • "find me a 2br near downtown"
      • "show me cheaper options"
      • "any pet-friendly studios?"
      • "what other places are like that one?"
    Do NOT use search if the user is asking about listings that are
    already in scope.

  "property" — Detailed FACTS about listings already in scope.
      • "does The James include water?"  (utilities)
      • "what's the deposit on the second one?"
      • "compare 1br pricing across these three"
      • "is parking included?"
      • "what amenities does Miro have?"
      • "is it pet-friendly?"

  "location" — Anything GEOGRAPHIC, COMMUTE, or SURROUNDINGS:
      • walk / transit / bike scores
      • schools nearby
      • commute time / distance to a place
      • "what grocery stores / restaurants / parks / hospitals / gyms / cafes are nearby?"
      • "how walkable is this area?"
      • "what's the neighborhood like?"
      • "how far is each from <place>?"
      • Any mention of: groceries, supermarket, Whole Foods, Trader Joe's,
        restaurants, dining, cafes, parks, gym, fitness, hospital,
        doctor, school, daycare, transit, BART, VTA, freeway, highway,
        commute, drive, walking distance, miles, blocks, neighborhood.

  "outreach" — Drafting / sending inquiry messages to leasing offices.
      • "email them about pet fees"
      • "reach out to ask about availability"
      • "draft a tour request"
      • "send a message to the top one"

PENDING CLARIFICATION: {pending}
  → If non-null, the named agent asked a question last turn and is
    waiting for an answer. The user's message is most likely answering
    that — route to the named agent UNLESS the user clearly switched
    topics.

CONTEXT:
  listings_in_scope: {n_in_scope} listing(s) currently surfaced
  recent_messages:
{recent}

USER MESSAGE: "{message}"

Respond with ONLY a JSON object:
{{"agent": "search|property|location|outreach", "reason": "<one short sentence>"}}"""


MENTION_RE = re.compile(r"@(search|property|location|outreach)\b", re.IGNORECASE)
VALID = {"search", "property", "location", "outreach"}


class AgentRouter(BaseAgent):
    name = "router"

    def route(self, message: str, session) -> tuple[str, str]:  # noqa: ANN001
        # Explicit @mention wins
        m = MENTION_RE.search(message)
        if m:
            return m.group(1).lower(), "explicit @mention"

        recent = []
        for turn in list(session.history)[-6:]:
            tag = turn.role if turn.role != "agent" else f"agent:{turn.agent}"
            recent.append(f"[{tag}] {turn.text[:140]}")

        pending_str = "null"
        if session.pending_clarification:
            pa, fields = session.pending_clarification
            pending_str = f"agent={pa} awaiting={fields}"

        prompt = ROUTE_PROMPT.format(
            n_in_scope=len(session.listings_in_scope),
            recent="\n".join(recent) if recent else "(no prior turns)",
            message=message[:500],
            pending=pending_str,
        )
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        try:
            obj = json.loads(text)
            agent = obj.get("agent", "").lower()
            reason = obj.get("reason", "")
        except json.JSONDecodeError:
            agent, reason = "search", "router parse failed; defaulting to search"
        if agent not in VALID:
            agent, reason = "search", f"invalid '{agent}'; defaulting to search"
        return agent, reason
