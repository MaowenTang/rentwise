"""Build apartments.com dataset from per-city JSONL crawl outputs.

Reads zillow_scraper/output/apartments_com_<city>.jsonl files,
normalizes each card → Listing-shaped dict, writes a single
api/data/apartments_com_listings.jsonl.gz.

Field map (apartments.com card → Listing v1 schema)
---------------------------------------------------
  listing_id           → zpid                  (prefixed "apt:" for source clarity)
  name                 → building_name / name
  full_address         → full_address          (parsed for street + city + state + zip)
  rent_by_bed          → rent_by_bed_type      (list of {num_beds, rent_min, rent_max})
  phone                → phone (raw)
  amenities            → has_pool / pets_allowed / parking_types   (lossy)
  photo_url            → primary_photo_url (raw)
  url                  → url
  city_slug            → city (slug → CamelCase)

Tier-1 limitations (documented):
  - No lat/lng (cards don't expose them; needs detail fetch or geocoding)
  - No walk/transit/bike scores (Tier 2)
  - No deposits / lease terms / description (Tier 2)
  - No sound_score yet (Tier 2)
"""
from __future__ import annotations

import gzip
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

LOG = logging.getLogger("apt-build")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

ROOT = Path(__file__).resolve().parent.parent
INPUT_DIR = Path("/Users/tangmaowen/Downloads/zillow_scraper/output")
DETAILS_PATH = INPUT_DIR / "apartments_com_details.jsonl"
OUTPUT = ROOT / "api" / "data" / "apartments_com_listings.jsonl.gz"


def slug_to_city(slug: str) -> str:
    """`san-jose-ca` → `San Jose`."""
    parts = slug.split("-")
    if parts and parts[-1] in ("ca", "tx", "ny"):  # drop state suffix
        parts = parts[:-1]
    return " ".join(p.capitalize() for p in parts)


_ADDR_RE = re.compile(
    r"^(?P<street>.+?),\s*(?P<city>[^,]+),\s*(?P<state>[A-Z]{2})\s*(?P<zip>\d{5})?\s*$"
)


def parse_full_address(s: str | None) -> dict[str, str]:
    """Best-effort split of '1140 Harrison St, San Francisco, CA 94103'."""
    if not s:
        return {}
    m = _ADDR_RE.match(s.strip())
    if not m:
        return {"street_address": s.strip()}
    return {
        "street_address": m.group("street").strip(),
        "city": m.group("city").strip(),
        "state": m.group("state").strip(),
        "zipcode": (m.group("zip") or "").strip() or None,
    }


# Amenity → structured field heuristics
_POOL_TOKENS = {"pool", "swimming pool"}
_PET_DOG_TOKENS = {"dog", "dogs", "dogs allowed", "pets allowed"}
_PET_CAT_TOKENS = {"cat", "cats", "cats allowed", "pets allowed"}
_PARKING_TOKENS = {
    "parking", "covered parking", "garage", "attached garage",
    "detached garage", "underground parking", "ev charging",
}
_LAUNDRY_IN_UNIT = {"in unit washer & dryer", "washer/dryer in unit", "in-unit laundry"}
_ELEVATOR_TOKENS = {"elevator"}


def categorize_amenities(amen: list[str]) -> dict[str, Any]:
    lc = {a.lower().strip() for a in amen}
    pets = []
    if any(t in lc for t in _PET_DOG_TOKENS):
        pets.extend(["LargeDogs", "SmallDogs"])
    if any(t in lc for t in _PET_CAT_TOKENS):
        pets.append("Cats")
    parking = []
    for tok in _PARKING_TOKENS:
        if tok in lc:
            if "garage" in tok:
                parking.append("Garage")
            elif "ev" in tok:
                parking.append("EV charging")
            else:
                parking.append("Parking")
    parking = sorted(set(parking))
    return {
        "has_pool": any(t in lc for t in _POOL_TOKENS),
        "pets_allowed": list(set(pets)),
        "parking_types": parking,
        "has_elevator": True if any(t in lc for t in _ELEVATOR_TOKENS) else None,
        "laundry_in_unit": any(t in lc for t in _LAUNDRY_IN_UNIT) or None,
    }


def load_details_index() -> dict[str, dict]:
    """Read apartments_com_details.jsonl → {listing_id: detail_record}.

    These are the Tier-2 enriched fields scraped from each detail page:
    lat/lng, sound_score, walk/transit/bike, description, neighborhood,
    office_hours, structured amenities, full unit list, reviews.
    """
    if not DETAILS_PATH.exists():
        return {}
    out: dict[str, dict] = {}
    for line in DETAILS_PATH.open(encoding="utf-8"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        lid = d.get("listing_id")
        if lid:
            out[lid] = d
    return out


def normalize_card(card: dict, city_slug: str, details: dict | None = None) -> dict | None:
    if not card.get("listing_id"):
        return None
    # Allow listings without card-level address if detail has it
    full_addr = card.get("full_address") or (details or {}).get("street_address")
    if not full_addr:
        return None

    addr_parts = parse_full_address(card.get("full_address"))
    rby = []
    for beds_str, lo_hi in (card.get("rent_by_bed") or {}).items():
        if not isinstance(lo_hi, list) or len(lo_hi) != 2:
            continue
        try:
            beds = int(beds_str)
        except (ValueError, TypeError):
            continue
        rby.append({
            "num_beds": beds,
            "rent_min": lo_hi[0],
            "rent_max": lo_hi[1],
        })

    amen_cats = categorize_amenities(card.get("amenities") or [])

    rec = {
        "kind": "apartment_building",
        "source": "apartments_com",
        "zpid": f"apt:{card['listing_id']}",
        "lot_id": card["listing_id"],
        "building_name": card.get("name"),
        "full_address": card.get("full_address"),
        "street_address": addr_parts.get("street_address"),
        "city": addr_parts.get("city") or slug_to_city(city_slug),
        "state": addr_parts.get("state") or "CA",
        "zipcode": addr_parts.get("zipcode"),
        "country": "US",
        "latitude": None,
        "longitude": None,
        "rent_by_bed_type": rby,
        "phone": card.get("phone"),
        "amenities_raw": card.get("amenities") or [],
        "primary_photo_url": card.get("photo_url"),
        "photo_count": card.get("photo_count"),
        "featured": card.get("featured", False),
        "url": card.get("url"),
        "city_slug": city_slug,
        **amen_cats,
        "walk_score": None,
        "transit_score": None,
        "bike_score": None,
        "description": None,
    }

    # ------ Tier 2: overlay detail-page fields if available --------------
    if details:
        # Geo
        if details.get("lat") is not None:
            rec["latitude"] = details["lat"]
        if details.get("lng") is not None:
            rec["longitude"] = details["lng"]
        # Address — prefer detail-page values when card was missing them
        if not rec["street_address"] and details.get("street_address"):
            rec["street_address"] = details["street_address"]
        if not rec["zipcode"] and details.get("zipcode"):
            rec["zipcode"] = details["zipcode"]
        if not rec["full_address"] and details.get("street_address"):
            rec["full_address"] = ", ".join(
                p for p in (
                    details.get("street_address"),
                    f"{details.get('city', '')}, {details.get('state', '')} "
                    f"{details.get('zipcode', '')}".strip().rstrip(","),
                ) if p
            )
        # Walk / Transit / Bike / Sound
        for k in ("walk_score", "transit_score", "bike_score", "sound_score"):
            if details.get(k) is not None:
                rec[k] = details[k]
        if details.get("sound_label"):
            rec["sound_label"] = details["sound_label"]
        # Neighborhood (apartments.com's breadcrumb-derived label)
        if details.get("neighborhood"):
            rec["neighborhood"] = details["neighborhood"]
        # Description
        if details.get("description") and not rec["description"]:
            rec["description"] = details["description"]
        # Office hours
        if details.get("office_hours"):
            rec["office_hours"] = details["office_hours"]
        # Structured amenities (richer than card amenity tokens)
        if details.get("amenity_features"):
            rec["amenity_features"] = details["amenity_features"]
            # Re-derive has_pool / has_elevator / parking from structured list
            structured_names = {
                (a.get("name") or "").lower()
                for a in details["amenity_features"] if isinstance(a, dict)
            }
            if "pool" in structured_names or "swimming pool" in structured_names:
                rec["has_pool"] = True
            if "elevator" in structured_names:
                rec["has_elevator"] = True
        # Pets flag
        if details.get("pets_allowed_flag") is True and not rec["pets_allowed"]:
            rec["pets_allowed"] = ["Cats", "LargeDogs", "SmallDogs"]
        # Telephone fallback
        if details.get("telephone") and not rec.get("phone"):
            rec["phone"] = details["telephone"]
        # Rent fallback (some cards had no per-bed prices but offers.lowPrice exists)
        if not rec["rent_by_bed_type"] and (details.get("rent_low") or details.get("rent_high")):
            rec["rent_by_bed_type"] = [{
                "num_beds": None,  # unspecified — use offers range
                "rent_min": details.get("rent_low"),
                "rent_max": details.get("rent_high"),
            }]
        # Rating + units (raw, for future use)
        if details.get("rating") is not None:
            rec["rating"] = details["rating"]
            rec["rating_count"] = details.get("rating_count")
        if details.get("units"):
            rec["units"] = details["units"]
        rec["_tier2_enriched"] = True
    return rec


def main():
    files = sorted(INPUT_DIR.glob("apartments_com_*.jsonl"))
    files = [f for f in files if "details" not in f.name]
    if not files:
        print("No apartments_com_*.jsonl files found in", INPUT_DIR, file=sys.stderr)
        sys.exit(1)
    LOG.info("Reading %d city files", len(files))

    details = load_details_index()
    LOG.info("Tier-2 details available for %d listings", len(details))

    OUTPUT.parent.mkdir(exist_ok=True)
    n_in = n_out = n_t2 = 0
    seen_ids: set[str] = set()
    with gzip.open(OUTPUT, "wt", encoding="utf-8") as fout:
        for fp in files:
            city_slug = fp.stem.replace("apartments_com_", "").replace("_", "-")
            for line in fp.open(encoding="utf-8"):
                n_in += 1
                try:
                    card = json.loads(line)
                except json.JSONDecodeError:
                    continue
                lid = card.get("listing_id")
                rec = normalize_card(card, city_slug, details=details.get(lid))
                if not rec:
                    continue
                if rec["zpid"] in seen_ids:
                    continue
                seen_ids.add(rec["zpid"])
                if rec.get("_tier2_enriched"):
                    n_t2 += 1
                fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n_out += 1
    LOG.info("Wrote %d listings (%d Tier-2-enriched) from %d cards → %s",
             n_out, n_t2, n_in, OUTPUT)
    LOG.info("Output size: %d KB", OUTPUT.stat().st_size // 1024)


if __name__ == "__main__":
    main()
