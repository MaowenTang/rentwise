"""Cross-agent tool registry — see docs/superpowers/specs/2026-05-05-agent-tool-use-design.md.

Each agent's "skills" are exposed as Anthropic tool-use tools that the
lead agent (the one the router dispatched) can invoke during its handle()
loop. Tools are pure functions of (zpid → fact) — no side effects, no
LLM calls inside (so loops can't blow up cost).

Architecture
------------
- The lead agent's handle() builds a tool list (excluding its own tools
  to prevent self-recursion).
- It runs an Anthropic tool-use loop: emit, capture tool_use, dispatch
  to the matching skill function, return tool_result, repeat.
- Hard cap: 5 tool calls per user turn.
- Each call is recorded in the returned ToolCallLog so the agent can
  cite sources in its final reply and main.py can surface them in
  ChatResponse.metadata.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from math import asin, cos, radians, sin, sqrt
from typing import Any, Callable

from listings import Listing


MAX_TOOL_CALLS = 5  # hard cap per user turn


# ----------------------- pure-function skills -----------------------------
#
# Each function returns a JSON-serializable value (or {"error": ...}). Never
# raises — bad input -> {"error": "..."} so the loop stays alive.


def _find_listing(zpid: str, scope: list[Listing]) -> Listing | None:
    for L in scope:
        if L.zpid == zpid:
            return L
    return None


def _safe_field(L: Listing, name: str) -> Any:
    """Pull a field from Listing or its raw dict, in that order."""
    if hasattr(L, name):
        return getattr(L, name)
    return L.raw.get(name)


_PROPERTY_FIELDS = {
    "rent_min", "rent_max", "rent_by_bed",
    "deposit_min", "deposit_max",
    "utilities_included", "pets_allowed",
    "parking_types", "has_pool", "has_elevator",
    "has_storage", "has_patio_balcony",
    "application_fee", "administrative_fee",
    "lease_terms",
}


def get_facts(zpid: str, fields: list[str], *, scope: list[Listing]) -> dict:
    """Property Analyst's tool — return requested fact fields for a listing.

    Caller passes the field names they want; we look them up on Listing or
    its `raw` dict. Unknown fields come back as null (with a hint in `_meta`).
    """
    L = _find_listing(zpid, scope)
    if L is None:
        return {"error": f"zpid {zpid} not in current shortlist scope"}
    out: dict[str, Any] = {"zpid": zpid, "name": L.name, "address": L.address}
    unknown: list[str] = []
    for f in fields:
        if f in _PROPERTY_FIELDS or hasattr(L, f) or f in L.raw:
            v = _safe_field(L, f)
            # rent_by_bed is a {int: tuple} — JSON-coerce it
            if f == "rent_by_bed" and isinstance(v, dict):
                v = {("Studio" if b == 0 else f"{b}BR"): {"min": mn, "max": mx}
                     for b, (mn, mx) in v.items()}
            out[f] = v
        else:
            unknown.append(f)
    if unknown:
        out["_meta"] = {"unknown_fields": unknown}
    return out


# ----------------------- location skills ----------------------------------

EARTH_RADIUS_MI = 3958.7613


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = radians(lat1), radians(lat2)
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(p1) * cos(p2) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_MI * asin(sqrt(a))


def get_walkability(zpid: str, *, scope: list[Listing]) -> dict:
    """Location agent's tool — walk / transit / bike scores + labels."""
    L = _find_listing(zpid, scope)
    if L is None:
        return {"error": f"zpid {zpid} not in current shortlist scope"}
    return {
        "zpid": zpid,
        "name": L.name,
        "walk_score": L.walk_score,
        "walk_label": L.raw.get("walk_label"),
        "transit_score": L.transit_score,
        "transit_label": L.raw.get("transit_label"),
        "bike_score": L.bike_score,
        "bike_label": L.raw.get("bike_label"),
        "sound_score": L.sound_score,
        "sound_label": L.sound_label,
    }


def get_commute(zpid: str, to_address: str | None = None,
                to_lat: float | None = None, to_lng: float | None = None,
                *, scope: list[Listing]) -> dict:
    """Location agent's tool — straight-line distance only (v0).

    Honest: we DON'T have Google Distance Matrix. We return Haversine
    miles + a coarse driving-time estimate (assume 25 mph urban average)
    so callers know to flag the answer as approximate.
    """
    L = _find_listing(zpid, scope)
    if L is None:
        return {"error": f"zpid {zpid} not in current shortlist scope"}
    if L.lat is None or L.lng is None:
        return {"error": f"listing {zpid} has no lat/lng"}
    if to_lat is None or to_lng is None:
        return {
            "error": "v0 cannot geocode addresses; pass to_lat + to_lng",
            "hint": "for now, only commute between two listings (both have lat/lng) is supported",
        }
    miles = _haversine_miles(L.lat, L.lng, to_lat, to_lng)
    return {
        "zpid": zpid,
        "name": L.name,
        "to_lat": to_lat,
        "to_lng": to_lng,
        "to_address": to_address,
        "miles_straight_line": round(miles, 2),
        "approx_drive_minutes": round(miles / 25 * 60),
        "_meta": "Straight-line distance + 25mph urban estimate. Not a real routing answer.",
    }


def nearby_pois(zpid: str, category: str, *, scope: list[Listing]) -> dict:
    """Location agent's tool — what does the marketing copy say about
    nearby groceries/restaurants/parks/transit/schools?

    No external API: we surface neighborhood_description + highlights
    so the lead can quote them. The lead's LLM filters by the requested
    category.
    """
    L = _find_listing(zpid, scope)
    if L is None:
        return {"error": f"zpid {zpid} not in current shortlist scope"}
    return {
        "zpid": zpid,
        "name": L.name,
        "category_hint": category,
        "walk_score": L.walk_score,
        "neighborhood": L.neighborhood,
        "neighborhood_description": (L.raw.get("neighborhood_description") or "")[:1500],
        "neighborhood_highlights": [
            {"name": h.get("name"), "description": (h.get("description") or "")[:200]}
            for h in (L.raw.get("neighborhood_highlights") or [])[:8]
        ],
        "schools": [
            {"name": s.get("name"), "rating": s.get("rating"),
             "distance_mi": s.get("distance"), "level": s.get("level")}
            for s in (L.raw.get("schools") or [])[:6]
        ],
    }


# ----------------------- reviews skills -----------------------------------

def summarize_reviews(zpid: str, *, scope: list[Listing]) -> dict:
    """Resident Reviews tool — quick aggregate + recent complaint themes.

    Imports lazily to avoid pulling the reviews datastore on every call.
    Returns rating, count, and a short summary string (no LLM call inside
    — that's the lead's job).
    """
    L = _find_listing(zpid, scope)
    if L is None:
        return {"error": f"zpid {zpid} not in current shortlist scope"}

    # Try the apartments.com aggregate baked into raw
    rating = L.raw.get("rating")
    rating_count = L.raw.get("rating_count")
    out: dict[str, Any] = {
        "zpid": zpid,
        "name": L.name,
        "rating": rating,
        "rating_count": rating_count,
    }
    # Surface the JSON-LD review[] excerpt if present
    reviews = L.raw.get("reviews") or []
    if reviews:
        out["recent_reviews"] = [
            {
                "author": r.get("author") or "anonymous",
                "rating": r.get("rating"),
                "date": r.get("date"),
                "excerpt": (r.get("body") or "")[:280],
            }
            for r in reviews[:5]
            if isinstance(r, dict)
        ]
    return out


# ----------------------- search skill -------------------------------------

def find_listings(query: str, max_rent: int | None = None,
                  beds: int | None = None,
                  *, all_listings: list[Listing]) -> dict:
    """Search agent's tool — sub-query against the in-memory listings.

    NOT the full RankingService — just a hard filter so the lead can
    say "find me cheaper alternatives in the same neighborhood". The
    lead is responsible for synthesis.
    """
    from listings import filter_listings, parse_location_phrase
    target_city, _ = parse_location_phrase(query)
    filtered = filter_listings(
        all_listings,
        max_rent=max_rent,
        min_beds=beds,
        max_beds=beds,
        neighborhood=query if target_city else None,
    )[:10]
    return {
        "query": query,
        "filters": {"max_rent": max_rent, "beds": beds, "city": target_city},
        "count": len(filtered),
        "results": [
            {
                "zpid": L.zpid,
                "name": L.name,
                "address": L.address,
                "rent_min": L.rent_min,
                "url": L.url,
            }
            for L in filtered
        ],
    }


# ----------------------- tool definitions for Anthropic --------------------

# Each entry is (name, description, json_schema_input). The handler is
# selected by name in dispatch_tool() below.
TOOL_DEFS: list[dict] = [
    {
        "name": "property__get_facts",
        "description": (
            "Property Analyst — return rent / deposit / fees / utilities / "
            "amenities / lease-terms fields for a listing. Use when you "
            "need hard facts beyond what's in the user-visible scope."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "zpid": {"type": "string", "description": "Listing zpid (must be in current scope)"},
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Field names to retrieve (e.g. ['deposit_min','utilities_included','application_fee'])",
                },
            },
            "required": ["zpid", "fields"],
        },
    },
    {
        "name": "location__get_walkability",
        "description": (
            "Location & Commute — walk / transit / bike / sound scores for a listing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"zpid": {"type": "string"}},
            "required": ["zpid"],
        },
    },
    {
        "name": "location__get_commute",
        "description": (
            "Location & Commute — straight-line miles + ROUGH driving estimate "
            "(25mph urban) between a listing and a target lat/lng. v0 has NO "
            "real routing — flag the answer as approximate. Skip if no lat/lng."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "zpid": {"type": "string"},
                "to_address": {"type": "string", "description": "Human-readable target (display only)"},
                "to_lat": {"type": "number"},
                "to_lng": {"type": "number"},
            },
            "required": ["zpid", "to_lat", "to_lng"],
        },
    },
    {
        "name": "location__nearby_pois",
        "description": (
            "Location & Commute — surface what the listing's marketing copy + "
            "highlights say about nearby groceries / restaurants / parks / "
            "transit / schools. No external API call; you'll get raw text the "
            "lead must filter."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "zpid": {"type": "string"},
                "category": {
                    "type": "string",
                    "description": "groceries | restaurants | parks | transit | schools | gym | other",
                },
            },
            "required": ["zpid", "category"],
        },
    },
    {
        "name": "reviews__summarize",
        "description": (
            "Resident Reviews — aggregate rating + recent review excerpts for a listing. "
            "Use when the user asks about quality / complaints / management responsiveness."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"zpid": {"type": "string"}},
            "required": ["zpid"],
        },
    },
    {
        "name": "search__find_listings",
        "description": (
            "Search — hard-filter sub-query against ALL listings (not just current scope). "
            "Use when the lead needs to surface cheaper / different alternatives mid-answer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text city / neighborhood phrase"},
                "max_rent": {"type": "integer"},
                "beds": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
]


# ----------------------- dispatcher ---------------------------------------

@dataclass
class ToolCallLog:
    """One executed tool call — surfaced to frontend as message metadata."""
    tool: str
    args: dict
    result_preview: str       # short string for the citation footer
    latency_ms: int
    error: str | None = None


def _agent_for_tool(tool_name: str) -> str:
    """Map tool name → owning agent label for citation."""
    if tool_name.startswith("property__"):
        return "property"
    if tool_name.startswith("location__"):
        return "location"
    if tool_name.startswith("reviews__"):
        return "reviews"
    if tool_name.startswith("search__"):
        return "search"
    return "unknown"


def dispatch_tool(
    name: str,
    args: dict,
    *,
    scope: list[Listing],
    all_listings: list[Listing],
) -> tuple[Any, ToolCallLog]:
    """Run a tool by name. Returns (result, log_entry)."""
    t0 = time.monotonic()
    err: str | None = None
    try:
        if name == "property__get_facts":
            result = get_facts(args["zpid"], args.get("fields") or [], scope=scope)
        elif name == "location__get_walkability":
            result = get_walkability(args["zpid"], scope=scope)
        elif name == "location__get_commute":
            result = get_commute(
                args["zpid"],
                to_address=args.get("to_address"),
                to_lat=args.get("to_lat"),
                to_lng=args.get("to_lng"),
                scope=scope,
            )
        elif name == "location__nearby_pois":
            result = nearby_pois(args["zpid"], args.get("category", "other"), scope=scope)
        elif name == "reviews__summarize":
            result = summarize_reviews(args["zpid"], scope=scope)
        elif name == "search__find_listings":
            result = find_listings(
                args.get("query", ""),
                max_rent=args.get("max_rent"),
                beds=args.get("beds"),
                all_listings=all_listings,
            )
        else:
            result = {"error": f"unknown tool: {name}"}
            err = result["error"]
    except Exception as e:  # noqa: BLE001
        result = {"error": f"{type(e).__name__}: {e}"}
        err = result["error"]

    latency_ms = int((time.monotonic() - t0) * 1000)
    # Build a short preview of the result for the citation footer
    if err:
        preview = err[:80]
    elif isinstance(result, dict):
        # Pick a couple of human-readable fields for the footer
        bits = []
        for k in ("name", "rent_min", "walk_score", "rating", "miles_straight_line", "count"):
            if k in result and result[k] is not None:
                bits.append(f"{k}={result[k]}")
        preview = " ".join(bits) or "(ok)"
    else:
        preview = str(result)[:80]
    return result, ToolCallLog(tool=name, args=args, result_preview=preview, latency_ms=latency_ms, error=err)


def filter_tools_for_lead(lead_name: str) -> list[dict]:
    """Drop the lead's own tools so it can't recurse into itself."""
    return [t for t in TOOL_DEFS if _agent_for_tool(t["name"]) != lead_name]
