#!/usr/bin/env python3
"""Batch-extract neighborhood + building mentions and sentiment from the
Reddit corpus (tools/data/reddit_posts.jsonl). Produces two outputs:

  api/data/reddit_neighborhood_sentiment.jsonl   ← driven into ranker (C)
  api/data/reddit_building_mentions.jsonl        ← used for social proof (D)

Each Reddit post is sent through Claude Haiku once. Posts are processed
serially with a small concurrent queue (asyncio gather of 6) to keep API
spend under ~$0.20 total.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POSTS = ROOT / "tools" / "data" / "reddit_posts.jsonl"
OUT_NEIGH = ROOT / "api" / "data" / "reddit_neighborhood_sentiment.jsonl"
OUT_BUILD = ROOT / "api" / "data" / "reddit_building_mentions.jsonl"

# Load ANTHROPIC_API_KEY from api/.env
env_path = ROOT / "api" / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        if line.startswith("ANTHROPIC_API_KEY=") and "ANTHROPIC_API_KEY" not in os.environ:
            os.environ["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip()

try:
    from anthropic import AsyncAnthropic
except ImportError:
    print("pip install anthropic", file=sys.stderr)
    sys.exit(1)

EXTRACT_PROMPT = """You read a Reddit post about Bay Area apartment hunting and extract structured signals.

Return a JSON object with two arrays:

{{
  "neighborhoods": [
    {{"name": "<lowercase city or neighborhood>", "sentiment": "positive|negative|mixed", "quote": "<short verbatim excerpt, max 150 chars>"}}
  ],
  "buildings": [
    {{"name": "<proper-case building name as referenced>", "sentiment": "positive|negative|mixed", "quote": "<short verbatim, max 150 chars>"}}
  ]
}}

Rules:
- ONLY include neighborhoods explicitly named (e.g. "Willow Glen", "Tenderloin", "Berryessa"). Do NOT include broad regions like "South Bay" or "Bay Area".
- ONLY include named apartment buildings/complexes (e.g. "Miro", "Avalon Willow Glen", "The Fay"). NOT generic phrases like "my apartment" or "the place I'm in".
- "sentiment" should reflect the post author's stance toward that neighborhood/building, not general background.
- If a neighborhood/building is mentioned but no clear sentiment, OMIT it (don't guess).
- "quote" should be a real substring from the post that supports the sentiment.
- If nothing qualifies, return empty arrays.
- Output ONLY the JSON object, no prose, no fences.

POST_TITLE: {title}

POST_BODY (truncated to 4000 chars):
{body}
"""


def _normalize_name(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s


async def extract_one(client: AsyncAnthropic, post: dict) -> dict:
    title = post.get("title", "")[:300]
    body = (post.get("selftext", "") or "")[:4000]
    prompt = EXTRACT_PROMPT.format(title=title, body=body)
    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        # Strip optional code fence
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:].strip()
        data = json.loads(raw)
    except Exception as e:
        print(f"  post {post.get('id')} extract failed: {e}", file=sys.stderr)
        return {"neighborhoods": [], "buildings": []}
    return {
        "neighborhoods": data.get("neighborhoods") or [],
        "buildings": data.get("buildings") or [],
    }


async def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY missing", file=sys.stderr)
        sys.exit(1)
    client = AsyncAnthropic()

    posts = []
    with POSTS.open() as f:
        for line in f:
            try:
                posts.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    print(f"Extracting from {len(posts)} Reddit posts (6-way concurrent)...")

    sem = asyncio.Semaphore(6)
    results: list[tuple[dict, dict]] = []

    async def run(p):
        async with sem:
            r = await extract_one(client, p)
            return p, r

    chunks = await asyncio.gather(*[run(p) for p in posts])
    for p, r in chunks:
        results.append((p, r))

    # Aggregate neighborhood sentiment
    neigh_acc: dict[str, dict] = defaultdict(
        lambda: {"name": "", "positive": 0, "negative": 0, "mixed": 0, "quotes": []}
    )
    build_index: dict[str, list[dict]] = defaultdict(list)

    SENTIMENT_TO_SCORE = {"positive": 1, "negative": -1, "mixed": 0}

    for post, ext in results:
        for n in ext["neighborhoods"]:
            name = _normalize_name(n.get("name"))
            if not name:
                continue
            sent = n.get("sentiment", "mixed")
            acc = neigh_acc[name]
            acc["name"] = name
            if sent in ("positive", "negative", "mixed"):
                acc[sent] += 1
            quote = n.get("quote", "").strip()
            if quote and len(acc["quotes"]) < 5:
                acc["quotes"].append({
                    "sentiment": sent,
                    "quote": quote[:200],
                    "subreddit": post.get("subreddit"),
                    "permalink": post.get("permalink"),
                    "post_id": post.get("id"),
                })
        for b in ext["buildings"]:
            name = _normalize_name(b.get("name"))
            if not name:
                continue
            build_index[name].append({
                "building": name,
                "display_name": b.get("name", "").strip(),
                "sentiment": b.get("sentiment", "mixed"),
                "quote": (b.get("quote") or "").strip()[:200],
                "subreddit": post.get("subreddit"),
                "permalink": post.get("permalink"),
                "post_id": post.get("id"),
                "score": post.get("score", 0),
            })

    # Compute aggregate sentiment_score per neighborhood:
    #   (positive - negative) / (positive + negative + mixed)
    #   Range [-1, 1]; we'll map to [0, 10] in ranker.
    OUT_NEIGH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_NEIGH.open("w", encoding="utf-8") as fout:
        for name, acc in sorted(neigh_acc.items()):
            total = acc["positive"] + acc["negative"] + acc["mixed"]
            if total < 1:
                continue
            score = (acc["positive"] - acc["negative"]) / max(total, 1)
            fout.write(json.dumps({
                "neighborhood": name,
                "sentiment_score": round(score, 3),
                "positive_count": acc["positive"],
                "negative_count": acc["negative"],
                "mixed_count": acc["mixed"],
                "total_mentions": total,
                "quotes": acc["quotes"],
            }, ensure_ascii=False) + "\n")
    print(f"Wrote {sum(1 for _ in OUT_NEIGH.open())} neighborhoods to {OUT_NEIGH}")

    with OUT_BUILD.open("w", encoding="utf-8") as fout:
        for name, mentions in sorted(build_index.items()):
            fout.write(json.dumps({
                "building": name,
                "mentions": sorted(mentions, key=lambda m: -m.get("score", 0)),
                "mention_count": len(mentions),
            }, ensure_ascii=False) + "\n")
    print(f"Wrote {sum(1 for _ in OUT_BUILD.open())} buildings to {OUT_BUILD}")


if __name__ == "__main__":
    asyncio.run(main())
