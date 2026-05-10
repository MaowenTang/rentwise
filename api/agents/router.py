"""Agent Router — LLM-driven dispatch.

Reads the latest user message + recent chat history + current listings
in scope, then picks ONE agent to handle the turn. Multi-agent fan-out
is v1 per the design spec §3.A1.

Explicit @mentions override the router:
  @search ...    → SearchAgent
  @property ...  → PropertyAnalystAgent
  @location ...  → LocationCommuteAgent
  @outreach ...  → OutreachAgent
  @reviews ...   → ResidentReviewsAgent
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
    "Show Me More" / next-batch signals (ALWAYS route to search, no
    profile update needed — SearchAgent handles the fast-path internally):
      • "show me more" / "more options" / "next batch" / "see more"
      • "再推几个" / "看更多" / "再来几个" / "多看几个" / "多推几个"
    中文 re-search with new criteria (ALWAYS route to search):
      • "再找几个" / "换几个看看" / "还有其他的吗"
      • "这些都不太好" / "有没有其他选择" / "再搜一下"
      • "找便宜一点的" / "有没有更好的"
    Do NOT use search if the user is asking about listings that are
    already in scope.

  "property" — Detailed FACTS about listings already in scope. This is
    ALWAYS the right choice for questions about a specific listing's:
      • rent / deposit / fees / lease term / utilities included
      • parking (cost, availability, type, garage vs street)
      • pet policy, pet fees, pet weight limits
      • amenities the listing itself advertises (gym, pool, laundry)
      • application fee, admin fee, move-in costs
      • lease length / minimum stay / break-lease fee
    English: "does X include water?" / "what's the deposit?" / "is parking
      included?" / "compare 1br pricing" / "what's the application fee?"
    中文 (CRITICAL — always property, never search/location):
      • "押金多少" / "有停车费吗" / "停车费多少" / "宠物费"
      • "水电费包含吗" / "包水电吗" / "包不包" / "需不需要"
      • "签多久合同" / "最少租多久" / "申请费"
      • "1bd多少钱" / "studio多钱" / "对比一下租金"

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

  "reviews" — Resident reviews, tenant sentiment, or building reputation.
    Use ALWAYS when the user asks about what residents/tenants think:
      • "what do residents say about [building]?"
      • "is it noisy?" / "how's the noise level?"
      • "how's management?" / "are they responsive?"
      • "any maintenance issues?"
      • "is it safe?" / "how's the neighborhood safety?"
      • "what's the vibe like?" / "do people like living there?"
      • "any reviews?" / "what's the rating?" / "stars?"
      • "what do tenants think?" / "is it worth it?"
    中文:
      • "住户评价怎么样" / "居民评分" / "有没有差评"
      • "噪音大吗" / "管理好不好" / "维修快吗" / "安不安全"
    Do NOT use reviews for neighborhood walkability or commute — use location.
    Do NOT use reviews for listing facts (deposit, parking) — use property.

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
{{"agent": "search|property|location|outreach|reviews", "reason": "<one short sentence>"}}"""


MENTION_RE = re.compile(r"@(search|property|location|outreach|reviews)\b", re.IGNORECASE)
VALID = {"search", "property", "location", "outreach", "reviews"}


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
