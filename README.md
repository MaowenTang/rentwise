# RentWise — v0 Scaffold

A minimal runnable demo of the multi-agent apartment-hunting chat. **Not the production app** — just enough to validate the UX direction. See `docs/superpowers/specs/2026-05-01-rentwise-design.md` for the full spec.

## What's in this scaffold

- **Search Agent only** (Property Analyst, Location, Outreach are placeholder chips).
- **910 San Jose listings** loaded from the Zillow scraper output.
- 3-pane Slack-style chat UI.
- No auth, no DB, no streaming, no real cards — markdown text only.

## Prerequisites

- Node 20+ (you have 23 ✅)
- Python 3.12 ✅
- An [Anthropic API key](https://console.anthropic.com/settings/keys) (free trial credits available).

## Setup (one-time)

```bash
# 1. API setup
cd api
cp .env.example .env
# Edit .env — set ANTHROPIC_API_KEY=sk-ant-...
# (LISTINGS_PATH already points to the existing scraper output)

# 2. Web setup
cd ../web
cp .env.local.example .env.local   # API URL is already correct for local dev
```

## Run (two terminals)

```bash
# Terminal 1 — API
cd ~/Downloads/rentwise/api
source .venv/bin/activate
uvicorn main:app --reload --port 8000

# Terminal 2 — Web
cd ~/Downloads/rentwise/web
npm run dev
```

Open http://localhost:3000.

In the chat box, try:

- `2BR under $3500 near downtown San Jose, dog-friendly`
- `studio with a pool, walk score above 80`
- `cheapest 1-bed that allows cats`

## Sanity checks

- `curl http://localhost:8000/healthz` should show `listings_loaded: 910` and `anthropic_key_present: true`.
- The sidebar shows a green dot ("api ready") when the frontend successfully reaches the backend.

## Known v0 limitations

- No streaming — full reply lands at once.
- No "Pin to right rail" wired up yet.
- Other 3 agents are disabled chips.
- No persistence — refreshing the page clears chat history.
- No auth — anyone with the URL can use it.

These are addressed in v1 per the design spec.
