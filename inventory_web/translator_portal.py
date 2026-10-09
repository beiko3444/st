"""'번역기' menu: send a signed-in user to the translator on the Raspberry Pi.

The translator runs on the Pi behind a Cloudflare quick tunnel whose URL
changes on every restart; the Pi publishes it as `translator.json` in the
same gist as `monitor.json`. This site looks the URL up and redirects with a
short-lived token signed with TRANSLATOR_SHARED_SECRET, which the translator
exchanges for its own session cookie (see translator/auth.py, kept in step).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Callable, Mapping, Optional

LOGIN_PURPOSE = "translator-login"
LOGIN_TOKEN_TTL = 120
GIST_FILE = "translator.json"


def login_token(secret: str, now: Optional[float] = None) -> str:
    expires = int((time.time() if now is None else now) + LOGIN_TOKEN_TTL)
    digest = hmac.new(secret.encode("utf-8"), f"{LOGIN_PURPOSE}:{expires}".encode("utf-8"), hashlib.sha256)
    return f"{expires}.{digest.hexdigest()}"


def translator_gist_url(monitor_gist_url: str) -> Optional[str]:
    """`.../raw[/<sha>]/monitor.json` -> `.../raw/translator.json` (always the latest revision)."""
    index = monitor_gist_url.find("/raw")
    if index < 0:
        return None
    return monitor_gist_url[: index + len("/raw")] + "/" + GIST_FILE


def resolve_translator_url(
    monitor_gist_url: str,
    resolve_gist: Callable[[str], Optional[str]],
    env: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    env = os.environ if env is None else env
    fixed = (env.get("TRANSLATOR_URL") or "").strip()
    if fixed:
        return fixed.rstrip("/")
    gist = (env.get("TRANSLATOR_URL_GIST") or "").strip() or translator_gist_url(monitor_gist_url or "")
    if not gist:
        return None
    url = resolve_gist(gist)
    return url.rstrip("/") if url else None
