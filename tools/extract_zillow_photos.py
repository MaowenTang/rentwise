"""Extract primary photo URL from existing raw_details/*.json — no scraping.

Background
----------
The original enrich.py (Zillow scraper) saved photoCount but dropped the
actual photo URLs. The raw __NEXT_DATA__ blob it cached to
output/raw_details/<key>.json contains the full photo array, so we can
recover URLs without re-scraping (no CAPTCHA risk).

For each apartment_building or single_home raw file we pull:
  - zpid                              (canonical key — joins to live data)
  - primary_photo_url                 (first photo, mid-resolution webp/jpg)

Output: api/data/zillow_photos.jsonl.gz (one record per zpid, ~50KB)

Then api/listings.py merges this overlay onto each Listing.raw at load
time so Listing.raw["primary_photo_url"] is populated for both Zillow
and apartments.com sources, and shortlist_payload() picks it up.
"""
from __future__ import annotations

import gzip
import json
import logging
import sys
from pathlib import Path
from typing import Any

LOG = logging.getLogger("zillow-photos")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = Path("/Users/tangmaowen/Downloads/zillow_scraper/output/raw_details")
OUTPUT = ROOT / "api" / "data" / "zillow_photos.jsonl.gz"


def _safe_get(d: dict | None, *path: str, default=None):
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return default if cur is None else cur


def _pick_url(photo: dict) -> str | None:
    """Pick a reasonable mid-res URL from a Zillow photo node."""
    # Shape 1 (apartment_building): photo.mixedSources.{webp|jpeg} → list of
    # {url, width}. Pick the one closest to width=576 — large enough to look
    # crisp at 300x200 thumbnail size, small enough to not bloat the page.
    mixed = photo.get("mixedSources") or {}
    for fmt in ("webp", "jpeg", "jpg"):
        sources = mixed.get(fmt) or []
        if not sources:
            continue
        # Sort by distance from target width
        target = 576
        sorted_sources = sorted(
            sources,
            key=lambda s: abs((s.get("width") or 0) - target),
        )
        for s in sorted_sources:
            url = s.get("url")
            if url:
                return url

    # Shape 2 (single_home): photo.url directly, or photo.subjectPhoto, etc.
    for key in ("url", "imageUrl", "primaryUrl"):
        url = photo.get(key)
        if url:
            return url

    # Shape 3: photo.thumbsUrl or .miniUrl as last-resort
    for key in ("thumbsUrl", "miniUrl"):
        url = photo.get(key)
        if url:
            return url

    return None


def extract_apartment(raw: dict) -> tuple[str | None, str | None]:
    """apt_*.json — building.photos[0]."""
    b = _safe_get(raw, "props", "pageProps", "componentProps",
                  "initialReduxState", "gdp", "building")
    if not isinstance(b, dict):
        return None, None
    zpid = b.get("zpid") or b.get("buildingId") or b.get("lotId")
    photos = b.get("photos") or b.get("galleryPhotos") or []
    if not isinstance(photos, list) or not photos:
        return (str(zpid) if zpid else None), None
    url = _pick_url(photos[0]) if isinstance(photos[0], dict) else None
    return (str(zpid) if zpid else None), url


def extract_single_home(raw: dict) -> tuple[str | None, str | None]:
    """zpid_*.json — gdpClientCache is a STRING-encoded JSON map keyed by
    GraphQL query-shape with `{property: {responsivePhotos: [...]}}` inside."""
    cache_str = _safe_get(raw, "props", "pageProps", "componentProps",
                          "gdpClientCache")
    if not isinstance(cache_str, str):
        # Some pages may have it as a dict already
        if isinstance(cache_str, dict):
            cache = cache_str
        else:
            return None, None
    else:
        try:
            cache = json.loads(cache_str)
        except json.JSONDecodeError:
            return None, None

    if not isinstance(cache, dict) or not cache:
        return None, None

    # Pick first cached property entry
    for v in cache.values():
        if not isinstance(v, dict):
            continue
        prop = v.get("property")
        if not isinstance(prop, dict):
            continue
        zpid = prop.get("zpid")
        photos = (prop.get("responsivePhotos")
                  or prop.get("photos")
                  or prop.get("originalPhotos") or [])
        if isinstance(photos, list) and photos and isinstance(photos[0], dict):
            url = _pick_url(photos[0])
            if url:
                return (str(zpid) if zpid else None), url
        return (str(zpid) if zpid else None), None
    return None, None


def main():
    if not RAW_DIR.exists():
        LOG.error("raw_details dir missing: %s", RAW_DIR)
        sys.exit(1)

    files = sorted(RAW_DIR.glob("*.json"))
    LOG.info("Scanning %d raw files in %s", len(files), RAW_DIR)

    OUTPUT.parent.mkdir(exist_ok=True)
    n_in = n_apt = n_home = n_with_url = 0
    seen_zpids: set[str] = set()

    with gzip.open(OUTPUT, "wt", encoding="utf-8") as fout:
        for fp in files:
            n_in += 1
            try:
                raw = json.loads(fp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                LOG.debug("skip %s: %s", fp.name, e)
                continue

            # Heuristic by filename prefix
            name = fp.stem
            zpid = url = None
            if name.startswith(("apt_", "b_")):
                zpid, url = extract_apartment(raw)
                n_apt += 1
            else:
                # single_*, zpid_*, or numeric — try single home shape
                zpid, url = extract_single_home(raw)
                n_home += 1

            if not zpid or zpid in seen_zpids:
                continue
            seen_zpids.add(zpid)
            if not url:
                continue
            n_with_url += 1
            fout.write(json.dumps(
                {"zpid": zpid, "primary_photo_url": url},
                ensure_ascii=False,
            ) + "\n")

    LOG.info("Wrote %d records (%d unique zpids with photos) → %s",
             n_with_url, n_with_url, OUTPUT)
    LOG.info("Scanned: %d files (%d apt-shape, %d single-home-shape)",
             n_in, n_apt, n_home)
    LOG.info("Output size: %d KB", OUTPUT.stat().st_size // 1024)


if __name__ == "__main__":
    main()
