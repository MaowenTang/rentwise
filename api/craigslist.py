"""Craigslist CSV loader + Zillow dedup-and-merge.

Pipeline:
  1. parse_craigslist(path)  -> list[dict]   # raw CSV rows
  2. normalize_craigslist(row) -> dict       # apartment_building-shaped
  3. merge_into_zillow(zillow_listings, cl_normalized) -> list[Listing]
       - matches by Haversine ≤ 50m AND bed-count overlap
       - merges fields (Zillow priority), records source
"""
from __future__ import annotations

import csv
import logging
import re
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
from typing import Any

from listings import Listing

LOG = logging.getLogger("rentwise.api")

# --- bbox lookup for cheap city tagging ---------------------------------
# Each entry: (city, lat_min, lat_max, lng_min, lng_max). Order matters —
# more specific (smaller) boxes first.
CITY_BBOXES: list[tuple[str, float, float, float, float]] = [
    # South Bay (specific cities first, San Jose catchall last)
    ("Cupertino",        37.30, 37.35, -122.10, -122.00),
    ("Sunnyvale",        37.33, 37.42, -122.08, -121.97),
    ("Mountain View",    37.36, 37.45, -122.12, -122.00),
    ("Palo Alto",        37.40, 37.48, -122.18, -122.05),
    ("Santa Clara",      37.32, 37.40, -121.99, -121.92),
    ("Milpitas",         37.40, 37.49, -121.93, -121.84),
    ("Campbell",         37.27, 37.30, -121.98, -121.92),
    ("Saratoga",         37.25, 37.30, -122.06, -121.95),
    ("Los Gatos",        37.20, 37.27, -122.01, -121.93),
    ("San Jose",         37.20, 37.46, -122.05, -121.75),

    # East Bay
    ("Fremont",          37.48, 37.61, -122.07, -121.85),
    ("Union City",       37.56, 37.62, -122.06, -121.95),
    ("Hayward",          37.60, 37.71, -122.13, -121.99),
    ("Pleasanton",       37.62, 37.72, -121.93, -121.83),
    ("Dublin",           37.69, 37.75, -121.97, -121.86),
    ("Livermore",        37.65, 37.75, -121.84, -121.66),
    ("Oakland",          37.70, 37.85, -122.30, -122.10),
    ("Berkeley",         37.84, 37.91, -122.31, -122.22),
    ("Albany",           37.88, 37.91, -122.32, -122.28),
    ("El Cerrito",       37.91, 37.95, -122.34, -122.28),
    ("Richmond",         37.91, 37.97, -122.40, -122.30),
    ("San Pablo",        37.97, 38.02, -122.40, -122.30),
    ("Hercules",         38.00, 38.05, -122.32, -122.27),
    ("Pinole",           37.99, 38.02, -122.32, -122.26),
    ("Concord",          37.93, 38.02, -122.05, -121.95),
    ("Walnut Creek",     37.85, 37.93, -122.10, -121.95),
    ("Lafayette",        37.86, 37.92, -122.15, -122.08),
    ("Pittsburg",        37.99, 38.06, -121.92, -121.83),
    ("Antioch",          37.96, 38.05, -121.85, -121.72),

    # Peninsula / SF
    ("San Mateo",        37.49, 37.59, -122.40, -122.27),
    ("Redwood City",     37.44, 37.55, -122.30, -122.18),
    ("South San Francisco", 37.62, 37.68, -122.48, -122.38),
    ("Daly City",        37.65, 37.72, -122.51, -122.42),
    ("San Bruno",        37.61, 37.65, -122.45, -122.40),
    ("San Francisco",    37.70, 37.83, -122.52, -122.35),

    # Marin / North Bay
    ("San Rafael",       37.93, 38.02, -122.57, -122.46),
    ("Novato",           38.07, 38.15, -122.62, -122.50),
    ("Vallejo",          38.07, 38.18, -122.30, -122.18),
    ("Benicia",          38.04, 38.10, -122.20, -122.10),
    ("Napa",             38.25, 38.35, -122.35, -122.22),
    ("Sonoma",           38.25, 38.35, -122.55, -122.40),
    ("Petaluma",         38.20, 38.30, -122.70, -122.55),
    ("Santa Rosa",       38.40, 38.50, -122.78, -122.65),
    ("Sebastopol",       38.38, 38.45, -122.85, -122.78),

    # Outside Bay Area but appearing in CSV
    ("Santa Cruz",       36.95, 37.05, -122.10, -121.95),
    ("Capitola",         36.95, 37.00, -121.97, -121.92),
    ("Sacramento",       38.50, 38.70, -121.55, -121.30),
]


def city_from_latlng(lat: float, lng: float) -> str:
    for city, lat_lo, lat_hi, lng_lo, lng_hi in CITY_BBOXES:
        if lat_lo <= lat <= lat_hi and lng_lo <= lng <= lng_hi:
            return city
    return "Bay Area"


# --- price + attribute parsers ------------------------------------------

_DOLLAR_RE = re.compile(r"\$?([\d,]+)")


def parse_price(s: str | None) -> int | None:
    if not s:
        return None
    m = _DOLLAR_RE.search(s)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_attributes(attrs: str | None) -> dict[str, Any]:
    """Pull pet/laundry/parking/lease signals out of the attributes pipe-list.

    Sample input:
      "2BR / 1Ba | 930ft2 | available now | application fee details: |
       $40 Application Fee | rent period: | monthly | apartment |
       dogs are OK - wooof | laundry on site | detached garage"
    """
    out: dict[str, Any] = {
        "pets_allowed": [],
        "lease_period": None,
        "application_fee": None,
        "is_furnished": None,
        "is_smoke_free": None,
    }
    if not attrs:
        return out
    parts = [p.strip().lower() for p in attrs.split("|")]

    pets: list[str] = []
    for p in parts:
        if "dogs are ok" in p or "dogs ok" in p:
            pets.append("Dogs")
        if "cats are ok" in p or "cats ok" in p:
            pets.append("Cats")
    out["pets_allowed"] = pets

    for p in parts:
        if p == "monthly" or "month-to-month" in p:
            out["lease_period"] = "Month-to-month"
        elif "year" in p and "lease" in p:
            out["lease_period"] = p
        if "furnished" in p and "un" not in p:
            out["is_furnished"] = True
        if "no smoking" in p or "smoke-free" in p:
            out["is_smoke_free"] = True
        if "application fee" in p:
            out["application_fee"] = parse_price(p)
    return out


_PHONE_RE = re.compile(r"\(?\b(\d{3})\)?[\s\-]?(\d{3})[\s\-]?(\d{4})\b")


def parse_phone(text: str) -> str | None:
    if not text:
        return None
    m = _PHONE_RE.search(text)
    if not m:
        return None
    return f"({m.group(1)}) {m.group(2)}-{m.group(3)}"


def parse_deposit_from_description(text: str) -> tuple[int | None, int | None]:
    """Find 'Security Deposit: $1,400' or similar in the description."""
    if not text:
        return None, None
    m = re.search(
        r"(?:security\s+deposit|deposit)\s*[:\-]?\s*\$?([\d,]+)",
        text, re.IGNORECASE,
    )
    if m:
        try:
            v = int(m.group(1).replace(",", ""))
            return v, v
        except ValueError:
            pass
    return None, None


# --- normalize one row to the apartment_building schema ------------------

def normalize_craigslist(row: dict[str, str]) -> dict[str, Any]:
    """CSV row -> dict consumable by `Listing.from_jsonl_record`."""
    post_id = (row.get("post_id") or "").strip()
    if not post_id:
        return {}
    title = (row.get("title") or "").strip()
    description = row.get("description") or ""
    address_text = row.get("address") or ""

    try:
        lat = float(row.get("latitude")) if row.get("latitude") else None
        lng = float(row.get("longitude")) if row.get("longitude") else None
    except (TypeError, ValueError):
        lat = lng = None

    city = city_from_latlng(lat, lng) if (lat is not None and lng is not None) else "Bay Area"

    price = parse_price(row.get("price"))
    try:
        beds = int(row["bedrooms"]) if row.get("bedrooms") else None
    except (TypeError, ValueError):
        beds = None
    try:
        sqft = int(row["sqft"]) if row.get("sqft") else None
    except (TypeError, ValueError):
        sqft = None

    rent_by_bed = []
    if beds is not None and price is not None:
        rent_by_bed.append(
            {
                "num_beds": beds,
                "label": "Studio" if beds == 0 else f"{beds}BR",
                "rent_min": price,
                "rent_max": price,
            }
        )

    floor_plans = []
    if beds is not None and price is not None:
        floor_plans.append(
            {
                "fp_zpid": f"craigslist_{post_id}",
                "beds": beds,
                "baths": int(row["bathrooms"]) if row.get("bathrooms") else None,
                "sqft_min": sqft,
                "sqft_max": sqft,
                "price_min": price,
                "price_max": price,
                "n_units": 1,
                "units": [],
            }
        )

    attrs = parse_attributes(row.get("attributes"))
    phone = parse_phone(description)
    deposit_min, deposit_max = parse_deposit_from_description(description)

    # Building name: prefer the cleaned title, fall back to address
    building_name = title or (address_text.split(" near ")[0] if address_text else "Craigslist listing")
    building_name = building_name[:80]

    full_address = ", ".join(p for p in (address_text, f"{city}, CA") if p)

    parking_types = []
    if row.get("parking"):
        parking_types = [row["parking"]]

    laundry = []
    if row.get("laundry"):
        laundry = [row["laundry"]]

    return {
        "kind": "craigslist",
        "zpid": f"cl_{post_id}",
        "lot_id": None,
        "building_name": building_name,
        "full_address": full_address,
        "street_address": address_text,
        "city": city,
        "state": "CA",
        "zipcode": "",
        "country": "USA",
        "county": "",
        "latitude": lat,
        "longitude": lng,
        "neighborhood": city,  # bbox city as a coarse neighborhood proxy
        "neighborhood_description": (description or "")[:600],
        "neighborhood_highlights": [],

        "phone": phone,
        "agent_name": "Craigslist Listing Owner",
        "rental_applications_accepted_type": None,
        "rental_product_type": "CRAIGSLIST",

        "description": description[:2000] if description else "",
        "unit_count": 1,
        "available_unit_count": 1,
        "summary_building_details": [],
        "summary_laundry": laundry,
        "list_price_includes_required_monthly_fees": False,

        "deposit_min": deposit_min,
        "deposit_max": deposit_max,
        "application_fee": attrs.get("application_fee"),
        "administrative_fee": None,
        "utilities_included": [],
        "lease_lengths": attrs.get("lease_period"),
        "lease_terms": [attrs["lease_period"]] if attrs.get("lease_period") else [],
        "rentals_disclaimer": None,

        "rent_by_bed_type": rent_by_bed,
        "floor_plans": floor_plans,
        "n_floor_plans": len(floor_plans),

        "building_amenities": [],
        "unit_features": [],
        "policies": [],
        "special_features": [],
        "common_unit_amenities": [],
        "appliances": [],
        "community_rooms": [],
        "outdoor_common_areas": [],
        "parking_types": parking_types,
        "parking_description": None,
        "parking_rent_description": None,
        "security_types": [],
        "view_types": [],
        "sports_courts": [],
        "floor_coverings": [],
        "air_conditioning": "Unknown",
        "heating_source": "Unknown",
        "is_furnished": attrs.get("is_furnished"),
        "is_smoke_free": attrs.get("is_smoke_free"),
        "is_low_income": False,
        "is_senior_housing": False,
        "is_student_housing": False,

        "has_pool": None,
        "has_barbecue": None,
        "has_elevator": None,
        "has_fireplace": None,
        "has_hot_tub": None,
        "has_storage": None,
        "has_disabled_access": None,
        "has_ceiling_fan": None,
        "has_pet_park": None,
        "has_sauna": None,
        "has_dry_cleaning_drop_off": None,
        "has_24h_maintenance": None,
        "has_online_rent_payment": None,
        "has_online_maintenance_portal": None,
        "has_onsite_management": None,
        "has_package_service": None,
        "has_patio_balcony": None,
        "has_valet_trash": None,
        "has_guest_suite": None,
        "has_bicycle_storage": None,
        "has_assisted_living": False,
        "has_shared_laundry": "laundry" in (row.get("laundry") or "").lower(),
        "has_spanish_speaking_staff": None,

        "pets_allowed": attrs.get("pets_allowed") or [],
        "pet_groups": [],

        "walk_score": None,
        "walk_score_label": None,
        "transit_score": None,
        "transit_score_label": None,
        "bike_score": None,
        "bike_score_label": None,

        "schools": [],
        "faqs": [],
        "n_faqs": 0,
        "office_hours_raw": None,
        "photo_count": int(row.get("image_count") or 0),

        "url": row.get("url") or "",

        "_data_sources": ["craigslist"],
        "_craigslist_post_id": post_id,
        "_craigslist_posted_date": row.get("posted_date"),
        "_craigslist_updated_date": row.get("updated_date"),
    }


# --- dedup-and-merge against existing Zillow listings -------------------

EARTH_M = 6371000  # meters


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = radians(lat1), radians(lat2)
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(p1) * cos(p2) * sin(dlng / 2) ** 2
    return 2 * EARTH_M * asin(sqrt(a))


def _build_spatial_index(listings: list[Listing]) -> dict[tuple[int, int], list[Listing]]:
    """Bin listings by 0.001° lat/lng cells (~110m × 88m)."""
    idx: dict[tuple[int, int], list[Listing]] = {}
    for L in listings:
        if L.lat is None or L.lng is None:
            continue
        key = (round(L.lat * 1000), round(L.lng * 1000))
        idx.setdefault(key, []).append(L)
    return idx


def _find_match(
    cl_lat: float, cl_lng: float, cl_beds: int | None,
    idx: dict[tuple[int, int], list[Listing]],
    threshold_m: float = 50.0,
) -> Listing | None:
    """Find the closest existing listing within `threshold_m` whose
    bed-set overlaps the Craigslist bed count (or whose bed-set is empty,
    e.g. an apartment building with no listed plans)."""
    cx, cy = round(cl_lat * 1000), round(cl_lng * 1000)
    best: tuple[float, Listing] | None = None
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for L in idx.get((cx + dx, cy + dy), []):
                if L.lat is None or L.lng is None:
                    continue
                d = _haversine_m(cl_lat, cl_lng, L.lat, L.lng)
                if d > threshold_m:
                    continue
                # Bed-overlap check (skip if both have explicit beds and disagree)
                if cl_beds is not None and L.rent_by_bed:
                    if cl_beds not in L.rent_by_bed:
                        # Mismatch on beds — could still be the same building
                        # with multiple plans; allow if existing is an apartment
                        # building (multiple plans) but NOT for single-home (1 plan).
                        is_single = L.raw.get("kind") in ("single_home", "single_home_raw")
                        if is_single:
                            continue
                if best is None or d < best[0]:
                    best = (d, L)
    return best[1] if best else None


def _merge(existing: Listing, cl: dict[str, Any]) -> None:
    """Mutate `existing` to absorb the Craigslist data without overwriting
    populated Zillow fields. Tracks sources in raw._data_sources.
    """
    raw = existing.raw

    # Track source
    sources = raw.get("_data_sources") or [raw.get("kind", "zillow")]
    if "craigslist" not in sources:
        sources.append("craigslist")
    raw["_data_sources"] = sources

    # Append Craigslist post id list (a building may have multiple Craigslist posts)
    cl_ids = raw.get("_craigslist_post_ids") or []
    cl_ids.append(cl.get("_craigslist_post_id"))
    raw["_craigslist_post_ids"] = cl_ids

    # Add a new floor plan if its bed count isn't already covered
    cl_fps = cl.get("floor_plans") or []
    if cl_fps:
        cl_beds = cl_fps[0].get("beds")
        if cl_beds is not None and cl_beds not in existing.rent_by_bed:
            cl_fp = cl_fps[0]
            existing.rent_by_bed[cl_beds] = (cl_fp["price_min"], cl_fp["price_max"])
            # Also patch raw.floor_plans for downstream consumers
            existing_fps = raw.get("floor_plans") or []
            existing_fps.append(cl_fp)
            raw["floor_plans"] = existing_fps

    # Take description if existing has none
    if not existing.description and cl.get("description"):
        existing.description = cl["description"][:500]
        raw["description"] = cl["description"]

    # Phone fallback
    if not raw.get("phone") and cl.get("phone"):
        raw["phone"] = cl["phone"]

    # Lease terms fallback
    if not raw.get("lease_terms") and cl.get("lease_terms"):
        raw["lease_terms"] = cl["lease_terms"]
    if not raw.get("lease_lengths") and cl.get("lease_lengths"):
        raw["lease_lengths"] = cl["lease_lengths"]

    # Pet policy union
    cur = {p.lower() for p in (existing.pets_allowed or [])}
    for p in (cl.get("pets_allowed") or []):
        if p.lower() not in cur:
            existing.pets_allowed.append(p)
            cur.add(p.lower())

    # Deposit fallback
    if existing.deposit_min is None and cl.get("deposit_min") is not None:
        existing.deposit_min = cl["deposit_min"]
    if existing.deposit_max is None and cl.get("deposit_max") is not None:
        existing.deposit_max = cl["deposit_max"]


def merge_into_zillow(
    listings: list[Listing], cl_records: list[dict],
    *, threshold_m: float = 50.0,
) -> tuple[list[Listing], int, int]:
    """Returns (combined_listings, n_merged, n_added)."""
    idx = _build_spatial_index(listings)
    n_merged = n_added = 0
    for cl in cl_records:
        if not cl or not cl.get("zpid"):
            continue
        lat, lng = cl.get("latitude"), cl.get("longitude")
        if lat is None or lng is None:
            continue
        cl_beds = None
        if cl.get("rent_by_bed_type"):
            cl_beds = cl["rent_by_bed_type"][0].get("num_beds")

        match = _find_match(lat, lng, cl_beds, idx, threshold_m)
        if match is not None:
            _merge(match, cl)
            n_merged += 1
        else:
            new_listing = Listing.from_jsonl_record(cl)
            listings.append(new_listing)
            # Also index it so subsequent CL rows in the same spot can dedupe
            key = (round(lat * 1000), round(lng * 1000))
            idx.setdefault(key, []).append(new_listing)
            n_added += 1
    return listings, n_merged, n_added


def load_craigslist(path: str | Path) -> list[dict]:
    """Read CSV, return list of normalized dicts (skips bad rows)."""
    p = Path(path)
    if not p.exists():
        LOG.warning("craigslist: file not found at %s", p)
        return []
    out: list[dict] = []
    with p.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            n = normalize_craigslist(row)
            if n:
                out.append(n)
    return out
