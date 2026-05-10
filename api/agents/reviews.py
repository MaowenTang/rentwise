"""Resident Reviews Agent — surfaces resident sentiment for in-scope listings.

Flow:
  1. Identify which listing the user is asking about:
       a. Name/ordinal substring match (fast, no LLM)
       b. If ambiguous, fall back to LLM disambiguation
       c. If still None: prompt user to specify
  2. Fetch reviews via get_reviews_or_fetch() — blocks on cache miss (≤2s
     timeout); falls back to [] if Google Places is unreachable.
  3. If [] (no data): honest "no reviews found yet" message.
  4. If aggregate_only (star rating but no review text): format rating card.
  5. If full review text available: run Claude to summarize by category.

NOTE — no streaming preamble in v1:
  The frontend uses a single-shot JSON response model (r.json() on /chat).
  SSE / chunked streaming is deferred to v1.1. This agent returns a complete
  response; cold-cache turns may feel ~2s longer but require no frontend changes.
  When SSE lands, add a preamble token before get_reviews_or_fetch() is called.

Interface contract (implemented by Chuck in api/reviews_fetcher.py):
  get_reviews(zpid: str) -> list[ReviewsResult]
      Non-blocking cache-only lookup. Returns [] on miss.
      Called by SearchAgent for background pre-warm on top-5 results.

  get_reviews_or_fetch(zpid: str, listing_stub: dict) -> list[ReviewsResult]
      Cache-first. Blocks on live Google Places fetch if miss (max ~2s).
      Falls back to [] on timeout or API error.
      listing_stub keys: zpid, name, address, lat (float|None), lng (float|None)

ReviewsResult dict schema (one record per data source):
  {
    "source":       str,           # "google_places" | "yelp" | "reddit"
    "rating":       float | None,  # aggregate star rating (e.g. 3.8)
    "review_count": int | None,    # total public reviews on that platform
    "verified":     bool,          # address match confirmed (haversine ≤500m)
    "verified_by":  str | None,    # e.g. "haversine_500m"
    "aggregate_only": bool,        # True = no review text, only aggregate stats
    "fetched_at":   str,           # ISO 8601 timestamp
    "url":          str | None,    # link to source page (Yelp/Google Maps)
    "reviews": [                   # None or [] if aggregate_only=True
      {
        "text":   str,
        "rating": float | None,
        "date":   str | None,      # ISO date, e.g. "2025-03-14"
      }
    ] | None
  }
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date

from .base import AgentReply, BaseAgent

# ---------------------------------------------------------------------------
# Deferred import — stub until Chuck ships api/reviews_fetcher.py
# ---------------------------------------------------------------------------
try:
    from reviews_fetcher import get_reviews, get_reviews_or_fetch  # noqa: F401
except ImportError:
    def get_reviews(zpid: str) -> list[dict]:  # type: ignore[misc]
        """Stub: returns [] until reviews_fetcher.py is implemented."""
        return []

    async def get_reviews_or_fetch(zpid: str, listing_stub: dict) -> list[dict]:  # type: ignore[misc]
        """Stub: returns [] until reviews_fetcher.py is implemented."""
        return []


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_IDENTIFY_PROMPT = """You are the Resident Reviews Agent for RentWise.
The user is asking about resident reviews for a specific apartment building.

Listings currently in scope (numbered 1..N in the order they appeared in chat):
{listings_summary}

User message: "{message}"

Which single listing is the user most likely asking about?
Respond with ONLY a JSON object — no prose, no fences:
{{"zpid": "<zpid>", "name": "<building name>"}}

If you cannot identify a specific listing (ambiguous request, nothing in scope,
or user is asking generally), respond with:
{{"zpid": null, "name": null}}
"""


_SUMMARIZE_PROMPT = """You are RentWise's Resident Reviews Agent. A prospective
tenant is asking what current and past residents think of **{building_name}**.

User's question: "{user_question}"

Analyze the reviews below and write a structured resident sentiment summary.

INSTRUCTIONS:
1. **Lead with what's asked** — If the user asked about a specific dimension
   (noise, management, maintenance, safety), put that category FIRST and give
   it the most depth. Include other categories briefly if they have strong signals.

2. **Recency weighting** — Weight reviews from the last 12 months more heavily
   than older ones. Note when sentiment has shifted over time (e.g., "management
   improved in late 2024 after staff change"). If all reviews are older than
   12 months, add: *"Note: all available reviews are from [year]."*

3. **Category structure** — Organize findings into these sections, but ONLY
   include a section if at least one review addresses it:
   - **Management & Responsiveness** — lease staff, complaint handling, communication
   - **Maintenance** — repair speed, build quality, HVAC, appliances
   - **Noise & Neighbors** — soundproofing, neighbor quality, proximity to traffic
   - **Safety & Security** — lighting, locks, package theft, neighborhood safety
   - **Overall Vibe** — community feel, common areas, value for money

4. **Citations** — For each category, give one concrete example:
   ✓ "Maintenance (⚠️ mixed): Several 2024 reviews cite slow response times
      (3–5 day waits for repairs); one Dec 2024 reviewer notes improvement after
      the building switched management companies."
   ✗ "Maintenance: Some residents had issues."

5. **Source conflicts** — If ratings differ significantly across sources
   (e.g., Google 4.5★ vs Yelp 2.0★), surface the discrepancy explicitly:
   *"Ratings differ across platforms — Google reviewers average 4.5★ while
   Yelp shows 2.0★. The text reviews may explain why."*

6. **Honesty** — Do NOT soften negative feedback. Prospective tenants rely on
   this to make a $15k+/year decision.

7. **Coverage disclaimer** — End with one line:
   "Based on {n_reviews} review(s) from {sources} (most recent: {latest_date}).
   Coverage may be partial — check {source_links} for the full picture."

Today's date: {today}

REVIEWS (JSON — includes source, text, rating, date):
{reviews_json}

Respond in plain Markdown. No JSON. Start directly with the category summaries.
Aim for 200–350 words total.
"""

_NO_DATA_MSG = (
    "No resident reviews found yet for **{building_name}**. "
    "Review data is fetched on demand and cached — if this is the first time "
    "anyone has asked about this building, the data may still be loading. "
    "Try asking again in a moment, or I can tell you what the listing itself "
    "says about the building and its amenities."
)

_AGGREGATE_TEMPLATE = """\
**{building_name}** — Resident Rating Summary

{sources_block}
_Review text is not available from current data sources. \
Visit {source_links} to read individual reviews._"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ORDINALS: dict[str, int] = {
    "first": 0, "1st": 0,
    "second": 1, "2nd": 1,
    "third": 2, "3rd": 2,
    "fourth": 3, "4th": 3,
    "fifth": 4, "5th": 4,
}


def _resolve_listing_heuristic(message: str, in_scope: list) -> tuple[str | None, str | None]:
    """Fast name/ordinal match — no LLM required.

    Returns (zpid, name) of the matched listing, or (None, None) if unclear.
    """
    if not in_scope:
        return None, None

    msg = message.lower()

    # Ordinal references ("first", "2nd", "#3", etc.)
    indexes: set[int] = set()
    for word, idx in _ORDINALS.items():
        if re.search(rf"\b{word}\b", msg):
            indexes.add(idx)
    for m in re.finditer(r"#?(\d{1,2})\b", msg):
        try:
            n = int(m.group(1)) - 1
            if 0 <= n < min(len(in_scope), 10):
                indexes.add(n)
        except ValueError:
            pass

    # Building name substring match
    name_hits: list[int] = []
    for i, L in enumerate(in_scope):
        if L.name and len(L.name) > 4 and L.name.lower() in msg:
            name_hits.append(i)

    picks = sorted(indexes | set(name_hits))

    if len(picks) == 1:
        L = in_scope[picks[0]]
        return L.zpid, L.name

    # Unambiguous "this / that" → top listing
    if not picks and any(p in msg for p in ("this", "that", "the place", "it", "here")):
        L = in_scope[0]
        return L.zpid, L.name

    return None, None  # ambiguous or multiple matches


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class ResidentReviewsAgent(BaseAgent):
    """Surfaces resident sentiment (Google Places / Yelp / Reddit) for a listing."""

    name = "reviews"

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _identify_listing(self, message: str, session) -> tuple[str | None, str | None]:  # noqa: ANN001
        """Identify which in-scope listing the user is asking about.

        Returns (zpid, name). Either value may be None if no match found.
        """
        scope = session.listings_in_scope
        if not scope:
            return None, None

        # Single listing: unambiguous
        if len(scope) == 1:
            return scope[0].zpid, scope[0].name

        # Fast heuristic first (avoids an LLM round-trip in most cases)
        zpid, name = _resolve_listing_heuristic(message, scope)
        if zpid:
            return zpid, name

        # LLM disambiguation for genuinely ambiguous messages
        summary_lines = [
            f"  {i + 1}. zpid={L.zpid}  name={L.name}  address={L.address}"
            for i, L in enumerate(scope[:10])
        ]
        prompt = _IDENTIFY_PROMPT.format(
            listings_summary="\n".join(summary_lines),
            message=message[:300],
        )
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        try:
            obj = json.loads(text)
            return obj.get("zpid"), obj.get("name")
        except (json.JSONDecodeError, AttributeError):
            # LLM failed — fall back to top listing
            return scope[0].zpid, scope[0].name

    def _listing_stub(self, session, zpid: str) -> dict:  # noqa: ANN001
        """Build minimal listing dict for the fetcher (avoids importing Listing here)."""
        for L in session.listings_in_scope:
            if L.zpid == zpid:
                return {
                    "zpid": L.zpid,
                    "name": L.name,
                    "address": L.address,
                    "lat": L.lat,
                    "lng": L.lng,
                }
        return {"zpid": zpid}

    def _format_aggregate(self, building_name: str, records: list[dict]) -> str:
        """Format a star-rating-only response when no review text is available."""
        source_lines: list[str] = []
        link_parts: list[str] = []

        for r in records:
            src = r.get("source", "unknown")
            rating = r.get("rating")
            count = r.get("review_count")
            verified = r.get("verified", False)
            url = r.get("url", "")

            label = src.replace("_", " ").title()
            stars = f"{rating:.1f}★" if rating is not None else "No rating"
            count_str = f" ({count:,} reviews)" if count is not None else ""
            verified_note = " ✓ address confirmed" if verified else " (unverified match)"
            source_lines.append(f"- **{label}**: {stars}{count_str}{verified_note}")

            if url:
                link_parts.append(f"[{label}]({url})")

        sources_block = "\n".join(source_lines) if source_lines else "- No rating data available"
        source_links = " · ".join(link_parts) if link_parts else "the building's page directly"

        return _AGGREGATE_TEMPLATE.format(
            building_name=building_name,
            sources_block=sources_block,
            source_links=source_links,
        )

    def _summarize_reviews(self, building_name: str, records: list[dict], message: str = "") -> str:
        """Run Claude summarization over full review text, with category extraction."""
        all_reviews: list[dict] = []
        source_names: list[str] = []
        link_parts: list[str] = []
        latest_date = ""

        for r in records:
            src = r.get("source", "unknown")
            label = src.replace("_", " ").title()
            url = r.get("url", "")
            if url:
                link_parts.append(f"[{label}]({url})")
            source_names.append(label)

            for rv in (r.get("reviews") or []):
                text = (rv.get("text") or "").strip()
                if not text:
                    continue
                item = {
                    "source": src,
                    "text": text,
                    "rating": rv.get("rating"),
                    "date": rv.get("date"),
                }
                all_reviews.append(item)
                d = rv.get("date") or ""
                if d and d > latest_date:
                    latest_date = d

        if not all_reviews:
            # All records ended up aggregate-only — downgrade gracefully
            return self._format_aggregate(building_name, records)

        source_links = " · ".join(link_parts) if link_parts else "the building's page directly"

        prompt = _SUMMARIZE_PROMPT.format(
            building_name=building_name,
            user_question=message or "What do residents say about this building?",
            today=str(date.today()),
            n_reviews=len(all_reviews),
            sources=", ".join(sorted(set(source_names))) or "unknown",
            latest_date=latest_date or "unknown",
            source_links=source_links,
            reviews_json=json.dumps(all_reviews, indent=2, ensure_ascii=False),
        )
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=900,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()

    # ------------------------------------------------------------------
    # handle
    # ------------------------------------------------------------------

    def handle(self, message: str, session) -> AgentReply:  # noqa: ANN001
        # ── Step 1: identify which listing ──────────────────────────────
        zpid, name = self._identify_listing(message, session)

        if zpid is None:
            return AgentReply(
                agent=self.name,
                text=(
                    "I'd love to pull up resident reviews — which building are you "
                    "curious about? Just say the name or number (e.g. 'the first one' "
                    "or 'The James') and I'll fetch what residents are saying."
                ),
                awaiting=["listing_selection"],
                metadata={"reviews_status": "needs_selection"},
            )

        building_name = name or zpid

        # ── Step 2: fetch reviews (blocks up to ~2s on cache miss) ──────
        # NOTE: No streaming preamble in v1 — frontend uses single-shot JSON.
        # When SSE lands (v1.1), emit a preamble chunk here before this call.
        listing_stub = self._listing_stub(session, zpid)
        records = asyncio.run(get_reviews_or_fetch(zpid, listing_stub))

        # ── Step 3: no data at all ───────────────────────────────────────
        if not records:
            return AgentReply(
                agent=self.name,
                text=_NO_DATA_MSG.format(building_name=building_name),
                metadata={"zpid": zpid, "reviews_status": "no_data", "n_reviews": 0},
            )

        # ── Step 4: aggregate-only (star rating but no review text) ─────
        all_aggregate = all(r.get("aggregate_only", False) for r in records)
        if all_aggregate:
            return AgentReply(
                agent=self.name,
                text=self._format_aggregate(building_name, records),
                metadata={
                    "zpid": zpid,
                    "reviews_status": "aggregate_only",
                    "sources": [r.get("source") for r in records],
                },
            )

        # ── Step 5: full review text — Claude summarization ─────────────
        return AgentReply(
            agent=self.name,
            text=self._summarize_reviews(building_name, records, message),
            metadata={
                "zpid": zpid,
                "reviews_status": "full",
                "sources": [r.get("source") for r in records],
                "n_reviews": sum(
                    len(r.get("reviews") or []) for r in records
                ),
            },
        )
