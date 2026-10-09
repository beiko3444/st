from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .codex_client import CodexError
from .service import AUTO_STYLE, TranslationService

ROOT = Path(__file__).resolve().parent
LOOPBACK_HOSTS = ["127.0.0.1", "localhost"]


class TranslateBody(BaseModel):
    text: str
    style: str = AUTO_STYLE
    targets: Optional[list[str]] = None


class LoginBody(BaseModel):
    method: str = "browser"


def create_app(service: Optional[TranslationService] = None, *, allowed_hosts: Optional[list[str]] = None) -> FastAPI:
    svc = service or TranslationService.from_env()

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await svc.close()

    app = FastAPI(title="KR Translator", lifespan=lifespan)
    app.state.service = svc
    # The server spends the signed-in ChatGPT plan, so by default only pages
    # served from this machine may call it (blocks DNS-rebinding requests).
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts or LOOPBACK_HOSTS)
    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

    @app.middleware("http")
    async def same_origin_posts(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method == "POST" and origin and origin.split("://", 1)[-1] != request.headers.get("host"):
            return JSONResponse({"ok": False, "error": "cross-origin request blocked"}, status_code=403)
        return await call_next(request)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(ROOT / "static" / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return await svc.status()

    @app.post("/api/login")
    async def login(body: LoginBody) -> dict[str, Any]:
        try:
            return await svc.start_login(device=body.method == "device")
        except CodexError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/logout")
    async def logout() -> dict[str, Any]:
        try:
            await svc.logout()
        except CodexError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/translate")
    async def translate(body: TranslateBody) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            events = svc.translate(body.text, style=body.style, targets=body.targets)
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
