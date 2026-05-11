"""User profile + ranking service.

The profile is built up turn-by-turn by ProfileUpdater (LLM-extracts revealed
prefs). RankingService scores any listing against the current profile and
returns a 0-100 overall score plus per-feature breakdown for the UI.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from math import asin, cos, radians, sin, sqrt
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx
import numpy as np

from anthropic import Anthropic

from listings import Listing

# fastembed is an optional dependency (lighter than sentence-transformers;
# uses ONNX runtime, no PyTorch required). The app degrades gracefully when
# it's not installed — P3 semantic component is simply skipped.
try:
    from fastembed import TextEmbedding as _FastTextEmbedding
    _FASTEMBED_OK = True
except ImportError:
    _FASTEMBED_OK = False

LOG = logging.getLogger(__name__)

EARTH_MI = 3958.7613


def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = radians(lat1), radians(lat2)
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(p1) * cos(p2) * sin(dlon / 2) ** 2
    return 2 * EARTH_MI * asin(sqrt(a))


@dataclass
class CommuteTarget:
    name: str            # "Apple HQ"
    address: str = ""    # full address if known
    lat: float | None = None
    lng: float | None = None
    max_minutes: int | None = None  # soft cap for scoring


# Common Bay Area city aliases. When `avoid` contains a short form like "SF",
# we expand to also check for the full city name in listing.address/city.
# Keys must be lowercased; values are tokens that, if found in the listing
# blob, count as a hit. (Substring match, so "san francisco" catches
# "San Francisco, CA 94110".)
_CITY_ALIASES: dict[str, tuple[str, ...]] = {
    "sf": ("sf", "san francisco"),
    "sf proper": ("sf", "san francisco"),
    "san francisco": ("sf", "san francisco"),
    "the city": ("san francisco",),
    "sj": ("sj", "san jose"),
    "san jose": ("san jose",),
    "downtown sj": ("downtown, san jose", "downtown san jose"),
    "downtown san jose": ("downtown, san jose", "downtown san jose"),
    "downtown sf": ("downtown, san francisco", "soma", "financial district"),
    "tenderloin": ("tenderloin",),
    "ssf": ("south san francisco", "ssf"),
    "south san francisco": ("south san francisco",),
    "oakland": ("oakland",),
    "berkeley": ("berkeley",),
    "fremont": ("fremont",),
    "hayward": ("hayward",),
    "san mateo": ("san mateo",),
    "palo alto": ("palo alto",),
    "central palo alto": ("palo alto, ca", "downtown palo alto", "university ave"),
    "downtown palo alto": ("palo alto, ca", "downtown palo alto", "university ave"),
    "mountain view": ("mountain view",),
    "sunnyvale": ("sunnyvale",),
    "cupertino": ("cupertino",),
    "santa clara": ("santa clara",),
    "milpitas": ("milpitas",),
    "willow glen": ("willow glen",),
    "redwood city": ("redwood city",),
    "menlo park": ("menlo park",),
    "burlingame": ("burlingame",),
    "belmont": ("belmont",),
    "san bruno": ("san bruno",),
    "millbrae": ("millbrae",),
    "daly city": ("daly city",),
    "walnut creek": ("walnut creek",),
}


# Review-related preference keywords that should activate the reviews
# scoring component (queries cached resident reviews for sentiment hits).
_REVIEW_PREFERENCE_HINTS = (
    "quiet", "safe", "peaceful", "no noise", "no highway noise",
    "thin walls", "thick walls", "good neighbors", "well-maintained",
    "good for sleeping", "no late-night noise",
)
# Positive / negative tokens scanned within the review text.
_REVIEW_POSITIVE_TOKENS = (
    "quiet", "peaceful", "safe", "well maintained", "well-maintained",
    "clean", "responsive management", "good neighbors", "thick walls",
    "no noise", "calm",
)
_REVIEW_NEGATIVE_TOKENS = (
    "loud", "noisy", "thin walls", "roach", "rat", "bug infestation",
    "unsafe", "dangerous", "break-in", "broken", "mold", "leak",
    "rude management", "smell", "construction noise", "highway noise",
)


def _profile_wants_review_signal(profile: "UserProfile") -> bool:
    blob = " ".join(profile.must_haves + profile.nice_to_haves + [profile.notes or ""]).lower()
    return any(h in blob for h in _REVIEW_PREFERENCE_HINTS)


def _review_sentiment_score(
    zpid: str,
    listing_name: str | None = None,
    listing_lat: float | None = None,
    listing_lng: float | None = None,
) -> float | None:
    """Score 0-10 based on cached reviews. Returns None when no cached
    reviews exist (so the ranker can skip the component cleanly).

    When listing_name is provided, falls back to cross-namespace lookup
    (matches building name + geo proximity) for listings whose zpid isn't
    directly in the review cache.
    """
    try:
        from reviews_fetcher import get_reviews, get_reviews_by_listing
    except ImportError:
        return None
    records = get_reviews(zpid)
    if not records and listing_name:
        records = get_reviews_by_listing(
            zpid, listing_name, listing_lat, listing_lng,
        )
    if not records:
        return None
    # Aggregate text + ratings across all sources.
    all_text = []
    ratings = []
    for rec in records:
        if rec.get("rating") is not None:
            try:
                ratings.append(float(rec["rating"]))
            except (TypeError, ValueError):
                pass
        for r in (rec.get("reviews") or []):
            if r.get("text"):
                all_text.append(str(r["text"]).lower())
    if not all_text and not ratings:
        return None
    text_blob = " ".join(all_text)
    pos = sum(1 for t in _REVIEW_POSITIVE_TOKENS if t in text_blob)
    neg = sum(1 for t in _REVIEW_NEGATIVE_TOKENS if t in text_blob)
    # Start from rating-based score if available; otherwise neutral 5.
    if ratings:
        avg = sum(ratings) / len(ratings)
        # Most aggregate ratings are 1-5; map to 0-10
        base = max(0.0, min(10.0, (avg - 1.0) * 2.5))
    else:
        base = 5.0
    # Apply text sentiment tweak: each pos token +0.5, each neg -1.0 (asymmetric)
    adjusted = base + (pos * 0.5) - (neg * 1.0)
    return max(0.0, min(10.0, adjusted))


# ---------------------------------------------------------------------------
# Reddit community sentiment — neighborhood-level signal extracted from
# r/SanJose, r/bayarea, r/BayAreaRealEstate posts. Built by
# tools/extract_reddit_sentiment.py. Loaded lazily on first use.
# Schema per line:
#   {"neighborhood": "willow glen", "sentiment_score": 0.5,
#    "positive_count": 2, "negative_count": 0, ...}
# ---------------------------------------------------------------------------

_REDDIT_SENTIMENT: dict[str, dict] | None = None


def _load_reddit_sentiment() -> dict[str, dict]:
    global _REDDIT_SENTIMENT
    if _REDDIT_SENTIMENT is not None:
        return _REDDIT_SENTIMENT
    _REDDIT_SENTIMENT = {}
    path = (
        os.path.dirname(os.path.abspath(__file__))
        + "/data/reddit_neighborhood_sentiment.jsonl"
    )
    if not os.path.exists(path):
        LOG.info("Reddit neighborhood sentiment file not found at %s", path)
        return _REDDIT_SENTIMENT
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    name = (rec.get("neighborhood") or "").lower().strip()
                    if name:
                        _REDDIT_SENTIMENT[name] = rec
                except json.JSONDecodeError:
                    continue
        LOG.info(
            "Loaded Reddit sentiment for %d neighborhoods",
            len(_REDDIT_SENTIMENT),
        )
    except OSError as e:
        LOG.warning("reddit sentiment load failed: %s", e)
    return _REDDIT_SENTIMENT


def _community_sentiment_score(listing: Listing) -> float | None:
    """Map listing.neighborhood / city to a 0-10 score based on aggregated
    Reddit sentiment. Returns None when no Reddit signal exists.
    """
    sent = _load_reddit_sentiment()
    if not sent:
        return None
    candidates: list[str] = []
    if listing.neighborhood:
        candidates.append(listing.neighborhood.lower())
    # Also try parsed city from address
    if listing.address:
        parts = [p.strip().lower() for p in listing.address.split(",")]
        if len(parts) >= 2:
            candidates.append(parts[-2])
    for cand in candidates:
        if cand in sent:
            # sentiment_score is in [-1, 1]; map to [0, 10]:
            #   -1 → 0, 0 → 5, +1 → 10
            return (sent[cand]["sentiment_score"] + 1) * 5
    return None


# ---------------------------------------------------------------------------
# Verifier registry — replaces naive substring match for must_haves /
# nice_to_haves. Each verifier maps a preference text → 0-10 score. Returns
# None when the verifier doesn't apply (so the caller can fall back to a
# generic check or skip).
# ---------------------------------------------------------------------------

def _verify_parking(pref: str, listing: Listing) -> float | None:
    """Match preferences like 'parking', 'parking under $200', 'parking
    included', 'covered parking', 'garage'.
    """
    p = pref.lower()
    if "parking" not in p and "garage" not in p:
        return None
    has_parking = bool(listing.parking_types) or "garage" in (listing.description or "").lower()
    # Cost-bounded parking: parse first dollar amount from pref.
    import re
    m = re.search(r"\$?\s*(\d{2,4})", p)
    if m and ("under" in p or "≤" in p or "<=" in p or "<" in p or "less than" in p):
        cap = int(m.group(1))
        # Look at raw parking fee field if available; otherwise scan description.
        raw_fee = None
        if isinstance(listing.raw, dict):
            raw_fee = listing.raw.get("parking_fee") or listing.raw.get("parking_cost")
        if raw_fee is not None:
            try:
                fee = float(raw_fee)
                return 10.0 if fee <= cap else max(0.0, 10.0 - (fee - cap) / 10.0)
            except (TypeError, ValueError):
                pass
        # Scan description for "$X parking" pattern
        desc = (listing.description or "").lower()
        for match in re.finditer(r"\$\s*(\d{2,4})\s*(?:[/\s]+(?:mo|month|monthly))?\s*(?:for\s+)?parking", desc):
            fee = int(match.group(1))
            return 10.0 if fee <= cap else max(0.0, 10.0 - (fee - cap) / 10.0)
        # No explicit fee mentioned — give partial credit if parking exists
        return 7.0 if has_parking else 0.0
    # "parking included" / "free parking"
    if "included" in p or "free" in p or "no cost" in p:
        if has_parking:
            desc = (listing.description or "").lower()
            if "included" in desc or "free parking" in desc or "no cost" in desc:
                return 10.0
            return 5.0  # has parking but unclear if included
        return 0.0
    # Generic parking want
    return 10.0 if has_parking else 0.0


def _verify_in_unit_laundry(pref: str, listing: Listing) -> float | None:
    p = pref.lower()
    if not ("laundry" in p or "washer" in p or "dryer" in p or "w/d" in p):
        return None
    blob = ((listing.description or "") + " " + " ".join(listing.utilities_included or [])).lower()
    if any(k in blob for k in ("in-unit", "in unit laundry", "in-unit laundry", "in-unit w/d", "washer/dryer in", "stackable washer")):
        return 10.0
    if "laundry" in blob or "washer" in blob:
        return 5.0  # has some laundry, unclear if in-unit
    return 0.0


def _verify_balcony(pref: str, listing: Listing) -> float | None:
    p = pref.lower()
    if not ("balcony" in p or "patio" in p or "outdoor" in p):
        return None
    if listing.has_patio_balcony:
        return 10.0
    desc = (listing.description or "").lower()
    if "balcony" in desc or "patio" in desc:
        return 8.0
    return 0.0


def _verify_pool_or_gym(pref: str, listing: Listing) -> float | None:
    p = pref.lower()
    if "pool" in p:
        if listing.has_pool:
            return 10.0
        return 8.0 if "pool" in (listing.description or "").lower() else 0.0
    if "gym" in p or "fitness" in p:
        return 8.0 if "gym" in (listing.description or "").lower() or "fitness" in (listing.description or "").lower() else 0.0
    return None


def _verify_elevator(pref: str, listing: Listing) -> float | None:
    if "elevator" not in pref.lower():
        return None
    if listing.has_elevator:
        return 10.0
    return 5.0 if "elevator" in (listing.description or "").lower() else 0.0


_VERIFIERS = (
    _verify_parking,
    _verify_in_unit_laundry,
    _verify_balcony,
    _verify_pool_or_gym,
    _verify_elevator,
)


def _score_preference(pref: str, listing: Listing) -> float:
    """Try each verifier in turn. Falls back to substring match if no
    verifier applies (so the system still works for novel preference text).
    Returns 0-10.
    """
    for v in _VERIFIERS:
        s = v(pref, listing)
        if s is not None:
            return s
    # Fallback: substring match on the listing blob (same as old behavior).
    # We use a lightweight version here to avoid recomputing the full blob.
    p = pref.lower().strip()
    if not p:
        return 5.0  # empty pref — neutral
    text = (
        (listing.description or "") + " "
        + (listing.neighborhood or "") + " "
        + (listing.address or "")
    ).lower()
    return 10.0 if p in text else 0.0


_SHORT_TERM_HINTS = (
    "short-term", "short term", "shortterm", "month-to-month",
    "month to month", "no 12-month", "no 12 month", "no annual lease",
    "13-week", "13 week", "13week", "3-month", "3 month", "6-month",
    "6 month", "travel nurse", "rotational", "temporary",
)
_FURNISHED_HINTS = ("furnished",)


def _profile_wants_short_term(profile: "UserProfile") -> bool:
    blob = " ".join(profile.must_haves + profile.nice_to_haves + [profile.notes or ""]).lower()
    return any(h in blob for h in _SHORT_TERM_HINTS)


def _profile_wants_furnished(profile: "UserProfile") -> bool:
    blob = " ".join(profile.must_haves + profile.nice_to_haves + [profile.notes or ""]).lower()
    return any(h in blob for h in _FURNISHED_HINTS)


def _listing_lease_compatible(listing: Listing, max_lease_months: int) -> bool:
    """True iff the listing offers a lease ≤ max_lease_months. Conservative:
    unknown leases default to True (don't over-filter). Negative signals in
    the name/description (e.g. "12-mo lease") DO count against compatibility.
    """
    import re
    # First check explicit lease_lengths field if present.
    raw_lengths = listing.raw.get("lease_lengths") if isinstance(listing.raw, dict) else None
    if raw_lengths:
        nums = [int(m) for m in re.findall(r"\d+", str(raw_lengths))]
        if nums:
            return any(n <= max_lease_months for n in nums)
    # Second: scan name/description for long-lease signatures. These are
    # negative signals — if found, mark incompatible. (We don't infer
    # *compatibility* from text, only *incompatibility*, to stay conservative.)
    text = ((listing.name or "") + " " + (listing.description or "")).lower()
    # Patterns like "12-mo", "12 month lease", "12mo", "annual lease"
    if re.search(r"\b(12|13|14|15|18|24)\s*[- ]?\s*(mo|month|months)\b", text):
        return False
    if "annual lease" in text or "year lease" in text or "yearly lease" in text:
        return False
    return True


def _listing_is_furnished(listing: Listing) -> bool:
    if not isinstance(listing.raw, dict):
        return False
    if listing.raw.get("is_furnished") is True:
        return True
    desc = (listing.description or "").lower()
    name = (listing.name or "").lower()
    return "furnished" in desc or "furnished" in name


def _expand_avoid_terms(raw: str) -> tuple[str, ...]:
    """Expand a raw `avoid` keyword to a tuple of lowercased substrings to
    test against a listing blob. Falls back to just the keyword itself.
    """
    k = (raw or "").strip().lower()
    if not k:
        return ()
    if k in _CITY_ALIASES:
        return _CITY_ALIASES[k]
    # Best-effort partial alias (e.g. "loud sf nightlife" still gets the
    # "san francisco" check via tokenization).
    for alias, terms in _CITY_ALIASES.items():
        if alias in k:
            return tuple({k, *terms})
    return (k,)


# Mapbox geocoding fallback for commute targets the LLM extracts but that
# aren't in EMPLOYER_HQ. Cached in-process; never raises (degrades to None).
# Biased toward the Bay Area via proximity= and bbox=.
_GEOCODE_CACHE: dict[str, tuple[float, float, str] | None] = {}


# POI / airport alias table — Mapbox's fuzzy place search treats these poorly
# (e.g. "SFO" → SF city center; "biotech firm in San Mateo" → The Castro).
# Hand-curated short list catches the common cases before falling through.
_POI_ALIASES: dict[str, tuple[float, float, str]] = {
    # Airports
    "sfo": (37.6191, -122.3816, "San Francisco International Airport"),
    "san francisco international airport": (37.6191, -122.3816, "SFO Airport"),
    "san francisco airport": (37.6191, -122.3816, "SFO Airport"),
    "sjc": (37.3639, -121.929, "San Jose International Airport"),
    "san jose airport": (37.3639, -121.929, "SJC Airport"),
    "san jose international airport": (37.3639, -121.929, "SJC Airport"),
    "oak": (37.7126, -122.2197, "Oakland International Airport"),
    "oakland airport": (37.7126, -122.2197, "OAK Airport"),
    "oakland international airport": (37.7126, -122.2197, "OAK Airport"),
    # Hospitals / campuses commonly referenced informally
    "ucsf parnassus": (37.7634, -122.4577, "UCSF Parnassus"),
    "ucsf mission bay": (37.7679, -122.3923, "UCSF Mission Bay"),
    # Big-stadium / arena
    "chase center": (37.7680, -122.3878, "Chase Center"),
    "oracle park": (37.7786, -122.3893, "Oracle Park"),
}


# Bay Area city centroids — used to sanity-check Mapbox's response. If the
# user query names a city but Mapbox returns coords far from that city's
# centroid, we reject the result.
_CITY_CENTROIDS: dict[str, tuple[float, float]] = {
    "san francisco": (37.7749, -122.4194),
    "south san francisco": (37.6547, -122.4077),
    "san mateo": (37.5630, -122.3255),
    "burlingame": (37.5841, -122.3661),
    "millbrae": (37.5985, -122.3872),
    "san bruno": (37.6305, -122.4111),
    "daly city": (37.6879, -122.4702),
    "redwood city": (37.4852, -122.2364),
    "menlo park": (37.4530, -122.1817),
    "palo alto": (37.4419, -122.1430),
    "mountain view": (37.3861, -122.0839),
    "sunnyvale": (37.3688, -122.0363),
    "cupertino": (37.3230, -122.0322),
    "santa clara": (37.3541, -121.9552),
    "san jose": (37.3382, -121.8863),
    "oakland": (37.8044, -122.2712),
    "berkeley": (37.8715, -122.2730),
    "hayward": (37.6688, -122.0808),
    "fremont": (37.5485, -121.9886),
    "walnut creek": (37.9101, -122.0652),
    "alameda": (37.7652, -122.2416),
    "foster city": (37.5585, -122.2711),
    "belmont": (37.5202, -122.2758),
    "pacifica": (37.6138, -122.4869),
}


def _extract_city_keyword(query: str) -> str | None:
    """If `query` mentions a known Bay Area city (e.g. 'biotech firm in San
    Mateo'), return the city in lowercase. Used to validate Mapbox responses.
    """
    q = (query or "").lower()
    # Longest-first to prefer "south san francisco" over "san francisco".
    for city in sorted(_CITY_CENTROIDS.keys(), key=len, reverse=True):
        if city in q:
            return city
    return None


def geocode_place(query: str) -> tuple[float, float, str] | None:
    """Resolve a free-text place to (lat, lng, formatted_address).

    Resolution order:
      1. POI alias table (airports, hospitals, stadiums) — exact-match only.
      2. Mapbox geocoding API, *with response validation*: if the user's
         query names a city, the returned coordinates must be within ~15 km
         of that city's centroid. Mismatches are rejected to avoid the
         "biotech firm in San Mateo → The Castro, SF" failure mode.

    Returns None if the query is empty, no token, request fails, or the
    response fails city-centroid validation. Cached per process.
    """
    q = (query or "").strip()
    if not q:
        return None
    key = q.lower()
    if key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[key]

    # Step 1: POI alias hit (exact substring match for short aliases).
    for alias, (lat, lng, name) in _POI_ALIASES.items():
        if alias == key or alias in key:
            result = (lat, lng, name)
            _GEOCODE_CACHE[key] = result
            LOG.info("POI alias %r -> %s (%.4f, %.4f)", q, name, lat, lng)
            return result

    token = os.environ.get("MAPBOX_TOKEN") or os.environ.get("NEXT_PUBLIC_MAPBOX_TOKEN")
    if not token:
        _GEOCODE_CACHE[key] = None
        return None

    # Step 2: Mapbox geocoding.
    try:
        url = f"https://api.mapbox.com/geocoding/v5/mapbox.places/{quote(q)}.json"
        r = httpx.get(
            url,
            params={
                "access_token": token,
                "country": "us",
                "limit": 1,
                # Bias to Bay Area. proximity centers ranking; bbox hard-filters.
                "proximity": "-122.2,37.6",
                "bbox": "-122.8,36.9,-121.5,38.2",
            },
            timeout=3.0,
        )
        r.raise_for_status()
        features = r.json().get("features", [])
        if not features:
            _GEOCODE_CACHE[key] = None
            return None
        feat = features[0]
        lng, lat = float(feat["center"][0]), float(feat["center"][1])
        addr = feat.get("place_name", q)

        # Validate against city-centroid if the query names a known city.
        # Mapbox's mapbox.places endpoint is permissive — "biotech firm in
        # San Mateo" returns "The Castro, SF" because the only matching
        # token is "in". Reject if the returned coord is > ~15 km from the
        # named city centroid.
        city_kw = _extract_city_keyword(q)
        if city_kw:
            cx, cy = _CITY_CENTROIDS[city_kw]
            d_mi = haversine(lat, lng, cx, cy)
            # 5 mi (~8 km) — tight enough that "biotech firm in San Mateo"
            # which Mapbox resolves to South SF (~6.6 mi away) gets rejected
            # and falls back to the San Mateo centroid.
            if d_mi > 5.0:
                LOG.warning(
                    "geocode rejected for %r: result (%.4f,%.4f) is %.1f mi "
                    "from %s centroid", q, lat, lng, d_mi, city_kw,
                )
                # Fall back to the city centroid itself rather than returning
                # the wrong place — better to anchor coarsely on the right
                # city than precisely on the wrong one.
                fallback = (cx, cy, f"{city_kw.title()}, CA (centroid)")
                _GEOCODE_CACHE[key] = fallback
                return fallback

        result = (lat, lng, addr)
        _GEOCODE_CACHE[key] = result
        LOG.info("geocoded %r -> (%.4f, %.4f) %s", q, lat, lng, addr)
        return result
    except Exception as e:
        LOG.warning("geocode failed for %r: %s", q, e)
        _GEOCODE_CACHE[key] = None
        return None


# Well-known employer locations (so users can say "Apple", "Google" etc.)
EMPLOYER_HQ: dict[str, dict] = {
    "apple":  {"name": "Apple Park", "address": "One Apple Park Way, Cupertino, CA",
               "lat": 37.3349, "lng": -122.0090},
    "google": {"name": "Google HQ", "address": "1600 Amphitheatre Pkwy, Mountain View, CA",
               "lat": 37.4220, "lng": -122.0841},
    "meta":   {"name": "Meta HQ", "address": "1 Hacker Way, Menlo Park, CA",
               "lat": 37.4848, "lng": -122.1484},
    "facebook": {"name": "Meta HQ", "address": "1 Hacker Way, Menlo Park, CA",
                 "lat": 37.4848, "lng": -122.1484},
    "nvidia": {"name": "Nvidia HQ", "address": "2788 San Tomas Expy, Santa Clara, CA",
               "lat": 37.3677, "lng": -121.9693},
    "tesla":  {"name": "Tesla HQ", "address": "1 Tesla Rd, Austin, TX",  # actually Austin, but Fremont factory:
               "lat": 37.4936, "lng": -121.9446},  # using Fremont factory
    "netflix": {"name": "Netflix HQ", "address": "100 Winchester Cir, Los Gatos, CA",
                "lat": 37.2581, "lng": -121.9750},
    "linkedin": {"name": "LinkedIn HQ", "address": "1000 W Maude Ave, Sunnyvale, CA",
                 "lat": 37.4233, "lng": -122.0072},
    "adobe": {"name": "Adobe HQ", "address": "345 Park Ave, San Jose, CA",
              "lat": 37.3308, "lng": -121.8932},
    "cisco": {"name": "Cisco HQ", "address": "170 W Tasman Dr, San Jose, CA",
              "lat": 37.4109, "lng": -121.9350},
    "ebay":  {"name": "eBay HQ", "address": "2025 Hamilton Ave, San Jose, CA",
              "lat": 37.2962, "lng": -121.9292},
    "paypal": {"name": "PayPal HQ", "address": "2211 N 1st St, San Jose, CA",
               "lat": 37.3725, "lng": -121.9114},
    "intel":  {"name": "Intel HQ", "address": "2200 Mission College Blvd, Santa Clara, CA",
               "lat": 37.3878, "lng": -121.9627},
    "amd":    {"name": "AMD HQ", "address": "2485 Augustine Dr, Santa Clara, CA",
               "lat": 37.4091, "lng": -121.9760},
    # Healthcare / research
    "stanford hospital": {"name": "Stanford Hospital", "address": "300 Pasteur Dr, Stanford, CA 94305",
                          "lat": 37.4332, "lng": -122.1755},
    "stanford": {"name": "Stanford University", "address": "450 Jane Stanford Way, Stanford, CA 94305",
                 "lat": 37.4275, "lng": -122.1697},
    "ucsf": {"name": "UCSF Mission Bay", "address": "1825 4th St, San Francisco, CA 94158",
             "lat": 37.7679, "lng": -122.3923},
    "genentech": {"name": "Genentech", "address": "1 DNA Way, South San Francisco, CA 94080",
                  "lat": 37.6586, "lng": -122.3877},
    # SF tech (note: many have separate SF and South SF / Peninsula offices)
    "stripe": {"name": "Stripe (South SF)", "address": "354 Oyster Point Blvd, South San Francisco, CA",
               "lat": 37.6680, "lng": -122.3870},
    "stripe sf": {"name": "Stripe SF", "address": "510 Townsend St, San Francisco, CA",
                  "lat": 37.7706, "lng": -122.4031},
    "salesforce": {"name": "Salesforce Tower", "address": "415 Mission St, San Francisco, CA 94105",
                   "lat": 37.7898, "lng": -122.3972},
    "airbnb": {"name": "Airbnb HQ", "address": "888 Brannan St, San Francisco, CA 94103",
               "lat": 37.7715, "lng": -122.4055},
    "uber": {"name": "Uber HQ", "address": "1725 3rd St, San Francisco, CA 94158",
             "lat": 37.7700, "lng": -122.3878},
    "lyft": {"name": "Lyft HQ", "address": "185 Berry St, San Francisco, CA 94107",
             "lat": 37.7766, "lng": -122.3948},
    "openai": {"name": "OpenAI", "address": "Pioneer Building, 3180 18th St, San Francisco, CA",
               "lat": 37.7616, "lng": -122.4148},
    "anthropic": {"name": "Anthropic", "address": "548 Market St, San Francisco, CA 94104",
                  "lat": 37.7894, "lng": -122.4023},
    "pinterest": {"name": "Pinterest HQ", "address": "651 Brannan St, San Francisco, CA",
                  "lat": 37.7765, "lng": -122.4017},
    "square": {"name": "Block (Square) HQ", "address": "1955 Broadway, Oakland, CA 94612",
               "lat": 37.8086, "lng": -122.2696},
    "block": {"name": "Block (Square) HQ", "address": "1955 Broadway, Oakland, CA 94612",
              "lat": 37.8086, "lng": -122.2696},
    "doordash": {"name": "DoorDash HQ", "address": "303 2nd St, San Francisco, CA 94107",
                 "lat": 37.7867, "lng": -122.3961},
    "coinbase": {"name": "Coinbase", "address": "100 Pine St, San Francisco, CA 94111",
                 "lat": 37.7919, "lng": -122.3996},
    "robinhood": {"name": "Robinhood HQ", "address": "85 Willow Rd, Menlo Park, CA",
                  "lat": 37.4634, "lng": -122.1452},
    "roblox": {"name": "Roblox HQ", "address": "970 Park Pl, San Mateo, CA 94403",
               "lat": 37.5500, "lng": -122.3134},
    # Aliases
    "stanford med": {"name": "Stanford Hospital", "address": "300 Pasteur Dr, Stanford, CA 94305",
                     "lat": 37.4332, "lng": -122.1755},
    "stanford hospital and clinics": {"name": "Stanford Hospital", "address": "300 Pasteur Dr, Stanford, CA 94305",
                                       "lat": 37.4332, "lng": -122.1755},
}


@dataclass
class UserProfile:
    budget_max: int | None = None
    beds_min: int | None = None
    beds_max: int | None = None
    pets: list[str] = field(default_factory=list)        # ["dogs"], ["cats"]
    must_haves: list[str] = field(default_factory=list)  # ["pool", "in-unit laundry"]
    nice_to_haves: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)
    commute: CommuteTarget | None = None
    neighborhoods: list[str] = field(default_factory=list)
    user_name: str = ""           # for email signatures (Outreach Agent)
    move_in_date: str = ""        # ISO date or free text (Outreach Agent)
    notes: str = ""
    # User-customized component weights from the onboarding questionnaire.
    # When non-empty, RankingService uses these instead of DEFAULT_WEIGHTS.
    # Keys are RankingService component names (budget / commute / pets /
    # must_haves / walk_score / transit_score / etc.).
    weights: dict[str, float] = field(default_factory=dict)
    # Typed hard constraints — these drive pre_filter() binary exclusion.
    # Free-form must_haves/nice_to_haves above remain for qualitative prefs.
    # LLM populates these via the patch schema (see ProfileUpdater prompt).
    # Known keys:
    #   - budget_max_strict: bool — if True, budget_max is a hard cap (no slack)
    #   - parking_max_cost: int — parking fee USD/mo cap
    #   - parking_required: bool
    #   - furnished_required: bool
    #   - lease_max_months: int — max acceptable lease length (e.g. 6 for short-term)
    #   - in_unit_laundry_required: bool
    #   - balcony_required: bool
    #   - avoid_cities: list[str] — geographic hard-exclude (lowercased city names)
    #   - commute_strict: bool — if True, commute > 1.5x max → exclude
    constraints: dict = field(default_factory=dict)

    def is_rich_enough(self) -> bool:
        """Have we collected enough signal to do a useful search?

        Need at least 2 of: budget, beds, commute target, neighborhoods,
        OR a rich free-text note.
        """
        signals = sum(
            [
                bool(self.budget_max),
                bool(self.beds_min is not None or self.beds_max is not None),
                bool(self.commute),
                bool(self.neighborhoods),
                bool(self.must_haves),
                bool(self.notes and len(self.notes) > 30),
            ]
        )
        return signals >= 2

    def to_summary(self) -> str:
        """Short human-readable summary for chat / UI."""
        parts = []
        if self.user_name:
            parts.append(f"name: {self.user_name}")
        if self.budget_max:
            parts.append(f"budget ≤ ${self.budget_max:,}")
        if self.beds_min is not None or self.beds_max is not None:
            mn = self.beds_min if self.beds_min is not None else "?"
            mx = self.beds_max if self.beds_max is not None else "?"
            parts.append(
                "studio" if mn == 0 and mx == 0 else f"{mn}-{mx} bed"
            )
        if self.pets:
            parts.append(f"pets: {', '.join(self.pets)}")
        if self.commute:
            parts.append(f"near {self.commute.name}")
        if self.neighborhoods:
            parts.append(f"neighborhoods: {', '.join(self.neighborhoods[:2])}")
        if self.must_haves:
            parts.append(f"must: {', '.join(self.must_haves[:3])}")
        if self.move_in_date:
            parts.append(f"move-in: {self.move_in_date}")
        return " · ".join(parts) if parts else "(no preferences yet)"


@dataclass
class ScoreBreakdown:
    overall: float                    # 0-100
    components: dict[str, float]      # name -> 0-10 each
    explanation: str                  # human-readable why


class SemanticRanker:
    """P3 — bge-small-en-v1.5 cosine similarity as a RankingService component.

    Lifecycle:
      1. Call precompute(listings) once at startup to build the embedding index.
         Only listings with a non-empty description are indexed. Takes ~2-5 s
         for 3 k listings on first run (model download ~130 MB); subsequent
         runs use the fastembed ONNX cache.
      2. RankingService.score() calls similarity(zpid, profile) per listing.
         Returns None when the listing has no embedding or the profile has no
         textual signal — component is simply skipped.

    Embed dimension: 384 (bge-small-en-v1.5).
    Memory: ~5 MB for 3 k listings × 384 dims × float32.
    """

    MODEL_NAME = "BAAI/bge-small-en-v1.5"
    _DESCRIPTION_MAX = 600   # truncate description before embedding

    def __init__(self) -> None:
        self._model: Any = None
        self._embeddings: dict[str, np.ndarray] = {}   # zpid → unit-normed vec

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _model_instance(self):
        if self._model is None:
            if not _FASTEMBED_OK:
                raise RuntimeError("fastembed is not installed")
            self._model = _FastTextEmbedding(self.MODEL_NAME)
        return self._model

    @staticmethod
    def _unit(vec: np.ndarray) -> np.ndarray:
        n = float(np.linalg.norm(vec))
        return vec / n if n > 0 else vec

    def _build_query_text(self, profile: UserProfile) -> str | None:
        """Construct a natural-language query sentence from the profile."""
        parts: list[str] = []
        if profile.budget_max:
            parts.append(f"max rent ${profile.budget_max:,} per month")
        if profile.beds_min is not None:
            label = "studio" if profile.beds_min == 0 else f"{profile.beds_min} bedroom"
            parts.append(f"{label} apartment")
        if profile.pets:
            parts.append(f"pet friendly, allows {' and '.join(profile.pets)}")
        if profile.must_haves:
            parts.append(f"must have: {', '.join(profile.must_haves[:5])}")
        if profile.nice_to_haves:
            parts.append(f"nice to have: {', '.join(profile.nice_to_haves[:3])}")
        if profile.commute:
            parts.append(f"close to {profile.commute.name}")
        if profile.neighborhoods:
            parts.append(f"neighborhood: {', '.join(profile.neighborhoods[:2])}")
        if profile.notes and len(profile.notes) > 10:
            parts.append(profile.notes[:200])
        if not parts:
            return None
        return ". ".join(parts)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def precompute(self, listings: list[Listing]) -> None:
        """Pre-compute and store unit-normed embeddings for all indexed listings.

        Safe to call multiple times (idempotent — re-indexes any new listings).
        Skips listings without a description or zpid.
        Logs a summary on completion.
        """
        if not _FASTEMBED_OK:
            LOG.warning("P3 semantic ranking disabled: fastembed not installed")
            return

        texts: list[str] = []
        zpids: list[str] = []
        for L in listings:
            if L.zpid and L.description and L.zpid not in self._embeddings:
                texts.append(L.description[: self._DESCRIPTION_MAX])
                zpids.append(L.zpid)

        if not texts:
            LOG.info("SemanticRanker: no new listings to index (total=%d)", len(self._embeddings))
            return

        LOG.info("SemanticRanker: indexing %d listings …", len(texts))
        model = self._model_instance()
        vecs = list(model.embed(texts))
        for zpid, vec in zip(zpids, vecs):
            self._embeddings[zpid] = self._unit(np.array(vec, dtype="float32"))
        LOG.info(
            "SemanticRanker: indexed %d listings (total=%d)",
            len(texts), len(self._embeddings),
        )

    def similarity(self, zpid: str, profile: UserProfile) -> float | None:
        """Return cosine similarity in [0, 1], or None when not available.

        Cosine sim with unit-normed vectors = dot product.
        Range shrinks to [0, 1] for non-negative embeddings (bge produces this).
        """
        if not zpid or zpid not in self._embeddings:
            return None
        query_text = self._build_query_text(profile)
        if query_text is None:
            return None
        try:
            model = self._model_instance()
            q_vec = self._unit(np.array(next(model.embed([query_text])), dtype="float32"))
            return float(np.dot(self._embeddings[zpid], q_vec))
        except Exception as exc:  # noqa: BLE001
            LOG.debug("SemanticRanker.similarity error: %s", exc)
            return None


class RankingService:
    """Heuristic scoring — fast, deterministic, explainable.

    Each component is 0-10. Overall = weighted sum, normalized to 0-100.
    Components only count if the profile asked for that thing.
    """

    DEFAULT_WEIGHTS = {
        "budget": 3.0,
        "beds": 2.0,
        "pets": 2.0,
        "must_haves": 3.0,
        "nice_to_haves": 1.0,
        # avoid weight raised from 1.5 → 3.0 so geographic exclusions like
        # "not SF" can actually outweigh budget+walk_score
        "avoid": 3.0,
        # commute weight raised from 2.5 → 4.0 so a listing failing the 30-min
        # cap can't be saved by walk_score. Hard filter at 2× max_minutes
        # applied separately below.
        "commute": 4.0,
        "walk_score": 1.0,
        "transit_score": 0.5,
        "neighborhood": 1.5,
        # P3 semantic blend — bge-small-en-v1.5 cosine similarity.
        # Weight 2.0 makes it influential but not dominant; heuristic signals
        # (budget, beds, pets) still anchor the ranking.
        "semantic": 2.0,
        # Sound score (HowLoud Soundscore via apartments.com): 0=very loud,
        # 100=very quiet. We invert and rescale: quiet listings score high.
        # Default weight is small; users who pick "quiet" in onboarding get
        # this bumped via profile.weights.
        "sound_score": 0.8,
        # Resident reviews sentiment (cached, opportunistic). Only active when
        # profile contains review-related keywords (quiet/safe/peaceful/...).
        # Weight matches must_haves so this can be decisive when relevant.
        "reviews": 3.0,
        # Has-photo tie-breaker: avoids serving Mapbox a row of "No photo"
        # gray cards when otherwise-equal listings exist with photos. Small
        # weight so it doesn't sway real decisions, just tie-breaks.
        "has_photo": 0.7,
        # Community sentiment from Reddit (r/SanJose, r/BayAreaRealEstate, etc.)
        # — aggregated neighborhood-level signal. Weight ~1.5 — meaningful
        # but not dominant; people's online opinions are noisy.
        "community_sentiment": 1.5,
    }

    def __init__(self, semantic: SemanticRanker | None = None) -> None:
        self._semantic = semantic

    def pre_filter(
        self, listings: list[Listing], profile: UserProfile,
        commute_slack: float = 1.0,
    ) -> tuple[list[Listing], dict[str, int]]:
        """Binary-exclude listings that violate hard constraints.

        `commute_slack` lets callers progressively relax the commute cap
        (1.0 = strict; 1.5 = lenient). Use _pre_filter_with_fallback for
        the standard "try strict, then relax" behavior.

        Returns (filtered_listings, exclusion_counts).
        """
        c = profile.constraints or {}
        commute_strict = bool(c.get("commute_strict")) or bool(
            profile.commute and profile.commute.max_minutes and profile.commute.lat
        )
        max_commute_mi = None
        if commute_strict and profile.commute and profile.commute.max_minutes:
            # max_minutes / 2 = miles at 30 mph; multiply by slack.
            max_commute_mi = (profile.commute.max_minutes / 2.0) * commute_slack
        avoid_cities = list(c.get("avoid_cities") or [])
        # Also fold profile.avoid items that look like cities into hard filter.
        for raw in profile.avoid or []:
            terms = _expand_avoid_terms(raw)
            for t in terms:
                if t in _CITY_CENTROIDS and t not in avoid_cities:
                    avoid_cities.append(t)
        budget_strict = bool(c.get("budget_max_strict")) or bool(profile.budget_max)
        budget_cap = profile.budget_max if budget_strict else None

        counts = {
            "input": len(listings),
            "budget": 0, "beds": 0, "commute": 0,
            "avoid_city": 0, "lease": 0, "furnished": 0,
        }
        kept: list[Listing] = []
        for L in listings:
            if budget_cap and L.rent_min and L.rent_min > budget_cap:
                counts["budget"] += 1
                continue
            if profile.beds_min is not None and L.rent_by_bed:
                if not any(b >= profile.beds_min for b in L.rent_by_bed.keys()):
                    counts["beds"] += 1
                    continue
            if max_commute_mi is not None and L.lat and L.lng:
                d = haversine(L.lat, L.lng, profile.commute.lat, profile.commute.lng)
                if d > max_commute_mi:
                    counts["commute"] += 1
                    continue
            if avoid_cities:
                blob = (
                    (L.neighborhood or "") + " " + (L.address or "") + " " + (L.name or "")
                ).lower()
                excluded = False
                for city in avoid_cities:
                    if city == "san francisco" and "south san francisco" in blob:
                        continue
                    if city in blob:
                        excluded = True
                        break
                if excluded:
                    counts["avoid_city"] += 1
                    continue
            lease_cap = c.get("lease_max_months")
            if lease_cap and not _listing_lease_compatible(L, lease_cap):
                counts["lease"] += 1
                continue
            if c.get("furnished_required") and not _listing_is_furnished(L):
                counts["furnished"] += 1
                continue
            kept.append(L)
        counts["kept"] = len(kept)
        return kept, counts

    def pre_filter_with_fallback(
        self, listings: list[Listing], profile: UserProfile, target_min: int = 5,
    ) -> tuple[list[Listing], dict[str, int]]:
        """Strict-first, progressively-relax. Never silently re-admits
        violators of hard exclusions (avoid_city, lease, furnished);
        only the commute distance is relaxed.

        Order of relaxation:
          1. strict (commute_slack=1.0)  — user's stated radius
          2. lenient (1.5)               — 50% over (e.g. 30 min → 45 min)
          3. very lenient (2.0)          — 100% over
          4. drop commute entirely
        At each step we accept the result if it has >= target_min listings.
        """
        for slack in (1.0, 1.5, 2.0):
            kept, counts = self.pre_filter(listings, profile, commute_slack=slack)
            counts["commute_slack"] = slack
            if len(kept) >= target_min:
                return kept, counts
        # Final fallback: drop commute strict filter (still respect avoid_city,
        # budget, beds, lease, furnished — those are non-negotiable).
        c = profile.constraints or {}
        relaxed_profile = UserProfile(
            budget_max=profile.budget_max,
            beds_min=profile.beds_min,
            beds_max=profile.beds_max,
            pets=profile.pets,
            must_haves=profile.must_haves,
            nice_to_haves=profile.nice_to_haves,
            avoid=profile.avoid,
            commute=None,  # drop
            neighborhoods=profile.neighborhoods,
            constraints={k: v for k, v in c.items() if k != "commute_strict"},
            weights=profile.weights,
        )
        kept, counts = self.pre_filter(listings, relaxed_profile, commute_slack=1.0)
        counts["commute_slack"] = "dropped"
        return kept, counts

    def score(self, listing: Listing, profile: UserProfile) -> ScoreBreakdown:
        comps: dict[str, float] = {}
        # Prefer the user's custom weights from the onboarding questionnaire,
        # falling back to defaults for any component the user didn't rank.
        weights = {**self.DEFAULT_WEIGHTS, **(profile.weights or {})}

        active_weight = 0.0
        weighted_total = 0.0

        # Budget
        if profile.budget_max and listing.rent_min is not None:
            # Skip this component entirely when rent is unknown — Apartments.com
            # listings have 0% rent coverage and scoring them 0 would unfairly
            # bury them behind listings with known (but expensive) rents.
            if listing.rent_min <= profile.budget_max * 0.85:
                s = 10.0  # well under budget
            elif listing.rent_min <= profile.budget_max:
                # scale 5–10 between 85% and 100% of budget
                ratio = (profile.budget_max - listing.rent_min) / (profile.budget_max * 0.15)
                s = 5 + 5 * ratio
            else:
                s = max(0.0, 5 - (listing.rent_min - profile.budget_max) / (profile.budget_max * 0.10))
            comps["budget"] = round(s, 1)
            active_weight += weights["budget"]
            weighted_total += s * weights["budget"]

        # Beds
        if profile.beds_min is not None or profile.beds_max is not None:
            beds_avail = set(listing.rent_by_bed.keys())
            mn = profile.beds_min if profile.beds_min is not None else 0
            mx = profile.beds_max if profile.beds_max is not None else 10
            wanted = set(range(mn, mx + 1))
            overlap = beds_avail & wanted
            if overlap:
                s = 10.0
            elif beds_avail:
                # closest available
                closest = min(abs(b - mn) for b in beds_avail)
                s = max(0.0, 10 - closest * 3)
            else:
                s = 0.0
            comps["beds"] = round(s, 1)
            active_weight += weights["beds"]
            weighted_total += s * weights["beds"]

        # Pets
        if profile.pets:
            allowed = " ".join(p.lower() for p in (listing.pets_allowed or []))
            hits = sum(1 for p in profile.pets if p.lower() in allowed)
            s = (hits / len(profile.pets)) * 10
            comps["pets"] = round(s, 1)
            active_weight += weights["pets"]
            weighted_total += s * weights["pets"]

        # Must-haves — averaged per-preference score via verifier registry,
        # which checks structured fields (parking_types, has_pool,
        # has_patio_balcony, raw.parking_fee) before falling back to text
        # substring match. Each preference gets a 0-10 score, then averaged.
        if profile.must_haves:
            scores = [_score_preference(k, listing) for k in profile.must_haves]
            s = sum(scores) / len(scores)
            comps["must_haves"] = round(s, 1)
            active_weight += weights["must_haves"]
            weighted_total += s * weights["must_haves"]

        # Nice-to-haves — same verifier approach, but typically a wider set
        # of softer preferences.
        if profile.nice_to_haves:
            scores = [_score_preference(k, listing) for k in profile.nice_to_haves]
            s = sum(scores) / len(scores)
            comps["nice_to_haves"] = round(s, 1)
            active_weight += weights["nice_to_haves"]
            weighted_total += s * weights["nice_to_haves"]

        # Avoid (penalty). Each avoid keyword is expanded via _expand_avoid_terms
        # so that short forms like "SF" or "downtown SJ" also catch the full
        # city name in listing.city/address (e.g. "San Francisco, CA").
        if profile.avoid:
            blob = self._listing_blob(listing)
            hits = 0
            for raw in profile.avoid:
                if any(term in blob for term in _expand_avoid_terms(raw)):
                    hits += 1
            s = max(0.0, 10 - hits * 5)
            comps["avoid"] = round(s, 1)
            active_weight += weights["avoid"]
            weighted_total += s * weights["avoid"]

        # Commute (Haversine straight-line, v0 approximation)
        # Tracks a `hard_filter_violation` flag — when the user set an explicit
        # max_minutes cap and the listing is grossly out of range (>2× cap),
        # we floor the final score so it can't displace in-range candidates.
        hard_filter_violated = False
        if profile.commute and profile.commute.lat and profile.commute.lng:
            if listing.lat and listing.lng:
                miles = haversine(listing.lat, listing.lng, profile.commute.lat, profile.commute.lng)
                # Linear interpolation: full score (10) at 0 mi, min score (1) at max_mi.
                # If user set max_minutes (e.g. "30 min commute"), estimate max miles via
                # ~30 mph average (conservative; mostly surface streets in SJ/SF).
                # Otherwise default ceiling is 15 miles.
                max_mi = (profile.commute.max_minutes / 2.0) if profile.commute.max_minutes else 15.0
                max_mi = max(max_mi, 1.0)  # guard against 0
                s = max(1.0, 10.0 - (miles / max_mi) * 9.0)
                # Hard filter: only when user gave an explicit max_minutes and
                # the listing exceeds 2× that. Below this floor the listing
                # still appears in shortlist (no removal) but can't outrank
                # in-range candidates.
                if profile.commute.max_minutes and miles > max_mi * 2.0:
                    hard_filter_violated = True
            else:
                # Listing has no coordinates (e.g. many Craigslist "Undisclosed
                # Address" posts). When the user explicitly set a commute target,
                # we cannot verify proximity — score 0 instead of skipping, so
                # these listings don't get a free pass at overall=100.
                s = 0.0
                if profile.commute.max_minutes:
                    hard_filter_violated = True
            comps["commute"] = round(s, 1)
            active_weight += weights["commute"]
            weighted_total += s * weights["commute"]

        # Walk / transit — only activated when the user has expressed a preference
        # for walkability or transit (or has a commute target, for which transit
        # relevance is implied).  Avoids penalising car-centric users who never
        # mentioned walkability.
        _pref_blob = " ".join(
            profile.must_haves + profile.nice_to_haves
        ).lower()
        _walk_pref = bool(profile.commute) or any(
            kw in _pref_blob
            for kw in ("walk", "walkab", "pedestrian")
        )
        _transit_pref = bool(profile.commute) or any(
            kw in _pref_blob
            for kw in ("transit", "bart", "vta", "caltrain", "bus", "subway", "metro", "train")
        )

        if listing.walk_score is not None and _walk_pref:
            s = min(10.0, listing.walk_score / 10)
            comps["walk_score"] = round(s, 1)
            active_weight += weights["walk_score"]
            weighted_total += s * weights["walk_score"]
        if listing.transit_score is not None and _transit_pref:
            s = min(10.0, listing.transit_score / 10)
            comps["transit_score"] = round(s, 1)
            active_weight += weights["transit_score"]
            weighted_total += s * weights["transit_score"]

        # Neighborhood preference
        if profile.neighborhoods and listing.neighborhood:
            ln = listing.neighborhood.lower()
            hit = any(n.lower() in ln or ln in n.lower() for n in profile.neighborhoods)
            s = 10.0 if hit else 3.0
            comps["neighborhood"] = round(s, 1)
            active_weight += weights["neighborhood"]
            weighted_total += s * weights["neighborhood"]

        # Sound score (HowLoud Soundscore via apartments.com).
        # Range 0-100 where 100 = very quiet. Scale linearly to 0-10.
        # Component is active whenever the listing has the data — the
        # weight (small by default) controls how much it matters; users
        # who care about quiet bump it via the onboarding ranking.
        if listing.sound_score is not None:
            s = max(0.0, min(10.0, listing.sound_score / 10))
            comps["sound_score"] = round(s, 1)
            active_weight += weights["sound_score"]
            weighted_total += s * weights["sound_score"]

        # P3 — Semantic similarity (bge-small-en-v1.5 cosine sim).
        # Only active when SemanticRanker is wired up AND the listing was indexed
        # (has a description) AND the profile has enough textual signal to embed.
        if self._semantic is not None:
            sim = self._semantic.similarity(listing.zpid or "", profile)
            if sim is not None:
                # Cosine sim is in [0, 1] for bge positive embeddings.
                # Scale to [0, 10].
                s = max(0.0, min(10.0, sim * 10))
                comps["semantic"] = round(s, 1)
                w = weights.get("semantic", 2.0)
                active_weight += w
                weighted_total += s * w

        # Resident reviews sentiment — only active when the profile explicitly
        # cares about review-derived qualities (quiet, safe, peaceful, ...).
        # Reads from in-process review cache; opportunistic (no cache → skip).
        if _profile_wants_review_signal(profile) and listing.zpid:
            rs = _review_sentiment_score(
                listing.zpid,
                listing_name=listing.name,
                listing_lat=listing.lat,
                listing_lng=listing.lng,
            )
            if rs is not None:
                comps["reviews"] = round(rs, 1)
                w = weights.get("reviews", 3.0)
                active_weight += w
                weighted_total += rs * w

        # Community sentiment from Reddit (neighborhood-level). Skips when
        # no Reddit signal exists for this listing's neighborhood/city.
        cs = _community_sentiment_score(listing)
        if cs is not None:
            comps["community_sentiment"] = round(cs, 1)
            w = weights.get("community_sentiment", 1.5)
            active_weight += w
            weighted_total += cs * w

        # Has-photo tie-breaker — small weight, always active. Listings with
        # a real photo_url score 10, others 5. Prevents Mapbox showing rows
        # of gray "No photo" cards.
        has_photo = bool(isinstance(listing.raw, dict) and listing.raw.get("primary_photo_url"))
        photo_score = 10.0 if has_photo else 5.0
        comps["has_photo"] = round(photo_score, 1)
        w = weights.get("has_photo", 0.7)
        active_weight += w
        weighted_total += photo_score * w

        # Stability anchor: floor active_weight at half the DEFAULT_WEIGHTS
        # sum so listings that match few user dimensions can't out-score
        # listings matching many dimensions just because their denominator
        # is smaller. (Without this, a listing with only budget=10 scored 100
        # while a listing with budget=10, commute=3, walk=8 scored ~70 even
        # though the latter is objectively better-matched.)
        _DENOM_FLOOR = sum(self.DEFAULT_WEIGHTS.values()) * 0.4
        denom = max(active_weight, _DENOM_FLOOR)
        # When floor kicks in, treat missing components as neutral (5.0)
        # contribution toward the floor delta.
        if denom > active_weight:
            weighted_total += 5.0 * (denom - active_weight)
        overall = (weighted_total / denom) * 10 if denom else 50.0
        # Hard filter penalty: cap listings that violate an explicit hard
        # constraint at 35/100. In-range candidates score 60-90 in practice,
        # so this guarantees they always rank above violators without removing
        # them from the result set (user can still see them).
        violations: list[str] = []
        if hard_filter_violated:
            violations.append("commute")
        # Short-term lease violation
        if _profile_wants_short_term(profile):
            if not _listing_lease_compatible(listing, max_lease_months=6):
                violations.append("long_lease")
        # Furnished violation: only filter if listing explicitly known to be
        # unfurnished; unknowns get a pass (conservative).
        if _profile_wants_furnished(profile):
            if isinstance(listing.raw, dict) and listing.raw.get("is_furnished") is False:
                violations.append("unfurnished")
        if violations:
            overall = min(overall, 35.0)
            comps["hard_filter"] = -1.0  # numeric so _explain sort works
        explanation = self._explain(comps)
        return ScoreBreakdown(
            overall=round(overall, 1),
            components=comps,
            explanation=explanation,
        )

    def _listing_blob(self, listing: Listing) -> str:
        parts: list[str] = []
        # `address` and `neighborhood` matter especially for `avoid` checks —
        # user says "avoid SF" but the SF reference often only lives in the
        # city/address, not the description.
        for field in ("description", "neighborhood", "address"):
            v = getattr(listing, field, None)
            if v:
                parts.append(str(v))
        for v in (listing.parking_types, listing.utilities_included):
            if v:
                parts.extend(str(x) for x in v)
        for attr in (
            "has_pool", "has_elevator", "has_storage", "has_patio_balcony"
        ):
            if getattr(listing, attr, False):
                parts.append(attr.replace("has_", ""))
        return " ".join(parts).lower()

    def _explain(self, comps: dict[str, float]) -> str:
        if not comps:
            return "no profile preferences set yet"
        ranked = sorted(comps.items(), key=lambda x: -x[1])
        return ", ".join(f"{k}:{v}" for k, v in ranked[:4])


# --------------------------- Profile Updater --------------------------------

UPDATE_PROMPT = """You extract / update a renter's preferences from chat messages.

Given the user's NEW message and their CURRENT profile, return a JSON
object with the fields that should change. Omit fields not mentioned.

ADD operations (extend a list or set a scalar):
  - budget_max: int (max monthly rent USD)
  - beds_min: int (0 = studio)
  - beds_max: int
  - pets_add: array of strings (e.g., ["dogs"])
  - must_haves_add: array of strings (concrete features they NEED)
  - nice_to_haves_add: array (features they'd like but not require)
  - avoid_add: array (things they explicitly don't want)
  - neighborhoods_add: array (e.g., ["Downtown", "Willow Glen"])
  - commute: {{"name":"<employer/place>","max_minutes":int_or_null}} OR
             {{"name":"...","address":"<full address>","max_minutes":int_or_null}}
    Multi-site / rotating jobs: if the user mentions multiple work locations
    (e.g. "I drive between Hayward and Redwood City") or periodic visits
    (e.g. "I fly out of SFO twice a month"):
      • If sites are clustered, pick the GEOGRAPHIC CENTER and put that as
        the commute name (e.g. Hayward + Redwood City + Concord → "Castro
        Valley" since it's roughly equidistant from all three).
      • If it's an airport with periodic visits, set
        name: "SFO" (or "SJC", "OAK") and address: "<airport name>".
      • Note the full multi-site list in notes_append so we don't lose info.
    Never return commute=null just because the user mentioned multiple
    locations — pick the best single anchor.
  - user_name: string
  - move_in_date: string
  - notes_append: short string to append to free-text notes
  - constraints: dict — **CRITICAL** typed hard constraints. Pre-filter
    relies on these to BINARY-EXCLUDE violators (not just down-rank them).
    You MUST populate every applicable key whenever the user expresses a
    hard requirement — don't just put it in must_haves_add and skip this.
    Known keys:
      "parking_max_cost": int     — parking fee USD/mo cap (e.g. 200)
      "parking_required": bool    — listing must offer parking
      "furnished_required": bool  — listing must be furnished
      "lease_max_months": int     — max lease (6 for short-term/travel nurse;
                                    7 for "no annual"; 12 if unspecified)
      "in_unit_laundry_required": bool
      "balcony_required": bool
      "avoid_cities": array[str]  — LOWERCASE city names to HARD-EXCLUDE.
                                    Recognized: san francisco, oakland,
                                    berkeley, san jose, downtown san jose,
                                    palo alto, mountain view, sunnyvale,
                                    cupertino, santa clara, milpitas,
                                    fremont, hayward, walnut creek,
                                    san mateo, burlingame, redwood city,
                                    millbrae, san bruno, daly city,
                                    south san francisco, tenderloin,
                                    soma, mission, financial district
      "budget_max_strict": bool   — true when user said "max", "absolute
                                    max", "cannot exceed", "under $X is
                                    a hard cap"
      "commute_strict": bool      — true when user said "30 min radius",
                                    "must be within", "≤30 min by car"
    HEURISTIC TRIGGERS (set the constraint key whenever you see):
      • "$200/mo parking", "parking under $200"  → parking_max_cost: 200
      • "free parking", "parking included"       → parking_required: true,
                                                    parking_max_cost: 0
      • "13-week contract", "3-month stay",
        "travel nurse", "no 12-month lease",
        "month-to-month"                          → lease_max_months: 6
      • "furnished", "no buying furniture",
        "corporate housing", "Blueground/Sonder" → furnished_required: true
      • "in-unit laundry", "washer/dryer",
        "w/d in unit"                            → in_unit_laundry_required: true
      • "balcony", "patio", "outdoor space"      → balcony_required: true
      • "not [city]", "no [city]", "don't want
        to live in [city]", "avoid [city]"        → avoid_cities: ["city"]
      • "max budget", "absolute max", "cannot
        exceed", "no more than $X"                → budget_max_strict: true
      • "30 min commute", "≤45 min radius",
        "must be within X minutes"                → commute_strict: true
    Multi-constraint example:
      "13-week travel nurse contract at UCSF Mission Bay, need furnished,
       budget $3500 max, no 12-month locks" →
      constraints: {{"lease_max_months":6, "furnished_required":true,
                    "budget_max_strict":true}}
    Multi-city avoid example:
      "want Peninsula but not SF or Oakland" →
      constraints: {{"avoid_cities":["san francisco","oakland"]}}

REMOVE operations (when the user negates / contradicts / changes their mind):
  - pets_remove: array of strings to remove from pets
  - must_haves_remove: array of strings to remove from must_haves
  - nice_to_haves_remove: array of strings to remove from nice_to_haves
  - avoid_remove: array of strings to remove from avoid
  - neighborhoods_remove: array of strings to remove from neighborhoods
  - clear_commute: true (drop the commute target)
  - clear_budget: true (drop budget_max)
  - clear_beds: true (drop beds_min/beds_max)

The values you put in *_remove arrays MUST match strings already in
CURRENT_PROFILE EXACTLY (case-insensitive substring match is OK).

CRITICAL — when the user negates something, propagate the removal:
  • "I don't want X" / "no X" / "不要 X" / "remove X" / "actually skip X"
    → REMOVE every related entry from must_haves, nice_to_haves, AND
      pets/neighborhoods if applicable, then ADD the negation to
      avoid_add.
  • "Trader Joe's isn't important" → remove from nice_to_haves but
    don't add to avoid.
  • Match loosely: if the profile has "near Trader Joe's" and the user
    says "no Trader Joe's", emit nice_to_haves_remove: ["near Trader Joe's"].

Look for indirect signals:
  - "near my work at Apple" -> commute: {{"name":"Apple"}}
  - "I have a dog" -> pets_add: ["dogs"]
  - "thin walls drove me crazy" -> avoid_add: ["thin walls"]
  - "I work from home" -> nice_to_haves_add: ["co-working space", "good wifi"]
  - "I fly out of SFO twice a month" -> commute: {{"name":"SFO","max_minutes":20}}
  - "sites in Hayward and Redwood City, next quarter Concord" ->
      commute: {{"name":"Castro Valley","max_minutes":30}}, notes_append:
      "rotating job sites: Hayward, Redwood City, Concord"

If the message is just a question (no preference reveal), return an empty
JSON object.

Respond with ONLY the JSON object — no prose, no fences.

CURRENT_PROFILE:
{profile}

USER_MESSAGE:
{message}
"""


class ProfileUpdater:
    def __init__(self, client: Anthropic | None = None, model: str = "claude-sonnet-4-6"):
        self._client = client
        self.model = model

    @property
    def client(self) -> Anthropic:
        if self._client is None:
            self._client = Anthropic()
        return self._client

    def update(self, message: str, profile: UserProfile) -> UserProfile:
        prompt = UPDATE_PROMPT.format(
            profile=json.dumps(asdict(profile), default=str, indent=2),
            message=message,
        )
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text.strip()
            if text.startswith("```"):
                text = text.strip("`")
                if text.lower().startswith("json"):
                    text = text[4:].strip()
            patch = json.loads(text)
        except Exception:
            return profile

        return self._apply_patch(profile, patch)

    def _apply_patch(self, p: UserProfile, patch: dict) -> UserProfile:
        # --- scalar add / clear ---
        if "budget_max" in patch and isinstance(patch["budget_max"], int):
            p.budget_max = patch["budget_max"]
        if "beds_min" in patch and isinstance(patch["beds_min"], int):
            p.beds_min = patch["beds_min"]
        if "beds_max" in patch and isinstance(patch["beds_max"], int):
            p.beds_max = patch["beds_max"]
        if patch.get("clear_budget") is True:
            p.budget_max = None
        if patch.get("clear_beds") is True:
            p.beds_min = None
            p.beds_max = None
        if patch.get("clear_commute") is True:
            p.commute = None

        # --- list adds ---
        for key, attr in [
            ("pets_add", "pets"),
            ("must_haves_add", "must_haves"),
            ("nice_to_haves_add", "nice_to_haves"),
            ("avoid_add", "avoid"),
            ("neighborhoods_add", "neighborhoods"),
        ]:
            vals = patch.get(key)
            if isinstance(vals, list):
                target = getattr(p, attr)
                for v in vals:
                    if isinstance(v, str) and v.strip() and v.lower() not in [
                        x.lower() for x in target
                    ]:
                        target.append(v.strip())

        # --- list removes (loose substring match, case-insensitive) ---
        for key, attr in [
            ("pets_remove", "pets"),
            ("must_haves_remove", "must_haves"),
            ("nice_to_haves_remove", "nice_to_haves"),
            ("avoid_remove", "avoid"),
            ("neighborhoods_remove", "neighborhoods"),
        ]:
            vals = patch.get(key)
            if isinstance(vals, list) and vals:
                cur = getattr(p, attr)
                kept: list[str] = []
                drop_terms = [v.lower() for v in vals if isinstance(v, str)]
                for item in cur:
                    item_low = item.lower()
                    # drop if any drop_term substring-matches the item
                    if any(t and (t == item_low or t in item_low or item_low in t) for t in drop_terms):
                        continue
                    kept.append(item)
                setattr(p, attr, kept)

        if isinstance(patch.get("commute"), dict):
            ct = patch["commute"]
            name = (ct.get("name") or "").strip()
            addr_hint = (ct.get("address") or "").strip()
            if name:
                hq = EMPLOYER_HQ.get(name.lower())
                if hq:
                    p.commute = CommuteTarget(
                        name=hq["name"],
                        address=hq.get("address", ""),
                        lat=hq.get("lat"),
                        lng=hq.get("lng"),
                        max_minutes=ct.get("max_minutes"),
                    )
                else:
                    # Fallback: try Mapbox geocoding. Prefer address+name if the
                    # LLM gave one, else just name. Bay-Area-biased in geocode_place.
                    query = f"{name}, {addr_hint}" if addr_hint else name
                    geo = geocode_place(query)
                    p.commute = CommuteTarget(
                        name=name,
                        address=(geo[2] if geo else addr_hint) or "",
                        lat=(geo[0] if geo else None),
                        lng=(geo[1] if geo else None),
                        max_minutes=ct.get("max_minutes"),
                    )
        if isinstance(patch.get("user_name"), str) and patch["user_name"].strip():
            p.user_name = patch["user_name"].strip()
        if isinstance(patch.get("move_in_date"), str) and patch["move_in_date"].strip():
            p.move_in_date = patch["move_in_date"].strip()
        if isinstance(patch.get("notes_append"), str) and patch["notes_append"].strip():
            sep = " " if p.notes else ""
            p.notes = (p.notes + sep + patch["notes_append"].strip())[:1000]
        # Typed constraints — merge into p.constraints, with light validation.
        if isinstance(patch.get("constraints"), dict):
            allowed = {
                "parking_max_cost": int,
                "parking_required": bool,
                "furnished_required": bool,
                "lease_max_months": int,
                "in_unit_laundry_required": bool,
                "balcony_required": bool,
                "avoid_cities": list,
                "budget_max_strict": bool,
                "commute_strict": bool,
            }
            for k, v in patch["constraints"].items():
                if k not in allowed:
                    continue
                expected = allowed[k]
                try:
                    if expected is bool:
                        p.constraints[k] = bool(v)
                    elif expected is int:
                        p.constraints[k] = int(v)
                    elif expected is list:
                        if isinstance(v, list):
                            p.constraints[k] = [str(x).lower() for x in v]
                except (TypeError, ValueError):
                    pass
        return p
