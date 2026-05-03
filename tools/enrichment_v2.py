"""Enrichment v2 — re-extract richer fields from existing raw_details/*.json.

The original enrich.py from the zillow_scraper project saves the full Zillow
__NEXT_DATA__ blob per listing to output/raw_details/<id>.json. The first
extraction pass surfaced ~60 fields; this script pulls another ~15
fields that the Outreach Agent and ranker can use, WITHOUT re-scraping
Zillow (no CAPTCHAs).

Input: zillow_scraper/output/raw_details/*.json
       (apt_<slug>.json for apartment_building, single_<zpid>.json for homes)
Output: rentwise/api/data/zillow_listings_v2.jsonl.gz

Run after the SF enrichment finishes so both SJ + SF raw_details are present.

Usage:
    python tools/enrichment_v2.py
    python tools/enrichment_v2.py --raw-dir /path/to/raw_details --out path.jsonl.gz
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_RAW_DIR = Path("/Users/tangmaowen/Downloads/zillow_scraper/output/raw_details")
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "api" / "data" / "zillow_listings_v2.jsonl.gz"


def get(d: dict | None, *path: str, default=None):
    """Safe nested-key getter."""
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return default if cur is None else cur


# ---------------------------- apartment_building ---------------------------

def extract_apartment(raw: dict) -> dict | None:
    """Build a v2 record from an apt_*.json file."""
    b = get(raw, "props", "pageProps", "componentProps", "initialReduxState", "gdp", "building")
    if not isinstance(b, dict):
        return None

    # Office hours — list of dicts like [{"day": "Mon-Fri", "open": "9:00", "close": "18:00"}, ...]
    hours_raw = get(b, "amenityDetails", "hours", default=[])
    hours_pretty = None
    if isinstance(hours_raw, list) and hours_raw:
        # Best-effort flatten — actual schema varies; keep raw too
        parts = []
        for h in hours_raw:
            if isinstance(h, dict):
                d = h.get("day") or h.get("days") or ""
                o = h.get("open") or h.get("openTime") or ""
                c = h.get("close") or h.get("closeTime") or ""
                if d and (o or c):
                    parts.append(f"{d} {o}-{c}")
        if parts:
            hours_pretty = "; ".join(parts)

    rec = {
        "kind": "apartment_building",
        "zpid": get(b, "zpid"),
        "lot_id": get(b, "lotId"),
        "building_name": get(b, "buildingName"),
        "marketing_name": get(b, "marketingName"),
        "full_address": get(b, "fullAddress"),
        "street_address": get(b, "streetAddress"),
        "city": get(b, "city"),
        "state": get(b, "state"),
        "zipcode": get(b, "zipcode"),
        "county": get(b, "county"),
        "country": get(b, "country"),
        "neighborhood": get(b, "neighborhood"),
        "latitude": get(b, "latitude"),
        "longitude": get(b, "longitude"),
        "description": get(b, "description"),
        "neighborhood_description": get(b, "neighborhoodDescription"),

        # --- v2 NEW: contact + outreach fields ---
        "phone_primary": get(b, "buildingPhoneNumber"),
        "agent_full_name": get(b, "contactInfo", "agentFullName"),
        "provider_listing_id": get(b, "contactInfo", "providerListingID"),
        "rental_apps_accepted": get(b, "contactInfo", "rentalApplicationsAcceptedType"),
        "office_hours_raw": hours_raw or None,
        "office_hours_pretty": hours_pretty,
        "housing_connector_link": get(b, "housingConnector", "hcLink"),
        "is_landlord_liaison_program": get(b, "isLandlordLiaisonProgram", default=False),

        # --- v2 NEW: identifying URLs + IDs ---
        "bdp_url": get(b, "bdpUrl"),
        "best_matched_unit_url": get(b, "bestMatchedUnit", "hdpUrl"),
        "best_matched_unit_number": get(b, "bestMatchedUnit", "unitNumber"),

        # --- v2 NEW: market context ---
        "availability_insight_title": get(b, "availabilityInsights", "title"),
        "availability_insight_description": get(b, "availabilityInsights", "description"),
        "marketing_treatments": get(b, "marketingTreatments") or [],
        "home_types": get(b, "homeTypes") or [],
        "best_guess_timezone": get(b, "bestGuessTimezone"),

        # --- v2 NEW: neighborhood adjacency ---
        "nearby_building_links": [
            {"name": l.get("text"), "url": l.get("path")}
            for l in (get(b, "nearbyBuildingLinks") or [])[:10]
            if isinstance(l, dict)
        ],
        "nearby_neighborhoods": [
            n.get("text") for n in (get(b, "nearbyNeighborhoods") or [])[:8]
            if isinstance(n, dict) and n.get("text")
        ],

        # --- v1 carry-over (existing fields, preserved) ---
        "walk_score": get(b, "walkScore", "walkscore"),
        "walk_score_label": get(b, "walkScore", "description"),
        "transit_score": get(b, "transitScore", "transit_score"),
        "transit_score_label": get(b, "transitScore", "description"),
        "bike_score": get(b, "bikeScore", "bikescore"),
        "bike_score_label": get(b, "bikeScore", "description"),
        "is_low_income": get(b, "isLowIncome", default=False),
        "is_senior_housing": get(b, "isSeniorHousing", default=False),
        "is_student_housing": get(b, "isStudentHousing", default=False),
        "is_waitlisted": get(b, "isWaitlisted", default=False),
        "available_unit_count": get(b, "rentalUnitsSummary", "availableUnitCount"),
        "unit_count": get(b, "rentalUnitsSummary", "unitCount"),
        "list_price_includes_required_monthly_fees": get(
            b, "rentalUnitsSummary", "listPriceIncludesRequiredMonthlyFees", default=False),

        # --- structured deep blobs preserved for downstream agents ---
        "floor_plans": get(b, "floorPlans") or [],
        "structured_amenities": get(b, "structuredAmenities") or {},
        "amenity_summary": get(b, "amenitySummary") or {},
        "detailed_pet_policy": get(b, "detailedPetPolicy") or {},
        "rental_costs_and_fees": get(b, "rentalCostsAndFees") or {},
        "assigned_schools": get(b, "assignedSchools") or [],
        "neighborhood_highlights": get(b, "neighborhoodHighlights") or [],
        "special_offers": get(b, "specialOffers") or [],
    }
    return rec


# ---------------------------- single home -----------------------------------

def extract_single_home(raw: dict) -> dict | None:
    """Build a v2 record from a single_*.json file."""
    p = get(raw, "props", "pageProps", "componentProps", "gdpClientCache")
    # The single-home GraphQL response has a different shape; gdpClientCache
    # is a stringified JSON keyed by query hash. Walk it to find property.
    if isinstance(p, str):
        try:
            cache = json.loads(p)
        except json.JSONDecodeError:
            cache = {}
    else:
        cache = p or {}

    # Find the property dict in the cache values
    prop = None
    if isinstance(cache, dict):
        for v in cache.values():
            if isinstance(v, dict) and isinstance(v.get("property"), dict):
                prop = v["property"]
                break

    if not prop:
        # Fallback: also check raw.property under different paths
        prop = get(raw, "props", "pageProps", "componentProps", "property")

    if not isinstance(prop, dict):
        return None

    address = prop.get("address") or {}
    return {
        "kind": "single_home",
        "zpid": str(prop.get("zpid")) if prop.get("zpid") else None,
        "street_address": prop.get("streetAddress"),
        "full_address": (
            f"{prop.get('streetAddress', '')}, "
            f"{prop.get('city', '')}, {prop.get('state', '')} {prop.get('zipcode', '')}"
        ).strip(", "),
        "city": prop.get("city"),
        "state": prop.get("state"),
        "zipcode": prop.get("zipcode"),
        "county": prop.get("county"),
        "country": prop.get("country"),
        "neighborhood": (address.get("neighborhood")
                         or get(prop, "neighborhoodRegion", "name")
                         or get(prop, "parentRegion", "name")),
        "latitude": prop.get("latitude"),
        "longitude": prop.get("longitude"),

        "rent_min": prop.get("baseRent") or prop.get("price"),
        "rent_max": prop.get("baseRent") or prop.get("price"),
        "bedrooms": prop.get("bedrooms"),
        "bathrooms": prop.get("bathrooms"),
        "living_area": prop.get("livingArea") or prop.get("livingAreaValue"),
        "lot_size": prop.get("lotSize"),
        "year_built": prop.get("yearBuilt"),
        "monthly_hoa_fee": prop.get("monthlyHoaFee"),
        "rent_zestimate": prop.get("rentZestimate"),
        "zestimate": prop.get("zestimate"),
        "days_on_zillow": prop.get("daysOnZillow"),

        "description": prop.get("description"),

        # contact (single-homes typically have an owner/agent listing)
        "phone_primary": get(prop, "rentalListingOwnerContact", "phoneNumber"),
        "agent_full_name": get(prop, "postingContact", "name"),
        "agent_photo": get(prop, "postingContact", "photo"),
        "rental_apps_accepted": prop.get("rentalApplicationsAcceptedType"),

        # tour / virtual
        "virtual_tour_url": prop.get("virtualTourUrl"),
        "third_party_virtual_tour_url": (
            get(prop, "thirdPartyVirtualTour", "externalUrl")
            or get(prop, "thirdPartyVirtualTour", "staticUrl")
        ),
        "interactive_floor_plan_url": prop.get("interactiveFloorPlanUrl"),
        "tour_view_count": prop.get("tourViewCount"),
        "rental_available_tour_times": prop.get("rentalAvailableTourTimes") or [],

        # cost breakdown
        "rental_costs_and_fees": prop.get("rentalCostsAndFees") or {},
        "total_required_monthly_min_fee": prop.get("totalRequiredMonthlyMinFee"),
        "total_required_monthly_max_fee": prop.get("totalRequiredMonthlyMaxFee"),
        "list_price_includes_required_monthly_fees": prop.get(
            "listPriceIncludesRequiredMonthlyFees", False),

        # status / posting type
        "home_status": prop.get("homeStatus"),
        "home_type": prop.get("homeType"),
        "posting_product_type": prop.get("postingProductType"),
        "is_featured": prop.get("isFeatured"),
        "is_zillow_owned": prop.get("isZillowOwned"),

        # photos + media
        "photo_count": prop.get("photoCount"),
        "responsive_photos": [
            (p.get("url") if isinstance(p, dict) else None)
            for p in (prop.get("responsivePhotos") or [])[:30]
        ],

        # schools (different schema than apartment_building)
        "assigned_schools": prop.get("assignedSchools") or prop.get("schools") or [],

        # tax / price history
        "price_history": prop.get("priceHistory") or [],
        "tax_history": prop.get("taxHistory") or [],
        "property_tax_rate": prop.get("propertyTaxRate"),

        # reso facts (the big bag of structured details)
        "reso_facts": prop.get("resoFacts") or {},

        # nearby
        "nearby_buildings": [
            (b.get("addressStreet") if isinstance(b, dict) else None)
            for b in (prop.get("nearbyBuildings") or [])[:5]
        ],
    }


# ----------------------------- driver --------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap how many files to process (smoke test)")
    args = ap.parse_args()

    if not args.raw_dir.exists():
        print(f"ERROR: raw_dir not found: {args.raw_dir}", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)

    files = sorted(args.raw_dir.glob("*.json"))
    if args.limit:
        files = files[: args.limit]
    print(f"Processing {len(files)} raw_details files → {args.out}")

    n_apt = n_home = n_skip = 0
    with gzip.open(args.out, "wt", encoding="utf-8") as out_f:
        for i, f in enumerate(files, 1):
            try:
                raw = json.loads(f.read_text())
            except Exception as e:
                print(f"  [{i}] {f.name}: parse error - {e}", file=sys.stderr)
                n_skip += 1
                continue

            # File-prefix → primary extractor, with fallback. Three patterns:
            #   apt_<slug>.json  → apartment_building (gdp.building path)
            #   b_<slug>.json    → building (same gdp.building path; often sparse)
            #   zpid_<id>.json   → single home (gdpClientCache path)
            primary_apt = f.name.startswith(("apt_", "b_"))
            if primary_apt:
                rec = extract_apartment(raw)
                if rec is None or not rec.get("zpid"):
                    rec = extract_single_home(raw)
                    bucket = "home"
                else:
                    bucket = "apt"
            else:
                rec = extract_single_home(raw)
                if rec is None or not rec.get("zpid"):
                    rec = extract_apartment(raw)
                    bucket = "apt"
                else:
                    bucket = "home"

            if rec is None or not rec.get("zpid"):
                n_skip += 1
                if i % 200 == 0:
                    print(f"  [{i}/{len(files)}] skip ({f.name})")
                continue

            out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if bucket == "apt":
                n_apt += 1
            else:
                n_home += 1

            if i % 200 == 0:
                print(f"  [{i}/{len(files)}] kept apt={n_apt} home={n_home} skip={n_skip}")

    print()
    print(f"✓ Done · output: {args.out} ({args.out.stat().st_size // 1024} KB gzipped)")
    print(f"  apartment_building: {n_apt}")
    print(f"  single_home:        {n_home}")
    print(f"  skipped:            {n_skip}")

    # Quick coverage report on the new fields
    print(f"\nField coverage report (sampled from output):")
    with gzip.open(args.out, "rt", encoding="utf-8") as f:
        recs = [json.loads(line) for line in f]
    new_fields = [
        "phone_primary", "agent_full_name", "office_hours_pretty",
        "housing_connector_link", "marketing_name", "year_built",
        "rent_zestimate", "virtual_tour_url",
    ]
    for k in new_fields:
        n_have = sum(1 for r in recs if r.get(k))
        pct = (n_have / len(recs) * 100) if recs else 0
        print(f"  {k:35s}  {n_have:>4} / {len(recs)}  ({pct:5.1f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
