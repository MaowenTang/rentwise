"""Resident Reviews fetcher — cache-first, on-demand data pipeline.

Public interface (consumed by api/agents/reviews.py):

    get_reviews(zpid: str) -> list[ReviewsResult]
        Non-blocking cache-only lookup. Returns [] on miss.
        Used by SearchAgent to background pre-warm top-5 results.

    get_reviews_or_fetch(zpid: str, listing_stub: dict) -> list[ReviewsResult]
        Cache-first. On cache miss, performs a live fetch (Yelp aggregate +,
        when available, Google Places review text). Blocks ≤2s then returns
        whatever was retrieved (possibly []).

ReviewsResult schema:
    {
        "source":         str,           # "google_places" | "yelp" | "reddit"
        "rating":         float | None,  # aggregate star rating
        "review_count":   int | None,    # total reviews on platform
        "verified":       bool,          # haversine ≤500m confirmed
        "verified_by":    str | None,    # e.g. "haversine_500m"
        "aggregate_only": bool,          # True = no review text available
        "fetched_at":     str,           # ISO 8601 timestamp
        "url":            str | None,    # link to Yelp/Google Maps page
        "reviews":        list | None,   # [{text, rating, date}] or None
    }

Cache backend:
    File: tools/data/reviews_cache.jsonl  (one JSON record per line, keyed by zpid)
    TTL:  30 days (checked via fetched_at field)
    TODO: migrate to Supabase resident_reviews(zpid, source, data_json, fetched_at)
          when Supabase is provisioned — same interface, swap the backend.

Live fetch sources (in priority order):
    1. Google Places API (New) — review text + aggregate rating (GOOGLE_PLACES_API_KEY)
       POST places.googleapis.com/v1/places:searchText → up to 5 reviews + rating
       Requires "Places API (New)" enabled in GCP (not the legacy "Places API").
    2. Yelp Fusion API    — aggregate rating only (YELP_API_KEY)
       /v3/businesses/search → rating + review_count + url
       Note: /v3/businesses/{id}/reviews returns 404 on the free tier.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeAlias

import httpx

LOG = logging.getLogger("reviews_fetcher")

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_FILE = _REPO_ROOT / "tools" / "data" / "reviews_cache.jsonl"
BATCH_SEED_FILE = _REPO_ROOT / "tools" / "data" / "reviews.jsonl"  # Yelp batch output
CACHE_TTL_DAYS = 30
FETCH_TIMEOUT_S = 2.0  # max wait before returning partial/empty result

ReviewsResult: TypeAlias = dict  # see schema in module docstring

# ---------------------------------------------------------------------------
# In-memory cache (zpid → list[ReviewsResult])
# ---------------------------------------------------------------------------
_cache: dict[str, list[ReviewsResult]] = {}

# Cross-namespace lookup index. Yelp/Google reviews are keyed by Zillow's
# numeric zpid, but active listings often come from apartments.com (apt:*)
# or Craigslist (cl_*). To still credit a Craigslist listing for "Miro
# San Jose" with the existing Miro Yelp reviews, we build an index:
#   normalized_building_name → list[(zpid, lat, lng)]
# and look up by listing.name + lat/lng proximity.
_review_name_index: dict[str, list[tuple[str, float | None, float | None]]] = {}
_review_index_built: bool = False


def _normalize_building_name(name: str) -> str:
    """Lowercase + strip generic tokens so 'The Fay Apartments at San Jose'
    and 'fay-san-jose' both reduce to 'fay'. Used for cross-namespace
    review→listing matching.
    """
    import re
    if not name:
        return ""
    s = name.lower()
    # Strip Yelp URL city suffixes like "-san-jose", "-san-jose-2", "-oakland"
    s = re.sub(r"-(san[- ]jose|san[- ]francisco|oakland|berkeley|bay[- ]area|sf)(-?\d+)?$", " ", s)
    # Drop generic building-word tokens
    s = re.sub(
        r"\b(apartments?|homes?|residences?|apts?|tower|towers|at|the|of|and|by|in|on|"
        r"furnished|luxury|new|co[- ]living|family|community|residential|rentals?)\b",
        " ", s,
    )
    # Collapse non-alphanumeric to single space
    s = re.sub(r"[^a-z0-9]+", " ", s)
    tokens = s.split()
    # Drop very short noise tokens (1-char), but keep 2+ char meaningful words
    tokens = [t for t in tokens if len(t) >= 2]
    return " ".join(tokens)


def _build_review_name_index(listings_lookup: dict[str, tuple[float | None, float | None]] | None = None) -> None:
    """Build review→building-name index. Called lazily on first
    get_reviews_by_listing() call.

    listings_lookup: optional dict zpid → (lat, lng) so we can attach
    coordinates to each review for proximity verification. When None,
    coords stay None and we fall back to name-match-only.
    """
    global _review_index_built
    if _review_index_built:
        return
    import re
    _load_cache()  # make sure _cache is populated first
    for zpid, records in _cache.items():
        for rec in records:
            url = (rec.get("url") or "")
            m = re.search(r"/biz/([^?/]+)", url)
            if not m:
                continue
            raw = m.group(1)
            norm = _normalize_building_name(raw)
            if not norm:
                continue
            lat = lng = None
            if listings_lookup:
                lat, lng = listings_lookup.get(zpid, (None, None))
            _review_name_index.setdefault(norm, []).append((zpid, lat, lng))
    _review_index_built = True
    LOG.info(
        "Built review name index: %d unique normalized names from %d reviews",
        len(_review_name_index),
        sum(len(v) for v in _review_name_index.values()),
    )
_cache_loaded: bool = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_fresh(fetched_at_iso: str) -> bool:
    """True if fetched_at is within the TTL window."""
    try:
        fetched = datetime.fromisoformat(fetched_at_iso)
        age_days = (datetime.now(timezone.utc) - fetched).days
        return age_days < CACHE_TTL_DAYS
    except (ValueError, TypeError):
        return False


def _load_cache() -> None:
    """Load reviews_cache.jsonl + optional batch seed into _cache at startup.

    Merges both files; cache file takes precedence over seed file for the
    same zpid (it may be more recent).
    """
    global _cache, _cache_loaded
    if _cache_loaded:
        return

    combined: dict[str, list[ReviewsResult]] = {}

    # Load batch seed first (lower precedence)
    if BATCH_SEED_FILE.exists():
        try:
            with open(BATCH_SEED_FILE, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        zpid = str(rec.get("zpid", ""))
                        if not zpid:
                            continue
                        result = _batch_record_to_result(rec)
                        combined.setdefault(zpid, [])
                        # Only keep if we don't have a newer entry for this source
                        if not any(r["source"] == result["source"] for r in combined[zpid]):
                            combined[zpid].append(result)
                    except (json.JSONDecodeError, KeyError):
                        pass
            LOG.info("Seeded reviews cache from %s (%d zpids)", BATCH_SEED_FILE.name, len(combined))
        except OSError:
            pass

    # Load persistent cache (higher precedence — overwrites seed entries)
    if CACHE_FILE.exists():
        try:
            fresh_zpids: dict[str, list[ReviewsResult]] = {}
            with open(CACHE_FILE, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        zpid = str(entry.get("zpid", ""))
                        if not zpid:
                            continue
                        records = entry.get("records", [])
                        fetched_at = entry.get("fetched_at", "")
                        if _is_fresh(fetched_at):
                            fresh_zpids[zpid] = records
                    except (json.JSONDecodeError, KeyError):
                        pass
            combined.update(fresh_zpids)
            LOG.info(
                "Loaded reviews cache from %s (%d fresh zpids)",
                CACHE_FILE.name, len(fresh_zpids),
            )
        except OSError:
            pass

    _cache = combined
    _cache_loaded = True


def _batch_record_to_result(rec: dict) -> ReviewsResult:
    """Convert a tools/data/reviews.jsonl record to ReviewsResult format."""
    text = rec.get("text")
    return {
        "source": rec.get("source", "yelp"),
        "rating": rec.get("rating"),
        "review_count": rec.get("review_count"),
        "verified": rec.get("verified", False),
        "verified_by": rec.get("verified_by"),
        "aggregate_only": rec.get("aggregate_only", text is None),
        "fetched_at": rec.get("scraped_at", _now_iso()),
        "url": rec.get("url"),
        "reviews": [{"text": text, "rating": rec.get("rating"), "date": rec.get("review_date")}]
        if text else None,
    }


def _write_to_cache(zpid: str, records: list[ReviewsResult]) -> None:
    """Persist a zpid's records to the cache file and update in-memory index."""
    _cache[zpid] = records
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "zpid": zpid,
        "fetched_at": _now_iso(),
        "records": records,
    }
    with open(CACHE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _cache_is_fresh(zpid: str) -> bool:
    """Check if we have non-stale data for this zpid."""
    if zpid not in _cache:
        return False
    records = _cache[zpid]
    if not records:
        return False
    # Use the oldest fetched_at among the records as the conservative bound
    oldest = min(
        (r.get("fetched_at", "") for r in records),
        default=""
    )
    return _is_fresh(oldest)


# ---------------------------------------------------------------------------
# Haversine (duplicated here so fetcher has no dep on tools/)
# ---------------------------------------------------------------------------
def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ---------------------------------------------------------------------------
# Live fetch: Yelp (aggregate only — reviews endpoint restricted on free tier)
# ---------------------------------------------------------------------------
async def _fetch_yelp(listing_stub: dict, client: httpx.AsyncClient) -> ReviewsResult | None:
    """Search Yelp for the listing; return aggregate rating record or None."""
    api_key = os.environ.get("YELP_API_KEY", "")
    if not api_key:
        return None

    name = listing_stub.get("name", "")
    address = listing_stub.get("address", "")
    city = listing_stub.get("city", "")
    lat = listing_stub.get("lat")
    lng = listing_stub.get("lng")
    location = f"{address}, {city}" if address else city

    try:
        resp = await client.get(
            "https://api.yelp.com/v3/businesses/search",
            params={"term": name, "location": location, "limit": 3, "categories": "apartments"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        if resp.status_code != 200:
            return None
        businesses = resp.json().get("businesses", [])
        if not businesses:
            return None
        biz = businesses[0]
    except httpx.HTTPError:
        return None

    # Haversine distance check
    biz_lat = biz.get("coordinates", {}).get("latitude")
    biz_lng = biz.get("coordinates", {}).get("longitude")
    verified = False
    verified_by = "yelp_business_match"

    if lat and lng and biz_lat and biz_lng:
        dist_m = _haversine_m(lat, lng, biz_lat, biz_lng)
        if dist_m > 500:
            LOG.debug("Yelp: %s discarded — %.0fm away", name, dist_m)
            return None
        verified = True
        verified_by = "haversine_500m"
    elif not lat:
        verified_by = "no_coords"

    agg_rating = biz.get("rating")
    agg_count = biz.get("review_count", 0)
    if agg_rating is None or agg_count == 0:
        return None

    return {
        "source": "yelp",
        "rating": float(agg_rating),
        "review_count": int(agg_count),
        "verified": verified,
        "verified_by": verified_by,
        "aggregate_only": True,      # reviews endpoint blocked on free tier
        "fetched_at": _now_iso(),
        "url": biz.get("url"),
        "reviews": None,
    }


# ---------------------------------------------------------------------------
# Live fetch: Google Places (New) — review text + aggregate rating
# ---------------------------------------------------------------------------
async def _fetch_google_places(listing_stub: dict, client: httpx.AsyncClient) -> ReviewsResult | None:
    """Fetch Google Places (New) reviews for a listing.

    Endpoint: POST https://places.googleapis.com/v1/places:searchText
    Returns up to 5 review texts plus aggregate rating from Google Maps.

    listing_stub expected keys: name, address, lat (float|None), lng (float|None)
    The 'city' key is optional; address typically includes city already.

    Verification: haversine distance ≤500m from listing coords.
    ~$0.017/call — cost is acceptable for on-demand user-triggered fetches.
    """
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY", "")
    if not api_key:
        return None

    name = listing_stub.get("name", "")
    address = listing_stub.get("address", "")
    lat = listing_stub.get("lat")
    lng = listing_stub.get("lng")

    # Build text query: "Building Name, Full Address" gives best match precision
    query = f"{name}, {address}".strip(", ") if address else name
    if not query:
        return None

    try:
        resp = await client.post(
            "https://places.googleapis.com/v1/places:searchText",
            json={"textQuery": query, "maxResultCount": 1},
            headers={
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": (
                    "places.id,places.displayName,places.rating,"
                    "places.userRatingCount,places.reviews,"
                    "places.googleMapsUri,places.location"
                ),
            },
            timeout=FETCH_TIMEOUT_S,
        )
        if resp.status_code != 200:
            LOG.warning(
                "Google Places API error %s for %s: %s",
                resp.status_code, name, resp.text[:300],
            )
            return None
        places = resp.json().get("places", [])
        if not places:
            LOG.debug("Google Places: no results for %r", query)
            return None
        place = places[0]
    except httpx.HTTPError as exc:
        LOG.warning("Google Places HTTP error for %s: %s", name, exc)
        return None

    # Haversine distance verification (≤500m required)
    loc = place.get("location", {})
    place_lat = loc.get("latitude")
    place_lng = loc.get("longitude")
    verified = False
    verified_by = "google_text_match"

    if lat and lng and place_lat and place_lng:
        dist_m = _haversine_m(lat, lng, place_lat, place_lng)
        if dist_m > 500:
            LOG.debug("Google Places: %s discarded — %.0fm away from listing coords", name, dist_m)
            return None
        verified = True
        verified_by = "haversine_500m"
    elif not lat:
        verified_by = "no_coords"

    rating = place.get("rating")
    review_count = place.get("userRatingCount")
    maps_url = place.get("googleMapsUri")

    # Parse review texts (API returns up to 5)
    raw_reviews = place.get("reviews") or []
    reviews: list[dict] = []
    for r in raw_reviews:
        # text field is a LocalizedText object: {"text": "...", "languageCode": "en"}
        text_obj = r.get("text") or {}
        text = (
            text_obj.get("text", "").strip()
            if isinstance(text_obj, dict)
            else str(text_obj).strip()
        )
        if not text:
            continue
        publish_time = r.get("publishTime", "")
        reviews.append({
            "text": text,
            "rating": r.get("rating"),
            "date": publish_time[:10] if publish_time else None,
            "author": (r.get("authorAttribution") or {}).get("displayName"),
        })

    return {
        "source": "google_places",
        "rating": float(rating) if rating is not None else None,
        "review_count": int(review_count) if review_count is not None else None,
        "verified": verified,
        "verified_by": verified_by,
        "aggregate_only": len(reviews) == 0,
        "fetched_at": _now_iso(),
        "url": maps_url,
        "reviews": reviews if reviews else None,
    }


# ---------------------------------------------------------------------------
# Live fetch: orchestrator
# ---------------------------------------------------------------------------
async def _live_fetch(listing_stub: dict) -> list[ReviewsResult]:
    """Attempt live fetch from all available sources.

    Priority: Google Places (review text) → Yelp (aggregate only).
    Returns whatever data was successfully retrieved within the timeout.
    """
    results: list[ReviewsResult] = []
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_S) as client:
        # Run sources concurrently (Google Places takes priority if both succeed)
        gp_result, yelp_result = await asyncio.gather(
            _fetch_google_places(listing_stub, client),
            _fetch_yelp(listing_stub, client),
            return_exceptions=True,
        )
        if not isinstance(gp_result, Exception) and gp_result is not None:
            results.append(gp_result)
        if not isinstance(yelp_result, Exception) and yelp_result is not None:
            results.append(yelp_result)
    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_reviews(zpid: str) -> list[ReviewsResult]:
    """Non-blocking cache-only lookup.

    Returns [] on miss — does NOT trigger a live fetch.
    Intended for SearchAgent's background pre-warm: call this after the top-5
    results are determined; if [], spawn a daemon thread that calls
    asyncio.run(get_reviews_or_fetch(...)) so the cache is warm by the time
    the user drills into a listing.

    Usage (SearchAgent daemon-thread pattern):
        # In SearchAgent._prewarm_reviews():
        def _fetch_one(zpid, stub):
            try:
                if not get_reviews(zpid):
                    asyncio.run(get_reviews_or_fetch(zpid, stub))
            except Exception:
                pass
        threading.Thread(target=_fetch_one, args=(zpid, stub), daemon=True).start()
    """
    _load_cache()
    if _cache_is_fresh(zpid):
        return _cache[zpid]
    return []


def get_reviews_by_listing(
    listing_zpid: str,
    listing_name: str | None,
    listing_lat: float | None = None,
    listing_lng: float | None = None,
    listings_lookup: dict[str, tuple[float | None, float | None]] | None = None,
    max_distance_mi: float = 0.5,
) -> list[ReviewsResult]:
    """Cross-namespace review lookup.

    1. Direct zpid hit (covers Zillow listings).
    2. Building-name match (covers Craigslist/apartments.com listings whose
       zpid namespace differs but which refer to the same building as a
       cached Yelp/Google review). Optionally verified by haversine
       distance < max_distance_mi.
    Returns [] on no match.
    """
    _load_cache()
    direct = _cache.get(listing_zpid) or []
    if direct and _cache_is_fresh(listing_zpid):
        return direct
    if not listing_name:
        return []
    _build_review_name_index(listings_lookup)
    norm = _normalize_building_name(listing_name)
    if not norm:
        return []
    # Token-overlap match: normalize listing → check if any cached building
    # has overlapping tokens (≥1 substantial token in common).
    listing_tokens = set(norm.split())
    if not listing_tokens:
        return []
    best_zpid: str | None = None
    best_overlap: int = 0
    best_distance: float | None = None
    for rev_norm, entries in _review_name_index.items():
        rev_tokens = set(rev_norm.split())
        overlap = len(listing_tokens & rev_tokens)
        if overlap == 0:
            continue
        # Pick the entry with closest geography if we have coords.
        for zpid, lat, lng in entries:
            if listing_lat is not None and lat is not None and listing_lng is not None and lng is not None:
                from math import asin, cos, radians, sin, sqrt
                p1, p2 = radians(listing_lat), radians(lat)
                dlat = radians(lat - listing_lat)
                dlng = radians(lng - listing_lng)
                a = sin(dlat / 2) ** 2 + cos(p1) * cos(p2) * sin(dlng / 2) ** 2
                d_mi = 2 * 3958.7613 * asin(sqrt(a))
                if d_mi > max_distance_mi:
                    continue  # name match but wrong building
                if best_distance is None or d_mi < best_distance:
                    best_distance = d_mi
                    best_zpid = zpid
                    best_overlap = overlap
            else:
                # No coords — accept name match (best-overlap wins)
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_zpid = zpid
    if best_zpid:
        return _cache.get(best_zpid, [])
    return []


async def get_reviews_or_fetch(zpid: str, listing_stub: dict) -> list[ReviewsResult]:
    """Cache-first lookup with live fetch fallback.

    On cache miss, performs a live fetch (Yelp aggregate + Google Places when
    available). Blocks at most FETCH_TIMEOUT_S seconds. Falls back to []
    on timeout or API error.

    listing_stub required keys: zpid, name, address, lat (float|None),
                                lng (float|None), city (str)

    Called by ResidentReviewsAgent when the user explicitly asks about a
    listing — NOT in the Search flow (use get_reviews() + create_task there).
    """
    _load_cache()

    # Cache hit — return immediately
    if _cache_is_fresh(zpid):
        LOG.debug("reviews cache HIT for zpid=%s", zpid)
        return _cache[zpid]

    # Cache miss — live fetch with timeout guard
    LOG.info("reviews cache MISS for zpid=%s — fetching live", zpid)
    try:
        records = await asyncio.wait_for(_live_fetch(listing_stub), timeout=FETCH_TIMEOUT_S)
    except asyncio.TimeoutError:
        LOG.warning("reviews live fetch timed out for zpid=%s", zpid)
        records = []
    except Exception as exc:
        LOG.warning("reviews live fetch error for zpid=%s: %s", zpid, exc)
        records = []

    if records:
        _write_to_cache(zpid, records)
    return records
