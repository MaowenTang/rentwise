"""apartments.com loader + Zillow merger (address-string match).

apartments.com Tier-1 cards don't expose lat/lng, so we can't reuse
craigslist.py's haversine merger directly. Instead we match by
normalized full_address string. For listings that don't match any
Zillow address we insert a new Listing.

On match (same address):
  - Zillow values stay primary for stable ids and rich detail (walk
    score, deposit, description) — the user is identified by zpid in
    the shortlist and we don't want apartments.com to renumber them.
  - apartments.com price is treated as authoritative when it differs
    (per user preference: "apartment上的噪声score不错" — they trust
    apartments.com data quality). Both values are kept in raw under
    `price_apartments_com` and `price_zillow` for transparent display.
  - apartments.com phone fills in if Zillow's is missing.

Tier-2 (later): visit apartments.com detail page when prices mismatch
to reconcile, plus ingest Sound Score / Quietness rating.
"""
from __future__ import annotations

import gzip
import json
import logging
import re
from pathlib import Path

from listings import Listing

LOG = logging.getLogger("rentwise.api")

# --------------------------- address normalization -------------------------

_STREET_TYPES = {
    "st": "st", "street": "st",
    "ave": "ave", "avenue": "ave",
    "blvd": "blvd", "boulevard": "blvd",
    "rd": "rd", "road": "rd",
    "dr": "dr", "drive": "dr",
    "ct": "ct", "court": "ct",
    "ln": "ln", "lane": "ln",
    "way": "way",
    "pl": "pl", "place": "pl",
    "ter": "ter", "terrace": "ter",
    "cir": "cir", "circle": "cir",
    "pkwy": "pkwy", "parkway": "pkwy",
    "hwy": "hwy", "highway": "hwy",
}

_PUNCT_RE = re.compile(r"[.,#]+")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_address(s: str | None) -> str | None:
    """Canonicalize an address for string-equality matching.

    "1140 Harrison St, San Francisco, CA 94103"
       → "1140 harrison st san francisco ca 94103"
    """
    if not s:
        return None
    s = s.lower()
    s = _PUNCT_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    # Collapse street suffixes
    parts = s.split(" ")
    out = [_STREET_TYPES.get(p, p) for p in parts]
    return " ".join(out)


def normalize_street(s: str | None) -> str | None:
    """Just the street component — for fuzzy match when zip differs."""
    if not s:
        return None
    s = s.lower()
    s = _PUNCT_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    parts = s.split(" ")
    out = [_STREET_TYPES.get(p, p) for p in parts]
    return " ".join(out)


# --------------------------- merge ---------------------------------------

def _build_address_index(listings: list[Listing]) -> dict[str, Listing]:
    """zillow_address_normalized → Listing."""
    idx: dict[str, Listing] = {}
    for L in listings:
        n = normalize_address(L.address)
        if n:
            idx[n] = L
        # also index street-only (helps when zips don't match)
        raw = L.raw or {}
        street_only = normalize_street(raw.get("street_address"))
        city = (raw.get("city") or "").lower().strip()
        if street_only and city and f"{street_only}|{city}" not in idx:
            idx[f"{street_only}|{city}"] = L
    return idx


def _apt_price_min(rec: dict) -> int | None:
    rby = rec.get("rent_by_bed_type") or []
    mins = [b["rent_min"] for b in rby if b.get("rent_min")]
    return min(mins) if mins else None


def _apt_price_max(rec: dict) -> int | None:
    rby = rec.get("rent_by_bed_type") or []
    maxs = [b["rent_max"] for b in rby if b.get("rent_max")]
    return max(maxs) if maxs else None


def _merge_into(zillow: Listing, apt: dict) -> None:
    """Apply apartments.com fields to a matched Zillow Listing."""
    apt_min = _apt_price_min(apt)
    apt_max = _apt_price_max(apt)

    # Track both prices in raw for transparent display
    zillow.raw.setdefault("_data_sources", []).append("apartments_com")
    if apt_min is not None or apt_max is not None:
        zillow.raw["price_apartments_com"] = {
            "rent_min": apt_min, "rent_max": apt_max,
            "rent_by_bed": apt.get("rent_by_bed_type"),
        }
    if zillow.rent_min is not None or zillow.rent_max is not None:
        zillow.raw["price_zillow"] = {
            "rent_min": zillow.rent_min, "rent_max": zillow.rent_max,
        }

    # apartments.com price is authoritative on mismatch (user preference)
    if apt_min is not None:
        zillow.rent_min = apt_min
    if apt_max is not None:
        zillow.rent_max = apt_max
    # Refresh rent_by_bed with apartments.com rows when present
    if apt.get("rent_by_bed_type"):
        for row in apt["rent_by_bed_type"]:
            beds = row.get("num_beds")
            if beds is None:
                continue
            zillow.rent_by_bed[beds] = (row.get("rent_min"), row.get("rent_max"))

    # Fill missing fields from apartments.com
    if not zillow.url and apt.get("url"):
        zillow.url = apt["url"]
    if apt.get("phone") and not zillow.raw.get("phone"):
        zillow.raw["phone"] = apt["phone"]
    if apt.get("primary_photo_url") and not zillow.raw.get("primary_photo_url"):
        zillow.raw["primary_photo_url"] = apt["primary_photo_url"]
    # Cross-link
    zillow.raw["apartments_com_url"] = apt.get("url")
    zillow.raw["apartments_com_id"] = apt.get("lot_id")


def merge_into_zillow(
    listings: list[Listing], apt_records: list[dict],
) -> tuple[list[Listing], int, int]:
    """Returns (combined_listings, n_merged, n_added).

    Match policy:
      1. exact normalized full_address
      2. street + city match (handles zip differences)
      3. else → new Listing
    """
    idx = _build_address_index(listings)

    n_merged = n_added = 0
    for apt in apt_records:
        full = normalize_address(apt.get("full_address"))
        match = idx.get(full) if full else None
        if match is None:
            street = normalize_street(apt.get("street_address"))
            city = (apt.get("city") or "").lower().strip()
            if street and city:
                match = idx.get(f"{street}|{city}")

        if match is not None:
            _merge_into(match, apt)
            n_merged += 1
        else:
            new_listing = _apt_to_listing(apt)
            if new_listing is None:
                continue
            listings.append(new_listing)
            n_full = normalize_address(apt.get("full_address"))
            if n_full:
                idx[n_full] = new_listing
            n_added += 1

    return listings, n_merged, n_added


def _apt_to_listing(apt: dict) -> Listing | None:
    """Build a Listing from a normalized apartments.com record."""
    rent_by_bed: dict[int, tuple[int | None, int | None]] = {}
    all_mins: list[int] = []
    all_maxs: list[int] = []
    for row in apt.get("rent_by_bed_type") or []:
        beds = row.get("num_beds")
        if beds is None:
            continue
        mn, mx = row.get("rent_min"), row.get("rent_max")
        rent_by_bed[beds] = (mn, mx)
        if mn is not None:
            all_mins.append(mn)
        if mx is not None:
            all_maxs.append(mx)

    # Skip listings without ANY price — they're useless for ranking
    if not all_mins and not all_maxs:
        return None

    raw = {
        **apt,
        "_data_sources": ["apartments_com"],
        "phone": apt.get("phone"),
        "primary_photo_url": apt.get("primary_photo_url"),
        "apartments_com_url": apt.get("url"),
        "apartments_com_id": apt.get("lot_id"),
    }

    return Listing(
        zpid=apt.get("zpid", ""),  # already prefixed "apt:"
        name=apt.get("building_name") or apt.get("street_address") or "(unnamed)",
        address=apt.get("full_address") or "",
        neighborhood=None,  # Tier 2
        lat=None,           # Tier 2: geocode or detail-fetch
        lng=None,
        rent_min=min(all_mins) if all_mins else None,
        rent_max=max(all_maxs) if all_maxs else None,
        rent_by_bed=rent_by_bed,
        walk_score=None,
        transit_score=None,
        bike_score=None,
        pets_allowed=apt.get("pets_allowed") or [],
        has_pool=bool(apt.get("has_pool")),
        has_elevator=apt.get("has_elevator"),
        has_storage=None,
        has_patio_balcony=None,
        parking_types=apt.get("parking_types") or [],
        utilities_included=[],
        deposit_min=None,
        deposit_max=None,
        description="",
        url=apt.get("url", ""),
        raw=raw,
    )


def load_apartments_com(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        LOG.warning("apartments.com: file not found at %s", p)
        return []
    out: list[dict] = []
    opener = gzip.open if str(p).endswith(".gz") else open
    with opener(p, "rt", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
                if rec:
                    out.append(rec)
            except json.JSONDecodeError:
                continue
    return out
