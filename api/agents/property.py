"""Property Analyst Agent — Q&A over listing facts."""
from __future__ import annotations

import json
import re

from listings import Listing

from .base import AgentReply, BaseAgent


def _looks_like_clarifying_question(text: str) -> bool:
    """Heuristic: short reply where the substantive content ends in '?'.

    Accepts markdown wrappers (**bold**, > quote) since LLMs often emphasize
    questions. Rejects long markdown answers that happen to contain a '?'.
    """
    t = text.strip()
    if len(t) > 500:
        return False
    # Strip common markdown wrappers from the end before checking for '?'
    cleaned = t.rstrip("* _`)] ").rstrip(" .!").rstrip("* _`)] ")
    if not cleaned.endswith("?"):
        return False
    # Reject if it has list markers or headers (those are answers)
    if t.startswith(("# ", "## ", "### ")):
        return False
    if "\n- " in t or "\n1. " in t or "\n2. " in t:
        return False
    return True

ANALYZE_PROMPT = """You are RentWise's Property Analyst Agent — the
careful, numerate one. Tenants come to you for hard facts about specific
listings: rent, deposits, fees, utilities, parking, lease terms, pet
policy, total monthly cost.

ALL_LISTINGS_IN_SCOPE below shows EVERY listing currently visible to the
user, numbered 1..N in the order they appeared in chat.

You have TOOLS available to consult other specialist agents — USE THEM
when the user's question goes beyond pure property facts:

  • Walk / transit / sound / bike score → call `location__get_walkability`
  • Nearby groceries / restaurants / parks / schools → call `location__nearby_pois`
  • Commute distance to a specific lat/lng → call `location__get_commute`
  • Resident reviews / rating / complaints → call `reviews__summarize`
  • Cheaper or alternative listings outside current scope → call `search__find_listings`

When you call a tool, CITE its output in your final reply with the agent's
name in @-mention form so the user sees the discussion:
  ✓ "Per @location: Walk Score 99 (Walker's Paradise)."
  ✓ "@reviews flags repeated elevator outages in 2024 (1.0 ★ recent review)."
  ✗ Don't quietly fold tool data in without attribution.

You can do TWO things:

A) ANSWER — Use ONLY facts in ALL_LISTINGS_IN_SCOPE plus tool results.
   If a field is null or missing AND no tool can fetch it, say "not
   listed in the source" — do NOT invent data.

   Match your depth to the question:

   • SINGLE LISTING, SIMPLE FACT (e.g. "押金多少", "is parking included")
     → 1–3 sentences. Cite the source field. No tools needed.

   • MULTI-LISTING COMPARISON / CALCULATION → Markdown table. Columns
     depend on the question — use these when relevant:
        # | Listing | Base rent | Included utilities | Estimated extras | Total/mo
     Then a 1–2 sentence takeaway naming the winner / outlier.

   • CROSS-CUTTING ("which is quieter", "which has best reviews") →
     CALL THE RIGHT TOOL for each listing in scope, then build a
     comparison table that cites the tool author per cell:
        # | Listing | Sound (@location) | Rating (@reviews) | …

   • CALCULATIONS — show the math. State assumptions clearly with
     ⚠️ disclaimer line.

   • AMBIGUOUS REFERENCE ("this place", "the second one") — resolve via
     LIKELY_TARGET_INDEXES; if still unclear, prefer the top-1.

B) ASK — Only ask if the question can't be answered without external info
   (e.g. user's family size for "is it big enough?"). Then ONE focused
   question, under 200 chars, ends with '?'. Don't ask when data + tools
   are enough.

LIKELY_TARGET_INDEXES (1-based, my best guess — verify against the message):
{likely_targets}

USER MESSAGE:
{user_message}

ALL_LISTINGS_IN_SCOPE:
{listings}

Reply in clear Markdown."""


def resolve_listings(message: str, in_scope: list[Listing]) -> list[Listing]:
    """Pick which listings the user is referring to."""
    if not in_scope:
        return []
    msg = message.lower()

    # Ordinal references: "first", "second", "1st", "#2", "第一", "第二", …
    # NOTE: cardinals ("one", "two", "three") deliberately excluded — they
    # match too liberally ("the one I like", "two of these", etc.).
    ordinals = {
        "first": 0, "1st": 0,
        "second": 1, "2nd": 1,
        "third": 2, "3rd": 2,
        "fourth": 3, "4th": 3,
        "fifth": 4, "5th": 4,
        # Chinese ordinals
        "第一": 0, "第1": 0,
        "第二": 1, "第2": 1,
        "第三": 2, "第3": 2,
        "第四": 3, "第4": 3,
        "第五": 4, "第5": 4,
        "第六": 5, "第6": 5,
        "第七": 6, "第7": 6,
        "第八": 7, "第8": 7,
        "第九": 8, "第9": 8,
        "第十": 9, "第10": 9,
    }
    indexes: set[int] = set()
    for word, idx in ordinals.items():
        if re.search(rf"\b{word}\b", msg):
            indexes.add(idx)
    for m in re.finditer(r"#?(\d{1,2})\b", msg):
        try:
            n = int(m.group(1)) - 1
            if 0 <= n < min(len(in_scope), 10):
                indexes.add(n)
        except ValueError:
            pass

    # Name match: any building name substring
    name_hits: list[int] = []
    for i, L in enumerate(in_scope):
        if L.name and len(L.name) > 4 and L.name.lower() in msg:
            name_hits.append(i)

    picks: list[int] = sorted(set(indexes) | set(name_hits))
    if not picks:
        # Default: the top result if user asks "this one" / "that place"
        if any(p in msg for p in ("this", "that", "the place", "it")):
            picks = [0]
    return [in_scope[i] for i in picks if i < len(in_scope)]


def _display_address(L: "Listing") -> str | None:
    """Return address suitable for LLM prompts.

    Falls back to neighborhood when Zillow hides the exact address so the LLM
    doesn't echo "(Undisclosed Address)" verbatim in its reply.
    """
    addr = (L.address or "").strip()
    if not addr or addr.lower() == "(undisclosed address)":
        return L.neighborhood or None
    return addr


def listing_card_for_llm(L: Listing, idx: int) -> dict:
    return {
        "index": idx + 1,
        "name": L.name,
        "address": _display_address(L),
        "neighborhood": L.neighborhood,
        "rent_min": L.rent_min,
        "rent_max": L.rent_max,
        "rent_by_bed": {
            ("Studio" if b == 0 else f"{b}BR"): {"min": mn, "max": mx}
            for b, (mn, mx) in L.rent_by_bed.items()
        },
        "deposit_min": L.deposit_min,
        "deposit_max": L.deposit_max,
        "utilities_included": L.utilities_included,
        "pets_allowed": L.pets_allowed,
        "has_pool": L.has_pool,
        "has_elevator": L.has_elevator,
        "has_storage": L.has_storage,
        "has_patio_balcony": L.has_patio_balcony,
        "parking_types": L.parking_types,
        "walk_score": L.walk_score,
        "transit_score": L.transit_score,
        "bike_score": L.bike_score,
        "schools": [
            {"name": s.get("name"), "rating": s.get("rating"),
             "distance": s.get("distance"), "level": s.get("level")}
            for s in (L.raw.get("schools") or [])[:5]
        ],
        "description_excerpt": (L.description or "")[:300],
        "url": L.url,
    }


class PropertyAnalystAgent(BaseAgent):
    name = "property"

    def __init__(self, all_listings: list[Listing] | None = None, **kw):
        super().__init__(**kw)
        # Held for tool-use loop's `search__find_listings` to query against
        # ALL listings, not just the current shortlist scope.
        self.all_listings = all_listings or []

    def handle(self, message: str, session) -> AgentReply:  # noqa: ANN001
        if not session.listings_in_scope:
            return AgentReply(
                agent=self.name,
                text=(
                    "I can dig into specifics on a listing once the Search "
                    "Agent has surfaced some. Try asking it for matches first."
                ),
            )

        likely_targets = resolve_listings(message, session.listings_in_scope)
        likely_indexes = [
            session.listings_in_scope.index(L) + 1 for L in likely_targets
        ]

        # Build LLM scope: start with up to 15 in-scope listings, then
        # ensure every resolved target is included even if it's beyond that
        # window (fixes the bug where listing #6+ couldn't be analysed).
        scope = list(session.listings_in_scope[:15])
        for L in likely_targets:
            if L not in scope:
                scope.append(L)
        cards = []
        for i, L in enumerate(scope):
            card = listing_card_for_llm(L, i)
            card["zpid"] = L.zpid  # explicit — tools key on it
            cards.append(card)

        prompt = ANALYZE_PROMPT.format(
            user_message=message,
            likely_targets=likely_indexes if likely_indexes else "(no clear ordinal — use your judgment)",
            listings=json.dumps(cards, indent=2, default=str),
        )

        # Run the tool-use loop. Property's own get_facts tool is filtered
        # out automatically (no self-recursion); it can call location +
        # reviews + search tools.
        text, tool_logs = self.tool_use_loop(
            prompt,
            scope=scope,
            all_listings=self.all_listings,
            max_tokens=1500,
        )

        is_question = _looks_like_clarifying_question(text)

        if not is_question:
            for L in likely_targets:
                session.add_to_shortlist(L, via="property")

        return AgentReply(
            agent=self.name,
            text=text,
            awaiting=["clarify"] if is_question else None,
            metadata={
                "resolved_zpids": [L.zpid for L in likely_targets],
                "in_scope_zpids": [L.zpid for L in scope],
                "phase": "clarifying" if is_question else "answer",
            },
            tool_calls=tool_logs,
        )
