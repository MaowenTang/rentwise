"""In-memory session state (v0 scaffold).

Per-session: chat history, evolving user profile, dynamic shortlist.
v1 moves all of this to Postgres + Realtime per the design spec §5.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from listings import Listing
from profile import RankingService, ScoreBreakdown, UserProfile

_KIND_LABELS: dict[str, str] = {
    "apartment_building": "Apartment Building",
    "single_home_raw": "Single Family Home",
}


@dataclass
class ChatTurn:
    role: str           # "user" | "agent" | "system"
    agent: str | None
    text: str


@dataclass
class ShortlistEntry:
    listing: Listing
    added_via: str       # "search" | "property" | "location" | "outreach" | "manual"
    added_at: float
    score: ScoreBreakdown | None = None


@dataclass
class Session:
    session_id: str
    history: deque[ChatTurn] = field(default_factory=lambda: deque(maxlen=40))
    listings_in_scope: list[Listing] = field(default_factory=list)
    profile: UserProfile = field(default_factory=UserProfile)
    shortlist: list[ShortlistEntry] = field(default_factory=list)
    # When an agent asks a clarifying question, we remember which agent is
    # waiting for a reply so the next user message can be routed back to it.
    pending_clarification: tuple[str, list[str]] | None = None

    def add_to_shortlist(self, listing: Listing, via: str) -> bool:
        """Returns True if newly added, False if already present."""
        for e in self.shortlist:
            if e.listing.zpid == listing.zpid:
                return False
        self.shortlist.append(
            ShortlistEntry(listing=listing, added_via=via, added_at=time.time())
        )
        return True

    def remove_from_shortlist(self, zpid: str) -> bool:
        before = len(self.shortlist)
        self.shortlist = [e for e in self.shortlist if e.listing.zpid != zpid]
        return len(self.shortlist) < before

    def rescore_shortlist(self, ranker: RankingService) -> None:
        for e in self.shortlist:
            e.score = ranker.score(e.listing, self.profile)
        self.shortlist.sort(
            key=lambda e: -(e.score.overall if e.score else 0)
        )

    def shortlist_payload(self) -> list[dict]:
        out = []
        for e in self.shortlist:
            L = e.listing
            out.append(
                {
                    "zpid": L.zpid,
                    "name": L.name,
                    "address": L.address,
                    "neighborhood": L.neighborhood,
                    "lat": L.lat,
                    "lng": L.lng,
                    "rent_min": L.rent_min,
                    "rent_max": L.rent_max,
                    "rent_by_bed": {
                        ("Studio" if b == 0 else f"{b}BR"): {"min": mn, "max": mx}
                        for b, (mn, mx) in L.rent_by_bed.items()
                    },
                    "walk_score": L.walk_score,
                    "transit_score": L.transit_score,
                    "url": L.url,
                    "photo_url": L.raw.get("primary_photo_url"),
                    "type_label": _KIND_LABELS.get(
                        L.raw.get("kind", ""), None
                    ),
                    "rationale": L.raw.get("_rationale", "") or None,
                    "score": e.score.overall if e.score else None,
                    "score_components": e.score.components if e.score else {},
                    "score_explanation": e.score.explanation if e.score else "",
                    "added_via": e.added_via,
                }
            )
        return out


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = Lock()

    def get(self, session_id: str) -> Session:
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                s = Session(session_id=session_id)
                self._sessions[session_id] = s
            return s

    def reset(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)
