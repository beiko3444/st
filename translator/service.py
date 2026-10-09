"""Paste-to-translate pipeline: Jev picks a style, GPT (via Codex) translates.

Each target language runs as its own ephemeral Codex thread, so English and
Simplified Chinese stream at the same time instead of one after the other.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import logging
import os
import shutil
import tempfile
import time
from typing import Any, AsyncIterator, Optional, Sequence

from .codex_client import CodexAppServer, CodexError, build_app_server_command, find_codex_binary
from .jev import JevClassifier, JevError
from .prompts import GENERAL, STYLES, TARGETS, build_instructions, build_back_instructions

log = logging.getLogger(__name__)

MAX_TEXT_LENGTH = 20_000
AUTO_STYLE = "auto"
# Lowest-latency effort the model offers wins; translation gains little from reasoning.
EFFORT_PREFERENCE = ("none", "minimal", "low")
PROMPT_MODES = ("base", "developer")
CACHE_SIZE = 200
INSTALL_HINT = "Codex CLI가 필요합니다: npm install -g @openai/codex"


class TranslationService:
    def __init__(
        self,
        codex: Optional[CodexAppServer],
        *,
        jev: Optional[JevClassifier] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        prompt_mode: str = "base",
        workdir: Optional[str] = None,
        owns_workdir: bool = False,
        missing_codex_hint: str = INSTALL_HINT,
        turn_timeout: float = 180.0,
    ) -> None:
        if prompt_mode not in PROMPT_MODES:
            raise ValueError(f"prompt_mode must be one of {PROMPT_MODES}")
        self.codex = codex
        self.jev = jev
        self.model = model
        self.effort = effort
        self.prompt_mode = prompt_mode
        self.workdir = workdir or tempfile.gettempdir()
        self._owns_workdir = owns_workdir
        self.missing_codex_hint = missing_codex_hint
        self.turn_timeout = turn_timeout
        self._auto_effort: Optional[str] = None
        self._auto_effort_ready = False
        self._login: dict[str, Any] = {}
        self._cache: collections.OrderedDict[str, dict[str, Any]] = collections.OrderedDict()
        self._background: set[asyncio.Task] = set()
        if codex is not None:
            codex.add_listener(self._on_notification)

    @classmethod
    def from_env(cls, env: Optional[dict[str, str]] = None) -> "TranslationService":
        env = dict(os.environ if env is None else env)
        jev = JevClassifier.from_env(env)
        model = env.get("TRANSLATOR_MODEL") or None
        effort = env.get("TRANSLATOR_EFFORT") or None
        prompt_mode = env.get("TRANSLATOR_PROMPT_MODE") or "base"
        codex_bin = find_codex_binary(env.get("TRANSLATOR_CODEX_BIN") or None)
        if codex_bin is None:
            return cls(None, jev=jev, model=model, effort=effort, prompt_mode=prompt_mode)
        # An empty working directory keeps any AGENTS.md or project config out
        # of the translation prompt.
        workdir = tempfile.mkdtemp(prefix="kr-translator-")
        extra_args = env.get("TRANSLATOR_CODEX_ARGS", "").split()
        codex = CodexAppServer(build_app_server_command(codex_bin, extra_args), cwd=workdir, env=env)
        return cls(
            codex,
            jev=jev,
            model=model,
            effort=effort,
            prompt_mode=prompt_mode,
            workdir=workdir,
            owns_workdir=True,
        )

    async def close(self) -> None:
        for task in list(self._background):
            task.cancel()
        if self.codex is not None:
            await self.codex.close()
        if self.jev is not None:
            await self.jev.close()
        if self._owns_workdir:
            shutil.rmtree(self.workdir, ignore_errors=True)

    # ------------------------------------------------------------------ account

    def _on_notification(self, method: str, params: dict) -> None:
        if method == "account/login/completed":
            self._login = {
                "pending": False,
                "success": bool(params.get("success")),
                "error": params.get("error"),
            }
            self._auto_effort_ready = False

    async def _codex(self) -> CodexAppServer:
        if self.codex is None:
            raise CodexError(self.missing_codex_hint)
        await self.codex.ensure_started()
        return self.codex

    async def status(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "jev": self.jev.describe() if self.jev else None,
            "login": self._login or None,
            "styles": [{"key": s.key, "label": s.label} for s in STYLES.values()],
            "targets": [
                {"code": t.code, "label": t.label, "native": t.native, "htmlLang": t.html_lang}
                for t in TARGETS.values()
            ],
        }
        try:
            codex = await self._codex()
            account = await codex.request("account/read", {"refreshToken": False})
        except CodexError as exc:
            result["codex"] = {"ok": False, "error": str(exc)}
            result["account"] = None
            return result
        result["codex"] = {"ok": True, "userAgent": codex.user_agent}
        result["account"] = (account or {}).get("account")
        result["requiresOpenaiAuth"] = (account or {}).get("requiresOpenaiAuth")
        if (result["account"] or {}).get("type") == "chatgpt":
            result["usage"] = await self._usage(codex)
        return result

    async def _usage(self, codex: CodexAppServer) -> Optional[dict[str, Any]]:
        try:
            limits = await codex.request("account/rateLimits/read", None, timeout=10)
        except CodexError:
            return None
        snapshot = (limits or {}).get("rateLimits") or {}
        windows = []
        for key in ("primary", "secondary"):
            window = snapshot.get(key)
            if isinstance(window, dict) and window.get("usedPercent") is not None:
                windows.append(
                    {
                        "usedPercent": window.get("usedPercent"),
                        "windowMinutes": window.get("windowDurationMins"),
                        "resetsAt": window.get("resetsAt"),
                    }
                )
        return {"windows": windows, "reached": snapshot.get("rateLimitReachedType")}

    async def start_login(self, device: bool = False) -> dict[str, Any]:
        codex = await self._codex()
        params = {"type": "chatgptDeviceCode"} if device else {"type": "chatgpt"}
        response = await codex.request("account/login/start", params)
        self._login = {"pending": True, "success": None, "error": None}
        return response or {}

    async def logout(self) -> None:
        codex = await self._codex()
        await codex.request("account/logout", None)
        self._login = {}
        self._auto_effort_ready = False

    # -------------------------------------------------------------- translation

    async def translate(
        self, text: str, *, style: str = AUTO_STYLE, targets: Optional[Sequence[str]] = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield UI events: start, jev, style, reset/delta per language, done or error, end.

        `targets` are the language codes checked in the page; `None` means all.
        """
        text = (text or "").strip()
        requested = TARGETS if targets is None else targets
        targets = [code for code in TARGETS if code in requested]
        if not text or not targets:
            yield {"type": "end"}
            return
        if len(text) > MAX_TEXT_LENGTH:
            yield {"type": "fatal", "message": f"한 번에 {MAX_TEXT_LENGTH:,}자까지 번역할 수 있습니다."}
            return

        started = time.perf_counter()
        yield {"type": "start", "targets": targets}

        style_key = style if style in STYLES else GENERAL
        source = "manual" if style in STYLES else "default"
        if style == AUTO_STYLE:
            if self.jev is None:
                yield {"type": "jev", "status": "off"}
            else:
                decision_event, picked = await self._ask_jev(text)
                yield decision_event
                if picked:
                    style_key, source = picked, "jev"
        yield {"type": "style", "key": style_key, "label": STYLES[style_key].label, "source": source}

        queue: asyncio.Queue = asyncio.Queue()
        tasks = []
        for code in targets:
            cached = self._cache_get(text, code, style_key)
            if cached is not None:
                queue.put_nowait({"type": "done", "lang": code, "cached": True, "ms": 0, **cached})
            else:
                tasks.append(asyncio.create_task(self._translate_one(code, text, style_key, queue, started)))
        remaining = len(targets)
        try:
            while remaining:
                event = await queue.get()
                if event["type"] in ("done", "error"):
                    remaining -= 1
                yield event
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
        yield {"type": "end", "ms": _elapsed_ms(started)}

    async def _ask_jev(self, text: str) -> tuple[dict[str, Any], Optional[str]]:
        assert self.jev is not None
        try:
            # httpx timeouts are per phase; this caps the whole call so a slow
            # Jev answer can never hold the translation back for long.
            decision = await asyncio.wait_for(self.jev.classify(text), self.jev.timeout + 0.25)
        except asyncio.TimeoutError:
            return {"type": "jev", "status": "skipped", "reason": f"{self.jev.timeout:g}초 초과"}, None
        except JevError as exc:
            return {"type": "jev", "status": "skipped", "reason": str(exc)}, None
        applied = self.jev.accepts(decision) and decision.label in STYLES
        event = {"type": "jev", "status": "ok", "applied": applied, **decision.to_dict()}
        return event, (decision.label if applied else None)

    async def _translate_one(
        self, code: str, text: str, style_key: str, queue: asyncio.Queue, started: float
    ) -> None:
        try:
            result = await self._run_turn(code, text, style_key, queue, started)
            if result is None:
                # The ChatGPT backend refused custom base instructions; keep
                # Codex's own base prompt and pass ours as developer instructions.
                # The other language may have switched the mode already.
                if self.prompt_mode == "base":
                    log.warning("base instructions rejected, switching to developer prompt mode")
                    self.prompt_mode = "developer"
                queue.put_nowait({"type": "reset", "lang": code})
                result = await self._run_turn(code, text, style_key, queue, started)
            if result is None:
                raise CodexError("번역 요청이 거부되었습니다.")
        except asyncio.CancelledError:
            raise
        except CodexError as exc:
            queue.put_nowait({"type": "error", "lang": code, "message": str(exc)})
            return
        except Exception as exc:  # keep the stream alive for the other language
            log.exception("translation failed")
            queue.put_nowait({"type": "error", "lang": code, "message": f"{type(exc).__name__}: {exc}"})
            return
        queue.put_nowait({"type": "translated", "lang": code, **result})
        try:
            back = await self._run_turn(code, result["text"], style_key, queue, started, back=True)
            if back is None:
                self.prompt_mode = "developer"
                queue.put_nowait({"type": "back_reset", "lang": code})
                back = await self._run_turn(code, result["text"], style_key, queue, started, back=True)
            if back is None:
                raise CodexError("한국어 확인 번역 요청이 거부되었습니다.")
            result["backText"] = back["text"]
            result["ms"] = _elapsed_ms(started)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Korean back translation failed")
            result["backError"] = "한국어 확인 번역을 완료하지 못했습니다. 다시 번역해 주세요."
        if result.get("backText"):
            self._cache_put(text, code, style_key, {"text": result["text"], "model": result["model"], "backText": result["backText"]})
        queue.put_nowait({"type": "done", "lang": code, "cached": False, **result})

    async def _run_turn(
        self, code: str, text: str, style_key: str, queue: asyncio.Queue, started: float, *, back: bool = False
    ) -> Optional[dict[str, Any]]:
        """Run one Codex turn; `None` means base instructions were rejected."""
        codex = await self._codex()
        instructions = build_back_instructions(TARGETS[code]) if back else build_instructions(TARGETS[code], STYLES[style_key])
        prefix = "back_" if back else ""
        omit_chinese_period = code == "zh" and not back
        prompt_mode = self.prompt_mode
        thread_params: dict[str, Any] = {
            "ephemeral": True,
            "cwd": self.workdir,
            "sandbox": "read-only",
            "approvalPolicy": "never",
            "model": self.model,
        }
        thread_params["baseInstructions" if prompt_mode == "base" else "developerInstructions"] = instructions
        thread = await codex.request("thread/start", thread_params)
        thread_id = thread["thread"]["id"]
        model = thread.get("model")
        events = codex.subscribe_thread(thread_id)
        turn_id: Optional[str] = None
        finished = False
        try:
            turn_params: dict[str, Any] = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text, "text_elements": []}],
            }
            effort = await self._effort(codex, model)
            if effort:
                turn_params["effort"] = effort
            turn = await codex.request("turn/start", turn_params)
            turn_id = turn["turn"]["id"]

            commentary: set[str] = set()
            current_item: Optional[str] = None
            final_text: Optional[str] = None
            streamed = ""
            first_token_ms: Optional[int] = None
            last_error: Optional[str] = None
            deadline = time.monotonic() + self.turn_timeout
            while True:
                try:
                    message = await asyncio.wait_for(events.get(), max(deadline - time.monotonic(), 0))
                except asyncio.TimeoutError:
                    raise CodexError("번역 응답 시간이 초과되었습니다.") from None
                if message is None:
                    raise CodexError(codex.exit_message())
                method, params = message
                if method == "item/started":
                    item = params.get("item") or {}
                    if item.get("type") != "agentMessage":
                        continue
                    if item.get("phase") == "commentary":
                        commentary.add(item.get("id"))
                        continue
                    if current_item is not None and item.get("id") != current_item:
                        queue.put_nowait({"type": prefix + "reset", "lang": code})
                        streamed = ""
                    current_item = item.get("id")
                elif method == "item/agentMessage/delta":
                    if params.get("itemId") in commentary:
                        continue
                    delta = params.get("delta") or ""
                    if omit_chinese_period:
                        delta = delta.replace("。", " ")
                    if not delta:
                        continue
                    if first_token_ms is None:
                        first_token_ms = _elapsed_ms(started)
                    streamed += delta
                    queue.put_nowait({"type": prefix + "delta", "lang": code, "text": delta})
                elif method == "item/completed":
                    item = params.get("item") or {}
                    if (
                        item.get("type") == "agentMessage"
                        and item.get("phase") != "commentary"
                        and item.get("id") not in commentary
                    ):
                        final_text = item.get("text") or ""
                        if omit_chinese_period:
                            final_text = final_text.replace("。", " ")
                elif method == "error":
                    error = params.get("error") or {}
                    if not params.get("willRetry"):
                        last_error = _error_text(error)
                elif method == "turn/completed":
                    finished = True
                    turn_info = params.get("turn") or {}
                    status = turn_info.get("status")
                    if status == "completed":
                        return {
                            "text": (final_text if final_text is not None else streamed).strip(),
                            "model": model,
                            "ms": _elapsed_ms(started),
                            "firstTokenMs": first_token_ms,
                        }
                    message_text = _error_text(turn_info.get("error") or {}) or last_error or f"turn {status}"
                    if prompt_mode == "base" and "instruction" in message_text.lower():
                        return None
                    raise CodexError(message_text)
        finally:
            codex.unsubscribe_thread(thread_id)
            if turn_id is not None and not finished:
                self._spawn(self._quiet_request(codex, "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}))
            self._spawn(self._quiet_request(codex, "thread/unsubscribe", {"threadId": thread_id}))

    async def _effort(self, codex: CodexAppServer, model: Optional[str]) -> Optional[str]:
        if self.effort:
            return self.effort
        if not self._auto_effort_ready:
            self._auto_effort = await self._pick_effort(codex, model)
            self._auto_effort_ready = True
        return self._auto_effort

    async def _pick_effort(self, codex: CodexAppServer, model: Optional[str]) -> Optional[str]:
        try:
            listing = await codex.request("model/list", {"includeHidden": True}, timeout=10)
        except CodexError:
            return None
        entries = [entry for entry in (listing or {}).get("data") or [] if isinstance(entry, dict)]
        entry = next((e for e in entries if model and model in (e.get("model"), e.get("id"))), None)
        if entry is None:
            return None
        supported = [
            option.get("reasoningEffort")
            for option in entry.get("supportedReasoningEfforts") or []
            if isinstance(option, dict)
        ]
        for effort in EFFORT_PREFERENCE:
            if effort in supported:
                return effort
        return None

    def _spawn(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    @staticmethod
    async def _quiet_request(codex: CodexAppServer, method: str, params: dict) -> None:
        with contextlib.suppress(CodexError):
            await codex.request(method, params, timeout=10)

    def _cache_key(self, text: str, code: str, style_key: str) -> str:
        raw = "\x00".join([self.model or "", code, style_key, text])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _cache_get(self, text: str, code: str, style_key: str) -> Optional[dict[str, Any]]:
        key = self._cache_key(text, code, style_key)
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
        return hit

    def _cache_put(self, text: str, code: str, style_key: str, value: dict[str, Any]) -> None:
        if not value.get("text"):
            return
        key = self._cache_key(text, code, style_key)
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _error_text(error: dict) -> str:
    parts = [error.get("message"), error.get("additionalDetails")]
    return " — ".join(str(part) for part in parts if part)
