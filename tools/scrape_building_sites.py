"""Building-website scraper — Step 3 of the data enrichment pipeline.

For a given list of (zpid, building_url) pairs, fetches each building's
official website and extracts contact info that Zillow doesn't expose:
  • leasing-office email
  • full property-manager / contact name
  • office hours (open/close times by day)
  • alternate phone, text line
  • sister-property links

Why this matters: Zillow's `buildingPhoneNumber` is the only contact
channel they expose. Outreach Agent's email-via-Gmail flow needs a real
mailto address, which we can only get by visiting the property's own
website.

URL DISCOVERY IS A SEPARATE CONCERN — Zillow doesn't include the building's
external website in its data path (we verified — `housingConnector.hcLink`
is null in 95%+ of records). So this script EXPECTS a CSV input that
maps zpid → URL. Three ways to populate that CSV:
  1. Manual: visit Zillow listing → click "Visit Website" if shown → copy
  2. Programmatic via Google Places API (paid):
       places.text_search(f"{building_name} apartments {city}")
     → take the website field. ~$0.017 per call × 2000 buildings = $34.
  3. LLM-guess: Claude predicts a plausible URL from name+address, then
     we verify by fetching and checking the page contains the building name.
     ~$0.001/call × 2000 = $2. Lower hit rate (~40%) but cheap.

This script focuses on the SCRAPING half once URLs are known. URL discovery
is its own subproject — see TODO at bottom.

Usage:
    python tools/scrape_building_sites.py \\
        --urls-csv tools/data/building_urls.csv \\
        --out tools/data/building_contacts.jsonl

Input CSV format (header required):
    zpid,building_url
    2058946048,https://246s12thstreet.com
    443795645,https://thefay-sj.com
    ...

Output JSONL: one record per zpid with extracted contact fields.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

# Common paths apartment websites use for contact info.
CONTACT_PATHS = [
    "/contact", "/contact-us", "/contact_us",
    "/leasing", "/leasing-office",
    "/about", "/about-us",
    "/get-in-touch",
    "/team",
    "/visit",
    "/info",
    "/",  # homepage too — many include the email at the bottom
]

# Email regex — RFC-lite, good enough for visible emails on apartment sites.
EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)

# Phone regex — US-formatted variants
PHONE_RE = re.compile(
    r"""
    \(?\b(\d{3})\)?            # area code, optional parens
    [\s\-.]?
    (\d{3})                     # 3 digits
    [\s\-.]?
    (\d{4})\b                   # 4 digits
    """,
    re.VERBOSE,
)

# Hours patterns — very common formats
HOURS_RE = re.compile(
    r"""
    (?:
      (?:Mon(?:day)?|Tue(?:s|sday)?|Wed(?:nesday)?|Thu(?:r|rs|rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)
      [\s\-–]*
      (?:Mon(?:day)?|Tue(?:s|sday)?|Wed(?:nesday)?|Thu(?:r|rs|rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)?
    )
    [:\s]*
    (\d{1,2}(?::\d{2})?\s*(?:[AaPp]\.?[Mm]\.?))
    \s*[\-–to]+\s*
    (\d{1,2}(?::\d{2})?\s*(?:[AaPp]\.?[Mm]\.?))
    """,
    re.VERBOSE,
)

# Things we DON'T want to capture as the leasing email
EMAIL_BLOCKLIST_PATTERNS = [
    r".*@(sentry|gmail|googlemail|example|test|placeholder|localhost)\.",
    r"^(noreply|no-reply|donotreply|webmaster|abuse|postmaster)@",
    r".*\.(png|jpg|svg|gif)$",
    r"\.(wixpress|squarespace|webflow|godaddy)\.com",
]
EMAIL_BLOCKLIST = [re.compile(p, re.IGNORECASE) for p in EMAIL_BLOCKLIST_PATTERNS]


@dataclass
class BuildingContact:
    zpid: str
    building_url: str
    fetched_pages: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    hours_snippets: list[str] = field(default_factory=list)
    error: str | None = None


def is_useful_email(e: str) -> bool:
    el = e.lower()
    if any(p.search(el) for p in EMAIL_BLOCKLIST):
        return False
    return True


def extract_from_html(html: str, base_url: str) -> tuple[set[str], set[str], set[str]]:
    """Return (emails, phones, hours_snippets) found in a page's HTML."""
    # Strip script + style content (often contains tracking emails / phones)
    cleaned = re.sub(
        r"<(script|style|noscript)[^>]*>.*?</\1>",
        " ",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # Emails: combine mailto: links + visible text (after stripping tags)
    mailto_emails = set(
        m.group(1).strip()
        for m in re.finditer(
            r'href="mailto:([^"?]+)', cleaned, re.IGNORECASE
        )
    )
    text_only = re.sub(r"<[^>]+>", " ", cleaned)
    visible_emails = set(EMAIL_RE.findall(text_only))
    all_emails = {e for e in (mailto_emails | visible_emails) if is_useful_email(e)}

    # Phones
    phones: set[str] = set()
    for m in PHONE_RE.finditer(text_only):
        phones.add(f"({m.group(1)}) {m.group(2)}-{m.group(3)}")

    # Hours snippets — find lines/spans that look like office hours
    hours_snippets: set[str] = set()
    for m in HOURS_RE.finditer(text_only):
        # Capture some surrounding context for human verification
        start, end = m.span()
        snippet = text_only[max(0, start - 30):min(len(text_only), end + 30)]
        snippet = re.sub(r"\s+", " ", snippet).strip()
        if 10 < len(snippet) < 200:
            hours_snippets.add(snippet)

    return all_emails, phones, hours_snippets


async def fetch_one(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        r = await client.get(url, timeout=10.0, follow_redirects=True)
        if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
            return r.text
    except (httpx.HTTPError, httpx.InvalidURL):
        pass
    return None


async def scrape_building(
    client: httpx.AsyncClient, zpid: str, building_url: str
) -> BuildingContact:
    """Try the homepage + common contact paths; aggregate findings."""
    out = BuildingContact(zpid=zpid, building_url=building_url)

    # Normalise URL
    parsed = urlparse(building_url)
    if not parsed.scheme:
        building_url = "https://" + building_url

    all_emails: set[str] = set()
    all_phones: set[str] = set()
    all_hours: set[str] = set()

    for path in CONTACT_PATHS:
        full = urljoin(building_url, path)
        html = await fetch_one(client, full)
        if html is None:
            continue
        out.fetched_pages.append(full)
        emails, phones, hours = extract_from_html(html, full)
        all_emails |= emails
        all_phones |= phones
        all_hours |= hours
        # Two pages with content is usually plenty — bail to save bandwidth
        if len(out.fetched_pages) >= 3 and all_emails:
            break

    if not out.fetched_pages:
        out.error = "no pages fetched (404 / unreachable / SPA-only / blocked)"

    out.emails = sorted(all_emails)
    out.phones = sorted(all_phones)
    out.hours_snippets = sorted(all_hours)[:5]  # cap to 5 most likely
    return out


async def run(urls_csv: Path, out_path: Path, concurrency: int = 8) -> None:
    pairs: list[tuple[str, str]] = []
    with urls_csv.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            zpid = (row.get("zpid") or "").strip()
            url = (row.get("building_url") or "").strip()
            if zpid and url:
                pairs.append((zpid, url))

    print(f"Loaded {len(pairs)} (zpid, url) pairs")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(
        headers={
            # Polite UA so apartment sites' WAFs don't flag us
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
    ) as client:

        async def task(zpid: str, url: str) -> BuildingContact:
            async with sem:
                return await scrape_building(client, zpid, url)

        tasks = [task(z, u) for z, u in pairs]
        n_with_email = 0
        with out_path.open("w", encoding="utf-8") as out_f:
            for i, coro in enumerate(asyncio.as_completed(tasks), 1):
                rec = await coro
                out_f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
                if rec.emails:
                    n_with_email += 1
                if i % 25 == 0 or i == len(tasks):
                    print(
                        f"  [{i}/{len(tasks)}]  emails captured for {n_with_email} "
                        f"(rate {n_with_email/i:.0%})"
                    )

    print(f"\n✓ Done → {out_path}")
    print(f"  total: {len(pairs)} buildings")
    print(f"  with email: {n_with_email} ({n_with_email / max(1, len(pairs)):.0%})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--urls-csv", type=Path, required=True,
                    help="CSV with header `zpid,building_url`")
    ap.add_argument("--out", type=Path,
                    default=Path(__file__).resolve().parent / "data" / "building_contacts.jsonl")
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()

    if not args.urls_csv.exists():
        print(f"ERROR: --urls-csv not found: {args.urls_csv}", file=sys.stderr)
        print()
        print("Create one with header `zpid,building_url` then re-run.", file=sys.stderr)
        print("Hint: see tools/data/building_urls.example.csv", file=sys.stderr)
        return 1

    asyncio.run(run(args.urls_csv, args.out, args.concurrency))
    return 0


# -------------------------------------------------------------------------
#  TODO — URL discovery (separate subproject)
# -------------------------------------------------------------------------
# Three approaches we considered:
#
# 1. Google Places API "Find Place from Text"
#       endpoint: places.googleapis.com/v1/places:searchText
#       query: f"{building_name} apartments {city} {state}"
#       returns:`websiteUri` field for each match
#       cost: ~$0.017 per call × 2000 buildings = ~$34 one-time
#       hit rate: 80–90% expected (Google has good coverage of apartment buildings)
#
# 2. LLM URL-guess + verify
#       Claude predicts plausible URL ("https://thefay-sj.com" from "The Fay" + address)
#       Verify by fetching → check if page text contains the building name + address ZIP
#       cost: ~$0.001 per call × 2000 = $2
#       hit rate: 30–40% expected
#
# 3. Crowdsource / curate
#       Build URL list manually from the 100 highest-traffic listings
#       Augment over time as users report missing data
#       cost: human time, $0
#
# For v1, start with (1) Google Places API — best ROI. The discovered
# URLs go straight into building_urls.csv and this script handles the rest.

if __name__ == "__main__":
    sys.exit(main())
