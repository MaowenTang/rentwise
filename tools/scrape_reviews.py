"""Resident Reviews scraper — Resident Reviews Agent data pipeline.

Fetches Reddit posts/comments and Yelp reviews for each listing, then
writes deduplicated results to tools/data/reviews.jsonl.

Data sources:
  • Reddit PRAW — searches r/bayarea, r/sanfrancisco, r/SanJose for
    posts/comments mentioning the building name + city.  Requires a Reddit
    OAuth app (script type).  Free, 1 req/sec limit.
  • Yelp Fusion API — searches by building name + address, then verifies the
    matched business is within 500 m of the listing's lat/lng (Haversine).
    Non-Places API (Business Search + Reviews): 300 calls/day, 5,000/month.

Output schema (tools/data/reviews.jsonl):
  {
    "zpid":        "2058946048",        -- links to Listing
    "source":      "reddit" | "yelp",
    "external_id": "<reddit post/comment id or yelp review id>",
    "text":        "...",
    "rating":      4.0,                 -- yelp only; null for reddit
    "review_date": "2024-11-03T14:22:00Z",
    "url":         "https://...",
    "verified":    true,                -- address/coord match confirmed
    "verified_by": "haversine_500m" | "no_coords" | "yelp_business_match",
    "scraped_at":  "2026-05-03T19:30:00Z"
  }

Usage:
    # First run — validate 100 listings, don't need full credentials yet:
    python tools/scrape_reviews.py --dry-run --limit 100

    # Real run with credentials:
    python tools/scrape_reviews.py \\
        --reddit-client-id <id> \\
        --reddit-client-secret <secret> \\
        --reddit-user-agent "rentwise:v0.1 (by u/maowen)" \\
        --yelp-api-key <key> \\
        --limit 100          # start with 100 for validation

    # Full run (after validation):
    python tools/scrape_reviews.py --reddit-... --yelp-... --limit 3081

    # Incremental re-run (skip already-scraped zpids):
    python tools/scrape_reviews.py --reddit-... --yelp-... --resume

Environment variable alternatives (preferred over CLI flags):
    REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USER_AGENT
    YELP_API_KEY
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import httpx

LOG = logging.getLogger("scrape_reviews")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
TOOLS_DIR = Path(__file__).resolve().parent
DATA_DIR = TOOLS_DIR / "data"
API_DATA_DIR = TOOLS_DIR.parent / "api" / "data"
OUT_FILE = DATA_DIR / "reviews.jsonl"

# ---------------------------------------------------------------------------
# Subreddits to search for building-level reviews
# ---------------------------------------------------------------------------
SUBREDDITS = ["bayarea", "sanfrancisco", "SanJose", "AskSF", "oakland"]

# ---------------------------------------------------------------------------
# Haversine distance (metres)
# ---------------------------------------------------------------------------

def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6_371_000  # Earth radius in metres
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ---------------------------------------------------------------------------
# Listing loader — reads all *.jsonl.gz files under api/data/
# ---------------------------------------------------------------------------

@dataclass
class ListingStub:
    zpid: str
    name: str
    address: str
    city: str
    lat: float | None
    lng: float | None


def _load_listings(limit: int | None = None) -> list[ListingStub]:
    """Load listing stubs from all JSONL.GZ files in api/data/."""
    seen_zpids: set[str] = set()
    stubs: list[ListingStub] = []

    gz_files = sorted(API_DATA_DIR.glob("zillow_listings*.jsonl.gz"))
    if not gz_files:
        LOG.error("No zillow_listings*.jsonl.gz found in %s", API_DATA_DIR)
        sys.exit(1)

    for gz_path in gz_files:
        LOG.info("Reading %s", gz_path.name)
        with gzip.open(gz_path) as f:
            for line in f:
                rec = json.loads(line)
                zpid = str(rec.get("zpid") or rec.get("lot_id") or "")
                if not zpid or zpid in seen_zpids:
                    continue
                seen_zpids.add(zpid)
                name = (
                    rec.get("building_name")
                    or rec.get("marketing_name")
                    or rec.get("name")
                    or ""
                ).strip()
                address = (
                    rec.get("full_address")
                    or rec.get("street_address")
                    or rec.get("address")
                    or ""
                ).strip()
                city = (rec.get("city") or "").strip()
                lat = rec.get("latitude") or rec.get("lat")
                lng = rec.get("longitude") or rec.get("lng")
                if not name:
                    continue
                stubs.append(ListingStub(
                    zpid=zpid, name=name, address=address, city=city,
                    lat=float(lat) if lat else None,
                    lng=float(lng) if lng else None,
                ))
                if limit and len(stubs) >= limit:
                    return stubs

    LOG.info("Loaded %d listing stubs", len(stubs))
    return stubs


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _load_existing_zpids(path: Path) -> set[str]:
    """Return set of zpids already in the output file (for --resume)."""
    if not path.exists():
        return set()
    zpids: set[str] = set()
    with open(path) as f:
        for line in f:
            try:
                zpids.add(json.loads(line)["zpid"])
            except (json.JSONDecodeError, KeyError):
                pass
    return zpids


def _append_reviews(path: Path, reviews: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        for r in reviews:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Reddit scraper (PRAW via REST — no praw library required)
# ---------------------------------------------------------------------------

class RedditClient:
    """Thin wrapper around Reddit's OAuth REST API (no praw dependency)."""

    TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
    SEARCH_URL = "https://oauth.reddit.com/r/{sub}/search.json"

    def __init__(self, client_id: str, client_secret: str, user_agent: str) -> None:
        self.user_agent = user_agent
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._http = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=15,
        )
        self._client_id = client_id
        self._client_secret = client_secret

    def _ensure_token(self) -> None:
        if self._token and time.time() < self._token_expiry - 60:
            return
        resp = self._http.post(
            self.TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(self._client_id, self._client_secret),
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + data.get("expires_in", 3600)
        self._http.headers["Authorization"] = f"Bearer {self._token}"

    def search(self, subreddit: str, query: str, limit: int = 25) -> list[dict]:
        """Search a subreddit; returns list of post dicts."""
        self._ensure_token()
        try:
            resp = self._http.get(
                self.SEARCH_URL.format(sub=subreddit),
                params={"q": query, "sort": "relevance", "limit": limit, "type": "link", "t": "all"},
            )
            resp.raise_for_status()
            return resp.json().get("data", {}).get("children", [])
        except httpx.HTTPError as e:
            LOG.warning("Reddit search failed (%s/%s): %s", subreddit, query, e)
            return []

    def comments(self, post_id: str, subreddit: str) -> list[dict]:
        """Fetch top-level comments for a post."""
        self._ensure_token()
        try:
            resp = self._http.get(
                f"https://oauth.reddit.com/r/{subreddit}/comments/{post_id}.json",
                params={"limit": 100, "depth": 2},
            )
            resp.raise_for_status()
            data = resp.json()
            if len(data) < 2:
                return []
            return data[1].get("data", {}).get("children", [])
        except httpx.HTTPError as e:
            LOG.warning("Reddit comments failed (%s/%s): %s", subreddit, post_id, e)
            return []


def _reddit_reviews(
    client: RedditClient,
    listing: ListingStub,
    dry_run: bool = False,
) -> list[dict]:
    """Fetch Reddit posts/comments mentioning this listing."""
    if not listing.name:
        return []

    city_hint = listing.city or "Bay Area"
    query = f'"{listing.name}" {city_hint}'
    now_iso = datetime.now(timezone.utc).isoformat()
    results: list[dict] = []
    seen_ids: set[str] = set()

    for sub in SUBREDDITS:
        if dry_run:
            LOG.info("[DRY RUN] Would search r/%s for: %s", sub, query)
            time.sleep(0.1)
            continue

        LOG.debug("Reddit search: r/%s q=%r", sub, query)
        posts = client.search(sub, query, limit=10)
        time.sleep(1)  # 1 req/sec Reddit limit

        for item in posts:
            post = item.get("data", {})
            post_id = post.get("id", "")
            if not post_id or post_id in seen_ids:
                continue
            seen_ids.add(post_id)

            # Include the post itself if it has text content
            selftext = (post.get("selftext") or "").strip()
            title = (post.get("title") or "").strip()
            body = selftext or title
            if body and len(body) > 20:
                created = post.get("created_utc")
                results.append({
                    "zpid": listing.zpid,
                    "source": "reddit",
                    "external_id": f"t3_{post_id}",
                    "text": f"{title}\n\n{selftext}".strip() if selftext else title,
                    "rating": None,
                    "review_date": datetime.fromtimestamp(created, timezone.utc).isoformat() if created else None,
                    "url": f"https://reddit.com{post.get('permalink', '')}",
                    "verified": True,
                    "verified_by": "reddit_name_match",
                    "scraped_at": now_iso,
                })

            # Fetch comments for relevant posts
            comment_items = client.comments(post_id, sub)
            time.sleep(1)
            for citem in comment_items:
                c = citem.get("data", {})
                cid = c.get("id", "")
                cbody = (c.get("body") or "").strip()
                if not cid or cid in seen_ids or not cbody or cbody in ("[deleted]", "[removed]"):
                    continue
                # Only include comment if it mentions the building name
                if listing.name.lower() not in cbody.lower():
                    continue
                seen_ids.add(cid)
                created = c.get("created_utc")
                results.append({
                    "zpid": listing.zpid,
                    "source": "reddit",
                    "external_id": f"t1_{cid}",
                    "text": cbody,
                    "rating": None,
                    "review_date": datetime.fromtimestamp(created, timezone.utc).isoformat() if created else None,
                    "url": f"https://reddit.com{post.get('permalink', '')}",
                    "verified": True,
                    "verified_by": "reddit_name_match",
                    "scraped_at": now_iso,
                })

    return results


# ---------------------------------------------------------------------------
# Yelp scraper
# ---------------------------------------------------------------------------

class YelpClient:
    SEARCH_URL = "https://api.yelp.com/v3/businesses/search"
    REVIEWS_URL = "https://api.yelp.com/v3/businesses/{id}/reviews"

    def __init__(self, api_key: str) -> None:
        self._http = httpx.Client(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )

    def search(self, name: str, address: str, city: str) -> dict | None:
        """Find a business on Yelp by name + location. Returns first hit or None."""
        location = f"{address}, {city}" if address else city
        try:
            resp = self._http.get(
                self.SEARCH_URL,
                params={"term": name, "location": location, "limit": 3, "categories": "apartments"},
            )
            resp.raise_for_status()
            businesses = resp.json().get("businesses", [])
            return businesses[0] if businesses else None
        except httpx.HTTPError as e:
            LOG.warning("Yelp search failed (%s): %s", name, e)
            return None

    def reviews(self, business_id: str) -> list[dict]:
        """Fetch up to 3 most recent reviews for a business."""
        try:
            resp = self._http.get(self.REVIEWS_URL.format(id=business_id))
            resp.raise_for_status()
            return resp.json().get("reviews", [])
        except httpx.HTTPError as e:
            LOG.warning("Yelp reviews failed (%s): %s", business_id, e)
            return []


def _yelp_reviews(
    client: YelpClient,
    listing: ListingStub,
    dry_run: bool = False,
    yelp_calls: list[int] | None = None,  # mutable counter [calls_today]
) -> list[dict]:
    """Fetch Yelp reviews for a listing. Returns empty list if address mismatch."""
    if yelp_calls is None:
        yelp_calls = [0]
    if yelp_calls[0] >= 280:  # keep buffer below 300/day non-Places API limit
        LOG.warning("Yelp daily limit approaching (%d calls), skipping %s", yelp_calls[0], listing.zpid)
        return []

    now_iso = datetime.now(timezone.utc).isoformat()

    if dry_run:
        LOG.info("[DRY RUN] Would search Yelp for: %s, %s", listing.name, listing.address)
        yelp_calls[0] += 1
        return []

    business = client.search(listing.name, listing.address, listing.city)
    yelp_calls[0] += 1
    time.sleep(0.5)

    if not business:
        LOG.debug("Yelp: no match for %s", listing.name)
        return []

    # Address verification: check coordinates are within 500m
    biz_lat = business.get("coordinates", {}).get("latitude")
    biz_lng = business.get("coordinates", {}).get("longitude")

    verified = False
    verified_by = "yelp_business_match"

    if listing.lat and listing.lng and biz_lat and biz_lng:
        dist_m = _haversine_m(listing.lat, listing.lng, biz_lat, biz_lng)
        if dist_m > 500:
            LOG.info(
                "Yelp: discarding %s — matched business is %.0fm away (>500m threshold)",
                listing.name, dist_m,
            )
            return []
        verified = True
        verified_by = "haversine_500m"
    elif not listing.lat:
        # No listing coords — can't verify by distance; mark as unverified
        verified = False
        verified_by = "no_coords"

    biz_id = business["id"]
    reviews = client.reviews(biz_id)
    yelp_calls[0] += 1
    time.sleep(0.5)

    results: list[dict] = []
    for r in reviews:
        rid = r.get("id", "")
        text = (r.get("text") or "").strip()
        if not rid or not text:
            continue
        created = r.get("time_created") or r.get("timestamp")
        rating = r.get("rating")
        results.append({
            "zpid": listing.zpid,
            "source": "yelp",
            "external_id": rid,
            "text": text,
            "rating": float(rating) if rating is not None else None,
            "review_date": created,
            "url": r.get("url") or business.get("url", ""),
            "verified": verified,
            "verified_by": verified_by,
            "scraped_at": now_iso,
        })

    if results:
        LOG.info(
            "Yelp: %s → %d review(s) (verified=%s, verified_by=%s)",
            listing.name, len(results), verified, verified_by,
        )
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Scrape Reddit + Yelp reviews for RentWise listings."
    )
    p.add_argument("--reddit-client-id", default=os.environ.get("REDDIT_CLIENT_ID"))
    p.add_argument("--reddit-client-secret", default=os.environ.get("REDDIT_CLIENT_SECRET"))
    p.add_argument("--reddit-user-agent", default=os.environ.get("REDDIT_USER_AGENT", "rentwise:v0.1 (by u/maowen)"))
    p.add_argument("--yelp-api-key", default=os.environ.get("YELP_API_KEY"))
    p.add_argument("--limit", type=int, default=100, help="Max listings to process (default: 100 for validation)")
    p.add_argument("--out", default=str(OUT_FILE), help="Output JSONL path")
    p.add_argument("--resume", action="store_true", help="Skip zpids already in output file")
    p.add_argument("--dry-run", action="store_true", help="Show what would be fetched, don't make real API calls")
    p.add_argument("--reddit-only", action="store_true", help="Skip Yelp (useful when under daily limit)")
    p.add_argument("--yelp-only", action="store_true", help="Skip Reddit")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    if args.verbose:
        LOG.setLevel(logging.DEBUG)

    out_path = Path(args.out)

    # Validate credentials
    if not args.dry_run:
        if not args.yelp_only and (not args.reddit_client_id or not args.reddit_client_secret):
            LOG.error(
                "Reddit credentials required. Set --reddit-client-id / --reddit-client-secret "
                "or REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET env vars.\n"
                "Apply at: https://www.reddit.com/prefs/apps (script type)"
            )
            sys.exit(1)
        if not args.reddit_only and not args.yelp_api_key:
            LOG.error(
                "Yelp API key required. Set --yelp-api-key or YELP_API_KEY env var.\n"
                "Apply at: https://www.yelp.com/developers/v3/manage_app"
            )
            sys.exit(1)

    # Load listings
    listings = _load_listings(limit=args.limit)

    # Resume: skip already-scraped zpids
    if args.resume:
        done = _load_existing_zpids(out_path)
        before = len(listings)
        listings = [L for L in listings if L.zpid not in done]
        LOG.info("Resume: skipping %d already-scraped zpids, %d remaining", before - len(listings), len(listings))

    if not listings:
        LOG.info("Nothing to scrape.")
        return

    # Init clients
    reddit: RedditClient | None = None
    yelp: YelpClient | None = None

    if not args.dry_run and not args.yelp_only:
        reddit = RedditClient(args.reddit_client_id, args.reddit_client_secret, args.reddit_user_agent)
    if not args.dry_run and not args.reddit_only:
        yelp = YelpClient(args.yelp_api_key)

    yelp_calls = [0]  # mutable day-counter
    total_reviews = 0

    LOG.info("Processing %d listings → %s", len(listings), out_path)

    for i, listing in enumerate(listings, 1):
        LOG.info("[%d/%d] %s (%s)", i, len(listings), listing.name, listing.zpid)
        reviews: list[dict] = []

        if not args.yelp_only:
            r_reviews = _reddit_reviews(reddit, listing, dry_run=args.dry_run)
            reviews.extend(r_reviews)
            if r_reviews:
                LOG.info("  Reddit: %d items", len(r_reviews))

        if not args.reddit_only:
            y_reviews = _yelp_reviews(yelp, listing, dry_run=args.dry_run, yelp_calls=yelp_calls)
            reviews.extend(y_reviews)
            if y_reviews:
                LOG.info("  Yelp: %d items", len(y_reviews))

        if reviews and not args.dry_run:
            _append_reviews(out_path, reviews)

        total_reviews += len(reviews)

    LOG.info(
        "Done. Processed %d listings, collected %d reviews → %s",
        len(listings), total_reviews, out_path,
    )
    if args.dry_run:
        LOG.info("(dry run — no files written, no API calls made)")


if __name__ == "__main__":
    main()
