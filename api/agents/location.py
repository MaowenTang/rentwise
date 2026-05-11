"""Location & Commute Agent — v0 scaffold.

v0 capabilities (no Maps API key required):
  - Walk / transit / bike scores from Zillow
  - Schools (name, rating, distance, level) from Zillow
  - Neighborhood description text
  - Haversine straight-line distance between two listings, OR between a
    listing and an arbitrary address IF the user gives lat/lng

v1 capabilities (Maps API in design spec §4.1):
  - Real walking / driving / transit commute times via Google Distance Matrix
  - Nearby groceries / hospitals / gyms / restaurants via Google Places

The agent is honest about which kind of answer it's giving.
"""
from __future__ import annotations

import json
from math import asin, cos, radians, sin, sqrt

from listings import Listing

from .base import AgentReply, BaseAgent
from .property import _looks_like_clarifying_question, resolve_listings

EARTH_RADIUS_MI = 3958.7613


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = radians(lat1), radians(lat2)
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(p1) * cos(p2) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_MI * asin(sqrt(a))


ANSWER_PROMPT = """You are RentWise's Location & Commute Agent. You answer
geographic questions about listings — walkability, transit, schools,
nearby amenities (groceries, restaurants, parks, gyms, hospitals),
neighborhood character, and distances.

Your data sources in CONTEXT for each listing:
  • walk_score / transit_score / bike_score (with labels)
  • neighborhood_description — marketing copy that often mentions specific
    nearby stores, restaurants, parks, transit lines, etc.
  • neighborhood_highlights — a structured list of named highlights
    (e.g. "Whole Foods Close", "Walking Distance Cafes").
  • community_amenities — building features (gym, pool, lounge, co-working).
  • outdoor_common_areas — courtyards, BBQ, etc.
  • schools — nearby schools with ratings and distances.
  • lat / lng — for pairwise straight-line distances between listings.

You can do TWO things:

A) ANSWER — Read the data and answer directly. When the user asks about
   nearby places (e.g. "groceries"):
     1. First scan neighborhood_description and neighborhood_highlights
        for explicit mentions (Whole Foods, Trader Joe's, "blocks from
        downtown dining", etc.) — quote them.
     2. Use walk_score as a coarse signal of overall amenity density:
        walk≥90 = "Walker's Paradise" (nearly everything reachable on foot)
        walk 70–89 = very walkable
        walk 50–69 = some errands on foot
        walk <50 = car-dependent.
     3. Be honest about what you can and can't see. If you don't have
        explicit grocery names for a listing, say so:
          "I don't have a specific grocery name for La Terraza in my
           data, but its walk score of 85 (Very Walkable) suggests
           daily-needs stores are reachable on foot. The Search Agent
           or Outreach Agent can surface specifics on request."

   For commute durations to arbitrary addresses you only have
   straight-line distance — say so explicitly, never invent a duration.

B) ASK — Only ask when truly necessary (e.g. user said "from work"
   without ever specifying where work is, AND there's no commute target
   in profile). Keep questions short (under 200 chars, end with '?').

  CRITICAL: If `context.commute_target` is present, the user's work
  location is ALREADY KNOWN — do NOT ask where they work. Just use
  context.commute_target.miles_straight_line_per_listing for distances
  and approx_minutes_at_30mph for ballpark commute durations. Cite the
  target by name (e.g. "Your commute to Apple Park is ~10 mi from
  Sunnyvale → roughly 20 minutes by car off-peak").

USER MESSAGE:
{user_message}

CONTEXT:
{context}

Reply in concise Markdown — tables work well for comparisons."""


class LocationCommuteAgent(BaseAgent):
    name = "location"

    def _build_context(self, listings: list[Listing], profile=None) -> dict:  # noqa: ANN001
        out = []
        for i, L in enumerate(listings):
            # Extract the rich neighborhood text + highlights so the LLM
            # can answer questions about groceries / restaurants / parks
            # / etc. that the listing's marketing copy mentions.
            highlights = []
            for h in (L.raw.get("neighborhood_highlights") or [])[:8]:
                highlights.append({
                    "name": h.get("name"),
                    "description": (h.get("description") or "")[:200],
                })

            # Pull out community amenities (gym, pool, co-working, etc.)
            community = []
            for cat in (L.raw.get("building_amenities") or []):
                if isinstance(cat, dict):
                    items = cat.get("items") or []
                    for it in items:
                        if isinstance(it, dict):
                            n = it.get("name") or it.get("label")
                            if n:
                                community.append(n)
                        elif isinstance(it, str):
                            community.append(it)
            for c in (L.raw.get("community_rooms") or []):
                if isinstance(c, str):
                    community.append(c)
                elif isinstance(c, dict) and c.get("name"):
                    community.append(c["name"])

            out.append(
                {
                    "index": i + 1,
                    "name": L.name,
                    "address": L.address,
                    "lat": L.lat,
                    "lng": L.lng,
                    "neighborhood": L.neighborhood,
                    # Full description so LLM can find references to nearby
                    # places (restaurants, shops, parks) the marketing copy mentions.
                    "neighborhood_description": (
                        L.raw.get("neighborhood_description") or ""
                    )[:1200],
                    "neighborhood_highlights": highlights,
                    "walk_score": L.walk_score,
                    "walk_label": L.raw.get("walk_label"),
                    "transit_score": L.transit_score,
                    "transit_label": L.raw.get("transit_label"),
                    "bike_score": L.bike_score,
                    "bike_label": L.raw.get("bike_label"),
                    "community_amenities": community[:20],
                    "outdoor_common_areas": L.raw.get("outdoor_common_areas") or [],
                    "schools": [
                        {
                            "name": s.get("name"),
                            "rating": s.get("rating"),
                            "distance_mi": s.get("distance"),
                            "level": s.get("level"),
                        }
                        for s in (L.raw.get("schools") or [])
                    ],
                    "description_excerpt": (L.description or "")[:300],
                }
            )
        ctx: dict = {"listings": out}
        # Surface the user's known work/commute target so the LLM doesn't
        # ask "where do you work?" when the profile already has it. Also
        # pre-compute straight-line distance from each listing to the
        # commute target since this is the most common question.
        if profile is not None and getattr(profile, "commute", None):
            ct = profile.commute
            target_info = {
                "name": ct.name,
                "address": ct.address or None,
                "lat": ct.lat,
                "lng": ct.lng,
                "max_minutes": ct.max_minutes,
            }
            if ct.lat and ct.lng:
                target_info["miles_straight_line_per_listing"] = []
                for i, L in enumerate(listings):
                    if L.lat and L.lng:
                        d = haversine_miles(L.lat, L.lng, ct.lat, ct.lng)
                        target_info["miles_straight_line_per_listing"].append({
                            "listing_index": i + 1,
                            "listing_name": L.name,
                            "miles": round(d, 2),
                            # 30 mph surface street estimate; explicit so LLM
                            # uses it as an approximation rather than guess.
                            "approx_minutes_at_30mph": int(round(d * 2)),
                        })
            ctx["commute_target"] = target_info
        return ctx

    def handle(self, message: str, session) -> AgentReply:  # noqa: ANN001
        if not session.listings_in_scope:
            return AgentReply(
                agent=self.name,
                text=(
                    "Ask the Search Agent for some listings first, then I "
                    "can answer location and commute questions about them."
                ),
            )

        likely_targets = resolve_listings(message, session.listings_in_scope)
        likely_indexes = [
            session.listings_in_scope.index(L) + 1 for L in likely_targets
        ]
        # Always include full scope; the LLM picks based on the message + hint.
        targets = session.listings_in_scope[:5]
        context = self._build_context(targets, profile=session.profile)
        if likely_indexes:
            context["_likely_target_indexes_1based"] = likely_indexes

        # If the user mentioned multiple listings, add pairwise straight-line distances.
        if len(targets) >= 2:
            pairs = []
            for i in range(len(targets)):
                for j in range(i + 1, len(targets)):
                    a, b = targets[i], targets[j]
                    if a.lat and a.lng and b.lat and b.lng:
                        d = haversine_miles(a.lat, a.lng, b.lat, b.lng)
                        pairs.append({"a": a.name, "b": b.name, "miles_straight_line": round(d, 2)})
            if pairs:
                context["pairwise_distances_straight_line"] = pairs

        # Stash limitation note for the LLM
        context["_capabilities_note"] = (
            "v0 scaffold: I can quote walk/transit/bike scores, neighborhood "
            "descriptions, schools, and Haversine straight-line distances. "
            "I CANNOT compute real driving / walking / transit durations "
            "(needs Google Maps Distance Matrix API — wired up in v1)."
        )

        prompt = ANSWER_PROMPT.format(
            user_message=message,
            context=json.dumps(context, indent=2, default=str),
        )
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        is_question = _looks_like_clarifying_question(text)

        if not is_question:
            for L in likely_targets:
                session.add_to_shortlist(L, via="location")

        return AgentReply(
            agent=self.name,
            text=text,
            awaiting=["clarify"] if is_question else None,
            metadata={
                "resolved_zpids": [L.zpid for L in likely_targets],
                "in_scope_zpids": [L.zpid for L in targets],
                "phase": "clarifying" if is_question else "answer",
            },
        )
