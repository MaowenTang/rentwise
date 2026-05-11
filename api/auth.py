"""Auth router — signup / login / me + JWT verification dependency.

Mounted in main.py via app.include_router(auth_router). Tokens are HS256
JWTs signed with JWT_SECRET (env or random per-process); 30-day exp.
"""
from __future__ import annotations

import os
import secrets
import time
from datetime import datetime, timedelta, timezone

import base64
import hashlib

import bcrypt
import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, EmailStr

from db import (
    UserRow,
    create_user,
    delete_memory_key,
    delete_user_cascade,
    export_user_data,
    get_user_by_email,
    get_user_by_id,
    load_profile,
    replace_memory,
    save_memory,
)

router = APIRouter(prefix="/auth", tags=["auth"])

JWT_SECRET = os.environ.get("JWT_SECRET") or secrets.token_urlsafe(32)
JWT_ALGORITHM = "HS256"
JWT_EXPIRES_DAYS = 30
MAGIC_LINK_TTL_MINUTES = 15  # short-lived

# bcrypt is hard-capped at 72 bytes. SHA256 the password first → base64
# (44 ASCII chars, well under 72) → bcrypt. This is the standard bcrypt_sha256
# construction; doing it manually avoids passlib/bcrypt version mismatches.


def _hash_password(password: str) -> str:
    pre = base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())
    return bcrypt.hashpw(pre, bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, stored_hash: str) -> bool:
    pre = base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())
    try:
        return bcrypt.checkpw(pre, stored_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


class SignupRequest(BaseModel):
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class AuthResponse(BaseModel):
    token: str
    user_id: str
    email: str


def _issue_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "iat": int(time.time()),
        "exp": datetime.now(tz=timezone.utc) + timedelta(days=JWT_EXPIRES_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _decode_token(token: str) -> str | None:
    """Return user_id or None on failure."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload.get("sub")
    except jwt.PyJWTError:
        return None


def get_current_user(authorization: str | None = Header(default=None)) -> UserRow:
    """FastAPI dependency: enforce Bearer JWT, return UserRow."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1]
    uid = _decode_token(token)
    if not uid:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user = get_user_by_id(uid)
    if not user:
        raise HTTPException(status_code=401, detail="User no longer exists")
    return user


def get_current_user_optional(authorization: str | None = Header(default=None)) -> UserRow | None:
    """Like get_current_user but returns None instead of 401. Used by /chat
    so anonymous sessions still work (no persistence)."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1]
    uid = _decode_token(token)
    if not uid:
        return None
    return get_user_by_id(uid)


@router.post("/signup", response_model=AuthResponse)
def signup(req: SignupRequest):
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be ≥8 characters")
    if get_user_by_email(req.email):
        raise HTTPException(status_code=409, detail="Email already registered")
    pw_hash = _hash_password(req.password)
    user = create_user(req.email, pw_hash)
    return AuthResponse(token=_issue_token(user.id), user_id=user.id, email=user.email)


@router.post("/login", response_model=AuthResponse)
def login(req: LoginRequest):
    found = get_user_by_email(req.email)
    if not found:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    user, pw_hash = found
    if not _verify_password(req.password, pw_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return AuthResponse(token=_issue_token(user.id), user_id=user.id, email=user.email)


@router.get("/me")
def me(user: UserRow = Depends(get_current_user)):
    return {"user_id": user.id, "email": user.email, "created_at": user.created_at}


@router.get("/export")
def export_my_data(user: UserRow = Depends(get_current_user)):
    """GDPR data portability: returns a JSON dump of everything we have
    on this user. Designed to be downloadable as `rentwise-data.json`.
    """
    data = export_user_data(user.id)
    if not data:
        raise HTTPException(status_code=404, detail="No data found")
    return data


# --- Long-term memory CRUD (user-facing; complements auto-extractor) -------

class MemoryUpdate(BaseModel):
    memory: dict


class MemoryPatch(BaseModel):
    key: str
    value: str


@router.get("/memory")
def get_memory(user: UserRow = Depends(get_current_user)):
    """Return current user's long-term memory dict."""
    _, memory = load_profile(user.id)
    return {"memory": memory or {}}


@router.put("/memory")
def put_memory(req: MemoryUpdate, user: UserRow = Depends(get_current_user)):
    """Replace the entire memory dict. Validates that values are short
    strings (frontend should match the schema the auto-extractor uses).
    """
    cleaned: dict[str, str] = {}
    for k, v in (req.memory or {}).items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        k_clean = k.strip().lower().replace(" ", "_")[:32]
        v_clean = v.strip()[:200]
        if k_clean and v_clean:
            cleaned[k_clean] = v_clean
    if len(cleaned) > 50:
        raise HTTPException(status_code=400, detail="Memory limit is 50 keys")
    replace_memory(user.id, cleaned)
    return {"ok": True, "memory": cleaned}


@router.patch("/memory")
def patch_memory(req: MemoryPatch, user: UserRow = Depends(get_current_user)):
    """Upsert a single memory key/value."""
    k_clean = (req.key or "").strip().lower().replace(" ", "_")[:32]
    v_clean = (req.value or "").strip()[:200]
    if not k_clean or not v_clean:
        raise HTTPException(status_code=400, detail="Key and value required")
    save_memory(user.id, {k_clean: v_clean})
    _, memory = load_profile(user.id)
    return {"ok": True, "memory": memory or {}}


@router.delete("/memory/{key}")
def delete_memory_endpoint(key: str, user: UserRow = Depends(get_current_user)):
    """Remove a single memory key."""
    removed = delete_memory_key(user.id, key.strip().lower().replace(" ", "_"))
    _, memory = load_profile(user.id)
    return {"ok": True, "removed": removed, "memory": memory or {}}


@router.delete("/me")
def delete_me(user: UserRow = Depends(get_current_user)):
    """GDPR right to erasure: cascade-delete this user + all their data.
    Irreversible. Frontend should clear localStorage tokens after this.
    """
    counts = delete_user_cascade(user.id)
    return {"ok": True, "deleted": counts}


# --- Magic-link (passwordless) login ----------------------------------------

class MagicLinkRequest(BaseModel):
    email: EmailStr


class MagicVerifyRequest(BaseModel):
    token: str


def _issue_magic_token(email: str) -> str:
    """Short-lived JWT just for sign-in link."""
    payload = {
        "email": email.lower().strip(),
        "purpose": "magic_link",
        "exp": datetime.now(tz=timezone.utc) + timedelta(minutes=MAGIC_LINK_TTL_MINUTES),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _decode_magic_token(token: str) -> str | None:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("purpose") != "magic_link":
            return None
        return payload.get("email")
    except jwt.PyJWTError:
        return None


def _send_magic_email(email: str, link: str) -> bool:
    """Send the magic link. Returns True if delivered, False if dev-mode
    (no SMTP configured). Dev-mode prints the link to stdout so the
    developer can copy-paste it from the uvicorn log.

    Production: configure SMTP_HOST + SMTP_USER + SMTP_PASSWORD + SMTP_FROM
    env vars. We use stdlib smtplib so there's no extra dep.
    """
    smtp_host = os.environ.get("SMTP_HOST")
    if not smtp_host:
        print(f"\n=== MAGIC LINK (dev mode, no SMTP configured) ===")
        print(f"  To:   {email}")
        print(f"  Link: {link}")
        print(f"  Expires in {MAGIC_LINK_TTL_MINUTES} minutes\n")
        return False
    try:
        import smtplib
        from email.mime.text import MIMEText
        sender = os.environ.get("SMTP_FROM", "noreply@rentwise.app")
        smtp_user = os.environ.get("SMTP_USER")
        smtp_pass = os.environ.get("SMTP_PASSWORD")
        smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        msg = MIMEText(
            f"Click here to sign in to RentWise:\n\n{link}\n\n"
            f"This link expires in {MAGIC_LINK_TTL_MINUTES} minutes. "
            f"If you didn't request this, ignore this email."
        )
        msg["Subject"] = "Sign in to RentWise"
        msg["From"] = sender
        msg["To"] = email
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as s:
            s.starttls()
            if smtp_user and smtp_pass:
                s.login(smtp_user, smtp_pass)
            s.send_message(msg)
        return True
    except Exception as e:
        # Log but don't reveal error to user (could enumerate accounts)
        print(f"  SMTP send failed for {email}: {e}")
        return False


@router.post("/magic-link")
def request_magic_link(req: MagicLinkRequest):
    """Send a sign-in link to the user's email. If the email is not yet
    registered, we auto-create the account when they click the link.
    Always returns 200 to prevent email enumeration; whether SMTP succeeded
    is not exposed.
    """
    base_url = os.environ.get("WEB_BASE_URL", "http://localhost:3000")
    token = _issue_magic_token(req.email)
    link = f"{base_url}/auth/magic?token={token}"
    _send_magic_email(req.email, link)
    return {"ok": True, "message": "If that email is registered, a sign-in link is on its way."}


@router.get("/google/config")
def google_config():
    """Frontend uses this to decide whether to show the Google button.
    Returns the public client_id when configured, or {"enabled": false}.
    """
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    if not client_id:
        return {"enabled": False}
    return {
        "enabled": True,
        "client_id": client_id,
        "redirect_uri": os.environ.get("WEB_BASE_URL", "http://localhost:3000") + "/auth/google",
    }


class GoogleCallbackRequest(BaseModel):
    code: str
    redirect_uri: str


@router.post("/google/exchange", response_model=AuthResponse)
def google_exchange(req: GoogleCallbackRequest):
    """Exchange a Google OAuth authorization code for our JWT.

    Frontend opens Google consent → Google redirects back to /auth/google
    with `?code=...`. The frontend page POSTs that code here. We do the
    server-side token exchange (keeps client_secret out of the browser),
    fetch userinfo, upsert user, return our app JWT.
    """
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise HTTPException(status_code=503, detail="Google OAuth not configured")
    import httpx
    try:
        tok_resp = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": req.code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": req.redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=10.0,
        )
        tok_resp.raise_for_status()
        tok_data = tok_resp.json()
        access_token = tok_data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="Google token exchange returned no access_token")
        uinfo = httpx.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10.0,
        )
        uinfo.raise_for_status()
        u = uinfo.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Google OAuth call failed: {e}")
    email = (u.get("email") or "").lower().strip()
    if not email or not u.get("email_verified"):
        raise HTTPException(status_code=403, detail="Google email not verified")
    found = get_user_by_email(email)
    if found:
        user, _ = found
    else:
        random_pw_hash = _hash_password(secrets.token_urlsafe(32))
        user = create_user(email, random_pw_hash)
    return AuthResponse(token=_issue_token(user.id), user_id=user.id, email=user.email)


@router.post("/magic-verify", response_model=AuthResponse)
def verify_magic_link(req: MagicVerifyRequest):
    """Exchange a magic-link token for a normal 30-day JWT. Auto-creates
    user if the email doesn't exist yet (passwordless first-touch signup).
    """
    email = _decode_magic_token(req.token)
    if not email:
        raise HTTPException(status_code=400, detail="Invalid or expired link")
    found = get_user_by_email(email)
    if found:
        user, _ = found
    else:
        # First-time sign-in via magic link: create account with random
        # password (user can never use it; they always sign in via link).
        random_pw_hash = _hash_password(secrets.token_urlsafe(32))
        user = create_user(email, random_pw_hash)
    return AuthResponse(token=_issue_token(user.id), user_id=user.id, email=user.email)
