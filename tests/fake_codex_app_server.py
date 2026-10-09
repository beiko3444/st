"""Stand-in for `codex app-server` used by tests/test_translator.py.

Speaks the same newline-delimited JSON-RPC as Codex 0.162.0 for the methods
the translator uses. Every request is appended to $FAKE_CODEX_LOG.

The "translation" is `[<lang>] <input>`, where <lang> is read from the
instructions. Input containing FAIL fails the turn, SLOW streams slowly,
COMMENTARY sends a commentary message before the answer, and
REJECT_BASE fails turns that set custom base instructions.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid

LOG = os.environ.get("FAKE_CODEX_LOG")
lock = threading.Lock()
logged_in = os.environ.get("FAKE_CODEX_LOGGED_IN") == "1"
threads: dict[str, dict] = {}
interrupted: set[str] = set()


def send(message: dict) -> None:
    with lock:
        sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def notify(method: str, params: dict) -> None:
    send({"method": method, "params": params})


def log_request(message: dict) -> None:
    if LOG:
        with lock, open(LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(message, ensure_ascii=False) + "\n")


def agent_item(item_id: str, text: str, phase=None) -> dict:
    return {"type": "agentMessage", "id": item_id, "text": text, "phase": phase,
            "memoryCitation": None, "delivery": None, "questions": None}


def run_turn(thread_id: str, turn_id: str, text: str) -> None:
    thread = threads[thread_id]
    instructions = thread.get("baseInstructions") or thread.get("developerInstructions") or ""
    back = "into Korean" in instructions
    lang = "ko" if back else ("zh" if "Simplified Chinese" in instructions else "en")

    def complete(status: str, error=None) -> None:
        notify("turn/completed", {"threadId": thread_id, "turn": {
            "id": turn_id, "items": [], "status": status, "error": error,
            "startedAt": None, "completedAt": None, "durationMs": None}})

    if "REJECT_BASE" in text and thread.get("baseInstructions"):
        complete("failed", {"message": "Instructions are not valid", "codexErrorInfo": None,
                            "additionalDetails": None, "misalignment": None})
        return
    if ("FAIL" in text and "BACK_FAIL" not in text) or (back and "BACK_FAIL" in text):
        notify("error", {"threadId": thread_id, "turnId": turn_id, "willRetry": True,
                         "error": {"message": "Reconnecting... 1/5"}})
        notify("error", {"threadId": thread_id, "turnId": turn_id, "willRetry": False,
                         "error": {"message": "usage limit reached"}})
        complete("failed", None)
        return
    if "COMMENTARY" in text:
        notify("item/started", {"threadId": thread_id, "turnId": turn_id,
                                "item": agent_item("c1", "", "commentary")})
        notify("item/agentMessage/delta", {"threadId": thread_id, "turnId": turn_id,
                                           "itemId": "c1", "delta": "Let me translate."})
        notify("item/completed", {"threadId": thread_id, "turnId": turn_id,
                                  "item": agent_item("c1", "Let me translate.", "commentary")})

    answer = f"[{lang}] {text}"
    notify("item/started", {"threadId": thread_id, "turnId": turn_id, "item": agent_item("m1", "")})
    delay = 0.3 if "SLOW" in text else 0.005
    for start in range(0, len(answer), 4):
        if turn_id in interrupted:
            complete("interrupted")
            return
        notify("item/agentMessage/delta", {"threadId": thread_id, "turnId": turn_id,
                                           "itemId": "m1", "delta": answer[start:start + 4]})
        time.sleep(delay)
    notify("item/completed", {"threadId": thread_id, "turnId": turn_id, "item": agent_item("m1", answer)})
    complete("completed")


def handle(message: dict):
    global logged_in
    method = message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        return {"userAgent": "kr_translator/0.0.0-fake", "codexHome": "/tmp/fake",
                "platformFamily": "unix", "platformOs": "linux"}
    if method == "account/read":
        account = {"type": "chatgpt", "email": "user@example.com", "planType": "plus"} if logged_in else None
        return {"account": account, "requiresOpenaiAuth": True}
    if method == "account/login/start":
        if params.get("type") == "chatgptDeviceCode":
            return {"type": "chatgptDeviceCode", "loginId": "login-2",
                    "verificationUrl": "https://auth.openai.com/codex/device", "userCode": "ABCD-1234"}
        logged_in = True

        def finish() -> None:
            time.sleep(0.05)
            notify("account/login/completed", {"loginId": "login-1", "success": True, "error": None})

        threading.Thread(target=finish, daemon=True).start()
        return {"type": "chatgpt", "loginId": "login-1",
                "authUrl": "https://auth.openai.com/oauth/authorize?client_id=fake"}
    if method == "account/logout":
        logged_in = False
        return {}
    if method == "account/rateLimits/read":
        return {"rateLimits": {"primary": {"usedPercent": 12.5, "windowDurationMins": 300, "resetsAt": 1},
                               "secondary": {"usedPercent": 3, "windowDurationMins": 10080, "resetsAt": 2},
                               "rateLimitReachedType": None}}
    if method == "model/list":
        return {"data": [{"id": "gpt-fake", "model": "gpt-fake", "isDefault": True,
                          "supportedReasoningEfforts": [{"reasoningEffort": "low", "description": ""},
                                                        {"reasoningEffort": "medium", "description": ""}],
                          "defaultReasoningEffort": "medium"}], "nextCursor": None}
    if method == "thread/start":
        thread_id = str(uuid.uuid4())
        threads[thread_id] = params
        return {"thread": {"id": thread_id, "ephemeral": True}, "model": params.get("model") or "gpt-fake"}
    if method == "turn/start":
        turn_id = str(uuid.uuid4())
        text = params["input"][0]["text"]
        threading.Thread(target=run_turn, args=(params["threadId"], turn_id, text), daemon=True).start()
        return {"turn": {"id": turn_id, "items": [], "status": "inProgress", "error": None}}
    if method == "turn/interrupt":
        interrupted.add(params.get("turnId"))
        return {}
    if method == "thread/unsubscribe":
        threads.pop(params.get("threadId"), None)
        return {"status": "unsubscribed"}
    raise KeyError(method)


def main() -> None:
    for line in sys.stdin:
        message = json.loads(line)
        log_request(message)
        if "id" not in message:
            continue
        try:
            send({"id": message["id"], "result": handle(message)})
        except KeyError as exc:
            send({"id": message["id"], "error": {"code": -32601, "message": f"unknown method {exc}"}})


if __name__ == "__main__":
    main()
