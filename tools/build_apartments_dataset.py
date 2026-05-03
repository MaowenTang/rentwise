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


def normalize_card(card: dict, city_slug: str) -> dict | None:
    if not card.get("listing_id") or not card.get("full_address"):
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
        # No lat/lng yet — Tier 2 / geocode pass
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
        # Lossy structured fields from amenity tokens
        **amen_cats,
        # Tier-1 has no walk/transit/bike scores or description
        "walk_score": None,
        "transit_score": None,
        "bike_score": None,
        "description": None,
    }
    return rec


def main():
    files = sorted(INPUT_DIR.glob("apartments_com_*.jsonl"))
    if not files:
        print("No apartments_com_*.jsonl files found in", INPUT_DIR, file=sys.stderr)
        sys.exit(1)
    LOG.info("Reading %d city files", len(files))

    OUTPUT.parent.mkdir(exist_ok=True)
    n_in = n_out = 0
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
                rec = normalize_card(card, city_slug)
                if not rec:
                    continue
                if rec["zpid"] in seen_ids:
                    continue
                seen_ids.add(rec["zpid"])
                fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n_out += 1
    LOG.info("Wrote %d listings (from %d cards) → %s", n_out, n_in, OUTPUT)
    LOG.info("Output size: %d KB", OUTPUT.stat().st_size // 1024)


if __name__ == "__main__":
    main()
