"""Async JSON-RPC client for `codex app-server` over stdio.

The app-server ships with the official Codex CLI (`npm install -g @openai/codex`).
It owns the ChatGPT sign-in stored in `$CODEX_HOME/auth.json`, so every turn
started through it runs on the signed-in ChatGPT plan instead of an API key.

Protocol notes (checked against `codex app-server generate-ts`, Codex 0.162.0):
- one JSON object per line, `{"id", "method", "params"}` requests without a
  `"jsonrpc"` field;
- `initialize` request, then an `initialized` notification;
- turn output streams as `item/agentMessage/delta` notifications and ends with
  `turn/completed`.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import itertools
import json
import logging
import os
import shutil
from typing import Any, Callable, Optional, Sequence

log = logging.getLogger(__name__)

CLIENT_NAME = "kr_translator"
CLIENT_VERSION = "1.0.0"

# Translation threads only need to write text. Turning these off removes the
# shell, browser, web search, plugin and sub-agent tools from the model's tool
# list. `-c features.<name>=false` is used instead of `--disable <name>` because
# Codex rejects unknown names given to `--disable` but only warns about unknown
# `-c` keys, so a renamed flag in a future Codex release cannot stop startup.
HARDENING_OVERRIDES: tuple[str, ...] = tuple(
    f"features.{name}=false"
    for name in (
        "shell_tool",
        "unified_exec",
        "apps",
        "plugins",
        "multi_agent",
        "image_generation",
        "browser_use",
        "computer_use",
        "in_app_browser",
        "view_image",
        "goals",
        "memories",
        "hooks",
        "tool_suggest",
        "sleep_tool",
    )
) + ('web_search="disabled"',)

_STDOUT_LIMIT = 32 * 1024 * 1024


class CodexError(RuntimeError):
    """Raised when the app-server is unavailable or answers with an error."""


def find_codex_binary(explicit: Optional[str] = None) -> Optional[str]:
    if explicit:
        return explicit if os.path.exists(explicit) else shutil.which(explicit)
    return shutil.which("codex")


def build_app_server_command(
    codex_bin: str, extra_args: Sequence[str] = (), harden: bool = True
) -> list[str]:
    command = [codex_bin, "app-server"]
    if harden:
        for override in HARDENING_OVERRIDES:
            command += ["-c", override]
    command += list(extra_args)
    return command


Listener = Callable[[str, dict], None]


class CodexAppServer:
    """One long-lived `codex app-server` child process shared by all requests."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        request_timeout: float = 30.0,
    ) -> None:
        self.command = list(command)
        self.cwd = cwd
        self.env = env
        self.request_timeout = request_timeout
        self.user_agent: Optional[str] = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._threads: dict[str, asyncio.Queue] = {}
        self._listeners: list[Listener] = []
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=40)
        self._start_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def add_listener(self, listener: Listener) -> None:
        """Receive every notification as `listener(method, params)`."""
        self._listeners.append(listener)

    async def ensure_started(self) -> None:
        async with self._start_lock:
            if self.running:
                return
            await self._start()

    async def _start(self) -> None:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=self.env,
                limit=_STDOUT_LIMIT,
            )
        except OSError as exc:
            self._proc = None
            raise CodexError(f"codex app-server를 실행하지 못했습니다: {exc}") from exc

        self._reader_task = asyncio.create_task(self._read_stdout(self._proc))
        self._stderr_task = asyncio.create_task(self._read_stderr(self._proc))
        result = await self.request(
            "initialize",
            {
                "clientInfo": {"name": CLIENT_NAME, "title": "KR Translator", "version": CLIENT_VERSION},
                "capabilities": None,
            },
            timeout=60.0,
        )
        self.user_agent = (result or {}).get("userAgent")
        await self.notify("initialized")

    async def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._proc = None

    async def request(self, method: str, params: Any = None, *, timeout: Optional[float] = None) -> Any:
        if not self.running:
            raise CodexError(self.exit_message())
        request_id = next(self._ids)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout or self.request_timeout)
        except asyncio.TimeoutError as exc:
            raise CodexError(f"codex app-server가 '{method}'에 응답하지 않습니다.") from exc
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: Any = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        await self._write(message)

    def subscribe_thread(self, thread_id: str) -> asyncio.Queue:
        """Queue that receives `(method, params)` for one thread; `None` means the server died."""
        queue: asyncio.Queue = asyncio.Queue()
        self._threads[thread_id] = queue
        return queue

    def unsubscribe_thread(self, thread_id: str) -> None:
        self._threads.pop(thread_id, None)

    async def _write(self, message: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            raise CodexError(self.exit_message())
        proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        try:
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise CodexError(self.exit_message()) from exc

    async def _read_stdout(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("codex app-server: non-JSON line %r", line[:200])
                    continue
                if isinstance(message, dict):
                    await self._dispatch(message)
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=2)
            error = CodexError(self.exit_message())
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            for queue in self._threads.values():
                queue.put_nowait(None)

    async def _read_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            self._stderr_tail.append(text)
            log.debug("codex app-server: %s", text)

    async def _dispatch(self, message: dict) -> None:
        method = message.get("method")
        if method is None:
            future = self._pending.get(message.get("id"))  # type: ignore[arg-type]
            if future is None or future.done():
                return
            if "error" in message:
                error = message.get("error") or {}
                future.set_exception(CodexError(str(error.get("message") or error)))
            else:
                future.set_result(message.get("result"))
            return

        params = message.get("params") or {}
        if "id" in message:
            await self._answer_server_request(message["id"], method)
            return

        thread_id = params.get("threadId") if isinstance(params, dict) else None
        if thread_id and thread_id in self._threads:
            self._threads[thread_id].put_nowait((method, params))
        for listener in list(self._listeners):
            try:
                listener(method, params)
            except Exception:  # a broken listener must not stop the reader
                log.exception("codex notification listener failed")

    async def _answer_server_request(self, request_id: Any, method: str) -> None:
        # Translation turns never need approvals or user input; refuse anything
        # the server asks so it does not wait on us.
        if method.endswith("/requestApproval"):
            reply: dict[str, Any] = {"id": request_id, "result": {"decision": "decline"}}
        elif method == "item/tool/requestUserInput":
            reply = {"id": request_id, "result": {"answers": {}}}
        else:
            reply = {"id": request_id, "error": {"code": -32601, "message": f"{method} is not supported"}}
        with contextlib.suppress(CodexError):
            await self._write(reply)

    def exit_message(self) -> str:
        code = self._proc.returncode if self._proc is not None else None
        tail = "\n".join(list(self._stderr_tail)[-5:])
        message = "codex app-server가 실행 중이 아닙니다"
        if code is not None:
            message += f" (exit {code})"
        return f"{message}.\n{tail}".strip()
