"""Outreach Agent — drafts inquiry emails.

v0 scaffold: drafts ONLY. Does not send. The user copies-pastes or, in v1,
clicks Send to dispatch via Gmail OAuth (see design spec §9).
"""
from __future__ import annotations

import json

from listings import Listing

from .base import AgentReply, BaseAgent
from .property import _looks_like_clarifying_question, resolve_listings

DRAFT_PROMPT = """You are RentWise's Outreach Agent. Draft inquiry emails to leasing
offices on behalf of the user. Emails are sent FROM the user's Gmail in
v1, so write in first person as the user (a prospective tenant).

You can do TWO things:

A) DRAFT — Output ONE email per listing as a Markdown block:

---
**To:** {{leasing_office_email_or_phone}}
**Subject:** ...
**Body:**

Hi there,

[body text]

Best,
[user_name]
---

Required elements in every email:
  • Subject line (short, listing-specific)
  • Greeting to the leasing office
  • Tenant identification ("My name is <user_name> ...")
  • Listing reference (building name + address)
  • Concrete questions from USER_INTENT
  • Polite close + next step

If the listing has only a phone number (no email), still draft the email
but prepend a note: "_Contact email not in source — would also work as a
phone-call script._"

B) ASK — ONLY ask if USER_PROFILE.user_name is empty (required for the
   signature). Then ask exactly: "What name should I sign these emails
   with?" — short, ends with '?'. Do NOT ask about move-in date,
   budget, tour times, or anything else — make reasonable assumptions
   or use neutral phrasing in the email instead. The user's CURRENT
   message + listings are enough to draft once you have a name.

USER_INTENT:
{user_message}

USER_PROFILE:
{profile}

LISTINGS:
{listings}
"""


class OutreachAgent(BaseAgent):
    name = "outreach"

    def handle(self, message: str, session) -> AgentReply:  # noqa: ANN001
        if not session.listings_in_scope:
            return AgentReply(
                agent=self.name,
                text=(
                    "Ask the Search Agent to surface some listings first, "
                    "then tell me which ones to reach out to."
                ),
            )

        likely_targets = resolve_listings(message, session.listings_in_scope)
        if likely_targets:
            targets = likely_targets[:3]
        else:
            # No explicit reference — default to drafting for the top 3.
            targets = session.listings_in_scope[:3]

        cards = []
        for i, L in enumerate(targets):
            cards.append(
                {
                    "index": session.listings_in_scope.index(L) + 1,
                    "name": L.name,
                    "address": L.address,
                    "phone": L.raw.get("phone"),
                    "agent_name": L.raw.get("agent_name") or "Leasing Office",
                    "rent_summary": (
                        f"${L.rent_min:,}–${L.rent_max:,}"
                        if L.rent_min and L.rent_max
                        else "?"
                    ),
                    "rent_by_bed": {
                        ("Studio" if b == 0 else f"{b}BR"): {"min": mn, "max": mx}
                        for b, (mn, mx) in L.rent_by_bed.items()
                    },
                    "url": L.url,
                }
            )

        from dataclasses import asdict
        prompt = DRAFT_PROMPT.format(
            user_message=message,
            profile=json.dumps(
                {
                    "user_name": session.profile.user_name,
                    "move_in_date": session.profile.move_in_date,
                    "budget_max": session.profile.budget_max,
                    "pets": session.profile.pets,
                },
                default=str,
            ),
            listings=json.dumps(cards, indent=2, default=str),
        )
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=2500,
            messages=[{"role": "user", "content": prompt}],
        )
        body = resp.content[0].text.strip()
        is_question = _looks_like_clarifying_question(body)

        if is_question:
            return AgentReply(
                agent=self.name,
                text=body,
                awaiting=["user_name"] if not session.profile.user_name else ["clarify"],
                metadata={"phase": "clarifying"},
            )

        note = (
            "\n\n> _v0 scaffold: drafts only — no real send. In v1 each email "
            "would have a 'Send via Gmail' button after you connect your "
            "account (see design spec §9)._"
        )
        for L in targets:
            session.add_to_shortlist(L, via="outreach")

        return AgentReply(
            agent=self.name,
            text=body + note,
            metadata={
                "drafted_for_zpids": [L.zpid for L in targets],
                "phase": "answer",
            },
        )
