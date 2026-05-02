"""Reddit harvester — Sprint 2, Step 1.

Fetches apartment-hunting posts from a curated set of Bay Area subreddits
using Reddit's public JSON API (no OAuth required for public reads).
Saves deduplicated raw posts to tools/data/reddit_posts.jsonl.

Usage:
    python tools/harvest_reddit.py                 # default: ~600 posts
    python tools/harvest_reddit.py --max-per-q 50  # smaller sample
    python tools/harvest_reddit.py --dry-run       # show what would be fetched
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Iterable

import httpx

OUT_DIR = Path(__file__).resolve().parent / "data"
OUT_FILE = OUT_DIR / "reddit_posts.jsonl"

# Bay Area-relevant subreddits where apartment-hunting posts appear.
SUBREDDITS = [
    "SanJose",
    "sanfrancisco",
    "BayAreaRealEstate",
    "SiliconValley",
    "AskSF",
    "oakland",
    "bayarea",
]

# Apartment-hunting query terms — Reddit's search is full-text.
QUERIES = [
    "looking for apartment",
    "moving to",
    "rental advice",
    "apartment recommendation",
    "best neighborhood",
    "where to live",
]

# Reddit requires a UA per their API rules; spoofing 'python:' is OK.
UA = "rentwise-research:v0.1 (by u/maowen)"

# Politeness: 1 req/sec keeps us well under Reddit's unauthenticated limits.
THROTTLE_S = 1.0


def is_relevant_post(p: dict) -> bool:
    """Heuristic gate before we spend an LLM call later."""
    if p.get("is_video"):
        return False
    if not p.get("selftext") or len(p["selftext"]) < 100:
        # Need actual text content (link posts and one-liners are useless)
        return False
    if p.get("locked") or p.get("removed_by_category"):
        return False
    title = (p.get("title") or "").lower()
    body = (p.get("selftext") or "").lower()
    blob = title + " " + body
    # Strong relevance signals
    for term in (
        "apartment", "rental", "rent ", "renting", "studio", "1br", "2br",
        "1 bed", "2 bed", "looking for a place", "moving to", "sublet",
        "lease", "landlord", "leasing",
    ):
        if term in blob:
            return True
    return False


def fetch(client: httpx.Client, url: str, params: dict | None = None) -> dict:
    """GET with retries on 429/5xx."""
    for attempt in range(3):
        try:
            r = client.get(url, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503):
                wait = 2 * (attempt + 1)
                print(f"  rate-limit/{r.status_code}, sleeping {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"  fetch failed: {r.status_code} {r.text[:120]}", file=sys.stderr)
            return {}
        except httpx.HTTPError as e:
            print(f"  fetch error: {e}", file=sys.stderr)
            time.sleep(2)
    return {}


def harvest_search(client: httpx.Client, sub: str, query: str, limit: int) -> Iterable[dict]:
    """Use Reddit search within a subreddit."""
    url = f"https://www.reddit.com/r/{sub}/search.json"
    params = {
        "q": query,
        "restrict_sr": "1",
        "sort": "relevance",
        "t": "year",
        "limit": str(limit),
    }
    data = fetch(client, url, params)
    children = (data.get("data") or {}).get("children") or []
    for c in children:
        post = c.get("data") or {}
        if is_relevant_post(post):
            yield post


def harvest_top(client: httpx.Client, sub: str, limit: int = 100) -> Iterable[dict]:
    """Pull top posts from a subreddit (catches popular ones search may miss)."""
    url = f"https://www.reddit.com/r/{sub}/top.json"
    params = {"t": "year", "limit": str(limit)}
    data = fetch(client, url, params)
    children = (data.get("data") or {}).get("children") or []
    for c in children:
        post = c.get("data") or {}
        if is_relevant_post(post):
            yield post


def slim(p: dict) -> dict:
    """Trim a Reddit post to the fields we'll actually use downstream.
    Critically — drop `author` and other PII to keep our dataset clean."""
    return {
        "id": p.get("id"),
        "subreddit": p.get("subreddit"),
        "title": p.get("title"),
        "selftext": p.get("selftext"),
        "score": p.get("score"),
        "num_comments": p.get("num_comments"),
        "created_utc": p.get("created_utc"),
        "permalink": f"https://reddit.com{p.get('permalink', '')}" if p.get("permalink") else None,
        "url": p.get("url"),
        "over_18": p.get("over_18", False),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-per-q", type=int, default=50,
                    help="Max posts per (subreddit, query) pair")
    ap.add_argument("--include-top", action="store_true", default=True,
                    help="Also pull top-of-year posts per subreddit")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show plan without fetching")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    plan = []
    for sub in SUBREDDITS:
        for q in QUERIES:
            plan.append(("search", sub, q))
        if args.include_top:
            plan.append(("top", sub, "(top-of-year)"))
    print(f"Plan: {len(plan)} fetch requests across {len(SUBREDDITS)} subs.")
    if args.dry_run:
        for kind, sub, q in plan:
            print(f"  {kind:6} r/{sub:25} {q}")
        return 0

    seen: set[str] = set()
    out_records: list[dict] = []

    # Resume: load existing ids if file already exists, dedupe against them
    if OUT_FILE.exists():
        with OUT_FILE.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    if rec.get("id"):
                        seen.add(rec["id"])
                        out_records.append(rec)
                except json.JSONDecodeError:
                    pass
        print(f"Resuming with {len(seen)} previously-saved posts.")

    headers = {"User-Agent": UA}
    with httpx.Client(headers=headers, follow_redirects=True) as client:
        for i, (kind, sub, q) in enumerate(plan, 1):
            print(f"[{i:>3}/{len(plan)}] {kind:6} r/{sub:22} {q}")
            if kind == "search":
                gen = harvest_search(client, sub, q, args.max_per_q)
            else:
                gen = harvest_top(client, sub, args.max_per_q)
            new_count = 0
            for post in gen:
                pid = post.get("id")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                out_records.append(slim(post))
                new_count += 1
            print(f"      → {new_count} new posts ({len(out_records)} total)")
            time.sleep(THROTTLE_S)

    # Write the full deduplicated set back atomically
    tmp = OUT_FILE.with_suffix(".jsonl.tmp")
    with tmp.open("w") as f:
        for r in out_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(OUT_FILE)

    print(f"\n✓ Saved {len(out_records)} posts → {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
