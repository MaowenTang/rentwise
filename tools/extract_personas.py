"""Persona extractor — Sprint 2, Step 2.

Reads tools/data/reddit_posts.jsonl, asks Claude Sonnet to extract a
structured renter persona from each post (matching our OnboardingResult
schema), saves to tools/data/personas.jsonl.

The extractor explicitly returns is_relevant=false for posts that aren't
first-person apartment hunting (landlord posts, jokes, complaints with
no preferences expressed, etc.) — those are filtered out.

Usage:
    python tools/extract_personas.py                 # process all new posts
    python tools/extract_personas.py --limit 20      # smoke test
    python tools/extract_personas.py --model opus    # use Opus (5× cost)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / "api" / ".env")

DATA_DIR = Path(__file__).resolve().parent / "data"
POSTS_FILE = DATA_DIR / "reddit_posts.jsonl"
OUT_FILE = DATA_DIR / "personas.jsonl"

DEFAULT_MODEL = "claude-sonnet-4-6"

EXTRACTION_PROMPT = """You read Reddit posts about apartment hunting and extract a STRUCTURED RENTER PERSONA from each one.

Return ONLY a JSON object with this exact shape:

{{
  "is_relevant": boolean,    // true ONLY if poster is themselves looking for a rental and expresses preferences
  "reason_skip": "string|null",  // if false: brief reason ("landlord post" / "complaint only" / "not specific" / etc.)
  "persona": {{
    "summary": "one-sentence persona description",
    "budget_max": int|null,    // monthly rent USD; infer from explicit mentions
    "beds_min": int|null,      // 0 = studio
    "beds_max": int|null,      // null if open-ended
    "pets": ["dogs"|"cats"]|[],
    "commute_target": "string|null",  // employer name or neighborhood (e.g. "Apple", "Stanford", "downtown SF")
    "must_haves": ["string", ...],    // explicit non-negotiables ("in-unit laundry", "parking", "pool")
    "avoid": ["string", ...],         // explicit deal-breakers ("thin walls", "carpet", "no parking")
    "neighborhoods": ["string", ...], // preferred neighborhoods/cities mentioned
    "life_stage": "string|null",      // "student" / "young professional" / "couple" / "family" / etc.
    "implicit_signals": ["string", ...],  // softer signals derived from context
    "importance_ranking": ["string", ...] // ORDER the following 6 keys by what matters most to THIS poster:
        // budget, commute, pets, amenities, walkable, transit
        // Use emphasis cues: "non-negotiable" / "must" / repeated mentions = high
        // "would be nice" / "ideally" / "if possible" = low
        // If the post doesn't give clear signal, infer from life_stage and content
  }}
}}

CRITICAL RULES:
1. If the post is from a LANDLORD, AGENT, COMPLAINT-ONLY (no preferences), or about something else (not apartment hunting), return {{"is_relevant": false, "reason_skip": "...", "persona": null}}.
2. Only fill fields the post actually mentions or strongly implies. Do NOT make up budget if not stated. NULL is better than fabricated.
3. The importance_ranking must be a permutation of [budget, commute, pets, amenities, walkable, transit] — all 6, no duplicates.
4. Output ONLY the JSON object — no prose, no markdown fences.

POST:
Subreddit: r/{subreddit}
Title: {title}

Body:
{body}
"""


def call_claude(client: Anthropic, model: str, post: dict) -> dict | None:
    title = (post.get("title") or "").strip()[:200]
    body = (post.get("selftext") or "").strip()
    if len(body) > 4000:
        body = body[:4000] + "\n[...truncated]"

    prompt = EXTRACTION_PROMPT.format(
        subreddit=post.get("subreddit") or "?",
        title=title,
        body=body,
    )
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        return json.loads(text)
    except Exception as e:
        print(f"    [error] {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap how many posts to process (for testing)")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"Claude model (default: {DEFAULT_MODEL})")
    ap.add_argument("--start", type=int, default=0,
                    help="Skip first N posts (resume)")
    args = ap.parse_args()

    if not POSTS_FILE.exists():
        print(f"ERROR: {POSTS_FILE} not found. Run harvest_reddit.py first.", file=sys.stderr)
        return 1

    if not os.environ.get("ANTHROPIC_API_KEY", "").startswith("sk-"):
        print("ERROR: ANTHROPIC_API_KEY not set in api/.env", file=sys.stderr)
        return 1

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    client = Anthropic()

    # Load all input posts
    posts: list[dict] = []
    with POSTS_FILE.open() as f:
        for line in f:
            try:
                posts.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    print(f"Loaded {len(posts)} posts from {POSTS_FILE.name}")

    # Resume — load already-processed ids
    done: set[str] = set()
    out_records: list[dict] = []
    if OUT_FILE.exists():
        with OUT_FILE.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    if rec.get("post_id"):
                        done.add(rec["post_id"])
                        out_records.append(rec)
                except json.JSONDecodeError:
                    pass
        print(f"Already processed: {len(done)}. Will skip those.")

    pending = [p for p in posts if p.get("id") and p["id"] not in done]
    if args.start:
        pending = pending[args.start:]
    if args.limit:
        pending = pending[: args.limit]
    print(f"Will process {len(pending)} posts using {args.model}")
    print()

    n_relevant = sum(1 for r in out_records if r.get("is_relevant"))
    n_skipped = len(out_records) - n_relevant
    n_failed = sum(1 for r in out_records if r.get("error"))

    for i, post in enumerate(pending, 1):
        pid = post["id"]
        title = (post.get("title") or "")[:60]
        print(f"[{i:>4}/{len(pending)}] {post.get('subreddit'):20} {pid:8} {title}")

        result = call_claude(client, args.model, post)
        rec: dict = {
            "post_id": pid,
            "subreddit": post.get("subreddit"),
            "permalink": post.get("permalink"),
            "title": post.get("title"),
        }

        if result is None:
            rec["error"] = "extraction failed"
            rec["is_relevant"] = False
            n_failed += 1
        else:
            rec["is_relevant"] = bool(result.get("is_relevant"))
            rec["reason_skip"] = result.get("reason_skip")
            rec["persona"] = result.get("persona")
            if rec["is_relevant"]:
                n_relevant += 1
                p = rec.get("persona") or {}
                summary = (p.get("summary") or "")[:80]
                print(f"        ✓ {summary}")
            else:
                n_skipped += 1
                print(f"        ✗ skip: {rec.get('reason_skip')}")

        # Append immediately so a crash doesn't lose progress
        with OUT_FILE.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out_records.append(rec)

        # Polite throttle (Anthropic limit is generous, just being kind)
        time.sleep(0.2)

    print()
    print(f"Done. Total saved: {len(out_records)}")
    print(f"  ✓ relevant personas: {n_relevant}")
    print(f"  ✗ skipped: {n_skipped}")
    print(f"  ⚠ failed: {n_failed}")
    print(f"\nOutput: {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
