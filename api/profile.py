"""User profile + ranking service.

The profile is built up turn-by-turn by ProfileUpdater (LLM-extracts revealed
prefs). RankingService scores any listing against the current profile and
returns a 0-100 overall score plus per-feature breakdown for the UI.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from math import asin, cos, radians, sin, sqrt

from anthropic import Anthropic

from listings import Listing

EARTH_MI = 3958.7613


def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = radians(lat1), radians(lat2)
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(p1) * cos(p2) * sin(dlon / 2) ** 2
    return 2 * EARTH_MI * asin(sqrt(a))


@dataclass
class CommuteTarget:
    name: str            # "Apple HQ"
    address: str = ""    # full address if known
    lat: float | None = None
    lng: float | None = None
    max_minutes: int | None = None  # soft cap for scoring


# Well-known employer locations (so users can say "Apple", "Google" etc.)
EMPLOYER_HQ: dict[str, dict] = {
    "apple":  {"name": "Apple Park", "address": "One Apple Park Way, Cupertino, CA",
               "lat": 37.3349, "lng": -122.0090},
    "google": {"name": "Google HQ", "address": "1600 Amphitheatre Pkwy, Mountain View, CA",
               "lat": 37.4220, "lng": -122.0841},
    "meta":   {"name": "Meta HQ", "address": "1 Hacker Way, Menlo Park, CA",
               "lat": 37.4848, "lng": -122.1484},
    "facebook": {"name": "Meta HQ", "address": "1 Hacker Way, Menlo Park, CA",
                 "lat": 37.4848, "lng": -122.1484},
    "nvidia": {"name": "Nvidia HQ", "address": "2788 San Tomas Expy, Santa Clara, CA",
               "lat": 37.3677, "lng": -121.9693},
    "tesla":  {"name": "Tesla HQ", "address": "1 Tesla Rd, Austin, TX",  # actually Austin, but Fremont factory:
               "lat": 37.4936, "lng": -121.9446},  # using Fremont factory
    "netflix": {"name": "Netflix HQ", "address": "100 Winchester Cir, Los Gatos, CA",
                "lat": 37.2581, "lng": -121.9750},
    "linkedin": {"name": "LinkedIn HQ", "address": "1000 W Maude Ave, Sunnyvale, CA",
                 "lat": 37.4233, "lng": -122.0072},
    "adobe": {"name": "Adobe HQ", "address": "345 Park Ave, San Jose, CA",
              "lat": 37.3308, "lng": -121.8932},
    "cisco": {"name": "Cisco HQ", "address": "170 W Tasman Dr, San Jose, CA",
              "lat": 37.4109, "lng": -121.9350},
    "ebay":  {"name": "eBay HQ", "address": "2025 Hamilton Ave, San Jose, CA",
              "lat": 37.2962, "lng": -121.9292},
    "paypal": {"name": "PayPal HQ", "address": "2211 N 1st St, San Jose, CA",
               "lat": 37.3725, "lng": -121.9114},
    "intel":  {"name": "Intel HQ", "address": "2200 Mission College Blvd, Santa Clara, CA",
               "lat": 37.3878, "lng": -121.9627},
    "amd":    {"name": "AMD HQ", "address": "2485 Augustine Dr, Santa Clara, CA",
               "lat": 37.4091, "lng": -121.9760},
}


@dataclass
class UserProfile:
    budget_max: int | None = None
    beds_min: int | None = None
    beds_max: int | None = None
    pets: list[str] = field(default_factory=list)        # ["dogs"], ["cats"]
    must_haves: list[str] = field(default_factory=list)  # ["pool", "in-unit laundry"]
    nice_to_haves: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)
    commute: CommuteTarget | None = None
    neighborhoods: list[str] = field(default_factory=list)
    user_name: str = ""           # for email signatures (Outreach Agent)
    move_in_date: str = ""        # ISO date or free text (Outreach Agent)
    notes: str = ""
    # User-customized component weights from the onboarding questionnaire.
    # When non-empty, RankingService uses these instead of DEFAULT_WEIGHTS.
    # Keys are RankingService component names (budget / commute / pets /
    # must_haves / walk_score / transit_score / etc.).
    weights: dict[str, float] = field(default_factory=dict)

    def is_rich_enough(self) -> bool:
        """Have we collected enough signal to do a useful search?

        Need at least 2 of: budget, beds, commute target, neighborhoods,
        OR a rich free-text note.
        """
        signals = sum(
            [
                bool(self.budget_max),
                bool(self.beds_min is not None or self.beds_max is not None),
                bool(self.commute),
                bool(self.neighborhoods),
                bool(self.must_haves),
                bool(self.notes and len(self.notes) > 30),
            ]
        )
        return signals >= 2

    def to_summary(self) -> str:
        """Short human-readable summary for chat / UI."""
        parts = []
        if self.user_name:
            parts.append(f"name: {self.user_name}")
        if self.budget_max:
            parts.append(f"budget ≤ ${self.budget_max:,}")
        if self.beds_min is not None or self.beds_max is not None:
            mn = self.beds_min if self.beds_min is not None else "?"
            mx = self.beds_max if self.beds_max is not None else "?"
            parts.append(
                "studio" if mn == 0 and mx == 0 else f"{mn}-{mx} bed"
            )
        if self.pets:
            parts.append(f"pets: {', '.join(self.pets)}")
        if self.commute:
            parts.append(f"near {self.commute.name}")
        if self.neighborhoods:
            parts.append(f"neighborhoods: {', '.join(self.neighborhoods[:2])}")
        if self.must_haves:
            parts.append(f"must: {', '.join(self.must_haves[:3])}")
        if self.move_in_date:
            parts.append(f"move-in: {self.move_in_date}")
        return " · ".join(parts) if parts else "(no preferences yet)"


@dataclass
class ScoreBreakdown:
    overall: float                    # 0-100
    components: dict[str, float]      # name -> 0-10 each
    explanation: str                  # human-readable why


class RankingService:
    """Heuristic scoring — fast, deterministic, explainable.

    Each component is 0-10. Overall = weighted sum, normalized to 0-100.
    Components only count if the profile asked for that thing.
    """

    DEFAULT_WEIGHTS = {
        "budget": 3.0,
        "beds": 2.0,
        "pets": 2.0,
        "must_haves": 3.0,
        "nice_to_haves": 1.0,
        "avoid": 1.5,
        "commute": 2.5,
        "walk_score": 1.0,
        "transit_score": 0.5,
        "neighborhood": 1.5,
    }

    def score(self, listing: Listing, profile: UserProfile) -> ScoreBreakdown:
        comps: dict[str, float] = {}
        # Prefer the user's custom weights from the onboarding questionnaire,
        # falling back to defaults for any component the user didn't rank.
        weights = {**self.DEFAULT_WEIGHTS, **(profile.weights or {})}

        active_weight = 0.0
        weighted_total = 0.0

        # Budget
        if profile.budget_max:
            if listing.rent_min is None:
                s = 0.0
            elif listing.rent_min <= profile.budget_max * 0.85:
                s = 10.0  # well under
            elif listing.rent_min <= profile.budget_max:
                # scale 5-10 between 85% and 100% of budget
                ratio = (profile.budget_max - listing.rent_min) / (profile.budget_max * 0.15)
                s = 5 + 5 * ratio
            else:
                s = max(0.0, 5 - (listing.rent_min - profile.budget_max) / (profile.budget_max * 0.10))
            comps["budget"] = round(s, 1)
            active_weight += weights["budget"]
            weighted_total += s * weights["budget"]

        # Beds
        if profile.beds_min is not None or profile.beds_max is not None:
            beds_avail = set(listing.rent_by_bed.keys())
            mn = profile.beds_min if profile.beds_min is not None else 0
            mx = profile.beds_max if profile.beds_max is not None else 10
            wanted = set(range(mn, mx + 1))
            overlap = beds_avail & wanted
            if overlap:
                s = 10.0
            elif beds_avail:
                # closest available
                closest = min(abs(b - mn) for b in beds_avail)
                s = max(0.0, 10 - closest * 3)
            else:
                s = 0.0
            comps["beds"] = round(s, 1)
            active_weight += weights["beds"]
            weighted_total += s * weights["beds"]

        # Pets
        if profile.pets:
            allowed = " ".join(p.lower() for p in (listing.pets_allowed or []))
            hits = sum(1 for p in profile.pets if p.lower() in allowed)
            s = (hits / len(profile.pets)) * 10
            comps["pets"] = round(s, 1)
            active_weight += weights["pets"]
            weighted_total += s * weights["pets"]

        # Must-haves
        if profile.must_haves:
            blob = self._listing_blob(listing)
            hits = sum(1 for k in profile.must_haves if k.lower() in blob)
            s = (hits / len(profile.must_haves)) * 10
            comps["must_haves"] = round(s, 1)
            active_weight += weights["must_haves"]
            weighted_total += s * weights["must_haves"]

        # Nice-to-haves
        if profile.nice_to_haves:
            blob = self._listing_blob(listing)
            hits = sum(1 for k in profile.nice_to_haves if k.lower() in blob)
            s = (hits / len(profile.nice_to_haves)) * 10
            comps["nice_to_haves"] = round(s, 1)
            active_weight += weights["nice_to_haves"]
            weighted_total += s * weights["nice_to_haves"]

        # Avoid (penalty)
        if profile.avoid:
            blob = self._listing_blob(listing)
            hits = sum(1 for k in profile.avoid if k.lower() in blob)
            s = max(0.0, 10 - hits * 5)
            comps["avoid"] = round(s, 1)
            active_weight += weights["avoid"]
            weighted_total += s * weights["avoid"]

        # Commute (Haversine straight-line, v0 approximation)
        if profile.commute and profile.commute.lat and profile.commute.lng \
                and listing.lat and listing.lng:
            miles = haversine(listing.lat, listing.lng, profile.commute.lat, profile.commute.lng)
            # Rough: <3 mi = 10, 3-6 = 8, 6-10 = 5, 10-15 = 3, >15 = 1
            if miles < 3:
                s = 10.0
            elif miles < 6:
                s = 8.0
            elif miles < 10:
                s = 5.0
            elif miles < 15:
                s = 3.0
            else:
                s = 1.0
            comps["commute"] = round(s, 1)
            active_weight += weights["commute"]
            weighted_total += s * weights["commute"]

        # Walk / transit
        if listing.walk_score is not None:
            s = min(10.0, listing.walk_score / 10)
            comps["walk_score"] = round(s, 1)
            active_weight += weights["walk_score"]
            weighted_total += s * weights["walk_score"]
        if listing.transit_score is not None:
            s = min(10.0, listing.transit_score / 10)
            comps["transit_score"] = round(s, 1)
            active_weight += weights["transit_score"]
            weighted_total += s * weights["transit_score"]

        # Neighborhood preference
        if profile.neighborhoods and listing.neighborhood:
            ln = listing.neighborhood.lower()
            hit = any(n.lower() in ln or ln in n.lower() for n in profile.neighborhoods)
            s = 10.0 if hit else 3.0
            comps["neighborhood"] = round(s, 1)
            active_weight += weights["neighborhood"]
            weighted_total += s * weights["neighborhood"]

        overall = (weighted_total / active_weight) * 10 if active_weight else 50.0
        explanation = self._explain(comps)
        return ScoreBreakdown(
            overall=round(overall, 1),
            components=comps,
            explanation=explanation,
        )

    def _listing_blob(self, listing: Listing) -> str:
        parts: list[str] = []
        for field in ("description", "neighborhood"):
            v = getattr(listing, field, None)
            if v:
                parts.append(str(v))
        for v in (listing.parking_types, listing.utilities_included):
            if v:
                parts.extend(str(x) for x in v)
        for attr in (
            "has_pool", "has_elevator", "has_storage", "has_patio_balcony"
        ):
            if getattr(listing, attr, False):
                parts.append(attr.replace("has_", ""))
        return " ".join(parts).lower()

    def _explain(self, comps: dict[str, float]) -> str:
        if not comps:
            return "no profile preferences set yet"
        ranked = sorted(comps.items(), key=lambda x: -x[1])
        return ", ".join(f"{k}:{v}" for k, v in ranked[:4])


# --------------------------- Profile Updater --------------------------------

UPDATE_PROMPT = """You extract / update a renter's preferences from chat messages.

Given the user's NEW message and their CURRENT profile, return a JSON
object with the fields that should change. Omit fields not mentioned.

ADD operations (extend a list or set a scalar):
  - budget_max: int (max monthly rent USD)
  - beds_min: int (0 = studio)
  - beds_max: int
  - pets_add: array of strings (e.g., ["dogs"])
  - must_haves_add: array of strings (concrete features they NEED)
  - nice_to_haves_add: array (features they'd like but not require)
  - avoid_add: array (things they explicitly don't want)
  - neighborhoods_add: array (e.g., ["Downtown", "Willow Glen"])
  - commute: {{"name":"<employer/place>","max_minutes":int_or_null}} OR
             {{"name":"...","address":"<full address>","max_minutes":int_or_null}}
  - user_name: string
  - move_in_date: string
  - notes_append: short string to append to free-text notes

REMOVE operations (when the user negates / contradicts / changes their mind):
  - pets_remove: array of strings to remove from pets
  - must_haves_remove: array of strings to remove from must_haves
  - nice_to_haves_remove: array of strings to remove from nice_to_haves
  - avoid_remove: array of strings to remove from avoid
  - neighborhoods_remove: array of strings to remove from neighborhoods
  - clear_commute: true (drop the commute target)
  - clear_budget: true (drop budget_max)
  - clear_beds: true (drop beds_min/beds_max)

The values you put in *_remove arrays MUST match strings already in
CURRENT_PROFILE EXACTLY (case-insensitive substring match is OK).

CRITICAL — when the user negates something, propagate the removal:
  • "I don't want X" / "no X" / "不要 X" / "remove X" / "actually skip X"
    → REMOVE every related entry from must_haves, nice_to_haves, AND
      pets/neighborhoods if applicable, then ADD the negation to
      avoid_add.
  • "Trader Joe's isn't important" → remove from nice_to_haves but
    don't add to avoid.
  • Match loosely: if the profile has "near Trader Joe's" and the user
    says "no Trader Joe's", emit nice_to_haves_remove: ["near Trader Joe's"].

Look for indirect signals:
  - "near my work at Apple" -> commute: {{"name":"Apple"}}
  - "I have a dog" -> pets_add: ["dogs"]
  - "thin walls drove me crazy" -> avoid_add: ["thin walls"]
  - "I work from home" -> nice_to_haves_add: ["co-working space", "good wifi"]

If the message is just a question (no preference reveal), return an empty
JSON object.

Respond with ONLY the JSON object — no prose, no fences.

CURRENT_PROFILE:
{profile}

USER_MESSAGE:
{message}
"""


class ProfileUpdater:
    def __init__(self, client: Anthropic | None = None, model: str = "claude-sonnet-4-6"):
        self._client = client
        self.model = model

    @property
    def client(self) -> Anthropic:
        if self._client is None:
            self._client = Anthropic()
        return self._client

    def update(self, message: str, profile: UserProfile) -> UserProfile:
        prompt = UPDATE_PROMPT.format(
            profile=json.dumps(asdict(profile), default=str, indent=2),
            message=message,
        )
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text.strip()
            if text.startswith("```"):
                text = text.strip("`")
                if text.lower().startswith("json"):
                    text = text[4:].strip()
            patch = json.loads(text)
        except Exception:
            return profile

        return self._apply_patch(profile, patch)

    def _apply_patch(self, p: UserProfile, patch: dict) -> UserProfile:
        # --- scalar add / clear ---
        if "budget_max" in patch and isinstance(patch["budget_max"], int):
            p.budget_max = patch["budget_max"]
        if "beds_min" in patch and isinstance(patch["beds_min"], int):
            p.beds_min = patch["beds_min"]
        if "beds_max" in patch and isinstance(patch["beds_max"], int):
            p.beds_max = patch["beds_max"]
        if patch.get("clear_budget") is True:
            p.budget_max = None
        if patch.get("clear_beds") is True:
            p.beds_min = None
            p.beds_max = None
        if patch.get("clear_commute") is True:
            p.commute = None

        # --- list adds ---
        for key, attr in [
            ("pets_add", "pets"),
            ("must_haves_add", "must_haves"),
            ("nice_to_haves_add", "nice_to_haves"),
            ("avoid_add", "avoid"),
            ("neighborhoods_add", "neighborhoods"),
        ]:
            vals = patch.get(key)
            if isinstance(vals, list):
                target = getattr(p, attr)
                for v in vals:
                    if isinstance(v, str) and v.strip() and v.lower() not in [
                        x.lower() for x in target
                    ]:
                        target.append(v.strip())

        # --- list removes (loose substring match, case-insensitive) ---
        for key, attr in [
            ("pets_remove", "pets"),
            ("must_haves_remove", "must_haves"),
            ("nice_to_haves_remove", "nice_to_haves"),
            ("avoid_remove", "avoid"),
            ("neighborhoods_remove", "neighborhoods"),
        ]:
            vals = patch.get(key)
            if isinstance(vals, list) and vals:
                cur = getattr(p, attr)
                kept: list[str] = []
                drop_terms = [v.lower() for v in vals if isinstance(v, str)]
                for item in cur:
                    item_low = item.lower()
                    # drop if any drop_term substring-matches the item
                    if any(t and (t == item_low or t in item_low or item_low in t) for t in drop_terms):
                        continue
                    kept.append(item)
                setattr(p, attr, kept)

        if isinstance(patch.get("commute"), dict):
            ct = patch["commute"]
            name = (ct.get("name") or "").strip()
            if name:
                hq = EMPLOYER_HQ.get(name.lower())
                if hq:
                    p.commute = CommuteTarget(
                        name=hq["name"],
                        address=hq.get("address", ""),
                        lat=hq.get("lat"),
                        lng=hq.get("lng"),
                        max_minutes=ct.get("max_minutes"),
                    )
                else:
                    p.commute = CommuteTarget(
                        name=name,
                        address=ct.get("address", "") or "",
                        max_minutes=ct.get("max_minutes"),
                    )
        if isinstance(patch.get("user_name"), str) and patch["user_name"].strip():
            p.user_name = patch["user_name"].strip()
        if isinstance(patch.get("move_in_date"), str) and patch["move_in_date"].strip():
            p.move_in_date = patch["move_in_date"].strip()
        if isinstance(patch.get("notes_append"), str) and patch["notes_append"].strip():
            sep = " " if p.notes else ""
            p.notes = (p.notes + sep + patch["notes_append"].strip())[:1000]
        return p
