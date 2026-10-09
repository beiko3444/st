"""Single sign-on from the inventory site to the translator.

The Vercel inventory site (already password protected) redirects to
`<translator>/auth?token=...` with a short-lived token signed with
TRANSLATOR_SHARED_SECRET. The translator checks it and keeps its own session
cookie, so there is no second password. `inventory_web/translator_portal.py`
creates the same tokens; keep the two in step.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Optional

LOGIN_PURPOSE = "translator-login"
SESSION_PURPOSE = "translator-session"
SESSION_COOKIE = "krt_session"
LOGIN_TOKEN_TTL = 120
SESSION_TTL = 60 * 60 * 24 * 30


def sign(secret: str, purpose: str, expires: int) -> str:
    digest = hmac.new(secret.encode("utf-8"), f"{purpose}:{expires}".encode("utf-8"), hashlib.sha256)
    return f"{expires}.{digest.hexdigest()}"


def verify(secret: str, purpose: str, token: Optional[str], now: Optional[float] = None) -> bool:
    if not secret or not token or "." not in token:
        return False
    expires_text = token.split(".", 1)[0]
    if not expires_text.isdigit():
        return False
    expires = int(expires_text)
    if expires < (time.time() if now is None else now):
        return False
    return hmac.compare_digest(token, sign(secret, purpose, expires))


def session_token(secret: str, now: Optional[float] = None) -> str:
    return sign(secret, SESSION_PURPOSE, int((time.time() if now is None else now) + SESSION_TTL))
