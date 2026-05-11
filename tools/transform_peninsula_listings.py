#!/usr/bin/env python3
"""Transform raw apartments.com Peninsula JSONL files into the enriched
schema that backend `apartments_com.load_apartments_com` expects.

Source: ~/Downloads/zillow_scraper/output/apartments_com_<city>_ca.jsonl
Sink:   ~/Downloads/rentwise/api/data/apartments_com_peninsula.jsonl.gz

Adds: latitude/longitude (Mapbox geocode), city/state/zipcode parsed from
full_address, has_pool/has_elevator/laundry_in_unit parsed from amenities,
zpid = "apt:" + listing_id.
"""
import gzip
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import httpx

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = Path.home() / "Downloads" / "zillow_scraper" / "output"
OUT = ROOT / "api" / "data" / "apartments_com_peninsula.jsonl.gz"

PENINSULA_CITIES = [
    "burlingame", "daly_city", "san_bruno", "san_mateo",
    "redwood_city", "south_san_francisco", "menlo_park",
    # Optional adds if files exist:
    "millbrae", "foster_city", "belmont", "pacifica", "san_carlos",
]

MAPBOX_TOKEN = os.environ.get("MAPBOX_TOKEN") or os.environ.get("NEXT_PUBLIC_MAPBOX_TOKEN")
if not MAPBOX_TOKEN:
    # Pull from the rentwise api .env
    env_path = ROOT / "api" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("MAPBOX_TOKEN="):
                MAPBOX_TOKEN = line.split("=", 1)[1].strip()
                break
if not MAPBOX_TOKEN:
    print("ERROR: MAPBOX_TOKEN not set", file=sys.stderr)
    sys.exit(1)


_geo_cache: dict[str, tuple[float, float] | None] = {}


def geocode(address: str) -> tuple[float, float] | None:
    if address in _geo_cache:
        return _geo_cache[address]
    try:
        r = httpx.get(
            f"https://api.mapbox.com/geocoding/v5/mapbox.places/{quote(address)}.json",
            params={
                "access_token": MAPBOX_TOKEN,
                "country": "us",
                "limit": 1,
                "proximity": "-122.2,37.6",
                "bbox": "-122.8,36.9,-121.5,38.2",
            },
            timeout=5.0,
        )
        r.raise_for_status()
        features = r.json().get("features", [])
        if not features:
            _geo_cache[address] = None
            return None
        lng, lat = features[0]["center"]
        result = (float(lat), float(lng))
        _geo_cache[address] = result
        return result
    except Exception as e:
        print(f"  geocode failed for {address!r}: {e}", file=sys.stderr)
        _geo_cache[address] = None
        return None


def parse_address(full_address: str) -> tuple[str, str, str]:
    """Parse 'Street, City, CA 94010' into (city, state, zipcode)."""
    # Try ", City, ST ZIP" pattern
    m = re.search(r",\s*([^,]+),\s*([A-Z]{2})\s+(\d{5})", full_address)
    if m:
        return m.group(1).strip(), m.group(2), m.group(3)
    return "", "CA", ""


AMENITY_FLAGS = {
    "pool": "has_pool",
    "swimming pool": "has_pool",
    "elevator": "has_elevator",
    "in unit washer & dryer": "laundry_in_unit",
    "in-unit washer/dryer": "laundry_in_unit",
    "washer/dryer in unit": "laundry_in_unit",
    "in unit laundry": "laundry_in_unit",
}


def parse_amenities(amenities: list[str]) -> dict:
    flags = {}
    pets = []
    parking_types = []
    text = " ".join(a.lower() for a in (amenities or []))
    for kw, flag in AMENITY_FLAGS.items():
        if kw in text:
            flags[flag] = True
    if "pets allowed" in text or "pet friendly" in text or "dog" in text:
        pets = ["cats", "dogs"]
    for kw in ("garage", "covered parking", "carport", "surface parking"):
        if kw in text:
            parking_types.append(kw.replace(" parking", "").strip())
    return {**flags, "pets_allowed": pets, "parking_types": parking_types}


def transform_rent_by_bed(raw: dict) -> list[dict]:
    """Convert {"1": [min, max], "2": [min, max]} → [{num_beds:1, rent_min, rent_max}, ...]"""
    out = []
    for beds_str, (mn, mx) in raw.items():
        try:
            beds = int(beds_str)
        except (TypeError, ValueError):
            continue
        out.append({"num_beds": beds, "rent_min": mn, "rent_max": mx})
    return out


def transform(rec: dict) -> dict | None:
    """Transform one raw record to backend schema."""
    listing_id = rec.get("listing_id")
    if not listing_id:
        return None
    full_address = rec.get("full_address") or ""
    city, state, zipcode = parse_address(full_address)
    if not full_address:
        return None
    coords = geocode(full_address)
    if not coords:
        return None
    lat, lng = coords
    rby = transform_rent_by_bed(rec.get("rent_by_bed") or {})
    if not rby:
        return None  # no price = useless
    amenity_flags = parse_amenities(rec.get("amenities") or [])
    return {
        "zpid": f"apt:{listing_id}",
        "lot_id": listing_id,
        "source": "apartments_com",
        "kind": "apartment_building",
        "country": "US",
        "building_name": rec.get("name") or "",
        "street_address": rec.get("street_address") or "",
        "full_address": full_address,
        "city": city,
        "city_slug": rec.get("city_slug") or "",
        "state": state,
        "zipcode": zipcode,
        "latitude": lat,
        "longitude": lng,
        "rent_by_bed_type": rby,
        "amenities_raw": rec.get("amenities") or [],
        "primary_photo_url": rec.get("photo_url"),
        "photo_count": rec.get("photo_count"),
        "phone": rec.get("phone"),
        "url": rec.get("url"),
        "featured": rec.get("featured", False),
        "walk_score": None,
        "transit_score": None,
        "bike_score": None,
        "has_pool": amenity_flags.get("has_pool", False),
        "has_elevator": amenity_flags.get("has_elevator", None),
        "laundry_in_unit": amenity_flags.get("laundry_in_unit", False),
        "pets_allowed": amenity_flags["pets_allowed"],
        "parking_types": amenity_flags["parking_types"],
    }


def main():
    out_records = []
    seen_ids = set()
    n_geocode_calls = 0
    start = time.time()

    for city_slug in PENINSULA_CITIES:
        f = SRC_DIR / f"apartments_com_{city_slug}_ca.jsonl"
        if not f.exists():
            print(f"  skip {city_slug}: file missing")
            continue
        n_in = n_out = n_fail = 0
        with f.open() as fp:
            for line in fp:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                n_in += 1
                if rec.get("listing_id") in seen_ids:
                    continue
                seen_ids.add(rec["listing_id"])
                if not _geo_cache.get(rec.get("full_address", "")):
                    n_geocode_calls += 1
                t = transform(rec)
                if t:
                    out_records.append(t)
                    n_out += 1
                else:
                    n_fail += 1
        print(f"  {city_slug}: in={n_in} out={n_out} failed={n_fail}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8") as fout:
        for r in out_records:
            fout.write(json.dumps(r) + "\n")

    elapsed = time.time() - start
    print(f"\nWrote {len(out_records)} records to {OUT}")
    print(f"Geocoding API calls: {n_geocode_calls}; elapsed: {elapsed:.1f}s")


if __name__ == "__main__":
    main()
