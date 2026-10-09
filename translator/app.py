from __future__ import annotations

import contextlib
import html
import json
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import auth
from .codex_client import CodexError
from .images import ImageAttachment, parse_image
from .jobs import JobRegistry
from .service import AUTO_STYLE, TranslationService

ROOT = Path(__file__).resolve().parent
LOOPBACK_HOSTS = ["127.0.0.1", "localhost"]
# Quick tunnel hostnames change on every restart.
DEPLOYED_HOSTS = ["*.trycloudflare.com", *LOOPBACK_HOSTS]
PUBLIC_PATHS = {"/auth", "/healthz"}
MAX_POLL_WAIT = 25.0


class TranslateBody(BaseModel):
    text: str = ""
    image: Optional[str] = None
    style: str = AUTO_STYLE
    targets: Optional[list[str]] = None


class JobBody(TranslateBody):
    # The page's previous job, cancelled when a newer paste arrives.
    replaces: Optional[str] = None


class LoginBody(BaseModel):
    method: str = "browser"


def create_app(
    service: Optional[TranslationService] = None,
    *,
    allowed_hosts: Optional[list[str]] = None,
    shared_secret: Optional[str] = None,
    portal_url: Optional[str] = None,
) -> FastAPI:
    """In deployed mode, the public Beiko link issues visitor sessions.
    Server account management is available only through the local CLI."""
    svc = service or TranslationService.from_env()
    jobs = JobRegistry(svc)
    deployed = bool(shared_secret)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await jobs.close()
            await svc.close()

    app = FastAPI(title="KR Translator", lifespan=lifespan)
    app.state.service = svc
    # The server spends the signed-in ChatGPT plan, so it only answers to
    # expected host names (this also blocks DNS-rebinding requests).
    default_hosts = DEPLOYED_HOSTS if deployed else LOOPBACK_HOSTS
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts or default_hosts)
    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

    def has_session(request: Request) -> bool:
        return not deployed or auth.verify(
            shared_secret or "", auth.SESSION_PURPOSE, request.cookies.get(auth.SESSION_COOKIE)
        )

    @app.middleware("http")
    async def guard(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method in ("POST", "DELETE") and origin:
            own_hosts = {request.headers.get("host"), request.headers.get("x-forwarded-host")}
            if origin.split("://", 1)[-1] not in own_hosts:
                return JSONResponse({"ok": False, "error": "cross-origin request blocked"}, status_code=403)
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/static/") or has_session(request):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"ok": False, "error": "unauthorized", "portalUrl": portal_url}, status_code=401)
        if portal_url:
            return RedirectResponse(portal_url, status_code=303)
        return _message_page("Beiko 사이트의 '번역기' 메뉴로 접속하세요.", status_code=401)

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/auth")
    async def sign_in(request: Request, token: str = "") -> Any:
        if not deployed:
            return RedirectResponse("/", status_code=303)
        if not auth.verify(shared_secret or "", auth.LOGIN_PURPOSE, token):
            return _message_page(
                "링크가 만료되었거나 올바르지 않습니다. Beiko 사이트의 '번역기' 메뉴로 다시 들어오세요.",
                status_code=403,
                link=portal_url,
            )
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            auth.SESSION_COOKIE,
            auth.session_token(shared_secret or ""),
            max_age=auth.SESSION_TTL,
            httponly=True,
            secure=_is_https(request),
            samesite="lax",
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(ROOT / "static" / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        result = await svc.status()
        result["deployed"] = deployed
        result["portalUrl"] = portal_url
        if deployed:
            # Public visitors need availability, never the owner's account details.
            result["ready"] = bool(result.get("codex", {}).get("ok") and (
                result.get("account") or result.get("requiresOpenaiAuth") is False
            ))
            for key in ("account", "usage", "login", "requiresOpenaiAuth"):
                result.pop(key, None)
            result["codex"] = {"ok": bool(result.get("codex", {}).get("ok"))}
        return result

    @app.post("/api/login")
    async def login(body: LoginBody) -> dict[str, Any]:
        if deployed:
            raise HTTPException(status_code=403, detail="서버 계정은 관리자만 변경할 수 있습니다.")
        try:
            return await svc.start_login(device=body.method == "device")
        except CodexError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/logout")
    async def logout() -> dict[str, Any]:
        if deployed:
            raise HTTPException(status_code=403, detail="서버 계정은 관리자만 변경할 수 있습니다.")
        try:
            await svc.logout()
        except CodexError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True}

    def read_image(value: Optional[str]) -> Optional[ImageAttachment]:
        if value is None:
            return None
        try:
            return parse_image(value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/jobs")
    async def start_job(body: JobBody) -> dict[str, str]:
        job = jobs.start(
            body.text, style=body.style, targets=body.targets, replaces=body.replaces, image=read_image(body.image)
        )
        return {"id": job.id}

    @app.get("/api/jobs/{job_id}")
    async def poll_job(job_id: str, after: int = 0, wait: float = Query(20.0, ge=0)) -> dict[str, Any]:
        result = await jobs.wait(job_id, after, min(wait, MAX_POLL_WAIT))
        if result is None:
            raise HTTPException(status_code=404, detail="job not found")
        return result

    @app.delete("/api/jobs/{job_id}")
    async def cancel_job(job_id: str) -> dict[str, bool]:
        return {"ok": jobs.cancel(job_id)}

    @app.post("/api/translate")
    async def translate(body: TranslateBody) -> StreamingResponse:
        """Server-Sent Events stream for local use and scripts (quick tunnels cannot carry SSE)."""

        image = read_image(body.image)

        async def stream() -> AsyncIterator[str]:
            events = svc.translate(body.text, style=body.style, targets=body.targets, image=image)
            try:
                async for event in events:
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            finally:
                # Closing early (the page sent a newer paste) interrupts the
                # running Codex turns instead of letting them finish unseen.
                await events.aclose()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def _is_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "")
    return request.url.scheme == "https" or forwarded == "https" or '"https"' in request.headers.get("cf-visitor", "")


def _message_page(message: str, *, status_code: int, link: Optional[str] = None) -> HTMLResponse:
    link_html = f'<p><a href="{html.escape(link)}">Beiko 사이트로 이동</a></p>' if link else ""
    return HTMLResponse(
        f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>KR 즉시 번역기</title><link rel="stylesheet" href="/static/app.css"></head>
<body><div class="overlay"><div class="card"><h1>KR 즉시 번역기</h1>
<p>{html.escape(message)}</p>{link_html}</div></div></body></html>""",
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )
