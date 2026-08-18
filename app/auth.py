"""Single shared-password gate with signed session cookies."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time

from fastapi import HTTPException, Request

from . import config


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str) -> str:
    digest = hmac.new(
        config.SESSION_SECRET.encode(), payload.encode(), hashlib.sha256
    ).digest()
    return _b64e(digest)


def issue_token() -> str:
    """Mint a session token valid for SESSION_TTL_HOURS."""
    payload = _b64e(f"{int(time.time())}:{secrets.token_hex(8)}".encode())
    return f"{payload}.{_sign(payload)}"


def token_valid(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    payload, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign(payload)):
        return False
    try:
        issued = int(_b64d(payload).decode().split(":", 1)[0])
    except (ValueError, UnicodeDecodeError):
        return False
    return time.time() - issued <= config.SESSION_TTL_HOURS * 3600


def password_matches(candidate: str) -> bool:
    # compare_digest keeps the check constant-time.
    return hmac.compare_digest(candidate or "", config.APP_PASSWORD)


class LoginLimiter:
    """Per-client attempt counter, so the password cannot be brute forced."""

    def __init__(self) -> None:
        self._hits: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def locked_for(self, client: str) -> int:
        with self._lock:
            count, until = self._hits.get(client, (0, 0.0))
            remaining = int(until - time.time())
            return remaining if count >= config.LOGIN_MAX_ATTEMPTS and remaining > 0 else 0

    def record_failure(self, client: str) -> None:
        with self._lock:
            count, until = self._hits.get(client, (0, 0.0))
            if until and until < time.time():
                count = 0  # previous lockout expired, start a fresh window
            count += 1
            self._hits[client] = (count, time.time() + config.LOGIN_LOCKOUT_SECONDS)

    def clear(self, client: str) -> None:
        with self._lock:
            self._hits.pop(client, None)


limiter = LoginLimiter()


def client_key(request: Request) -> str:
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get(
        "x-forwarded-for", ""
    )
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def is_authenticated(request: Request) -> bool:
    if not config.AUTH_ENABLED:
        return True
    return token_valid(request.cookies.get(config.SESSION_COOKIE))


async def require_session(request: Request) -> None:
    """FastAPI dependency guarding every endpoint that touches user data."""
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Sign in to continue.")


def cookie_kwargs() -> dict:
    return {
        "key": config.SESSION_COOKIE,
        "httponly": True,
        "samesite": "lax",
        "secure": config.COOKIE_SECURE,
        "max_age": config.SESSION_TTL_HOURS * 3600,
        "path": "/",
    }
