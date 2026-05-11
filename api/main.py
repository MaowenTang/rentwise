"""RentWise API — v0 scaffold.

Per turn:
  1. ProfileUpdater extracts revealed prefs from the user message.
  2. Agent Router dispatches to one of 4 specialist agents.
  3. The agent runs and may add listings to the shortlist.
  4. Shortlist is re-scored against the (possibly updated) profile.
  5. Response includes: reply, agent, profile summary, shortlist with scores.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from dataclasses import asdict

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agents.location import LocationCommuteAgent
from agents.outreach import OutreachAgent
from agents.property import PropertyAnalystAgent
from agents.reviews import ResidentReviewsAgent
from agents.router import AgentRouter
from agents.search import SearchAgent
from listings import load_listings
from profile import ProfileUpdater, RankingService, SemanticRanker
from session import ChatTurn, SessionStore

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
LOG = logging.getLogger("rentwise.api")

STATE: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    LOG.info("loading listings...")
    listings = load_listings()
    LOG.info("loaded %d listings", len(listings))

    # P3 — Pre-compute semantic embeddings (bge-small-en-v1.5 via fastembed).
    # Gated behind ENABLE_SEMANTIC_RANKING env var (default: false) so the
    # 130 MB ONNX model download and ~200-250 MB resident RAM cost are only
    # incurred when the host has headroom.  On Render free tier (512 MB RAM,
    # ephemeral disk — model re-downloads on every deploy) keep this off;
    # flip to "true" when upgrading to a paid plan with persistent disk.
    # The heuristic ranker continues to work correctly when this is off.
    semantic: SemanticRanker | None = None
    if os.environ.get("ENABLE_SEMANTIC_RANKING", "false").lower() == "true":
        LOG.info("P3 semantic ranking enabled — pre-computing embeddings...")
        semantic = SemanticRanker()
        semantic.precompute(listings)
    else:
        LOG.info("P3 semantic ranking disabled (set ENABLE_SEMANTIC_RANKING=true to enable)")

    ranker = RankingService(semantic=semantic)
    STATE["listings"] = listings
    STATE["ranker"] = ranker
    STATE["sessions"] = SessionStore()
    STATE["profile_updater"] = ProfileUpdater()

    # Build cross-namespace review index so listings without direct cache
    # hits (Craigslist cl_*, apartments.com apt:*) can still pick up
    # reviews from buildings with matching names (verified by lat/lng).
    try:
        from reviews_fetcher import _build_review_name_index
        zpid_to_coords = {L.zpid: (L.lat, L.lng) for L in listings if L.zpid}
        _build_review_name_index(zpid_to_coords)
    except Exception as e:
        LOG.warning("review name-index build failed: %s", e)
    STATE["agents"] = {
        "search": SearchAgent(listings, ranker=ranker),
        # Property is the pilot for cross-agent tool-use; needs the full
        # listings pool so its `search__find_listings` tool can search
        # beyond the current shortlist scope.
        "property": PropertyAnalystAgent(all_listings=listings),
        "location": LocationCommuteAgent(),
        "outreach": OutreachAgent(),
        "reviews": ResidentReviewsAgent(),
    }
    STATE["router"] = AgentRouter()
    yield
    STATE.clear()


app = FastAPI(title="RentWise API", version="0.0.3", lifespan=lifespan)

# --- Auth + user persistence ------------------------------------------------
from db import init_db, log_chat_event, log_interaction, save_profile, load_profile  # noqa: E402
from auth import router as auth_router, get_current_user, get_current_user_optional  # noqa: E402

init_db()
app.include_router(auth_router)

# CORS — accept localhost (dev) + any explicit origins from CORS_ORIGINS env
# (comma-separated). Plus any *.vercel.app preview/prod domain via regex.
_origins = ["http://localhost:3000", "http://127.0.0.1:3000"]
_extra = os.environ.get("CORS_ORIGINS", "").strip()
if _extra:
    _origins += [o.strip() for o in _extra.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    session_id: str
    message: str
    # Client-side state mirror — used to re-hydrate session when backend
    # instance restarts (Render free tier spins down after 15min idle).
    # Frontend sends its current profile + the zpids it has on screen
    # every turn; backend uses them only if its session is empty.
    client_profile: dict | None = None
    client_scope_zpids: list[str] | None = None


class ChatResponse(BaseModel):
    reply: str
    agent: str
    router_reason: str | None = None
    metadata: dict | None = None
    profile: dict
    profile_summary: str
    shortlist: list[dict]
    # Cross-agent tool-call log — when the lead agent calls another
    # agent's tool (e.g. property → location.get_walkability), each
    # call is recorded so the frontend can render a "🔧 Used:" footer
    # under the message bubble. Empty when the lead used no tools.
    tool_calls: list[dict] = []


class ShortlistMutation(BaseModel):
    session_id: str
    zpid: str


class CommuteInit(BaseModel):
    name: str
    address: str | None = None
    max_minutes: int | None = None


class OnboardingPayload(BaseModel):
    """Result of the 3-step onboarding questionnaire.

    importance_ranking is an ordered list of feature keys (most important first).
    Maps to RankingService component weights via a fixed schedule:
      rank 1 -> 4.0, rank 2 -> 3.0, rank 3 -> 2.0,
      rank 4 -> 1.5, rank 5 -> 1.0, rank 6 -> 0.5
    Recognized keys: budget, commute, pets, amenities, walkable, transit.
    """
    session_id: str
    user_name: str = ""
    budget_max: int | None = None
    beds_min: int | None = None
    beds_max: int | None = None
    pets: list[str] = []
    commute: CommuteInit | None = None
    must_haves: list[str] = []
    avoid: list[str] = []
    importance_ranking: list[str] = []


class ProfileRemoval(BaseModel):
    """Remove or clear a profile field.

    For list fields (pets, must_haves, nice_to_haves, avoid, neighborhoods),
    pass `value` to remove a specific item. Pass `value=null` to clear
    the whole list.

    For scalar fields (budget_max, beds_min, beds_max, commute, notes),
    pass `value=null` to clear.
    """
    session_id: str
    field: str
    value: str | None = None


@app.get("/healthz")
def healthz():
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    return {
        "ok": True,
        "listings_loaded": len(STATE.get("listings", [])),
        "anthropic_key_present": key.startswith("sk-") and "..." not in key,
        "agents": list(STATE.get("agents", {}).keys()),
    }


@app.get("/listings")
def listings_summary():
    L = STATE.get("listings", [])
    return {
        "count": len(L),
        "sample": [
            {"name": x.name, "address": x.address, "rent_min": x.rent_min}
            for x in L[:5]
        ],
    }


def _profile_dict(session) -> dict:  # noqa: ANN001
    d = asdict(session.profile)
    if d.get("commute") is None:
        d.pop("commute", None)
    return d


def _profile_from_dict(d: dict):
    """Re-hydrate a UserProfile dataclass from a JSON dict (frontend mirror).

    Tolerant of missing fields; falls back to defaults. Used when the
    backend instance restarted but the frontend still has the user's
    profile in React state.
    """
    from profile import CommuteTarget, UserProfile
    p = UserProfile()
    for k in ("user_name", "move_in_date", "notes"):
        v = d.get(k)
        if isinstance(v, str):
            setattr(p, k, v)
    for k in ("budget_max", "beds_min", "beds_max"):
        v = d.get(k)
        if isinstance(v, int):
            setattr(p, k, v)
    for k in ("pets", "must_haves", "nice_to_haves", "avoid", "neighborhoods"):
        v = d.get(k)
        if isinstance(v, list):
            setattr(p, k, [x for x in v if isinstance(x, str)])
    c = d.get("commute")
    if isinstance(c, dict) and c.get("name"):
        p.commute = CommuteTarget(
            name=c.get("name") or "",
            address=c.get("address") or "",
            lat=c.get("lat"),
            lng=c.get("lng"),
            max_minutes=c.get("max_minutes"),
        )
    w = d.get("weights")
    if isinstance(w, dict):
        p.weights = {k: float(v) for k, v in w.items() if isinstance(v, (int, float))}
    return p


def _hydrate_session_from_client(session, req: "ChatRequest") -> bool:
    """If the session is empty (likely a post-restart fresh session),
    populate it from the client-supplied state. Returns True if anything
    was hydrated.
    """
    hydrated = False
    p = session.profile
    profile_empty = (
        p.budget_max is None
        and not p.pets
        and not p.must_haves
        and not p.nice_to_haves
        and not p.avoid
        and not p.commute
        and not p.user_name
    )
    if profile_empty and req.client_profile:
        session.profile = _profile_from_dict(req.client_profile)
        hydrated = True

    if not session.listings_in_scope and req.client_scope_zpids:
        by_zpid = STATE["agents"]["search"].by_zpid
        session.listings_in_scope = [
            by_zpid[z] for z in req.client_scope_zpids if z in by_zpid
        ]
        # Also seed the shortlist so the right rail stays consistent.
        for L in session.listings_in_scope:
            session.add_to_shortlist(L, via="rehydrate")
        if session.listings_in_scope:
            hydrated = True

    if hydrated:
        session.rescore_shortlist(STATE["ranker"])
    return hydrated


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, current_user=Depends(get_current_user_optional)):
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key.startswith("sk-") or "..." in key:
        raise HTTPException(
            status_code=500, detail="ANTHROPIC_API_KEY not set. Edit api/.env."
        )

    sessions: SessionStore = STATE["sessions"]
    session = sessions.get(req.session_id)

    # Authed users: seed empty session with their persisted profile (so a
    # fresh tab on Render still has their long-term preferences).
    if current_user and not session.history and not session.profile.budget_max:
        stored = load_profile(current_user.id)
        if stored:
            try:
                session.profile = _profile_from_dict(stored)
                LOG.info("[%s] loaded persisted profile for user %s",
                         req.session_id[:8], current_user.email)
            except Exception as e:
                LOG.warning("[%s] failed to load profile for %s: %s",
                            req.session_id[:8], current_user.email, e)

    # Re-hydrate from frontend mirror if the session is empty (e.g. backend
    # restarted between turns and lost in-memory state).
    if _hydrate_session_from_client(session, req):
        LOG.info(
            "[%s] re-hydrated session from client mirror (profile=%s, scope=%d)",
            req.session_id[:8],
            session.profile.to_summary(),
            len(session.listings_in_scope),
        )

    session.history.append(ChatTurn(role="user", agent=None, text=req.message))

    # Snapshot profile before LLM update — used for training log
    profile_before_dict = _profile_dict(session) if current_user else None

    # 1. Update profile from this turn (parallel-safe with router because
    #    router doesn't read the profile, but the search agent does).
    updater: ProfileUpdater = STATE["profile_updater"]
    profile_before_summary = session.profile.to_summary()
    session.profile = updater.update(req.message, session.profile)
    profile_after_summary = session.profile.to_summary()
    if profile_before_summary != profile_after_summary:
        LOG.info(
            "[%s] profile: %s → %s",
            req.session_id[:8], profile_before_summary, profile_after_summary,
        )

    # 2. Route
    router: AgentRouter = STATE["router"]
    agent_id, reason = router.route(req.message, session)
    LOG.info("[%s] route → %s (%s)", req.session_id[:8], agent_id, reason)

    # 3. Dispatch
    agent = STATE["agents"][agent_id]
    reply = agent.handle(req.message, session)
    session.history.append(ChatTurn(role="agent", agent=agent_id, text=reply.text))

    # 3b. Track / clear pending clarification so next turn routes correctly
    if reply.awaiting:
        session.pending_clarification = (agent_id, reply.awaiting)
        LOG.info("[%s] %s is awaiting %s", req.session_id[:8], agent_id, reply.awaiting)
    else:
        if session.pending_clarification:
            LOG.info("[%s] cleared pending clarification", req.session_id[:8])
        session.pending_clarification = None

    # 4. Always rescore the shortlist after a turn (profile may have changed,
    #    or new entries may have been added).
    ranker: RankingService = STATE["ranker"]
    session.rescore_shortlist(ranker)

    # 5. Persist for authed users: latest profile snapshot + chat event log.
    chat_event_id = None
    if current_user:
        try:
            save_profile(current_user.id, _profile_dict(session))
            ranked_zpids = (reply.metadata or {}).get("ranked_zpids") or [
                e.listing.zpid for e in session.shortlist[:5]
            ]
            chat_event_id = log_chat_event(
                user_id=current_user.id,
                session_id=req.session_id,
                user_message=req.message,
                agent_id=reply.agent,
                router_reason=reason,
                profile_before=profile_before_dict,
                profile_after=_profile_dict(session),
                ranked_zpids=ranked_zpids,
                reply_text=reply.text,
            )
        except Exception as e:
            LOG.warning("[%s] persistence failed: %s", req.session_id[:8], e)

    response_metadata = dict(reply.metadata or {})
    if chat_event_id:
        response_metadata["chat_event_id"] = chat_event_id

    return ChatResponse(
        reply=reply.text,
        agent=reply.agent,
        router_reason=reason,
        metadata=response_metadata,
        profile=_profile_dict(session),
        profile_summary=session.profile.to_summary(),
        shortlist=session.shortlist_payload(),
        tool_calls=getattr(reply, "tool_calls", []) or [],
    )


@app.post("/shortlist/remove")
def shortlist_remove(req: ShortlistMutation, current_user=Depends(get_current_user_optional)):
    session = STATE["sessions"].get(req.session_id)
    removed = session.remove_from_shortlist(req.zpid)
    session.rescore_shortlist(STATE["ranker"])
    if current_user and removed:
        try:
            log_interaction(
                user_id=current_user.id, session_id=req.session_id,
                event_type="remove", zpid=req.zpid,
            )
        except Exception as e:
            LOG.warning("interaction log failed: %s", e)
    return {
        "ok": True,
        "removed": removed,
        "shortlist": session.shortlist_payload(),
    }


# --- Interaction logging (training data signal) -----------------------------

class InteractionEvent(BaseModel):
    session_id: str
    event_type: str            # "click" | "save" | "show_more" | etc.
    zpid: str | None = None
    rank_position: int | None = None
    chat_event_id: str | None = None
    extra: dict | None = None


@app.post("/events/track")
def events_track(req: InteractionEvent, current_user=Depends(get_current_user_optional)):
    """Receive frontend events: which listing was clicked / saved / etc.
    Anonymous users get a 204-equivalent no-op; only authed users have
    rows written. Used to build a preference-learning dataset.
    """
    if not current_user:
        return {"ok": True, "logged": False}
    try:
        iid = log_interaction(
            user_id=current_user.id,
            session_id=req.session_id,
            event_type=req.event_type,
            zpid=req.zpid,
            rank_position=req.rank_position,
            chat_event_id=req.chat_event_id,
            extra=req.extra,
        )
        return {"ok": True, "logged": True, "event_id": iid}
    except Exception as e:
        LOG.warning("events_track failed: %s", e)
        raise HTTPException(status_code=500, detail="event log failed")


@app.get("/events/my-history")
def my_history(current_user=Depends(get_current_user), limit: int = 50):
    """Authed user can see their own chat history (debugging / transparency)."""
    import sqlite3
    from db import DB_PATH
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, session_id, timestamp, user_message, agent_id, "
            "       ranked_zpids_json "
            "FROM chat_events WHERE user_id = ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (current_user.id, limit),
        ).fetchall()
    return {
        "events": [
            {
                "id": r["id"],
                "session_id": r["session_id"],
                "timestamp": r["timestamp"],
                "message": r["user_message"],
                "agent": r["agent_id"],
                "ranked_zpids": __import__("json").loads(r["ranked_zpids_json"] or "[]"),
            }
            for r in rows
        ]
    }


@app.post("/shortlist/add")
def shortlist_add(req: ShortlistMutation):
    session = STATE["sessions"].get(req.session_id)
    listings = STATE["listings"]
    target = next((L for L in listings if L.zpid == req.zpid), None)
    if target is None:
        raise HTTPException(status_code=404, detail="zpid not found")
    added = session.add_to_shortlist(target, via="manual")
    session.rescore_shortlist(STATE["ranker"])
    return {
        "ok": True,
        "added": added,
        "shortlist": session.shortlist_payload(),
    }


LIST_FIELDS = {"pets", "must_haves", "nice_to_haves", "avoid", "neighborhoods"}
SCALAR_FIELDS = {"budget_max", "beds_min", "beds_max", "commute", "notes"}

# Maps the 6 questionnaire feature keys to RankingService component names.
IMPORTANCE_TO_COMPONENT: dict[str, list[str]] = {
    "budget":    ["budget"],
    "commute":   ["commute"],
    "pets":      ["pets"],
    "amenities": ["must_haves", "nice_to_haves"],
    "walkable":  ["walk_score", "neighborhood"],
    "transit":   ["transit_score"],
    # New in Tier 2: HowLoud Soundscore from apartments.com — boosts
    # listings in quiet locations when the user marks "quiet" as important.
    "quiet":     ["sound_score"],
}
# Rank position (0-indexed) → weight value
RANK_WEIGHTS = [4.0, 3.0, 2.0, 1.5, 1.0, 0.5]


def _weights_from_ranking(ranking: list[str]) -> dict[str, float]:
    """Convert importance_ranking (ordered list of feature keys) to a
    RankingService-component → weight map."""
    weights: dict[str, float] = {}
    for i, feature in enumerate(ranking):
        w = RANK_WEIGHTS[i] if i < len(RANK_WEIGHTS) else 0.5
        for component in IMPORTANCE_TO_COMPONENT.get(feature, []):
            weights[component] = w
    return weights


@app.post("/profile/init")
def profile_init(req: OnboardingPayload):
    """Populate session profile from the onboarding questionnaire."""
    from profile import CommuteTarget, EMPLOYER_HQ, UserProfile

    session = STATE["sessions"].get(req.session_id)

    # Build a fresh profile (overwrites anything existing for clean onboarding)
    p = UserProfile()
    p.user_name = req.user_name.strip()
    p.budget_max = req.budget_max
    p.beds_min = req.beds_min
    p.beds_max = req.beds_max
    p.pets = list(req.pets or [])
    p.must_haves = list(req.must_haves or [])
    p.avoid = list(req.avoid or [])

    if req.commute and req.commute.name:
        nm = req.commute.name.strip()
        hq = EMPLOYER_HQ.get(nm.lower())
        if hq:
            p.commute = CommuteTarget(
                name=hq["name"],
                address=hq.get("address", ""),
                lat=hq.get("lat"),
                lng=hq.get("lng"),
                max_minutes=req.commute.max_minutes,
            )
        else:
            p.commute = CommuteTarget(
                name=nm,
                address=req.commute.address or "",
                max_minutes=req.commute.max_minutes,
            )

    if req.importance_ranking:
        p.weights = _weights_from_ranking(req.importance_ranking)

    session.profile = p

    # Auto-run the search agent now that profile is fully populated.
    # Synthesize an initial user message that summarizes their criteria.
    parts = []
    if p.budget_max:
        parts.append(f"under ${p.budget_max:,}")
    if p.beds_min is not None:
        parts.append("studio" if p.beds_min == 0 else f"{p.beds_min}BR")
    if p.commute:
        parts.append(f"near {p.commute.name}")
    if p.pets:
        parts.append(f"allows {', '.join(p.pets).lower()}")
    if p.must_haves:
        parts.append(f"with {', '.join(p.must_haves[:3])}")
    synthetic_msg = "Find me a place " + ", ".join(parts) if parts else "Show me my best matches"

    session.history.append(ChatTurn(role="user", agent=None, text=synthetic_msg))

    search_agent = STATE["agents"]["search"]
    reply = search_agent.handle(synthetic_msg, session)
    session.history.append(ChatTurn(role="agent", agent="search", text=reply.text))
    session.pending_clarification = None

    STATE["ranker"]  # ensure import path; rescore happens in agent
    session.rescore_shortlist(STATE["ranker"])

    return {
        "ok": True,
        "profile": _profile_dict(session),
        "profile_summary": session.profile.to_summary(),
        "shortlist": session.shortlist_payload(),
        "initial_message": {
            "user": synthetic_msg,
            "agent": "search",
            "reply": reply.text,
        },
    }


@app.post("/profile/remove")
def profile_remove(req: ProfileRemoval):
    session = STATE["sessions"].get(req.session_id)
    p = session.profile
    field = req.field
    value = req.value

    if field in LIST_FIELDS:
        cur = getattr(p, field)
        if value is None:
            setattr(p, field, [])
        else:
            v_lower = value.lower()
            setattr(p, field, [x for x in cur if x.lower() != v_lower])
    elif field == "budget_max":
        p.budget_max = None
    elif field == "beds_min":
        p.beds_min = None
    elif field == "beds_max":
        p.beds_max = None
    elif field == "beds":  # convenience: clear both
        p.beds_min = None
        p.beds_max = None
    elif field == "commute":
        p.commute = None
    elif field == "notes":
        p.notes = ""
    else:
        raise HTTPException(status_code=400, detail=f"unknown field: {field}")

    session.rescore_shortlist(STATE["ranker"])
    return {
        "ok": True,
        "profile": _profile_dict(session),
        "profile_summary": session.profile.to_summary(),
        "shortlist": session.shortlist_payload(),
    }


@app.post("/session/reset")
def reset(req: dict):
    sid = req.get("session_id")
    if sid:
        STATE["sessions"].reset(sid)
    return {"ok": True}
