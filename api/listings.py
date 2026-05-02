"""Listing loader + simple in-memory filter.

For the v0 scaffold we keep all 910 enriched listings in memory and apply
hard-filter Python predicates (city, max rent, min beds, pet-friendly).
Replaced by Postgres + pgvector in v1 per the design spec.
"""
from __future__ import annotations

import gzip
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# Data-file resolution. Try (in order):
#   1. $LISTINGS_PATH env var
#   2. ./data/zillow_listings.jsonl.gz (production deploy)
#   3. ./data/zillow_listings.jsonl   (uncompressed alt)
#   4. ../zillow_scraper/output/zillow_san_jose_rentals_enriched.jsonl (local dev)
DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"


@dataclass
class Listing:
    zpid: str
    name: str
    address: str
    neighborhood: str | None
    lat: float | None
    lng: float | None
    rent_min: int | None
    rent_max: int | None
    rent_by_bed: dict[int, tuple[int | None, int | None]]  # beds -> (min, max)
    walk_score: int | None
    transit_score: int | None
    bike_score: int | None
    pets_allowed: list[str] = field(default_factory=list)
    has_pool: bool = False
    has_elevator: bool | None = None
    has_storage: bool | None = None
    has_patio_balcony: bool | None = None
    parking_types: list[str] = field(default_factory=list)
    utilities_included: list[str] = field(default_factory=list)
    deposit_min: int | None = None
    deposit_max: int | None = None
    description: str = ""
    url: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_jsonl_record(cls, rec: dict[str, Any]) -> "Listing":
        rent_by_bed: dict[int, tuple[int | None, int | None]] = {}
        all_mins: list[int] = []
        all_maxs: list[int] = []
        for fp in rec.get("rent_by_bed_type", []) or []:
            beds = fp.get("num_beds")
            if beds is None:
                continue
            mn, mx = fp.get("rent_min"), fp.get("rent_max")
            rent_by_bed[beds] = (mn, mx)
            if mn is not None:
                all_mins.append(mn)
            if mx is not None:
                all_maxs.append(mx)

        return cls(
            zpid=str(rec.get("zpid") or rec.get("lot_id") or ""),
            name=rec.get("building_name") or rec.get("street_address") or "(unnamed)",
            address=rec.get("full_address", ""),
            neighborhood=rec.get("neighborhood"),
            lat=rec.get("latitude") or rec.get("lat"),
            lng=rec.get("longitude") or rec.get("lng"),
            rent_min=min(all_mins) if all_mins else None,
            rent_max=max(all_maxs) if all_maxs else None,
            rent_by_bed=rent_by_bed,
            walk_score=rec.get("walk_score"),
            transit_score=rec.get("transit_score"),
            bike_score=rec.get("bike_score"),
            pets_allowed=rec.get("pets_allowed") or [],
            has_pool=bool(rec.get("has_pool")),
            has_elevator=rec.get("has_elevator"),
            has_storage=rec.get("has_storage"),
            has_patio_balcony=rec.get("has_patio_balcony"),
            parking_types=rec.get("parking_types") or [],
            utilities_included=rec.get("utilities_included") or [],
            deposit_min=rec.get("deposit_min"),
            deposit_max=rec.get("deposit_max"),
            description=(rec.get("description") or "")[:500],
            url=rec.get("url", ""),
            raw=rec,
        )

    def short_summary(self) -> str:
        beds = ",".join(
            f"{b}BR" if b > 0 else "Studio" for b in sorted(self.rent_by_bed)
        ) or "?"
        rent = (
            f"${self.rent_min:,}-${self.rent_max:,}"
            if self.rent_min and self.rent_max
            else "rent ?"
        )
        loc = f"{self.neighborhood}, " if self.neighborhood else ""
        return f"{self.name} ({beds}) {rent} — {loc}walk {self.walk_score}"


def _parse_dollar(s: Any) -> int | None:
    """Extract integer USD from a string like '$6,200' or '$1,500/mo'."""
    if isinstance(s, (int, float)):
        return int(s)
    if not isinstance(s, str):
        return None
    import re
    m = re.search(r"\$?([\d,]+)", s)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _normalize_single_home_raw(rec: dict[str, Any]) -> dict[str, Any]:
    """Re-shape a `single_home_raw` record into the same flat structure
    as `apartment_building`. The data is all there — just buried under
    `raw.property.*` and `raw.property.resoFacts.*`.

    Returns a dict that `Listing.from_jsonl_record()` can consume.
    """
    prop: dict = (rec.get("raw") or {}).get("property") or {}
    reso: dict = prop.get("resoFacts") or {}

    if not prop or not prop.get("zpid"):
        # Nothing useful to recover.
        return rec

    # --- Identity ---
    zpid = str(prop.get("zpid"))
    street = prop.get("streetAddress") or ""
    city = prop.get("city") or "San Jose"
    state = prop.get("state") or "CA"
    zipcode = prop.get("zipcode") or ""
    full_addr = ", ".join(p for p in (street, f"{city}, {state} {zipcode}".strip()) if p)

    # neighborhood: prefer address.neighborhood, fall back to neighborhoodRegion / parentRegion
    addr = prop.get("address") or {}
    neighborhood = (
        addr.get("neighborhood")
        or (prop.get("neighborhoodRegion") or {}).get("name")
        or (prop.get("parentRegion") or {}).get("name")
    )

    # --- Pricing ---
    # IMPORTANT: only trust `baseRent` (the rental field). DO NOT fall back to
    # `price` — for some listings `price` carries the sale price ($1-4M) even
    # when homeStatus is FOR_RENT, which would pollute results.
    base_rent = prop.get("baseRent")
    if isinstance(base_rent, (int, float)) and 200 <= base_rent <= 30000:
        rent_min = int(base_rent)
        rent_max = rent_min
    else:
        # Treat as missing — better to skip than to lie.
        rent_min = None
        rent_max = None

    # Build a single rent_by_bed_type entry so downstream filters work
    bedrooms = prop.get("bedrooms")
    rent_by_bed: list[dict] = []
    if rent_min is not None and isinstance(bedrooms, int):
        label = "Studio" if bedrooms == 0 else f"{bedrooms}BR"
        rent_by_bed.append(
            {
                "num_beds": bedrooms,
                "label": label,
                "rent_min": int(rent_min),
                "rent_max": int(rent_max),
            }
        )

    # --- Deposit (from resoFacts.feesAndDues) ---
    deposit_min = deposit_max = None
    for fee in (reso.get("feesAndDues") or []):
        if not isinstance(fee, dict):
            continue
        if (fee.get("type") or "").lower() == "deposit":
            d = _parse_dollar(fee.get("fee"))
            if d is not None:
                deposit_min = deposit_max = d
                break

    # --- Pets ---
    has_pets = reso.get("hasPetsAllowed")
    pets_allowed = []
    if has_pets:
        pets_allowed = list(reso.get("allowedPets") or [])

    # --- Schools (different schema) ---
    schools_norm: list[dict] = []
    for s in (prop.get("schools") or []):
        if not isinstance(s, dict):
            continue
        schools_norm.append(
            {
                "name": s.get("name"),
                "rating": s.get("rating"),
                "distance": s.get("distance"),
                "level": s.get("level"),
                "grades": s.get("grades"),
                "type": s.get("type"),
                "students_per_teacher": s.get("studentsPerTeacher"),
                "link": s.get("link"),
            }
        )

    # --- Amenities synthesis ---
    appliances = reso.get("appliances") or []
    parking_types = reso.get("parkingFeatures") or []
    laundry = reso.get("laundryFeatures") or []
    cooling = reso.get("cooling") or []
    heating = reso.get("heating") or []
    interior = reso.get("interiorFeatures") or []
    exterior = reso.get("exteriorFeatures") or []

    # Single floor plan
    living_area = prop.get("livingArea") or prop.get("livingAreaValue")
    floor_plans = []
    if isinstance(bedrooms, int):
        floor_plans.append(
            {
                "fp_zpid": zpid,
                "beds": bedrooms,
                "baths": prop.get("bathrooms"),
                "sqft_min": living_area,
                "sqft_max": living_area,
                "price_min": int(rent_min) if rent_min is not None else None,
                "price_max": int(rent_max) if rent_max is not None else None,
                "n_units": 1,
                "units": [],
            }
        )

    # Contact
    phone = (prop.get("rentalListingOwnerContact") or {}).get("phoneNumber")
    agent_name = (prop.get("postingContact") or {}).get("name") or "Owner / Listing Contact"

    # Photos count
    photo_count = prop.get("photoCount") or len(prop.get("responsivePhotos") or [])

    # Description
    description = prop.get("description") or ""

    # is_furnished, smoke-free etc
    is_furnished = reso.get("furnished")

    # available?
    home_status = prop.get("homeStatus")
    available_unit_count = 1 if home_status in ("FOR_RENT", "RENTAL") else 0

    # Compose the normalized record (matching apartment_building schema)
    norm: dict[str, Any] = {
        "kind": "single_home",  # marker that this was normalized
        "zpid": zpid,
        "lot_id": prop.get("parcelId"),
        "building_name": street or "Single-family rental",
        "full_address": full_addr,
        "street_address": street,
        "city": city,
        "state": state,
        "zipcode": zipcode,
        "country": prop.get("country") or "USA",
        "county": prop.get("county") or "",
        "latitude": prop.get("latitude"),
        "longitude": prop.get("longitude"),
        "neighborhood": neighborhood,
        # Single homes don't have curated neighborhood text — use the description
        # excerpt so Location Agent has *something* to mine for nearby places.
        "neighborhood_description": description[:600] if description else None,
        "neighborhood_highlights": [],

        "phone": phone,
        "agent_name": agent_name,
        "rental_applications_accepted_type": prop.get("rentalApplicationsAcceptedType"),
        "rental_product_type": prop.get("postingProductType") or "SINGLE_HOME",

        "description": description,
        "unit_count": 1,
        "available_unit_count": available_unit_count,
        "summary_building_details": [],
        "summary_laundry": laundry,
        "list_price_includes_required_monthly_fees": prop.get("listPriceIncludesRequiredMonthlyFees", False),

        "deposit_min": deposit_min,
        "deposit_max": deposit_max,
        "application_fee": None,
        "administrative_fee": None,
        "utilities_included": [],
        "lease_lengths": reso.get("leaseTerm"),
        "lease_terms": [reso.get("leaseTerm")] if reso.get("leaseTerm") else [],
        "rentals_disclaimer": None,

        "rent_by_bed_type": rent_by_bed,
        "floor_plans": floor_plans,
        "n_floor_plans": len(floor_plans),

        "building_amenities": [],
        "unit_features": [
            {"category": "Appliances", "items": appliances},
            {"category": "Interior", "items": interior},
            {"category": "Exterior", "items": exterior},
        ],
        "policies": [],
        "special_features": [],
        "common_unit_amenities": interior,
        "appliances": appliances,
        "community_rooms": [],
        "outdoor_common_areas": [],
        "parking_types": parking_types,
        "parking_description": None,
        "parking_rent_description": None,
        "security_types": [],
        "view_types": reso.get("view") or [],
        "sports_courts": [],
        "floor_coverings": [reso.get("flooring")] if reso.get("flooring") else [],
        "air_conditioning": "Central Air" if cooling else "Unknown",
        "heating_source": heating[0] if heating else "Unknown",
        "is_furnished": is_furnished,
        "is_smoke_free": None,
        "is_low_income": False,
        "is_senior_housing": bool(reso.get("isSeniorCommunity")),
        "is_student_housing": False,

        "has_pool": bool(reso.get("hasPrivatePool")) or bool(reso.get("hasSpa")),
        "has_barbecue": None,
        "has_elevator": None,
        "has_fireplace": bool(reso.get("hasFireplace")) if reso.get("hasFireplace") is not None else None,
        "has_hot_tub": bool(reso.get("hasSpa")) if reso.get("hasSpa") is not None else None,
        "has_storage": None,
        "has_disabled_access": bool(reso.get("accessibilityFeatures")) if reso.get("accessibilityFeatures") else None,
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
        "has_shared_laundry": False,
        "has_spanish_speaking_staff": None,

        "pets_allowed": pets_allowed,
        "pet_groups": [
            {
                "type": p.lower(),
                "label": p,
                "label_description": None,
                "size": None,
                "max_weight": reso.get("petsMaxWeight"),
                "max_number": None,
                "deposit": None,
                "monthly_fee": None,
                "one_time_fee": None,
            }
            for p in pets_allowed
        ],

        # Single homes lack walk/transit/bike scores from Zillow.
        # Set to None — RankingService gracefully skips missing components.
        "walk_score": None,
        "walk_score_label": None,
        "transit_score": None,
        "transit_score_label": None,
        "bike_score": None,
        "bike_score_label": None,

        "schools": schools_norm,
        "faqs": rec.get("faqs") or [],
        "n_faqs": len(rec.get("faqs") or []),
        "office_hours_raw": rec.get("office_hours_raw"),
        "photo_count": photo_count,

        "url": rec.get("url") or prop.get("hdpUrl"),

        # Preserve original under a sub-key for debugging
        "_normalized_from_single_home_raw": True,
    }
    return norm


def load_listings(
    path: str | os.PathLike[str] | None = None,
    *,
    skip_broken: bool = True,
) -> list[Listing]:
    """Load enriched listings.

    Normalizes `single_home_raw` records to the apartment_building shape
    so they're usable downstream. Records that still lack zpid/name/rent
    after normalization are dropped when skip_broken=True.
    """
    # Resolve data path with sensible fallbacks
    candidates = [
        path,
        os.environ.get("LISTINGS_PATH"),
        DEFAULT_DATA_DIR / "zillow_listings.jsonl.gz",
        DEFAULT_DATA_DIR / "zillow_listings.jsonl",
        Path("/Users/tangmaowen/Downloads/zillow_scraper/output/zillow_san_jose_rentals_enriched.jsonl"),
    ]
    p: Path | None = None
    for cand in candidates:
        if cand:
            cp = Path(cand)
            if cp.exists():
                p = cp
                break
    if p is None:
        raise FileNotFoundError(
            "Listings file not found. Set LISTINGS_PATH or place data at api/data/zillow_listings.jsonl[.gz]."
        )

    out: list[Listing] = []
    skipped = 0
    normalized_count = 0
    opener = gzip.open if p.suffix == ".gz" else open
    with opener(p, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            if rec.get("kind") == "single_home_raw":
                rec = _normalize_single_home_raw(rec)
                if rec.get("_normalized_from_single_home_raw"):
                    normalized_count += 1

            L = Listing.from_jsonl_record(rec)
            if skip_broken and (
                not L.zpid
                or L.name == "(unnamed)"
                or L.rent_min is None
            ):
                skipped += 1
                continue
            out.append(L)

    import logging
    log = logging.getLogger("rentwise.api")
    log.info(
        "load_listings: kept %d (incl. %d normalized single-homes), skipped %d",
        len(out), normalized_count, skipped,
    )

    # --- optionally merge Craigslist (apartment.com upstream) -------------
    cl_candidates = [
        os.environ.get("CRAIGSLIST_PATH"),
        DEFAULT_DATA_DIR / "craigslist_apartments.csv",
        Path("/Users/tangmaowen/Downloads/craigslist_apartments.csv"),
    ]
    cl_path = None
    for cand in cl_candidates:
        if cand and Path(cand).exists():
            cl_path = cand
            break
    if cl_path:
        # Local import to avoid circular dependency at module load.
        from craigslist import load_craigslist, merge_into_zillow
        cl_records = load_craigslist(cl_path)
        if cl_records:
            out, n_merged, n_added = merge_into_zillow(out, cl_records)
            log.info(
                "load_listings: craigslist merge → %d records (%d merged into Zillow, %d new)",
                len(out), n_merged, n_added,
            )

    return out


def filter_listings(
    listings: Iterable[Listing],
    *,
    max_rent: int | None = None,
    min_beds: int | None = None,
    max_beds: int | None = None,
    pets: str | None = None,  # "dogs", "cats", or None
    neighborhood: str | None = None,
) -> list[Listing]:
    out: list[Listing] = []
    for L in listings:
        if max_rent is not None:
            # If user specified a budget, REJECT listings without known rent.
            # Better to lose recall than to surface a $2M home in a $2k search.
            if L.rent_min is None:
                continue
            if L.rent_min > max_rent:
                continue
        if min_beds is not None and L.rent_by_bed:
            if not any(b >= min_beds for b in L.rent_by_bed):
                continue
        if max_beds is not None and L.rent_by_bed:
            if not any(b <= max_beds for b in L.rent_by_bed):
                continue
        if pets:
            allowed = {p.lower() for p in (L.pets_allowed or [])}
            if not any(pets.lower() in a for a in allowed):
                continue
        if neighborhood and L.neighborhood:
            if neighborhood.lower() not in L.neighborhood.lower():
                continue
        out.append(L)
    return out
