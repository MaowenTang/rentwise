# RentWise Evaluation — 20 Use Case Rank Table (2026-05-10)

Comprehensive ranking of RentWise's search agent across 20 personas, both
before and after two commute-geocoding fixes were applied. Designed as a
regression baseline + bug-tracking document for ongoing improvements.

## Test Environment

- **Backend**: local `uvicorn` at `localhost:8000`
- **Listings file**: `output/zillow_san_jose_rentals_enriched.jsonl` (2,286 listings, San Jose-biased)
- **Fixes applied before post-fix runs**:
  - Fix #1: Expanded `EMPLOYER_HQ` dict in `api/profile.py` from 14 → 35 employers
  - Fix #1: Added Mapbox geocoding fallback for unknown commute targets (`geocode_place()` in `api/profile.py`)
  - Fix #2: Listings with no `lat/lng` now score 0 for commute instead of being skipped (prevented Craigslist "Undisclosed Address" from getting overall=100)
- **Not yet applied** (see bug list at bottom)

## Rank Scale (matches user's original convention)

- **0** — Fundamentally broken (e.g. all locations far from target)
- **1** — Location correct, minor preference miss
- **2** — Location correct, one or two depth-verification gaps
- **3** — Mostly correct, several gaps
- **4** — Location partially correct, major constraint violations
- **5** — Location fundamentally wrong

## Full Result Table

| # | Persona | Pre-fix | **Post-fix** | Δ | Critical issue (post-fix) |
|---|---------|---------|--------------|---|---------------------------|
| 1 | Google SWE (MV Caltrain) | 1 | **2** | ↓1 | #1 lands in Downtown SJ; Caltrain proximity unverified |
| 2 | SFO Frequent Traveler | 2 | **5** | ↓3 | `commute=None`; #1 jumps to **Burbank, LA** |
| 3 | Apple Park 2BR | 4 | **2** | ↑2 | Top 5 all West SJ / Sunnyvale / Saratoga ✅ |
| 4 | Meta Hybrid + nightlife | 3 | **3** | = | Downtown SJ still takes 3 of top 5; dataset bias |
| 5 | Stanford Grad budget $2200 | 0 | **1** | ↑1 | Stevens Creek $1036 in-budget, but still ~25mi away |
| 6 | East Bay BART | 4 | **4** | = | Listings have `neighborhood=None`; can't verify BART proximity |
| 7 | Family 3BR North SJ | 2 | **2** | = | Top 5 perfectly North SJ / Berryessa ✅ |
| 8 | Car-free San Mateo | 1 | **5** | ↓4 | **Mapbox geocoded "biotech firm in San Mateo" → The Castro, SF** |
| 9 | UC Berkeley researcher | 3 | **3** | = | Top 4 Berkeley local; Top 1 still SF cross-bridge |
| 10 | Cross-Bay 2BR Mission Bay+RWC | 4 | **4** | = | Only anchors partner side; no geographic midpoint |
| 11 | Stanford Hospital Night Nurse | 5 | **3** | ↑2 | Commute geocoding fix directly saved this |
| 12 | Genentech Scientist | 5 | **4** | ↑1 | Top 1 still SF — `avoid` substring-match bug |
| 13 | Adobe PM Hybrid SJ | 2 | **2** | = | "I've lived there" → correctly excludes Downtown SJ |
| 14 | Stripe SF Backend Eng | 4 | **4** | = | Peninsula dataset gap; SF correctly excluded |
| 15 | Uber Full-Time Driver | — | **4** | new | Mapbox geocoded "SFO" → SF city center (not airport) |
| 16 | Freelance UX Designer | — | **3** | new | Soft commute (60-min biweekly) handled correctly |
| 17 | Late-Shift Restaurant Server | — | **4** | new | Oakland and Downtown SJ surface for Mission night-shift |
| 18 | Construction Foreman Multi-site | — | **5** | new | Multi-site → LLM set `commute=None` entirely |
| 19 | Travel Nurse 13-week UCSF | — | **3** | new | UCSF Mission Bay precise; #5 is "12-mo lease" listing |
| 20 | Single Mom + 2 Kids | — | **2** | new | "Close to mom in Hayward" correctly anchored ✅ |

## Distribution Comparison

| Rank | Pre-fix (n=14) | Post-fix (n=20) |
|------|----------------|-----------------|
| 0    | 1 (UC5)        | 0               |
| 1    | 2              | 1 (UC5)         |
| 2    | 3              | 4 (UC3/UC7/UC13/UC20) |
| 3    | 4              | 5 (UC4/UC9/UC11/UC16/UC19) |
| 4    | 6              | 7 (UC1/UC6/UC10/UC12/UC14/UC15/UC17) |
| 5    | 2              | 3 (UC2/UC8/UC18) |
| **Average** | **2.86** | **3.10**   |

Mean post-fix rank is slightly *worse* than pre-fix despite winning UC3/UC11/UC12
because of regressions on UC1/UC2/UC8 and harder cases UC15/UC17/UC18 added.

---

## Per-Case Detail

### UC1 — Google SWE Near Caltrain (post-fix rank 2)
- **Prompt**: "I work at Google in Mountain View and go to the office 3 times a week. Looking for a modern 1-bedroom apartment under $3400 with easy Caltrain access. Prefer walkable neighborhoods and in-unit laundry."
- **commute**: Google HQ (37.422, -122.0841) ✅
- **Top 5**:
  1. 574 W Hedding St #2 · Downtown SJ · $2500 · score 77.4 ⚠️ (Downtown SJ, far from MV)
  2. Brookview 1BD · Sunnyvale · $2220 · 63.7 ✅
  3. Sensible Apartment · Sunnyvale · $2851 · 63.1 ✅
  4. Assigned Parking · Mountain View · $2305 · 62.6 ✅
  5. 1Br/1Ba Updated · Sunnyvale · $2450 · 60.1 ✅
- **Gaps**: Caltrain proximity unverified; "modern apartment building" unverified; #1 is Downtown SJ

### UC2 — SFO Frequent Traveler (post-fix rank 5)
- **Prompt**: "I travel frequently for work and fly out of SFO about twice a month. Looking for a safe and quiet apartment with easy access to the airport and public transit. Budget under $3200 for a 1-bedroom."
- **commute**: `None` ❌ — LLM didn't extract SFO as a periodic commute target
- **Top 5**:
  1. 190 Boston Ave #5 · **Burbank** · $2500 · 71.9 ❌ (Los Angeles County)
  2. Centerra · Downtown SJ · $2583
  3. New Century Commons · Downtown SJ · $2195
  4. Town Park Towers · Downtown SJ · $1573
  5. 101 San Fernando · Downtown SJ · $2557
- **Gaps**: Periodic / non-daily commute not modeled; airport not a recognized commute target type

### UC3 — Apple Park 2BR (post-fix rank 2)
- **Prompt**: "I work at Apple Park in Cupertino and drive to work daily. Looking for a safe apartment with parking and relatively short commute times. Budget under $4000 for a 2-bedroom. Prefer quieter suburban neighborhoods."
- **commute**: Apple Park (37.319, -122.029) ✅
- **beds**: 2-2 ✅
- **Top 5**:
  1. 7100 Rainbow Dr APT 7 · West SJ · 2BR $3250 · 89.5 ✅
  2. Downstairs 2-bedroom · Sunnyvale · 2BR $2795 · 87.8 ✅
  3. Sahara Sands · West SJ · 2BR $3095 · 87.3 ✅
  4. 2/bd Coat Closet · SJ · 2BR $3204 · 87.3 ✅
  5. 2 Bedroom hardwood · Saratoga · 2BR $2695 · 86.9 ✅
- **Gaps**: Parking & "quiet suburban" unverified at depth

### UC4 — Meta Hybrid + Nightlife (post-fix rank 3)
- **Prompt**: "I work at Meta in Menlo Park and only commute twice a week. I care more about nightlife, restaurants, and social activities than minimizing commute. Looking for a modern apartment under $3800."
- **commute**: Meta HQ (37.4848, -122.1484) ✅
- **Top 5**:
  1. The Ryden · Downtown SJ · $3082
  2. Smart Samsung gas range · Oakland · $2415
  3. 101 San Fernando · Downtown SJ · $2557
  4. One South Market · Downtown SJ · $2753
  5. The James · Downtown SJ · $2976
- **Gaps**: Expected Redwood City / Palo Alto / Downtown San Mateo / MV downtown — all absent; "nightlife" lifestyle preference lost in dataset bias

### UC5 — Stanford Grad Student (post-fix rank 1)
- **Prompt**: "I'm a Stanford graduate student looking for affordable housing near campus. Budget under $2200 and I rely on public transportation or biking. I don't need luxury amenities but want a safe area and grocery stores nearby."
- **commute**: Stanford University (37.4247, -122.1703) ✅
- **Top 5**:
  1. Town Park Towers · Downtown SJ · $1573 · 46.7
  2. Stevens Creek · West SJ · $1036 · 46.6 (suspicious low price, possible bad data)
  3. Shires Memorial · Downtown SJ · $1647
  4. Griffith Apartments · Downtown SJ
  5. 99s14th · Downtown SJ
- **Gaps**: All Top 5 ≥25 mi from Stanford; bike/transit reasoning insufficient; Peninsula dataset gap

### UC6 — East Bay BART Commuter (post-fix rank 4)
- **Prompt**: "I'm a marketing manager working in downtown San Francisco. I'm looking for a 2-bedroom apartment in the East Bay, specifically near a BART station like Walnut Creek or San Leandro, so my commute is under 45 mins. My budget is $3,200. I have a car, so I need a dedicated parking spot."
- **commute**: Downtown SF (37.780, -122.420), max 45min ✅
- **Top 5**:
  1. Liberty Hill · neighborhood=`None` · 2BR $1999
  2. Bel Tempo · neighborhood=`None` · 2BR $2015
  3. Belleview · neighborhood=`None` · 2BR $2025
  4. Shutters · neighborhood=`None` · 2BR $2350
  5. Kingston Place · neighborhood=`None` · 2BR $2235
- **Gaps**: BART station proximity unverified; many East Bay listings lack `neighborhood` field

### UC7 — Family of Four + Elderly Parent, 3BR (post-fix rank 2)
- **Prompt**: "We are a family of four with two young kids and an elderly parent. We need a spacious 3-bedroom, 2-bathroom apartment in North San Jose or Berryessa. Our max budget is $4,000. It needs to be a quiet neighborhood with a playground or park nearby."
- **commute**: `None` (no commute mentioned)
- **beds**: 3-3 ✅
- **Top 5**:
  1. Waterford Place · North SJ · $4379 ⚠️ (over budget)
  2. North Park Apartment Homes · North SJ · $4880 ⚠️ (over budget)
  3. Vista 99 · North SJ · $4508 ⚠️ (over budget)
  4. 3233 Rockport Ave #1 · Berryessa · $3675 ✅
  5. 3167 Creekside Dr · Berryessa · $3700 ✅
- **Gaps**: Top 3 exceed $4000 hard cap; playground proximity unverified

### UC8 — Car-free Biotech Worker, San Mateo (post-fix rank 5)
- **Prompt**: "I just moved from NYC and don't plan on buying a car. I'll be working at a biotech firm in San Mateo. I need a modern Studio or 1-bedroom with a budget of $3,000. It's crucial that I can walk to a grocery store (like Whole Foods) and a gym within 10 minutes."
- **commute**: ❌ Mapbox returned **"The Castro, San Francisco" (37.7546, -122.435)** for "biotech firm in San Mateo"
- **Top 5**:
  1. Beautifully remodeled 1+ bedroom · SF · $950
  2. Bright 1BD 1BA apartment · SF · $1520
  3. Bright 1BD 1BA rental · SF · $2714
  4. Smart Samsung gas range · Oakland · $2415
  5. Studio, Resident Lounges · Oakland · $1852
- **Gaps**: Geographic anchor completely wrong; this is a regression caused by Fix #1 (Mapbox fallback) being too eager on ambiguous "X in Y" phrasing

### UC9 — UC Berkeley Researcher (post-fix rank 3)
- **Prompt**: "I'm a researcher at UC Berkeley looking for a quiet 1-bedroom apartment. My budget is $2,500. I need a place with lots of natural light and hardwood floors because I work from home a lot. Please avoid noisy areas with lots of undergraduate parties."
- **commute**: UC Berkeley (37.871, -122.262) ✅
- **Top 5**:
  1. Beautifully remodeled 1+ · SF · $950 (cross-bridge)
  2. Closet Space & Private Balcony · Oakland · $1200 ✅ (near)
  3. Bright 1BD 1BA · SF · $1520 (cross-bridge)
  4. Adorable sunny 1 bedroom · **Berkeley** · $2200 ✅
  5. Car Charging Stations · Oakland · $2087 ✅
- **Gaps**: "Avoid undergraduate parties" not geographically interpreted (south of campus = Telegraph Ave); "hardwood floors" / "natural light" features unverified

### UC10 — Cross-Bay Dual-Income Couple (post-fix rank 4)
- **Prompt**: "My partner works in Mission Bay (SF) and I work in Redwood City. We are looking for a 2-bedroom apartment that's roughly halfway for both of us to keep commutes balanced. Our budget is $4,800."
- **commute**: Mission Bay (partner) — only one of two anchors captured
- **Top 5**: All SF (4) + Oakland (1) — all near partner's side, none near RWC midpoint
- **Gaps**: No multi-commute geographic-balancing logic; expected San Mateo / Foster City absent

### UC11 — Stanford Hospital Night-Shift Nurse (post-fix rank 3)
- **Prompt**: see full prompt in UC4 of original "Night-Shift Nurse" definition
- **commute**: Stanford Hospital (37.4332, -122.1755) ✅ (in expanded EMPLOYER_HQ)
- **Top 5** (post-fix #2 retest):
  1. Brookview 1BD · Sunnyvale · $2220 · commute 6.6 ✅
  2. West Menlo Park Garden Studio · Bay Area · $2450 · commute 9.6 ✅
  3. Assigned Parking · Mountain View · $2305 · commute 7.1 ✅
  4. Town Park Towers · Downtown SJ · $1573 · commute 1.0 ⚠️ (walk_score pushed up)
  5. Unlock a New Chapter · Fremont · $2167 · commute 2.2 ⚠️
- **Gaps**: Reviews never invoked; must_haves unverified; commute weight 2.5 too weak to fully exclude #4/#5

### UC12 — Genentech Scientist Peninsula North (post-fix rank 4)
- **Prompt**: see UC12 definition
- **commute**: Genentech (37.6586, -122.3877) ✅
- **Top 5**:
  1. Bright 1BD 1BA · **San Francisco** · $1520 · 69.5 ❌ (violates "Not SF proper")
  2. 1BR Apt All Utilities · Oakland · $1950 · 65.6
  3. 1 bedroom Cozy · Hayward · $1650 · 63.6
  4. Bike Racks · Milpitas · $1982 · 63.5
  5. Super Cute 1-Bedroom · Walnut Creek · $1899 · 63.5
- **Gaps**: `avoid: ["SF proper"]` extracted but bug #5 (substring-match only) lets SF listings through

### UC13 — Adobe PM Hybrid San Jose (post-fix rank 2)
- **Prompt**: see UC13 definition
- **commute**: Adobe HQ (37.3308, -121.8932) ✅
- **avoid**: ["downtown San Jose"] ✅
- **Top 5**:
  1. 3rd St. Apartments · Fairgrounds · $2095 ✅ (adjacent, not in downtown)
  2. THRIVE · Buena Vista · $2095 ✅
  3. The Glen Creek Two · Willow Glen · $2095 ✅
  4. Garden Glen · Willow Glen · $1975 ✅
  5. Buena Vista Apartments · Buena Vista · $1975 ✅
- **Gaps**: must_haves and review verification still missing, but result quality high

### UC14 — Stripe SF Backend Engineer (post-fix rank 4)
- **commute**: Stripe South SF (37.668, -122.387) ✅
- **Top 5**: Oakland · Evergreen SJ · West SJ · Willow Glen · Cambrian Park
- **Gaps**: Zero Peninsula results (dataset gap); SF correctly excluded

### UC15 — Full-Time Rideshare Driver (post-fix rank 4)
- **commute**: ❌ Mapbox returned SF city center (37.779, -122.419) for "SFO"
- **Top 5**: 2× SF, 3× Downtown SJ (50 mi from real SFO)
- **Gaps**: Airport-name resolution; 24h grocery filter; overnight parking verification

### UC16 — Freelance UX Designer (post-fix rank 3)
- **commute**: "San Francisco (client meetings)" 60-min ✅ (soft cap correctly extracted)
- **Top 5**: 2× SF, Oakland, SF, Berkeley
- **Gaps**: Peninsula expected (Burlingame/SM/RC) absent; natural light / thick walls / balcony features unverified

### UC17 — Late-Shift Restaurant Server (post-fix rank 4)
- **commute**: Mission District (37.757, -122.419) ✅ (precise, not generic SF)
- **Top 5**: 2× SF (close, good), Oakland, Downtown SJ, Oakland
- **Gaps**: Oakland & Downtown SJ inappropriate for midnight Muni return; late-night safety not modeled

### UC18 — Construction Foreman Multi-site (post-fix rank 5)
- **commute**: `None` ❌ — LLM saw Hayward + Redwood City + Concord and gave up
- **Top 5**: All South Bay (Edenvale, West SJ, Downtown SJ, North SJ, Santa Teresa)
- **Gaps**: Multi-site commute optimization; freeway-access positive signal; truck-size parking detection

### UC19 — Travel Nurse 13-Week Contract (post-fix rank 3)
- **commute**: UCSF Mission Bay (37.769, -122.396) ✅ (Mapbox precise)
- **Top 5**: All SF with commute 8.4-9.3 ✅
- **Gaps**: #5 is **"TMLP 12-mo. Luxury high-rise"** — name literally says "12-mo" but recommended to user who said "no 12-month locks"; furnished/short-term lease filter absent

### UC20 — Single Mom + 2 Kids (post-fix rank 2)
- **commute**: `None` ✅ ("work from home" correctly handled)
- **Top 5**: All Hayward, 2BR $2499-$2660 ✅ ("close to mom in Hayward" anchored neighborhood preference)
- **beds**: 2-3 ✅
- **Gaps**: School district quality unverified; park/playground walkability unverified; all Top 5 near budget cap

---

## Bug Tracker

### ✅ Fixed in this round

| # | Bug | Affected cases | Fix |
|---|-----|----------------|-----|
| 1 | Commute targets not in hardcoded `EMPLOYER_HQ` dict had `lat=lng=None`, causing commute scoring to be skipped entirely | UC11/UC12/UC14 (pre-fix) | Expanded dict + added Mapbox `geocode_place()` fallback in `api/profile.py` |
| 2 | Listings without lat/lng (mostly Craigslist "Undisclosed Address") were skipped in commute scoring, leaving them with full marks on budget/beds/avoid → overall=100 → topped results | UC4 (pre-fix) | `api/profile.py` ranker: when profile has commute but listing has no coords, score 0 instead of skipping |

### ❌ Open bugs (priority ordered)

| # | Bug | Affected cases | Suggested fix | Effort |
|---|-----|----------------|---------------|--------|
| 5 | `avoid` only substring-matches against `description`; doesn't check listing's `city`, `neighborhood`, or `address` field. User says "avoid SF" but listings with city=SF and no "SF" in description slip through. | UC12 (Top 1 = SF) | Extend `_listing_blob` to include `city`/`neighborhood`/`address` for `avoid` check specifically; or do geographic exclusion via lat/lng polygon | S |
| 7 | Mapbox geocoding of abbreviations (`SFO`) and fuzzy descriptions (`biotech firm in San Mateo`) lands at wrong location. Mapbox treats SFO as the SF metro area; treats "biotech firm in San Mateo" as a Castro SF address. | UC8 (1→5 regression), UC15 (Uber driver) | (a) Maintain a small POI/airport alias dict (`SFO` → 37.6191, -122.3816; `SJC` → 37.3639, -121.929; `OAK` → 37.7126, -122.2197). (b) Validate Mapbox response by checking `place_name` includes the *target city* keyword from the query; reject mismatches. (c) Cap Mapbox confidence with a sanity check distance to the named city centroid. | M |
| 8 | Multi-site commute → LLM sets `commute=None` entirely. Periodic / weekly-rotating job sites unmodeled. | UC18 (5), UC2 (5) — periodic airport trips | Extend `CommuteTarget` schema to support a list of targets with weights/frequencies; ranker takes weighted-average distance. LLM prompt change in `api/profile.py:511-541` to allow extracting multiple commute anchors. | M |
| 9 | No furnished / short-term lease field detection. UC19 #5 was literally titled "TMLP 12-mo." (twelve-month lock) but recommended to a 13-week travel nurse. | UC19 | Add `furnished: bool` and `lease_term_min: int` to Listing extraction (apartments.com lists these). When profile contains "short-term," "furnished," "3-month," etc., filter or strongly penalize. | M |
| 3 | Reviews agent (`agents/reviews.py`) is never proactively invoked. `tool_calls = 0` for all 20 cases despite multiple cases having "reviews say…" as the core ask. | All 20 cases (UC11/UC12 most affected) | The search agent should auto-invoke reviews for top-N candidates when `must_haves` or `nice_to_haves` contain review-related terms ("quiet," "safe," "no highway noise," "peaceful"). Currently appears to require explicit user request. | L |
| 4 | `must_haves` and `nice_to_haves` scoring is naive text-substring matching of preference keywords against listing description blob. "Parking under $200" doesn't get verified against actual parking-fee data; "quiet building" doesn't get verified against review sentiment. | All cases | Replace with per-preference verifiers: parking-cost extractor, amenity presence check, review-sentiment match, etc. Build a verifier registry keyed on preference type. | L |
| 6 | Dataset bias toward San Jose. Listings file `output/zillow_san_jose_rentals_enriched.jsonl` has ~2300 listings; Peninsula (94010, 94401-94403), East Bay (Berkeley, Walnut Creek, San Leandro), and Marin are sparse. | UC6/UC7/UC9/UC10/UC14 (Peninsula); UC2 (regional) | Extend `zillow_scraper` to cover Peninsula and East Bay ZIPs; merge multiple JSONL files at API startup. Not a code bug — a data-coverage gap. | M (data work) |
| 10 | Commute weight (default 2.5) is too small relative to the budget+beds+avoid stack. A listing failing the 30-min commute by a wide margin can still rank top 5 if it nails budget and beds. | UC11 #4 Town Park (commute 1.0 still ranked), UC14 Top 5 all far from South SF | Increase commute weight to 4.0-5.0, OR apply hard filter when `commute_score < 2.0` and `max_minutes` was explicitly set. | S |

### Bug-to-Case Matrix

```
            UC1 UC2 UC3 UC4 UC5 UC6 UC7 UC8 UC9 UC10 UC11 UC12 UC13 UC14 UC15 UC16 UC17 UC18 UC19 UC20
Bug 3 (rv)   .   .   .   .   .   .   .   .   .   .    X    X    .    .    .    .    X    .    X    .
Bug 4 (mh)   X   .   X   .   X   .   X   .   X   X    X    X    X    X    X    X    X    X    X    X
Bug 5 (av)   .   .   .   .   .   .   .   .   .   .    .    X    .    .    .    .    .    .    .    .
Bug 6 (dt)   .   .   .   X   X   X   .   .   X   X    .    .    .    X    .    X    .    .    .    .
Bug 7 (gc)   .   .   .   .   .   .   .   X   .   .    .    .    .    .    X    .    .    .    .    .
Bug 8 (mc)   .   X   .   .   .   .   .   .   .   X    .    .    .    .    X    .    .    X    .    .
Bug 9 (lt)   .   .   .   .   .   .   .   .   .   .    .    .    .    .    .    .    .    .    X    .
Bug 10 (cw)  X   .   .   .   .   .   .   .   .   .    X    .    .    X    .    .    .    .    .    .
```

Legend: rv=reviews never called, mh=must_haves naive matching, av=avoid substring only,
dt=dataset bias, gc=geocode misinterpretation, mc=multi-commute, lt=lease term filter,
cw=commute weight too low

### Suggested Fix Order (by ROI)

1. **Bug #7 (geocode validation)** — small change, fixes UC8 regression (5→1) and UC15 (4→2)
2. **Bug #5 (avoid extends to city/neighborhood)** — small change, fixes UC12 (4→3)
3. **Bug #10 (commute weight tuning)** — config change, helps UC11/UC14
4. **Bug #8 (multi-commute schema)** — medium change, unlocks UC2/UC18 (5→3?)
5. **Bug #9 (lease term filter)** — needs data extraction, fixes UC19
6. **Bug #3 (auto-invoke reviews)** — orchestration change, lifts all "quiet/safe" cases
7. **Bug #4 (real must_haves verification)** — largest refactor, but highest ceiling
8. **Bug #6 (data coverage)** — data engineering work in `zillow_scraper`

## Files Changed in This Round

- `api/profile.py`:
  - Added `import os`, `from urllib.parse import quote`, `import httpx`
  - Added `_GEOCODE_CACHE` module-level dict
  - Added `geocode_place(query)` function (lines ~57-110)
  - Expanded `EMPLOYER_HQ` from 14 → 35 employers (lines ~125-180)
  - Modified commute fallback in `_apply_patch` (lines ~700-720): now calls `geocode_place()` when employer dict miss
  - Modified `RankingService.score` commute block (lines ~492-510): scores 0 instead of skipping when listing lacks lat/lng
- `api/.env`: Added `MAPBOX_TOKEN`

## How to Reproduce This Eval

```bash
# 1. Ensure local API is running with fixes applied
cd ~/Downloads/rentwise/api
.venv/bin/uvicorn main:app --port 8000

# 2. Run any single case
SID="uc-$(date +%s)"
curl -sS -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id":"'$SID'","message":"<paste prompt>","history":[]}' \
  | python3 -m json.tool

# 3. Inspect:
#    - response.profile.commute.lat / lng (geocoding success)
#    - response.shortlist[0..4] (top 5 with full score components)
#    - response.tool_calls (should ideally be > 0 for review-heavy cases)
```

## Production Deployment Status

⚠️ **None of these fixes are live on `https://rentwise-api-ug16.onrender.com`** as of this eval. To deploy:

1. Commit changes in `api/profile.py` and push to `main`
2. In Render dashboard → Environment Variables, add `MAPBOX_TOKEN` (same value as in `api/.env`)
3. Render auto-redeploys on push
4. Re-run UC11 against production URL to confirm `commute.lat` is now populated
