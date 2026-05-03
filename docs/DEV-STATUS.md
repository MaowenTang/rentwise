# RentWise — Development Status & Handoff

**Snapshot date**: 2026-05-03
**Latest commit**: `a22f0b4` on `main`
**Repo**: https://github.com/MaowenTang/rentwise (private)
**Live URL**: https://web-rentwise.vercel.app
**Backend URL**: https://rentwise-api-ug16.onrender.com

This document is the canonical "what's done / what's in flight / what's
next" reference for any developer or AI agent picking up the project.
Read this before touching code.

---

## TL;DR for new contributors

RentWise is a multi-agent rental advisory chat — 4 specialist LLM agents
(Search · Property · Location · Outreach) collaborate inside a single
chat room to help renters find Bay Area apartments. v0 is live and
functional. Mid-flight right now: a 3-4 hour Zillow scrape of San Francisco
to expand inventory beyond the 1,087 SJ-only baseline, plus offline tools
(`tools/enrichment_v2.py`, `tools/scrape_building_sites.py`) to pull richer
contact data for the Outreach Agent.

**Critical context** before changing anything:
- Backend is **stateful in-memory** (Render free tier). Spin-down loses
  sessions. Frontend mirrors profile + scope on every `/chat` and the
  backend re-hydrates — see §3 Architecture decisions.
- Vercel project is in the `rentwise` team; GitHub auto-deploy works
  because @MaowenTang is a team member (was the source of an earlier
  bug, fixed in commit `ba00dc4`).
- Data file `api/data/zillow_listings.jsonl.gz` (12 MB) is committed to
  git. The richer `zillow_listings_v2.jsonl.gz` (4.4 MB) is also
  committed but **not yet wired into the live API** — the loader still
  reads v1.

---

## 1. Live deployment topology

```
                                                 GitHub auto-deploy
                                                 (rentwise team)
                                                       │
            ┌──────────────────┐                       │
            │  github.com/     │  push to main         │
Browser ──► │  MaowenTang/     │ ─────────────────────►│
            │  rentwise        │                       │
            └──────────────────┘                       │
                                                       ↓
                          ┌────────────────────────────┴────────┐
                          │                                      │
                          ▼                                      ▼
                  ┌───────────────┐                    ┌──────────────────┐
                  │  Vercel       │                    │  Render (free)   │
                  │  rentwise/web │                    │  rentwise-api    │
                  │  (Next.js 16) │                    │  (FastAPI 0.115) │
                  │  Root: web/   │                    │  Root: api/      │
                  └───────┬───────┘                    │  Python 3.12.7   │
                          │                             └─────────┬────────┘
                          ▼                                       │
              https://web-rentwise.vercel.app  ─── /chat ─────────┘
                                                /profile/init
                                                /profile/remove
                                                /shortlist/*
                                                /healthz
```

**Render free tier behavior**: instance spins down after ~15 min of no
requests. First cold request takes ~50 sec to wake. Frontend
`fetchWithRetry` wraps every call with 60s + 90s retry timeouts, so
this is mostly transparent to users.

**Costs (current month, ~10 friend-testers usage)**:
- Vercel: $0 (Hobby tier)
- Render: $0 (free tier, $5 trial credit ~unused)
- Anthropic API: **~$1-3 so far** (heavy: SearchAgent rank, PropertyAnalyst
  multi-listing tables. Light: Router, ProfileUpdater)

---

## 2. Repo layout

```
rentwise/
├── api/                         # FastAPI backend
│   ├── main.py                  # endpoints: /chat /profile/init /profile/remove
│   │                            #            /shortlist/* /healthz
│   ├── agents/
│   │   ├── base.py              # BaseAgent + AgentReply dataclass
│   │   ├── router.py            # LLM-driven dispatcher (1 call/turn)
│   │   ├── search.py            # NL → filter → heuristic score → LLM rerank → render
│   │   ├── property.py          # multi-listing Q&A with rich markdown tables
│   │   ├── location.py          # walk/transit/schools/Haversine commute
│   │   └── outreach.py          # CAN-SPAM-compliant draft emails
│   ├── profile.py               # UserProfile, RankingService, ProfileUpdater,
│   │                            # CommuteTarget, EMPLOYER_HQ
│   ├── session.py               # in-memory SessionStore + Session dataclass
│   ├── listings.py              # JSONL loader + single_home normalizer + filter
│   ├── craigslist.py            # CSV loader + lat/lng dedup-merge into Zillow
│   ├── data/
│   │   ├── zillow_listings.jsonl.gz       # 12 MB, 1,087 SJ + Craigslist Bay Area
│   │   ├── zillow_listings_v2.jsonl.gz    # 4.4 MB, richer fields, NOT YET WIRED
│   │   └── craigslist_apartments.csv      # 596 KB, 352 Bay Area
│   ├── requirements.txt         # fastapi 0.115 / anthropic 0.39 / httpx<0.28
│   ├── runtime.txt              # python-3.12.7 (Render reads this)
│   ├── nixpacks.toml            # Render build config
│   ├── railway.json             # legacy, unused
│   ├── Procfile                 # `web: uvicorn main:app --host 0.0.0.0 --port $PORT`
│   ├── .python-version          # 3.12.7
│   └── .env                     # ANTHROPIC_API_KEY (gitignored)
│
├── web/                         # Next.js 16 frontend
│   ├── app/
│   │   ├── page.tsx             # ALL UI in one file: chat, sidebar, shortlist,
│   │   │                        # onboarding modal, mobile drawers, message rendering
│   │   ├── layout.tsx
│   │   └── globals.css          # Tailwind + custom prose tweaks
│   ├── package.json             # next 16.2.4, react-markdown, remark-gfm
│   ├── next.config.ts
│   └── .env.local               # NEXT_PUBLIC_API_URL=...
│
├── tools/                       # Offline data-processing scripts (not in API path)
│   ├── harvest_reddit.py        # Sprint 2: pulls apartment-hunt posts via Reddit JSON API
│   ├── extract_personas.py      # Sprint 2: Claude Sonnet → structured persona JSON
│   ├── enrichment_v2.py         # NEW: re-extract richer fields from raw_details/*.json
│   ├── scrape_building_sites.py # NEW: fetches building URLs for email/hours
│   ├── data/
│   │   ├── reddit_posts.jsonl   # 132 posts harvested
│   │   ├── personas.jsonl       # 10 sample personas extracted
│   │   └── building_urls.example.csv
│   └── README.md
│
├── docs/
│   ├── superpowers/specs/
│   │   ├── 2026-05-01-rentwise-design.md   # full v1 design spec (724 lines)
│   │   └── 2026-05-01-v0-spec.md           # v0 implementation spec
│   └── DEV-STATUS.md            # ← you are here
│
├── DEPLOY.md                    # how to deploy to Vercel + Render
├── README.md                    # quickstart
└── .gitignore
```

---

## 3. Architecture decisions (and the bugs that motivated them)

### 3.1 Backend session is a transparent cache, frontend is source of truth

**Decision**: every `/chat` request from the frontend includes
`client_profile` and `client_scope_zpids` mirrors. If the backend session
is empty (i.e., Render restarted), it re-hydrates from the client.

**Why**: Render free tier spins down after 15 min idle. In-memory
`SessionStore` loses everything. Without rehydration, the user fills out
onboarding, gets recommendations, leaves the tab open, comes back 20 min
later — the agent re-asks budget like nothing happened.

**Files**: `api/main.py:_hydrate_session_from_client`,
`web/app/page.tsx:sendText`. Hydration triggers when `profile_empty AND
client_profile`. See commit `d8baa0d`.

### 3.2 Heuristic ranking, not learned ranking

**Decision**: `RankingService.score()` is component-weighted Python
(budget, beds, pets, must_haves, commute, walk, transit, neighborhood,
nice_to_haves, avoid). User-customizable weights from onboarding's
`importance_ranking` (rank 1 → 4.0, rank 2 → 3.0, ..., rank 6 → 0.5).

**Why we explicitly chose not to do two-tower or LTR**: see
`docs/superpowers/specs/2026-05-01-v0-spec.md` §6. Short version: zero
training data, interpretability requirement, MVP cold-start economics.

**Roadmap**: Sprint 3 (in `tools/`) builds the synthetic training set.
Sprint 4 trains a two-tower fine-tune of `bge-small-en-v1.5` on the
labeled (persona, listing, label) triples.

### 3.3 Coarse-rank semantic blend (planned, not built)

**Decision**: planned upgrade — add sentence-transformer (bge) cosine
as one component in `RankingService.DEFAULT_WEIGHTS` (weight 2.0). Hard
filter unchanged; LLM rerank unchanged. Captures qualitative intent
("vintage charm", "work-from-home friendly") that the structured
heuristic misses.

**Why now**: explicitly debated in the chat — "粗排=model, 精排=agent".
Conclusion: yes for 粗排 (4-6h work, 0 cost), defer 精排-as-agent until
Maps API + schools API are wired (otherwise marginal lift).

**Status**: agreed but not started. Next contributor pickup: see
"What's planned" §6.

### 3.4 Onboarding flushes pending chip text on submit

**Decision**: when user types "in-unit laundry" in the must-have field
but doesn't press Enter, `submit()` flushes `mustInput` into the array
before sending.

**Why**: real bug reported by user — onboarding's must_haves came back
empty because they didn't press Enter on each chip.

**File**: `web/app/page.tsx` inside the `OnboardingQuestionnaire`
component. Also strips `STARTER_CHIPS` off the welcome message after
submit so they don't contradict the user's actual budget. See `876dde6`.

### 3.5 Profile updater handles negation

**Decision**: ProfileUpdater (a Sonnet call after every user turn) emits
both `*_add` and `*_remove` patches. When user says "actually no Trader
Joe's", it both ADDS to `avoid` AND REMOVES the stale "near Trader Joe's"
from `nice_to_haves`.

**Why**: bug — user contradicted themselves and saw both old and new
chips.

**File**: `api/profile.py:UPDATE_PROMPT` + `_apply_patch`.

### 3.6 Multi-modal data merging

`api/listings.py:load_listings()` runs this pipeline at API startup:

1. Read `zillow_listings.jsonl.gz` (910 raw Zillow records)
2. Drop empty-shell records, normalize 620 `single_home_raw` records to
   apartment-shape (mapping `raw.property.*` → top-level fields)
3. Read `craigslist_apartments.csv` (352 Bay Area)
4. **Spatial dedup**: Haversine ≤ 50m + bed-overlap match
5. Merge: Zillow data wins where populated, Craigslist fills gaps
   (description, phone, lease terms, additional floor plans, deposits)
6. Tag each record's sources in `_data_sources: ["apartment_building",
   "craigslist"]`

Final state: **1,087 records** (256 apt + 620 home + 209 CL new + 2 merged).

### 3.7 Agent routing — Chinese-aware, with pending-clarification stickiness

**Decision**: Router prompt includes Chinese examples for each agent's
trigger keywords. After an agent asks a clarifying question, the next
user message is biased to route back to that same agent.

**Why**: user typed "有停车费吗" and it routed to Search instead of
Property because router prompt was English-only. Fixed in `d8baa0d`.

**File**: `api/agents/router.py:ROUTE_PROMPT`.

---

## 4. What's done (shipped)

### Product
- ✅ 4 specialist agents in a single chat room (Search · Property · Location · Outreach)
- ✅ LLM-driven router with explicit `@mention` override
- ✅ Multi-turn multi-agent dialogue: any agent can ask clarifying
      questions, `pending_clarification` routes user's answer back
- ✅ 3-step onboarding questionnaire with drag-to-rank importance
- ✅ Custom RankingService weights from importance_ranking
- ✅ Live shortlist auto-pinning + dynamic re-rank on every message
- ✅ User-editable profile chips (`✕` to remove)
- ✅ Negation handling in ProfileUpdater
- ✅ Cross-source data merge: Zillow + Craigslist with spatial dedup
- ✅ `@mention` typeahead in chat input
- ✅ Light-theme consulting-grade UI (3-pane: sidebar + chat + shortlist)
- ✅ Mobile responsive (slide-over drawers below md breakpoint)
- ✅ Cold-start chips on welcome message + post-search guidance chips
- ✅ Cold-start-aware fetch: 60s + 90s timeout retries, friendly errors

### Backend hygiene
- ✅ /profile/init endpoint with auto-search after questionnaire
- ✅ Session re-hydration from client mirror
- ✅ Per-bed-type pricing display ("1BR $1,036" not "rent ?")
- ✅ User-bed filter on display (only show requested bed-type rows)
- ✅ "from $X" rendering when only rent_min known

### Data pipeline
- ✅ Zillow scraper for SJ (output → 256 apt + 620 single home)
- ✅ Craigslist CSV loader + normalizer
- ✅ City-bbox tagger covering ~30 Bay Area cities
- ✅ `tools/harvest_reddit.py` + `tools/extract_personas.py` (Sprint 2)
- ✅ `tools/enrichment_v2.py` framework (re-extracts richer fields)
- ✅ `tools/scrape_building_sites.py` framework

### Deploy
- ✅ Vercel (Next.js, root=`web/`) auto-deploy from main
- ✅ Render (FastAPI, root=`api/`) auto-deploy from main
- ✅ ANTHROPIC_API_KEY in Render env vars
- ✅ NEXT_PUBLIC_API_URL in Vercel env vars
- ✅ CORS allows `*.vercel.app` regex + explicit origins env

---

## 5. What's in flight RIGHT NOW (as of 2026-05-03 ~01:45)

### Active scrape: Zillow San Francisco enrichment
- **Background task**: `bmd63jjg1` running `enrich.py --metro san-francisco-ca`
- **Phase 1**: ✅ Done — 976 SF listings (`output/zillow_san_francisco_ca_rentals.jsonl`)
- **Phase 2**: 🏃 In progress — currently around `[200/976]` enriched
- **Monitor task**: `b34xe3xf1` (persistent, watches enrich.log)
- **Estimated finish**: ~5 hours from start (~6 AM if started 1 AM)
- **Blocking on**: occasional manual CAPTCHAs (replay handles ~70%)

### What happens automatically when SF enrich finishes
1. `output/raw_details/` directory will have ~2050 JSON blobs (existing
   ~1100 SJ + new ~950 SF)
2. Re-run `python tools/enrichment_v2.py` to extract the richer field set
   from BOTH SJ and SF
3. Output: `api/data/zillow_listings_v2.jsonl.gz` (full Bay Area, ~9 MB
   gzipped)
4. **NOT yet wired into the live API** — `listings.py` still loads v1.
   Wiring requires updating `load_listings()` to prefer v2 when present.
5. After wiring + push: backend has ~2,050 listings (up from 1,087)

---

## 6. What's planned (next contributor pickups)

Order = priority. All decisions debated already; just need to execute.

### P1 — Wire v2 listings into the live API
**Effort**: ~30 min
**Files**: `api/listings.py`
**Steps**:
1. Update `load_listings()` to prefer `zillow_listings_v2.jsonl.gz` when
   present, fall back to v1.
2. The v2 records have extra fields (`phone_primary`,
   `housing_connector_link`, `marketing_treatments`, etc.) — surface
   them in the `Listing` dataclass + thread to agents.
3. Run end-to-end test: a search query should still return results.
4. Push.

### P2 — Run v2 extractor after SF enrich finishes
**Effort**: ~5 min run + ~10 min verify + push
**Steps**:
1. Wait for SF Phase 2 done signal in monitor `b34xe3xf1`
2. `python tools/enrichment_v2.py` (no flags — uses defaults)
3. Verify field coverage report — `phone_primary` should be ~60%+
4. `git add api/data/zillow_listings_v2.jsonl.gz && git commit && git push`
5. Render auto-deploys with the bigger dataset

### P3 — Coarse-rank semantic blend
**Effort**: 4-6 hours
**Files**: `api/profile.py:RankingService`, `api/agents/search.py`
**Steps**:
1. Add `sentence-transformers==3.0.1` to `api/requirements.txt`
2. Pre-compute listing embeddings at startup, store in
   `api/data/listing_embeddings.npy` (one-time, ~5 min)
3. Add `semantic_match` component to `DEFAULT_WEIGHTS` (weight 2.0)
4. In `RankingService.score`: encode user persona text, cosine vs
   listing embedding, scale 0-10
5. Document "semantic" in `IMPORTANCE_TO_COMPONENT` mapping (in main.py)
   so onboarding's importance_ranking can route to it
6. Bump SearchAgent's top-K from 25 to 50 (better coarse → broader pool
   for fine rerank)

### P4 — Building website scraper run
**Effort**: 1 day (split: URL discovery + scraping)
**Files**: `tools/scrape_building_sites.py`
**Blocker**: URL discovery — Zillow's `housing_connector_link` only
covers ~12% of buildings. Need one of:
- **Path A (recommended)**: Google Places API "Find Place from Text".
  Cost: ~$34 one-time for 2050 buildings. Needs GCP project +
  billing-enabled Places API key. Hit rate: 80-90%.
- **Path B**: LLM URL guess. Cheap (~$2) but lower hit rate (~30-40%).
- **Path C**: Manual curation for top 100 highest-traffic listings.

After URLs known: run `scrape_building_sites.py` → 70%+ of buildings get
extracted email/hours/alternate phone. Outreach Agent can finally do
real `mailto:` flow in v1.

### P5 — Outreach Agent: Gmail OAuth send
**Effort**: 2-3 days
**Status**: drafting only in v0 (renders preview cards in chat).
**v1**: Supabase Auth Google OAuth → Gmail API send → Pub/Sub reply
ingestion → reply summarized into chat. See design spec §9.

### P6 — Real Maps API for Location Agent
**Effort**: 1 day
**Status**: v0 uses Haversine straight-line distance.
**v1**: Google Distance Matrix for real drive/walk/transit times.
Caching + rate-limit budget design needed.

### P7 — Trained two-tower reranker (Sprint 3-5)
**Status**: full plan exists in design spec §6.
**Prerequisites**: 
- Sprint 2 done (Reddit harvester + persona extractor) ✅
- Sprint 3: dual-judge labeling (~$600 in API costs to label ~25k pairs)
- Sprint 4: train bge-small fine-tune with LoRA, ~$5 GPU
- Sprint 5: A/B harness in production
**Time**: ~5 weeks total

---

## 7. Known issues / gotchas

### Don't do these
- ❌ Don't change Vercel project ownership — the Vercel project is
      currently in the `rentwise` team. GitHub auto-deploy works only
      because @MaowenTang is added as a team member. Moving it back to
      the personal `andreatangs-projects` account triggers the
      "team configuration" auth bug we hit in commit `ba00dc4`.
- ❌ Don't use `vercel.json` configurations — Vercel auto-detects
      Next.js correctly with Root Directory = `web` in the dashboard.
- ❌ Don't bump `anthropic` to 0.40+ without also testing httpx — they
      have a known proxies-arg incompat that broke the API earlier.
      We pin `httpx<0.28` for safety.
- ❌ Don't swap to Railway as the backend host — Render free tier was
      explicitly chosen for cost. Railway has a $5/mo minimum after
      trial credits expire.
- ❌ Don't set Render `PYTHON_VERSION` env var to a bleeding-edge
      version — Python 3.14 is what Render defaults to, and pydantic
      doesn't have wheels for it. We pin `3.12.7` in both
      `runtime.txt` and `.python-version`.

### Watch out for these
- ⚠️ `agent_full_name` from Zillow's `contactInfo.agentFullName` is
      "Leasing Agent" 80% of the time. Don't treat it as a real name.
      The v2 extractor surfaces it but agents shouldn't trust it.
- ⚠️ `office_hours` is essentially never populated from Zillow.
      `amenityDetails.hours = []` for every apartment we've checked.
      The Outreach Agent should NOT promise "we'll call during office
      hours" because we don't know them.
- ⚠️ The `SearchAgent.by_zpid` index is rebuilt at app startup and is
      the SOLE place we look up Listing objects by zpid (used by
      session re-hydration in main.py). If you change SearchAgent's
      structure, update `_hydrate_session_from_client` accordingly.
- ⚠️ `prompt cost` for some Property Analyst calls runs to ~$0.02
      because we send all 5 listings + their full description blobs.
      If you reduce listings_in_scope, watch for the LLM hallucinating
      about listings it can't see.
- ⚠️ `craigslist.csv` has ~30 listings tagged "Bay Area" because
      lat/lng fell outside the 30 city bboxes. Edge cases like
      Sacramento, Santa Cruz coast.

### Active dataset gaps
- No emails — Zillow doesn't expose them. Need building website scrape
  (P4 above) to fill.
- No real walk score / transit score on `kind=single_home` records (Zillow
  doesn't surface these for SFH listings).
- No reviews / sentiment data anywhere — would need separate scrape from
  apartments.com / Yelp / Reddit.

---

## 8. How to dev locally

### Backend
```bash
cd ~/Downloads/rentwise/api
python3 -m venv .venv          # if first time
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env           # then fill ANTHROPIC_API_KEY
uvicorn main:app --port 8000 --reload
```

### Frontend
```bash
cd ~/Downloads/rentwise/web
npm install                    # if first time
cp .env.local.example .env.local
npm run dev                    # opens localhost:3000, hot-reloads
```

### One-shot smoke test
```bash
curl -s http://localhost:8000/healthz
# → {"ok":true,"listings_loaded":1087,"anthropic_key_present":true,...}

curl -s -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id":"test","message":"1BR under $2500 in Oakland"}'
```

---

## 9. How to deploy

Auto-deploy is wired. Just push to `main`:

```bash
git add . && git commit -m "..." && git push origin main
```

Both Vercel and Render auto-build and deploy.
- Vercel build: ~1-2 min
- Render build: ~3-5 min (cold; pip install is the slow part)

**Manual fallback** if auto-deploy breaks:
- Frontend: `cd web && vercel --prod`
- Backend: SSH into Render dashboard → Manual Deploy → Clear build cache

See `DEPLOY.md` for full handbook.

---

## 10. Active background tasks (machine-only context)

If you're an AI agent inheriting this session, these are running:
- Task `bmd63jjg1`: SF Phase 2 enrichment, foreground browser visible
- Task `b34xe3xf1`: persistent log monitor, pings on CAPTCHA / progress

Stop them with `TaskStop` if you need to. The actual scraper script
lives at `~/Downloads/zillow_scraper/enrich.py`. Output goes to
`~/Downloads/zillow_scraper/output/`.

---

## 11. Pending design decisions (need user input)

These are flagged in `docs/superpowers/specs/2026-05-01-rentwise-design.md`
§14 but not yet resolved:

1. **Outreach contact discovery method** — Google Places vs LLM-guess vs
   manual curation for building URLs (P4 above).
2. **Domain name** — `rentwise.ai` / `rentwise.app` / something else.
3. **Pricing model** — free / subscription / pay-per-outreach.
4. **Beta tester recruiting** — do we have a list?
5. **Catalog agent shortlist** — which 3-5 of the candidate templates
   ship in v1 (Lease Clause Auditor / Roommate Splitter / Move-In
   Checklist / Neighborhood Vibe / HOA Rules Reader).

---

## Glossary

- **粗排 / coarse rank**: first-pass scoring over the filtered set.
  Currently `RankingService.score()` heuristic. Should add bge cosine.
- **精排 / fine rerank**: top-25 → top-5 with rationales. Currently a
  single Sonnet call. Future: multi-tool agent once Maps/Schools
  integrations exist.
- **Persona**: a structured user profile (matching `OnboardingResult`
  schema). Source: real users via questionnaire OR LLM-extracted from
  Reddit posts via `tools/extract_personas.py`.
- **Session re-hydration**: when backend session is empty (post-restart),
  rebuilding from frontend's mirror state via `_hydrate_session_from_client`.
- **Replay**: auto-solving Zillow's PerimeterX CAPTCHA by playing back
  recorded human press-and-hold gestures from `captcha_recordings.jsonl`.

---

**Maintainer**: Maowen (tmwlxd@gmail.com / @MaowenTang)
**Built with**: Claude Opus 4.7 / Sonnet 4.6 / Code agent SDK
**License**: Private — do not redistribute.
