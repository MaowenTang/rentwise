# Agent tool-use — design

**Status:** approved 2026-05-05 · pilot ships in same PR (Property as lead).

## Problem

Today's router picks one specialist per turn; that specialist replies in
isolation. Cross-cutting questions ("compare 692 Bush vs 333 Hyde — which
is quieter, what's the commute, what do reviews say?") force one agent
to either (a) hallucinate the other agents' specialties, or (b) reply
narrowly and let the user manually invoke the missing agent next turn.

## Decision

Move from **single-dispatch** to **lead + tool-callable specialists**:

- The router still picks one agent per user turn. That agent is the **lead**.
- The lead may invoke other agents' skills via Anthropic's tool-use API
  inside its `handle()` loop.
- Every tool call is recorded; final reply **cites** the tools that
  contributed ("per Location agent: 12 min by car…") so the chat reads
  like a discussion, not a magic box.

## Tool registry

Pure functions of `(zpid → fact)` registered as Anthropic tools.

| Agent | Tools |
|---|---|
| **Search** | `find_listings(query, filters)` |
| **Property Analyst** | `get_facts(zpid, fields[])` |
| **Location & Commute** | `get_commute(zpid, to_address)`, `get_walkability(zpid)`, `nearby_pois(zpid, category)` |
| **Resident Reviews** | `summarize_reviews(zpid)` |
| **Outreach** | (none — terminal action) |

## Coordinator model

Lead-as-tool-user. No new orchestrator class. Each agent's `handle()`
gains an optional tool-use loop. **No peer-to-peer calls** — only the
lead's loop sees the tool registry, which makes A↔B↔A cycles structurally
impossible.

## Visibility

- Final reply cites tool authors inline: *"per @location: 12 min…"*
- `metadata.tool_calls = [{agent, tool, args, latency_ms}, …]` on every
  ChatResponse.
- Frontend renders a small "🔧 Used: @location, @reviews (1.4s)" footer
  under the agent's message bubble.

This sets up Pattern C (visible inter-agent chat) cleanly: the footer
becomes inline messages by changing only the renderer.

## Guards

| Risk | Guard |
|---|---|
| Agent calls its own tool → recursion | Lead's tool registry excludes its own tools |
| Runaway tool calls inflate latency / cost | Hard cap: 5 tool calls per user turn |
| Tool gets a zpid not in current shortlist scope | Returns `{"error": "out of scope"}` instead of raising |
| LLM hallucinates a tool name | Anthropic SDK rejects unknown names; we log + skip |

## Migration

Single PR (per user request — skip the refactor-only gate):

1. New `api/agents/tools.py` — pure-function skills + Anthropic tool schema.
2. `api/agents/base.py` — extend with `tool_use_loop()` helper.
3. `api/agents/property.py` — pilot: rewrite `handle()` to use the loop.
4. `api/main.py` — surface `tool_calls` in response metadata.
5. `web/app/page.tsx` — render the tool-call footer.

Other agents (Search, Location, Reviews, Outreach) keep their current
`handle()` for now. Their tools are **callable but they themselves
remain single-dispatch**. Roll-out to other agents is a follow-up PR.

## Out of scope

- Peer-to-peer agent chat (Pattern C). Footer + citations are the bridge.
- Streaming. Tool calls happen synchronously inside one POST /chat.
- Persistent tool-call history (replay across restarts).
