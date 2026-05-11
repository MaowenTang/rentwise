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
    get_user_by_email,
    get_user_by_id,
)

router = APIRouter(prefix="/auth", tags=["auth"])

JWT_SECRET = os.environ.get("JWT_SECRET") or secrets.token_urlsafe(32)
JWT_ALGORITHM = "HS256"
JWT_EXPIRES_DAYS = 30

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
