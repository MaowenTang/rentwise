"""Search Agent — conversational discovery + ranked search + auto-shortlist.

Flow:
  1. ProfileUpdater (called from main.py before this agent) has already
     extracted any preferences from the user message.
  2. If profile.is_rich_enough() returns False, ask ONE clarifying
     question instead of searching.
  3. Otherwise: hard-filter by profile, score with RankingService,
     return top 5, auto-add to session.shortlist.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict

from listings import Listing, filter_listings
from profile import RankingService, UserProfile

from .base import AgentReply, BaseAgent

# "Show Me More" — user wants the next batch of listings without changing
# their search criteria.  Matched before the full search pipeline runs so
# we can fast-path from the stored candidate pool (no re-score, no LLM call).
_SHOW_MORE_RE = re.compile(
    r"\b(show\s+me\s+more|more\s+options|more\s+listings|next\s+batch|see\s+more|load\s+more)\b"
    r"|再推几个|还有其他的吗|看更多|再来几个|多看几个|再多看|多推几个",
    re.IGNORECASE,
)

# Reviews pre-warm — best-effort background fetch for top results.
# Deferred import so SearchAgent still works if reviews_fetcher.py isn't ready.
try:
    from reviews_fetcher import get_reviews, get_reviews_or_fetch as _fetch_reviews
    _REVIEWS_AVAILABLE = True
except ImportError:
    _REVIEWS_AVAILABLE = False


def _prewarm_reviews(listings: list[Listing]) -> None:
    """Spawn a daemon thread to pre-warm the reviews cache for top results.

    Called non-blocking from SearchAgent.handle() after results are ranked.
    For each listing not already in cache, triggers get_reviews_or_fetch()
    in the background so Resident Reviews Agent hits cache on the user's
    next question (Story 4).
    """
    if not _REVIEWS_AVAILABLE:
        return

    def _fetch_one(zpid: str, stub: dict) -> None:
        try:
            if not get_reviews(zpid):  # cache miss — fetch and populate
                # get_reviews_or_fetch is async; asyncio.run() creates a fresh
                # event loop in this daemon thread (safe — threads have no loop).
                import asyncio
                asyncio.run(_fetch_reviews(zpid, stub))
        except Exception:
            pass  # best-effort; never block or raise

    for L in listings[:5]:
        if not L.zpid:
            continue
        stub = {
            "zpid": L.zpid,
            "name": L.name,
            "address": L.address,
            "lat": L.lat,
            "lng": L.lng,
        }
        threading.Thread(target=_fetch_one, args=(L.zpid, stub), daemon=True).start()


CLARIFY_PROMPT = """You are RentWise's Search Agent. The user is starting an
apartment search but you don't yet have enough info to make a useful
recommendation.

Ask ONE concise, friendly question that would most improve the search.
Prioritize what's missing in this order:
  1. budget (max monthly rent)
  2. bed count (studio? 1BR? 2BR?)
  3. pets (do they have any? this is a hard filter — critical to know early)
  4. location signal (commute target like a workplace, OR preferred neighborhoods)
  5. other must-haves (parking, in-unit laundry, pool, etc.)

Already-known profile:
{profile_summary}

User's latest message:
"{message}"

Return ONLY the question text — one or two sentences max, no preamble.
Be specific. Example good questions:
  - "What's your max monthly budget?"
  - "1-bedroom or studio? Or open to either?"
  - "Where do you work or commute to most? I can prioritize listings closer to it."
"""


RANK_PROMPT = """You are RentWise's Search Agent. The user's evolving profile is:

{profile_json}

Below are {n} candidate listings (already pre-filtered to honor hard
budget/beds/pets constraints) with their RENT, WALK_SCORE, etc., plus
a heuristic SCORE we've computed against this profile.

Pick the BEST 5 to show the user, ranked from best to worst. Trust the
heuristic SCORE but feel free to tie-break on qualitative factors. For
each pick, write a SHORT one-sentence rationale that ties to the user's
profile.

Return ONLY a JSON array (no prose, no fences):
[
  {{"zpid": "...", "rationale": "..."}}, ...
]

If the data forced a tradeoff (e.g., over budget by 5%), prepend a single
relax note object: {{"_note": "<short note>"}}.

CANDIDATES:
{candidates}
"""


def _strip_fences(text: str) -> str:
    """Best-effort cleanup before json.loads.

    Handles:
      • ```json ... ``` code fences
      • Claude adding a "Sure, here's the ranking:" preamble or post-script
        — extracts the first [...] balanced JSON array from the text.
      • Trailing commentary after the array
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    # If text starts with [ or { we're already good. Otherwise try to find
    # the first JSON array in the response.
    if not text.startswith(("[", "{")):
        start = text.find("[")
        if start == -1:
            return text  # let json.loads fail loudly
        # Find matching `]` by depth counting (handles nested arrays).
        depth = 0
        end = -1
        in_str = False
        esc = False
        for i, ch in enumerate(text[start:], start=start):
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end != -1:
            text = text[start:end + 1]
    return text


class SearchAgent(BaseAgent):
    name = "search"

    def __init__(self, listings: list[Listing], ranker: RankingService | None = None, **kw):
        super().__init__(**kw)
        self.listings = listings
        self.by_zpid = {L.zpid: L for L in listings if L.zpid}
        self.ranker = ranker or RankingService()

    # --- conversational discovery ----------------------------------------

    def _clarify(self, message: str, profile: UserProfile) -> str:
        prompt = CLARIFY_PROMPT.format(
            profile_summary=profile.to_summary(),
            message=message,
        )
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()

    # --- ranking ---------------------------------------------------------

    def _candidate_card(self, L: Listing, score) -> dict:
        return {
            "zpid": L.zpid,
            "name": L.name,
            # Use neighborhood as address fallback when Zillow hides the exact
            # address — prevents "(Undisclosed Address)" from appearing in the
            # LLM rationale text the user sees in chat.
            "address": (
                L.address
                if L.address and L.address.strip() != "(Undisclosed Address)"
                else (L.neighborhood or "address not available")
            ),
            "neighborhood": L.neighborhood,
            "rent_min": L.rent_min,
            "rent_max": L.rent_max,
            "rent_by_bed": {
                ("Studio" if b == 0 else f"{b}BR"): {"min": mn, "max": mx}
                for b, (mn, mx) in L.rent_by_bed.items()
            },
            "walk_score": L.walk_score,
            "transit_score": L.transit_score,
            "pets_allowed": L.pets_allowed,
            "url": L.url,
            "_heuristic_score": score.overall,
            "_heuristic_components": score.components,
        }

    def _llm_rank(self, profile: UserProfile, scored: list[tuple[Listing, "ScoreBreakdown"]]) -> tuple[list[Listing], str | None]:
        # scored is already sorted descending by overall score; cap at 50
        # (bumped from 25 now that semantic blend helps the LLM distinguish
        # listings with similar heuristic scores via description quality)
        top = scored[:50]
        cards = [self._candidate_card(L, s) for L, s in top]
        prompt = RANK_PROMPT.format(
            profile_json=json.dumps(asdict(profile), default=str, indent=2),
            n=len(cards),
            candidates=json.dumps(cards, indent=2),
        )
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text
        text = _strip_fences(raw)
        try:
            picks = json.loads(text)
        except json.JSONDecodeError as e:
            import logging
            logging.getLogger("rentwise.api").warning(
                "rank parse failed: %s | raw[:200]=%r | stripped[:200]=%r",
                e, raw[:200], text[:200],
            )
            return [L for L, _ in top[:5]], None  # silently fall back; error already logged above

        note: str | None = None
        if isinstance(picks, list) and picks and isinstance(picks[0], dict) and "_note" in picks[0]:
            note = picks[0]["_note"]
            picks = picks[1:]

        ranked: list[Listing] = []
        rationales: dict[str, str] = {}
        for p in picks:
            if not isinstance(p, dict):
                continue
            zpid = str(p.get("zpid", ""))
            L = self.by_zpid.get(zpid)
            if L is not None:
                ranked.append(L)
                rationales[zpid] = p.get("rationale", "")
        if not ranked:
            ranked = [L for L, _ in top[:5]]
        for L in ranked:
            L.raw["_rationale"] = rationales.get(L.zpid, "")
        return ranked, note

    # --- markdown render -------------------------------------------------

    def _render(self, ranked: list[Listing], note: str | None, profile: UserProfile) -> str:
        lines: list[str] = ["**Top matches** _(also pinned to your shortlist on the right)_"]
        if note:
            lines.append("")
            lines.append(f"> ⚠️ {note}")
        lines.append("")

        # Precise bed targeting: if the user asked for 1BR specifically,
        # only show the 1BR row (not Studio / 2BR / 3BR rows that the
        # complex also has). Listing still passes the hard filter because
        # it offers 1BR — we just don't clutter the card with irrelevant
        # bed types.
        u_min = profile.beds_min
        u_max = profile.beds_max
        any_pref = u_min is not None or u_max is not None

        for i, L in enumerate(ranked, 1):
            loc = L.neighborhood or "—"
            url = L.url or ""
            rationale = L.raw.get("_rationale", "").strip()

            beds_in_scope = sorted(L.rent_by_bed)
            if any_pref:
                lo = u_min if u_min is not None else min(beds_in_scope, default=0)
                hi = u_max if u_max is not None else max(beds_in_scope, default=99)
                beds_to_show = [b for b in beds_in_scope if lo <= b <= hi] or beds_in_scope
            else:
                beds_to_show = beds_in_scope

            bed_lines: list[str] = []
            for b in beds_to_show:
                mn, mx = L.rent_by_bed[b]
                label = "Studio" if b == 0 else f"{b}BR"
                if mn and mx and mn != mx:
                    bed_lines.append(f"{label} ${mn:,}–${mx:,}")
                elif mn and mx:
                    bed_lines.append(f"{label} ${mn:,}")
                elif mn:
                    bed_lines.append(f"{label} from ${mn:,}")
                else:
                    bed_lines.append(f"{label} rent ?")
            bed_str = " · ".join(bed_lines) if bed_lines else "rent ?"

            lines.append(f"{i}. **{L.name}** · {loc}")
            lines.append(f"   {bed_str}")
            if rationale:
                lines.append(f"   {rationale}")
            if url:
                lines.append(f"   [View on Zillow]({url})")
            lines.append("")
        return "\n".join(lines).rstrip()

    # --- handle ----------------------------------------------------------

    def handle(self, message: str, session) -> AgentReply:  # noqa: ANN001
        profile: UserProfile = session.profile

        # ── "Show Me More" fast-path ──────────────────────────────────────────
        # User wants the next window of results with the same search criteria —
        # no profile update, no re-scoring, no LLM ranking call.
        # Requirements (Mira's AC): same sort order, different page.
        if _SHOW_MORE_RE.search(message) and session.search_candidate_pool:
            pool = session.search_candidate_pool
            fresh = [L for L in pool if L.zpid not in session.shown_zpids]
            if fresh:
                next_batch = fresh[:5]
                for L in next_batch:
                    session.add_to_shortlist(L, via="search")
                    session.shown_zpids.add(L.zpid)
                session.listings_in_scope = next_batch
                session.rescore_shortlist(self.ranker)
                _prewarm_reviews(next_batch)
                markdown = self._render(next_batch, None, profile)
                return AgentReply(
                    agent=self.name,
                    text=markdown,
                    metadata={
                        "phase": "results",
                        "next_batch": True,
                        "ranked_zpids": [L.zpid for L in next_batch],
                        "profile_summary": profile.to_summary(),
                    },
                )
            # Pool exhausted — fall through to a fresh full search below so
            # the user at least gets something (pool will be refreshed).

        if not profile.is_rich_enough():
            question = self._clarify(message, profile)
            return AgentReply(
                agent=self.name,
                text=question,
                awaiting=["profile_input"],
                metadata={
                    "phase": "clarifying",
                    "profile_summary": profile.to_summary(),
                },
            )

        # Hard-filter by profile, then score everything that passes.
        # Pass full pets/neighborhoods lists — filter_listings now handles
        # multi-value correctly (all pets required; any neighborhood matches).
        filtered = filter_listings(
            self.listings,
            max_rent=profile.budget_max,
            min_beds=profile.beds_min,
            max_beds=profile.beds_max,
            pets=profile.pets or None,
            neighborhoods=profile.neighborhoods or None,
        )
        if not filtered:
            # Soft-fallback: drop neighborhood filter
            filtered = filter_listings(
                self.listings,
                max_rent=profile.budget_max,
                min_beds=profile.beds_min,
                max_beds=profile.beds_max,
                pets=profile.pets or None,
            )
            relax_msg = " (relaxed neighborhood filter to find matches)" if filtered else ""
        else:
            relax_msg = ""

        if not filtered:
            return AgentReply(
                agent=self.name,
                text=(
                    "Nothing matched even after relaxing the neighborhood. "
                    "Want me to widen the budget or bed count?"
                ),
                metadata={"phase": "no_results", "profile_summary": profile.to_summary()},
            )

        # Dynamic Feedback Loop — exclude listings already shown this session so
        # re-searches surface fresh options instead of repeating the same results.
        # If exclusion empties the pool (user has seen everything), fall through
        # with all candidates so they at least get something.
        if session.shown_zpids:
            fresh = [L for L in filtered if L.zpid not in session.shown_zpids]
            if fresh:
                filtered = fresh

        # Hard pre-filter via RankingService.pre_filter_with_fallback —
        # tries strict commute cap first, then 1.5x and 2.0x; only as a last
        # resort drops the commute filter. avoid_cities, furnished, and lease
        # filters are *never* relaxed (they're true hard constraints).
        try:
            hard_filtered, exc = self.ranker.pre_filter_with_fallback(filtered, profile)
            if hard_filtered:
                filtered = hard_filtered
                slack = exc.get("commute_slack")
                if slack == "dropped":
                    relax_msg += " (relaxed commute filter — fewer matches in range)"
                elif isinstance(slack, float) and slack > 1.0:
                    relax_msg += f" (expanded commute by {int((slack-1)*100)}% to find matches)"
            # If hard_filtered is empty even after full relaxation, keep the
            # soft-filtered set rather than returning nothing; ranker.score
            # will still apply its hard_filter_violated penalty.
        except Exception:
            pass  # never break ranking; fall back to soft scoring

        scored = [(L, self.ranker.score(L, profile)) for L in filtered]
        scored.sort(key=lambda t: -t[1].overall)

        # Persist the full heuristic-sorted pool for "Show Me More" pagination.
        # Stored BEFORE LLM selection so the pool is stable (same sort order)
        # across multiple "show me more" calls without re-scoring.
        session.search_candidate_pool = [L for L, _ in scored]

        ranked, note = self._llm_rank(profile, scored)
        if relax_msg:
            note = (note or "") + relax_msg

        # Auto-add top 5 to shortlist; record shown zpids for re-search exclusion
        for L in ranked:
            session.add_to_shortlist(L, via="search")
            session.shown_zpids.add(L.zpid)
        session.listings_in_scope = ranked
        session.rescore_shortlist(self.ranker)

        # Pre-warm Resident Reviews cache for top results (Story 4).
        # Non-blocking: spawns daemon threads, never delays this response.
        _prewarm_reviews(ranked)

        markdown = self._render(ranked, note, profile)
        return AgentReply(
            agent=self.name,
            text=markdown,
            metadata={
                "phase": "results",
                "ranked_zpids": [L.zpid for L in ranked],
                "profile_summary": profile.to_summary(),
            },
        )
