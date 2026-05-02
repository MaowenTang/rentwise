# RentWise AI — Technical Design Spec

**Status:** Draft v1 · awaiting user review
**Date:** 2026-05-01
**Author:** Claude (Opus 4.7) on behalf of Maowen
**Decision target:** v1 polished launch in 2–3 months

---

## 1. Product summary

RentWise is a hosted web app where renters chat in a multi-agent group room
to find, analyze, and contact apartments. The product positions itself as
"Slack-for-apartment-hunting": every "team member" in the chat is an LLM
specialist agent, and they coordinate to fulfill the user's request.

Differentiators vs. Zillow / Apartments.com:

1. **Conversational search** — describe preferences in natural language
   instead of toggling filters.
2. **Multi-agent specialization** — distinct agents own search, property
   analysis, location/commute, and outreach. Each is independently
   inspectable and explainable.
3. **Autonomous outreach** — Outreach Agent emails leasing offices on the
   user's behalf via their Gmail, ingests replies, and summarizes back into
   chat — replacing the per-listing email back-and-forth tenants do today.
4. **User-extensible agents** — users can add specialist agents from a
   curated catalog (custom NL-defined agents in v1.1).

---

## 2. Goals and non-goals

### v1 goals (2–3 months)

- Live URL with Google sign-in
- Default 4 agents in every workspace: Search, Property Analyst,
  Location & Commute, Outreach
- Real Gmail OAuth + outreach agent that sends/receives email
- San Jose listings only at launch (910 listings already scraped)
- Curated catalog of 3–5 additional agent templates the user can enable
- Slack-inspired UI (sidebar + main chat + right-side artifact panel)
- Stripe billing scaffolding (toggled off; live in v1.1)
- Basic product analytics (PostHog), error tracking (Sentry)

### Explicit non-goals for v1

- Trained ML models (LTR, similarity NN, MoE re-ranking) — heuristic +
  LLM-as-ranker only. Original proposal §4.IV–V is deferred.
- CV image-aesthetics scoring — listings have photo URLs but we do not run
  CLIP/aesthetic models in v1.
- Crime / weather / power-outage data integrations — deferred.
- Apartments.com or government-portal scrapers — Zillow only.
- Multi-user shared workspaces (one workspace per user account in v1).
- Native mobile apps — responsive web only.
- Custom (non-catalog) agent creation by end users — v1.1.
- Subscription billing live — scaffolded but free during v1 beta.
- Real-time tour scheduling on landlord calendars — Outreach Agent can
  *propose* tour times in email but does not write to landlord calendars.

### Out-of-scope for the foreseeable

- Acting as a tenant-screening service (credit checks, background reports).
- Holding security deposits or rent payments (we are not a money
  transmitter).
- Lease signing / e-signature flows.

---

## 3. System architecture

```
┌────────────────────────── BROWSER (Next.js 14, App Router) ──────────────────────────┐
│  Sidebar │ Chat thread (streaming) │ Right rail: ListingCard │ MapCard │ EmailCard   │
└──────────────────────────────────────┬───────────────────────────────────────────────┘
                                       │ Supabase Realtime (Postgres CDC over WS)
┌──────────────────────────────────────▼───────────────────────────────────────────────┐
│                         FastAPI Orchestrator (Python 3.12, async)                     │
│  ┌──────────────────── Agent Router ────────────────────┐                            │
│  │ LLM-driven dispatch — decides which agent(s) handle  │                            │
│  │ each turn, optionally fans out to several in parallel│                            │
│  └─────┬─────────┬─────────┬─────────┬──────────────────┘                            │
│        ▼         ▼         ▼         ▼                                                │
│   ┌────────┐┌──────────┐┌────────┐┌──────────┐  ┌── Agent Registry ─┐                │
│   │ Search ││ Property ││Location││ Outreach │  │  workspace_agents │                │
│   │ Agent  ││ Analyst  ││+Commute││  Agent   │◄─┤  agent_templates  │                │
│   └───┬────┘└────┬─────┘└───┬────┘└────┬─────┘  └───────────────────┘                │
│       │          │          │          │                                              │
│       ▼          ▼          ▼          ▼                                              │
│  ┌─────────┐┌─────────┐┌─────────┐┌──────────┐                                       │
│  │pgvector ││Postgres ││  Maps   ││  Gmail   │                                       │
│  │listings ││facets+  ││  API    ││  API     │                                       │
│  │embedding││  text   ││ proxy   ││ (OAuth)  │                                       │
│  └─────────┘└─────────┘└─────────┘└──────────┘                                       │
│                                                                                       │
│  ┌──────── Background workers (Celery + Redis) ────────┐                             │
│  │ • Outreach send/receive  • Embedding indexer        │                             │
│  │ • Listing refresh ETL    • Photo cache hydrator     │                             │
│  └─────────────────────────────────────────────────────┘                             │
└──────────────────────────────────────────────────────────────────────────────────────┘
       │
       └── Supabase: Postgres + Auth (Google) + Realtime + Storage (photos cache)
       └── PostHog (analytics), Sentry (errors), Stripe (billing scaffold)
```

### Architectural decisions

**A1. Agent Router pattern (LLM-driven dispatch)**
A central LLM call (Claude Sonnet, low temperature) reads each new user
message + recent context and emits a structured tool call naming which
agent(s) to invoke. Multiple agents can be invoked in parallel. Alternatives
considered: every-agent-listens (noisy, duplicate replies); manual @mention
(works but loses the "they coordinate themselves" magic). Router wins.

**A2. Stateless agents, stateful chat**
Agents are pure functions: `(chat_context, listings_in_scope, tools) → reply`.
All state lives in Postgres (chat rooms, messages, user prefs, OAuth tokens,
listing inventory, outreach threads). Re-running an agent invocation is
deterministic given the same inputs (modulo LLM stochasticity).

**A3. Single Postgres + pgvector, not multiple datastores**
Listings, embeddings, facets, chat history, agent registry — all in one
Postgres database. Vector search via pgvector extension. We avoid Pinecone /
Weaviate / Elasticsearch in v1 — fewer moving parts, easier ops, sufficient
performance at our scale (≤100k listings).

**A4. Outreach in workers, not in request path**
Sending email, polling Gmail for replies, parsing/threading responses, and
producing chat summaries all happen in Celery workers. The Outreach Agent
visible in chat is the user-facing wrapper that schedules these jobs and
reads their results from Postgres. This keeps p95 chat latency under 2s
even when outreach is mid-flight.

**A5. Multi-city data model, single-city launch**
Schema includes `metro_id` everywhere. v1 ships with `metro=san-jose-ca` only.
Adding a new metro = run scraper + load + flip a feature flag. No code
changes required.

---

## 4. Agents

### 4.1 Default agents (in every workspace)

#### Search Agent
- **Role:** Translate user's natural-language preferences into a structured
  query, retrieve candidate listings from Postgres, score and rank them,
  surface as ListingCards in chat.
- **Tools:**
  - `parse_preferences(text) → StructuredQuery` (LLM tool, Claude)
  - `search_listings(query) → list[Listing]` (Postgres + pgvector hybrid)
  - `score_listings(listings, prefs) → ranked` (LLM-as-ranker on top-K)
  - `relax_filters(query) → relaxed_query` (when zero or few results)
- **Inputs:** Latest user message, prior preferences in chat, workspace city.
- **Outputs:** Up to N (default 5) ListingCards posted to the room, plus a
  short rationale message ("These match your dog-friendly + close-to-VTA
  request; I relaxed your $3000 ceiling to $3300 to surface 2BR options.").
- **Ranking model (v1):**
  1. **Hard filters** (beds, max rent, pets, city) — Postgres WHERE clause.
  2. **Soft scoring** — LLM scores each candidate against user prefs on a
     0–100 scale; uses listing facts + walk/transit/bike scores + school
     ratings + amenity list. Cached per (listing, prefs-hash).
  3. **Diverse re-rank (Airbnb-inspired, simplified)** — MMR-style: penalize
     candidates whose embedding cosine similarity to already-selected
     candidates exceeds a threshold (default 0.85). Embeddings = OpenAI
     `text-embedding-3-small` over a serialized listing facts string,
     stored in pgvector.
  4. **Search augmentation** — if the hard-filtered set has < 3 listings,
     LLM identifies which filter to relax (price by 10%, beds ±1, expand
     neighborhoods to adjacent ones) and re-queries; relaxed results posted
     as a separate "expanded matches" carousel.

#### Property Analyst Agent
- **Role:** Answer detailed questions about a specific listing using the
  scraped Zillow data.
- **Tools:**
  - `get_listing_facts(zpid) → ListingFacts` (Postgres lookup)
  - `extract_field(listing, question) → answer` (LLM over JSONL record)
  - `compare_listings(zpids) → ComparisonTable` (multi-listing tabular)
- **Example queries it handles:**
  - "Does this place include water?"
  - "What's the deposit on the 1-bed plan at The Fay?"
  - "Compare 1-bed pricing across these three."
  - "Which of these has the strictest pet policy?"
- **Citation discipline:** Every factual claim must cite the source field
  (e.g., "deposit_min: $500"). If the field is `null`, the agent says "not
  listed in the source" rather than hallucinating.

#### Location & Commute Agent
- **Role:** Geographic context — nearby amenities + commute calculations.
- **Tools:**
  - `nearby_places(address, type, radius_m) → Places[]` (Google Places
    Nearby Search; types include grocery_store, school, hospital, gym,
    transit_station, restaurant)
  - `commute(origin_address, dest_address, mode, depart_time) →
    DurationDistance` (Google Distance Matrix)
  - `geocode(address) → LatLng` (Google Geocoding)
- **Caching:** Aggressive Postgres-backed cache keyed on
  (origin, dest, mode, hour-bucket). 7-day TTL for nearby places, 1-day
  TTL for commute estimates.
- **Example queries it handles:**
  - "How far is this from downtown SJ by transit at 8am?"
  - "What grocery stores are within walking distance?"
  - "Compare commute times to my work address (123 Main St, Santa Clara)
    for these three listings."
- **Output:** Posts MapCards (embedded Mapbox/Google static map) and tabular
  commute summaries.

#### Outreach Agent
- **Role:** Send tenant-inquiry emails to leasing offices via the user's
  Gmail, ingest replies, summarize into chat, prompt user for follow-ups.
- **Tools:**
  - `draft_inquiry(listing, user_questions) → EmailDraft` (LLM)
  - `send_email(draft, user_oauth_token) → MessageId` (Gmail API)
  - `poll_replies(thread_id) → Message[]` (Gmail watch + pull)
  - `summarize_thread(messages) → Summary` (LLM)
- **Lifecycle:**
  1. User in chat: "Email these three to ask about availability for May 15
     and pet fees."
  2. Outreach Agent drafts three emails (one per listing); shows previews
     in chat as EmailCards; user clicks "Send all" or edits individual ones.
  3. Worker sends via Gmail API. Each email has a unique tracking marker
     in the body for thread reconciliation.
  4. Gmail push notifications (or 5-min polling fallback) deliver replies
     into our system.
  5. Outreach Agent posts a summarized reply into the chat room ("Crown
     Apartments confirmed availability May 15, $2,975/mo, $35 pet fee,
     wants to schedule tour Tue 5pm. Reply or accept?").
  6. User can reply directly in chat — agent drafts the response, sends
     via Gmail.
- **Send authority:** Default = "draft + 1-click send" (user reviews each).
  Power-user mode = "auto-send drafts that match a saved profile" — v1.1.
- **Compliance guardrails:**
  - Outreach is to a *business* email address (leasing office contact
    listed publicly on Zillow); not a consumer mailing list. CAN-SPAM
    requires sender identification (user is the sender, lawful) and an
    opt-out mechanism in the body.
  - Body always includes: user's real name, "I'm a prospective tenant,"
    listing reference, and "Reply STOP and I won't contact you again."
  - We rate-limit to ≤10 outreach emails per user per day in v1.

### 4.2 Agent extensibility model

**v1 ships with a curated catalog** of additional agents users can enable
per workspace. Custom NL-defined agents are deferred to **v1.1**.

**v1 catalog candidates** (pick 3–5 to ship at launch):

| Agent | Purpose |
|-------|---------|
| Lease Clause Auditor | User pastes a lease PDF; agent flags unusual clauses |
| Roommate Splitter | Computes per-roommate cost split given a listing + headcount |
| Move-In Checklist | Generates utility setup, address change, etc. tasks |
| Neighborhood Vibe | Synthesizes Reddit / online community vibe summaries |
| HOA / Building Rules Reader | Q&A over building rules text when present |

**v1.1 custom agent flow** (deferred):
1. User: "Add an agent that compares fitness amenities."
2. App generates: name, system prompt, tool access list (default = read-only
   listing access), and posts a preview.
3. User confirms; agent is provisioned in the workspace.
4. User can edit the system prompt at any time.

**Database model:** `agent_templates` (catalog, system-managed) +
`workspace_agents` (instances per workspace, with overrides for system
prompt and enabled tools).

---

## 5. Data model

### Core tables (Postgres)

```sql
-- Auth & accounts
users                  -- Supabase auth.users (managed)
profiles               -- public profile, full_name, avatar_url, default_workspace
gmail_oauth_tokens     -- encrypted, per user; refresh tokens

-- Workspaces & agents
workspaces             -- one per user in v1; future: shared
workspace_agents       -- which agents are enabled per workspace, with config
agent_templates        -- curated catalog (system-managed)

-- Chat
chat_rooms             -- one default per workspace; "search sessions" can spawn more
messages               -- sender_type ∈ {user, agent, system}, agent_id nullable,
                       -- content jsonb (text + cards), created_at
message_attachments    -- listing references, email cards, map cards

-- Listings (multi-city ready)
metros                 -- {san-jose-ca, ...}
listings               -- normalized from zillow_san_jose_rentals_enriched.jsonl
listing_floor_plans    -- per-floorplan beds/baths/sqft/price_min/price_max
listing_amenities      -- normalized many-to-many
listing_pet_policies   -- per-pet-type fees + deposits
listing_schools        -- nearby schools with rating/distance
listing_embeddings     -- pgvector(1536) for similarity / semantic search
listing_photos         -- urls; cached blobs in Supabase Storage

-- Search
saved_listings         -- user shortlist
search_queries         -- history; for re-ranking learning later

-- Outreach
outreach_threads       -- {user, listing, gmail_thread_id, status,
                       --  last_message_at, summary_text}
outreach_messages      -- denormalized cache of Gmail thread for chat display

-- Cache
maps_cache             -- (origin_norm, dest_norm, mode, hour_bucket) → result
nearby_cache           -- (lat_grid, lng_grid, type) → result
listing_score_cache    -- (zpid, prefs_hash) → llm score
```

### Indexes & extensions
- `pgvector` on `listing_embeddings.embedding` (HNSW, cosine).
- Trigram index on listing description / building_name for fuzzy search.
- Partial index on `listings WHERE active = true`.
- Composite index on `messages (chat_room_id, created_at desc)`.

### Initial data load
- Source: `~/Downloads/zillow_scraper/output/zillow_san_jose_rentals_enriched.jsonl`
  (910 listings as of 2026-05-01).
- ETL script normalizes JSON → relational tables; handles list-valued
  fields (amenities, pet_groups, schools, floor_plans).
- Embedding job runs after load: serialize each listing → embedding → store.
- Weekly refresh job (already scheduled via macOS launchd) re-runs scraper
  and incremental-loads new/changed records.

---

## 6. Frontend & chat UX

### Layout (Slack-inspired, denser/more polished — pending UX-target screenshot from user)

```
┌─────────────────────┬─────────────────────────────────┬───────────────────────────┐
│   SIDEBAR (240px)   │      MAIN CHAT (fluid)          │  ARTIFACT RAIL (380px)    │
│                     │                                 │                            │
│  ▸ Workspace switcher│   #search-session-1            │  Pinned listing:          │
│  ── Channels ──     │                                 │  ┌──────────────────────┐ │
│  # general          │   you · 10:14                   │  │ The Fay (Studio–2BR) │ │
│  # search-session-1 │   "2BR under 3500 near downtown"│  │ $2,950–$3,950        │ │
│  # search-session-2 │                                 │  │ 10 E Reed St         │ │
│  + new search       │   ▸ Search Agent · 10:14        │  │ walk 93 transit 65   │ │
│                     │   Found 7 matches. Top 5:       │  │ Pets: dogs/cats OK   │ │
│  ── Agents ──       │   [ListingCard][ListingCard]... │  │ [Email] [Save] [Map] │ │
│  ✓ Search           │                                 │  └──────────────────────┘ │
│  ✓ Property Analyst │   you · 10:16                   │                            │
│  ✓ Location/Commute │   "How far from VTA Diridon?"   │  Comparison table:        │
│  ✓ Outreach         │                                 │  ┌──────────────────────┐ │
│  + Add agent        │   ▸ Location/Commute · 10:16    │  │ ... 3-listing diff   │ │
│                     │   8 min walk via VTA Light Rail │  └──────────────────────┘ │
│  ── Settings ──     │   [MapCard]                     │                            │
│  ⚙ Gmail connected  │                                 │  Outreach status:         │
│  ⚙ Profile          │   [ Type a message... ]         │  ┌──────────────────────┐ │
│                     │                                 │  │ Sent 3, replies 1    │ │
└─────────────────────┴─────────────────────────────────┴───────────────────────────┘
```

### Key UX patterns

**Streaming.** Agent responses stream token-by-token via Supabase Realtime.
Long jobs (outreach send, embedding search > 1s) show a typing indicator
+ progress chip.

**Cards over plain text.** Listings, maps, emails render as rich cards with
inline actions (Save, Email, Compare, View on Zillow). Plain markdown text
is reserved for explanations and rationales.

**Right rail = current focus.** Pin a listing or comparison to the right
rail; it persists across messages so you can keep referring to "the third
one" without re-pasting context.

**Slash commands & @mentions.**
- `/search 2br dog-friendly under 3500` — explicit Search Agent invocation
- `@property compare these` — explicit Property Analyst invocation
- Default = router decides; mention overrides router.

**`+ Add agent` flow (v1).** Clicking opens a modal showing the curated
agent catalog (§4.2 list). User picks one; agent is added to the workspace
with default config. v1.1 adds a "Custom agent" tab to the same modal where
the user describes the agent in natural language.

**Search sessions as channels.** Each new top-level search spawns a
dedicated chat channel scoped to that intent. Lets users keep "downtown
vs. west side" inquiries separate. All channels share workspace agents.

**Empty states & onboarding.** First-time user: chat opens with a system
message from the orchestrator: "Hi! I'm RentWise. Tell me what kind of
place you're looking for — neighborhood, budget, must-haves." Connect-Gmail
prompt appears the first time the user asks the Outreach Agent to send.

### Component library
- shadcn/ui (Radix primitives + Tailwind) for base components.
- Custom: ChatMessage, ListingCard, MapCard, EmailCard, ComparisonTable,
  AgentBadge, AgentPicker (for `+ Add agent`).
- Mapbox GL JS for inline maps (Google Maps for *data*, Mapbox for *render*
  — cleaner separation, lower per-render cost).

---

## 7. External integrations

### LLM (Anthropic Claude)
- **Models:**
  - Routing & cheap tasks: `claude-sonnet-4-6`
  - Complex reasoning (ranking, comparison, lease analysis): `claude-opus-4-7`
- **Tooling:** Claude Messages API with tool-use; we DO NOT use the
  Anthropic Agent SDK in v1 (we orchestrate ourselves to keep the agent
  behavior fully visible/auditable in our own logs).
- **Prompt caching:** Aggressive caching of system prompt + listing context
  in agent calls. Expected 70%+ cache hit rate on hot listings.

### Google Maps Platform
- Required APIs: Places (Nearby Search), Distance Matrix, Geocoding.
- Billing: requires Google Cloud project with billing account; budget alerts.
- Estimated v1 cost at 100 active users / 5 commute queries per session:
  ~$50/mo on the Maps Platform free credit + paid spillover.

### Gmail API (per-user OAuth)
- Scopes: `gmail.send`, `gmail.readonly` (replies), `gmail.modify`
  (label our messages with a `RentWise/Outreach` label).
- Push notifications via Pub/Sub (preferred) or polling fallback.
- Refresh tokens stored encrypted (Supabase pgsodium or KMS).

### Mapbox (rendering only)
- Static map images for chat cards + interactive maps when expanded.

### PostHog (analytics)
- Funnel events: signup, first search, first listing pinned, first
  outreach send, first reply received, first tour scheduled (post-v1).

### Sentry (error tracking)
- Frontend (Next.js) + backend (FastAPI).

### Stripe (deferred to v1.1)
- Scaffold only in v1; pricing TBD.

---

## 8. Auth & accounts

- **Provider:** Supabase Auth.
- **Method:** Google OAuth (single login that also covers Gmail OAuth scope
  — incremental auth on first outreach attempt).
- **Session:** httpOnly cookies; refresh handled by Supabase JS client.
- **Workspace provisioning:** On signup, a default workspace + `general`
  chat room + 4 default agents are created in a single Postgres transaction.
- **Account deletion:** Cascade delete all user data; revoke OAuth tokens.
  Required for compliance with Google API user-data policies.

---

## 9. Outreach pipeline (detailed)

```
User in chat: "Email these three about pet fees for a small dog"
        │
        ▼
Outreach Agent:
  1. fetches listing.contact_email or contact_phone for each listing
     (zpid in scope; fall back to web-form-only flag)
  2. drafts 3 emails — uses listing context + user profile + question
  3. posts EmailCard previews in chat
        │
User clicks "Send all"
        │
        ▼
Backend (FastAPI):
  1. enqueues 3 send jobs in Celery
  2. each job: load Gmail OAuth, send via Gmail API, persist
     gmail_thread_id + tracking_marker, mark outreach_thread.status='sent'
        │
        ▼
Gmail Push Notification → Pub/Sub → /webhook/gmail
  1. authenticate webhook (Google signed JWT)
  2. fetch new messages, match to outreach_thread via tracking_marker
  3. persist outreach_messages
  4. enqueue summarization job
        │
        ▼
Summarization worker:
  1. LLM summarizes new messages + thread context
  2. inserts a message into chat with sender=outreach_agent
  3. Supabase Realtime pushes to client
        │
        ▼
User sees: "Crown Apartments replied: $35 pet fee, available May 15..."
```

**Failure modes & handling:**
- Bounce / undeliverable → mark thread `status=bounced`; notify user in chat.
- No reply within N days (configurable, default 4) → Outreach Agent prompts
  user: "No reply yet — want me to send a friendly follow-up?"
- Landlord asks for credit/background → Agent does NOT provide that data
  (out of scope); replies that user will follow up directly.
- User revokes Gmail OAuth → all in-flight outreach paused; chat shows
  reconnect prompt.

---

## 10. Deployment & ops

### Hosting
- **Frontend:** Vercel (Next.js 14, Edge runtime where possible).
- **Backend:** Railway or Fly.io (FastAPI Docker image, 2× small instances
  behind LB at launch).
- **Workers:** Same Railway/Fly cluster, separate Celery worker dyno.
- **Database:** Supabase managed Postgres (pgvector enabled).
- **Cache / queue:** Upstash Redis (managed, low-ops).

### CI/CD
- GitHub repo: `tangmaowen/rentwise`.
- GitHub Actions: lint, typecheck, unit tests on PR; deploy to Vercel
  preview + Railway preview environment.
- Main → production auto-deploy.

### Observability
- Structured logs (JSON) via stdlib `logging` + Vercel/Railway log streams.
- PostHog product analytics events.
- Sentry frontend + backend.
- Supabase metrics (DB CPU, connection pool).
- Uptime: Better Uptime or similar for /healthz.

### Cost envelope at 100 active users (rough)
| Service | Monthly |
|---------|--------:|
| Vercel Pro | $20 |
| Railway / Fly | $30–60 |
| Supabase Pro | $25 |
| Upstash Redis | $10 |
| Anthropic Claude | $80–200 (depends on usage) |
| Google Maps | $30–80 |
| Mapbox | $0 (free tier) |
| PostHog | $0 (free tier <1M events) |
| Sentry | $0 (free tier) |
| **Total** | **~$200–400** |

---

## 11. Security & legal

### Data handling
- PII: user email, name, OAuth tokens, chat history.
- Encryption at rest: Supabase default (AES-256). OAuth refresh tokens
  additionally encrypted at app layer using Supabase pgsodium (column-level
  envelope encryption with a key managed via Supabase Vault).
- TLS 1.2+ everywhere.
- Audit log table for sensitive actions (outreach send, account delete).

### Gmail user-data policy
- We must publish a privacy policy + ToS before going live.
- We must explain to users what data we read/send and why.
- We must support full data deletion on request.
- We use the *minimum* scopes (`gmail.send`, `gmail.readonly`,
  `gmail.modify` only on our label namespace).

### Zillow ToS
- Our scraper violates Zillow's ToS in the strict reading. For the v1 beta
  this is a known risk; we use the data we have and disclose origin to
  end users.
- Real-product mitigation path (post-v1): partner with a licensed listings
  data provider (Apartments.com API, RentSpider, MLS feeds) and migrate
  off scraped data.

### CAN-SPAM (outreach)
- User is the legal sender; we facilitate.
- Body must include: sender identification, valid physical address (the
  user's, not ours), opt-out mechanism, accurate subject line.
- We pre-template these elements; user can preview and reject.

### Terms users agree to at signup
- Permission for us to send email on their behalf via Gmail OAuth.
- Acknowledgment that outreach is direct from their Gmail (not from us).
- Acknowledgment that listings are from third-party sources and may be
  out-of-date.

---

## 12. Testing & evaluation

### Unit / integration
- Pytest backend; Vitest frontend.
- Each agent has unit tests for tool selection + golden-output snapshots
  on synthetic listings.

### Agent eval suite (lightweight, in-repo)
- Curated 30-question test set across all 4 agents:
  - Search: 10 NL queries with expected top-3 listings hand-labeled.
  - Property Analyst: 10 factual questions with expected answers from JSONL.
  - Location: 5 commute queries against canned ground-truth.
  - Outreach: 5 draft-email scenarios — checked for required elements
    (sender ID, listing reference, opt-out language).
- Run on every PR; diff against last main.

### LLM-as-judge for ranking quality
- For Search: judge model (Opus) scores ranking output vs. user prefs on
  precision-at-3. Tracks regressions between prompt/model changes.

### Manual user testing
- 5–10 friendly testers in San Jose during beta. Weekly 30-min sessions.
- Sentry + PostHog session replay (with consent) to catch UX dead-ends.

---

## 13. Roadmap

### v1 (target: live in 2–3 months)
- All of §2 v1 goals.
- Single-city (San Jose), 4 default agents + 3–5 catalog agents enabled,
  free beta, no payments.

### v1.1 (post-launch)
- Custom NL-defined agents (user describes → app generates system prompt + tools).
- Stripe subscription billing live.
- Saved searches with weekly digest emails ("3 new matches this week").
- Move-in tour scheduling agent (calendar integration).

### v2
- Multi-city expansion (Bay Area: SF, Oakland, Mountain View, Sunnyvale).
- Multi-modal CV scoring on listing photos (CLIP).
- NLP review summarization (synthesize Yelp / Reddit signals).
- Trained ranker (replace LLM-as-ranker with LTR model trained on our
  click data).
- Shared workspaces (couple / roommate group).

### v3+
- License legitimate listing data feeds; deprecate scraper.
- Mobile native apps (iOS first).
- Landlord-side product (the marketplace flip).

---

## 14. Open decisions / TBD

These need user input before implementation begins. Defaults are noted in
brackets where applicable.

1. **UX target reference** — User mentioned slock.ai as an inspiration with
   "more fancy." I cannot fetch slock.ai's content; need screenshots or a
   detailed description of the specific UX patterns to match. [DEFAULT:
   Slack-style 3-pane layout described in §6.]

2. **Agent extensibility model** — Curated catalog only? Custom NL-defined
   agents? Both? [DEFAULT: catalog-only in v1, custom in v1.1.]

3. **Domain name** — `rentwise.ai`? `rentwise.app`? Other? Needs to be
   purchased before we set up Vercel custom domain.

4. **GitHub repo location** — Personal account `tangmaowen/rentwise` or
   create an org `rentwise-ai/web`?

5. **Team split** — Proposal lists Maowen, Tianxin, Yanhuan. Will all three
   contribute code? If yes: do we need GitHub access provisioning + a basic
   PR workflow / review pattern documented?

6. **Beta tester recruiting** — Do we have a list, or do we need to
   build one before launch?

7. **Pricing model** — Free + ad-supported? Subscription tier? Pay-per-
   listing-contacted? Affects v1.1 Stripe wire-up. [DEFAULT: free during
   v1 beta; revisit pre-v1.1.]

8. **Catalog agent shortlist** — Which 3–5 of the candidates in §4.2 ship
   at launch? [DEFAULT: Lease Clause Auditor, Roommate Splitter,
   Move-In Checklist.]

9. **Outreach send authority** — Default to "draft + 1-click send" per
   email, or allow auto-send for some scenarios? [DEFAULT: 1-click send
   only in v1.]

10. **Outreach rate limits** — Confirm ≤10/day/user. Is this enough? Too
    aggressive? [DEFAULT: 10.]

---

## Appendix A — Source data fields available (from existing scrape)

The 910 enriched listings include these fields per record:

- Identity: `zpid`, `building_name`, `full_address`, `lat/lng`,
  `neighborhood`, `neighborhood_description`, `neighborhood_highlights`
- Pricing: `rent_min/max` per bed-type (studio/1BD/2BD/3BD), `deposit_min/max`,
  `application_fee`, `admin_fee`, `utilities_included`, `lease_terms`
- Capacity: `unit_count`, `available_unit_count`, `floor_plans` (per-plan
  beds/baths/sqft/price)
- Physical: `building_amenities`, `unit_features`, `appliances`,
  `community_rooms`, `outdoor_common_areas`, `parking_types`, `view_types`,
  ~25 boolean `has_*` flags (pool, elevator, storage, etc.)
- Policies: `pets_allowed`, `pet_groups` (per-pet-type weight/fee/deposit),
  `is_furnished`, `is_smoke_free`
- Quality scores (pre-computed externally): `walk_score`, `transit_score`,
  `bike_score` with labels
- Schools: per-listing array with `name`, `rating`, `distance`, `level`,
  `grades`, `type`, `students_per_teacher`
- Content: long-form `description`, `faqs`, photo URLs (in raw JSONL but
  not yet downloaded)
- Contact: `phone`, `agent_name` (no email — contact email is **not**
  surfaced by Zillow; the Outreach Agent will need to use phone as the
  fallback channel for many listings, OR scrape leasing-office sites for
  emails as a separate enrichment step).

**Critical note for outreach:** We do not have leasing-office emails in
the scraped data. The Outreach Agent's email channel will only work if we
either (a) extract emails from listing descriptions / building websites
during enrichment, or (b) require user to provide the email when initiating
outreach. **This is a gap between the proposal and the data — flagged in
§14 as TBD #11 below.**

11. **Outreach contact discovery** — How do we get leasing-office email
    addresses? Options:
    - (a) Add a sub-scraper that visits each listing's official building
      website and extracts contact emails.
    - (b) User provides the email per-outreach (manual).
    - (c) Send via web contact forms instead of email (much harder
      automation).
    - (d) Send via SMS/phone using Twilio (most listings have phone).
    [DEFAULT: (a) — add a contact-discovery sub-scraper as Phase 2.5 of
    enrichment. Visits each listing's `building_url` (when present),
    extracts `mailto:` and visible email addresses from contact pages,
    falls back to (b) user-provides for listings without discoverable
    emails. Twilio (d) deferred to v1.1 as a fallback channel.]
