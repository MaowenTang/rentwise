# Tools — Sprint 2 Pipeline

Offline data-processing scripts that build the **synthetic training set** for the future two-tower reranker. NOT part of the live API service.

```
Reddit JSON API ──► harvest_reddit.py ──► reddit_posts.jsonl
                                                  │
                                                  ▼
                                    extract_personas.py ──► personas.jsonl
                                    (Claude Sonnet)
```

---

## Step 1 · Harvest Reddit posts

Pulls apartment-hunting threads from 7 Bay Area subreddits via Reddit's public JSON API (no auth needed, no PRAW dependency).

```bash
cd ~/Downloads/rentwise
source api/.venv/bin/activate

# Dry run — see what it would fetch
python tools/harvest_reddit.py --dry-run

# Real run — defaults to ~600 posts (50/query × 6 queries × 7 subs + tops)
python tools/harvest_reddit.py

# Smaller sample
python tools/harvest_reddit.py --max-per-q 20
```

Output: `tools/data/reddit_posts.jsonl` — one slim post per line:

```json
{"id":"abc123","subreddit":"SanJose","title":"Looking for 1BR near downtown",
 "selftext":"Hi all...","score":42,"num_comments":18,
 "created_utc":1707432000,"permalink":"https://reddit.com/r/...",...}
```

The harvester is **resumable** — re-running it skips post IDs already saved.

**Notes**
- 1 req/sec throttle keeps us under Reddit's unauthenticated rate limit.
- We strip `author` and other PII at ingest. Only the public post body, title, and metadata are kept.
- Resume-safe: re-run anytime to top up.

---

## Step 2 · Extract personas

For each Reddit post, Claude Sonnet 4.6 extracts a structured persona matching our `OnboardingResult` schema, including the **importance_ranking** that drives our weighted scorer.

```bash
# Smoke test on 20 posts (~$0.10)
python tools/extract_personas.py --limit 20

# Process everything not yet seen (~$0.005 per post — ~$3 for 600 posts)
python tools/extract_personas.py

# Use Opus instead of Sonnet (5× cost, marginal quality lift here)
python tools/extract_personas.py --model claude-opus-4-7
```

Output: `tools/data/personas.jsonl`. Each record:

```json
{
  "post_id": "abc123",
  "subreddit": "SanJose",
  "permalink": "https://reddit.com/r/...",
  "title": "Looking for 1BR near downtown",
  "is_relevant": true,
  "persona": {
    "summary": "Junior software engineer at Adobe, dog-friendly + walkable",
    "budget_max": 2800,
    "beds_min": 1,
    "beds_max": 1,
    "pets": ["dogs"],
    "commute_target": "Adobe",
    "must_haves": ["in-unit laundry"],
    "avoid": ["thin walls"],
    "neighborhoods": ["Downtown", "Willow Glen"],
    "life_stage": "young professional",
    "implicit_signals": ["bikes everywhere"],
    "importance_ranking": ["budget","walkable","pets","amenities","commute","transit"]
  }
}
```

Posts that aren't first-person apartment hunting (landlord posts, complaints, off-topic) get `is_relevant: false` with a `reason_skip` and are filtered out downstream.

The extractor is **resumable** — re-running skips post_ids already in the output file.

---

## Cost & runtime — first full pass

| Item | Estimate |
|---|---|
| harvest_reddit.py | 5–10 min, free |
| extract_personas.py on ~600 posts (Sonnet) | ~30 min, ~$3 |
| Yield (relevant personas after `is_relevant: true` filter) | ~250–400 |

If 250-400 personas isn't enough variety, run the harvester for more time periods or add subreddits, then re-run the extractor.

---

## Spot-check before trusting

Before feeding these personas into Sprint 3 (random retrieval + dual-judge labeling), sample-check ~30 of them:

```bash
# Pull 10 random relevant personas + their permalinks for human eyeball
python -c "
import json, random
recs = [json.loads(l) for l in open('tools/data/personas.jsonl')]
relevant = [r for r in recs if r.get('is_relevant')]
for r in random.sample(relevant, 10):
    print(f\"--- {r['post_id']} ---\")
    print(r['permalink'])
    print(json.dumps(r['persona'], indent=2, ensure_ascii=False))
    print()
"
```

If the persona feels off (wrong importance order, hallucinated budget, missed avoid), tweak the `EXTRACTION_PROMPT` in `extract_personas.py` and re-run on those specific post IDs.

---

## What's next (Sprint 3, not yet built)

- `tools/build_training_pairs.py` — for each persona, randomly sample 50 listings, run dual-judge (Opus structured + Opus full-text), keep agreed labels → `data/training_pairs.jsonl`
- `tools/train_two_tower.py` — fine-tune sentence-transformers on the labeled pairs
- A/B harness in the live API to compare heuristic vs trained ranker
