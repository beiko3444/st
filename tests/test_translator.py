from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from inventory_web.translator_portal import login_token, resolve_translator_url, translator_gist_url
from translator import auth as translator_auth
from translator.app import create_app
from translator.codex_client import CodexAppServer, build_app_server_command
from translator.jev import JevClassifier
from translator.service import TranslationService

FAKE_SERVER = Path(__file__).resolve().parent / "fake_codex_app_server.py"


def jev_reply(choice: str, confidence: float) -> dict:
    return {
        "model": "jev-1.13.0",
        "answers": {
            "style": {
                "type": "choice",
                "choice": choice,
                "confidence": confidence,
                "probabilities": {choice: confidence, "other": round(1 - confidence, 3)},
            }
        },
        "usage": {"input_tokens": 50, "output_tokens": 1},
    }


class FakeCodexMixin:
    def make_service(self, *, logged_in: bool = True, jev: JevClassifier | None = None, **kwargs) -> TranslationService:
        self.tmp = tempfile.mkdtemp(prefix="translator-test-")
        self.log_path = Path(self.tmp) / "requests.jsonl"
        env = dict(os.environ, FAKE_CODEX_LOG=str(self.log_path), FAKE_CODEX_LOGGED_IN="1" if logged_in else "0")
        codex = CodexAppServer([sys.executable, str(FAKE_SERVER)], cwd=self.tmp, env=env, request_timeout=5)
        return TranslationService(codex, jev=jev, workdir=self.tmp, **kwargs)

    def requests(self, method: str) -> list[dict]:
        if not self.log_path.exists():
            return []
        lines = self.log_path.read_text(encoding="utf-8").splitlines()
        return [message for message in map(json.loads, lines) if message.get("method") == method]

    def cleanup_tmp(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


async def collect(service: TranslationService, text: str, **kwargs) -> list[dict]:
    return [event async for event in service.translate(text, **kwargs)]


def final_texts(events: list[dict]) -> dict[str, str]:
    return {event["lang"]: event["text"] for event in events if event["type"] == "done"}


def streamed_texts(events: list[dict]) -> dict[str, str]:
    texts: dict[str, str] = {}
    for event in events:
        if event["type"] == "reset":
            texts[event["lang"]] = ""
        elif event["type"] == "delta":
            texts[event["lang"]] = texts.get(event["lang"], "") + event["text"]
    return texts


class TranslationServiceTests(FakeCodexMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await self.service.close()
        self.cleanup_tmp()

    async def test_streams_english_and_chinese_in_parallel(self) -> None:
        self.service = self.make_service()
        events = await collect(self.service, "안녕하세요, 견적 부탁드립니다.")

        self.assertEqual(
            final_texts(events),
            {"en": "[en] 안녕하세요, 견적 부탁드립니다.", "zh": "[zh] 안녕하세요, 견적 부탁드립니다."},
        )
        self.assertEqual(streamed_texts(events), final_texts(events))
        self.assertEqual(events[-1]["type"], "end")
        self.assertIn({"type": "jev", "status": "off"}, events)

        threads = [r for r in self.requests("thread/start") if "into Korean" not in r["params"].get("baseInstructions", "")]
        self.assertEqual(len(threads), 2)
        for request in threads:
            params = request["params"]
            self.assertTrue(params["ephemeral"])
            self.assertEqual(params["sandbox"], "read-only")
            self.assertEqual(params["approvalPolicy"], "never")
            self.assertIn("Reply with the translation only", params["baseInstructions"])
        targets = sorted("Simplified Chinese" in r["params"]["baseInstructions"] for r in threads)
        self.assertEqual(targets, [False, True])
        # The fake model offers low/medium, so the fastest supported effort is used.
        self.assertEqual({r["params"].get("effort") for r in self.requests("turn/start")}, {"low"})

    async def test_commentary_messages_are_not_shown(self) -> None:
        self.service = self.make_service()
        events = await collect(self.service, "COMMENTARY 테스트", targets=["en"])

        self.assertEqual(final_texts(events), {"en": "[en] COMMENTARY 테스트"})
        self.assertEqual(streamed_texts(events), {"en": "[en] COMMENTARY 테스트"})

    async def test_failed_turn_reports_final_error(self) -> None:
        self.service = self.make_service()
        events = await collect(self.service, "FAIL", targets=["en"])

        errors = [event for event in events if event["type"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("usage limit reached", errors[0]["message"])

    async def test_rejected_base_instructions_fall_back_to_developer_mode(self) -> None:
        self.service = self.make_service()
        events = await collect(self.service, "REJECT_BASE 안녕")

        self.assertEqual(final_texts(events), {"en": "[en] REJECT_BASE 안녕", "zh": "[zh] REJECT_BASE 안녕"})
        self.assertEqual(self.service.prompt_mode, "developer")
        last_thread = self.requests("thread/start")[-1]["params"]
        self.assertNotIn("baseInstructions", last_thread)
        self.assertIn("Reply with the translation only", last_thread["developerInstructions"])

    async def test_repeated_text_is_served_from_cache(self) -> None:
        self.service = self.make_service()
        await collect(self.service, "같은 문장")
        events = await collect(self.service, "같은 문장")

        self.assertTrue(all(event["cached"] for event in events if event["type"] == "done"))
        self.assertEqual(len(self.requests("turn/start")), 4)

    async def test_back_translation_uses_finished_translation_as_input(self) -> None:
        self.service = self.make_service()
        events = await collect(self.service, "원문입니다", targets=["en"])
        translated = next(e for e in events if e["type"] == "translated")
        done = next(e for e in events if e["type"] == "done")
        self.assertEqual(done["backText"], "[ko] [en] 원문입니다")
        self.assertLess(events.index(translated), events.index(done))
        inputs = [r["params"]["input"][0]["text"] for r in self.requests("turn/start")]
        self.assertEqual(inputs, ["원문입니다", translated["text"]])
        again = await collect(self.service, "원문입니다", targets=["en"])
        cached = next(e for e in again if e["type"] == "done")
        self.assertTrue(cached["cached"])
        self.assertEqual(cached["backText"], done["backText"])
        self.assertEqual(len(self.requests("turn/start")), 2)

    async def test_back_translation_failure_preserves_foreign_translation(self) -> None:
        self.service = self.make_service()
        events = await collect(self.service, "BACK_FAIL", targets=["en"])
        done = next(e for e in events if e["type"] == "done")
        self.assertEqual(done["text"], "[en] BACK_FAIL")
        self.assertIn("backError", done)
        self.assertNotIn("backText", done)
        self.assertEqual(events[-1]["type"], "end")
        self.assertIsNone(self.service._cache_get("BACK_FAIL", "en", "general"))

    async def test_closing_the_stream_interrupts_running_turns(self) -> None:
        self.service = self.make_service()
        stream = self.service.translate("SLOW 긴 문장입니다", targets=["en"])
        async for event in stream:
            if event["type"] == "delta":
                break
        await stream.aclose()

        for _ in range(50):
            if self.requests("turn/interrupt"):
                break
            await asyncio.sleep(0.05)
        self.assertEqual(len(self.requests("turn/interrupt")), 1)

    async def test_jev_choice_sets_translation_style(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=jev_reply("business", 0.91))

        jev = JevClassifier("typesafe", api_key="ts-key", transport=httpx.MockTransport(handler))
        self.service = self.make_service(jev=jev)
        events = await collect(self.service, "단가 협상 가능할까요?", targets=["en"])

        decision = next(event for event in events if event["type"] == "jev")
        self.assertEqual((decision["status"], decision["label"], decision["applied"]), ("ok", "business", True))
        style = next(event for event in events if event["type"] == "style")
        self.assertEqual((style["key"], style["source"]), ("business", "jev"))
        self.assertIn("Business correspondence", self.requests("thread/start")[0]["params"]["baseInstructions"])

        request = seen[0]
        self.assertEqual(str(request.url), "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(request.headers["authorization"], "Bearer ts-key")
        body = json.loads(request.content)
        self.assertEqual(body["model"], "jev-latest")
        self.assertEqual(body["state"], "단가 협상 가능할까요?")
        question = body["questions"]["style"]
        self.assertEqual(question["type"], "choice")
        self.assertIn("other", question["criteria"])

    async def test_low_confidence_jev_keeps_general_style(self) -> None:
        jev = JevClassifier(
            "typesafe", api_key="k", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=jev_reply("casual", 0.3)))
        )
        self.service = self.make_service(jev=jev)
        events = await collect(self.service, "음...", targets=["en"])

        decision = next(event for event in events if event["type"] == "jev")
        self.assertFalse(decision["applied"])
        style = next(event for event in events if event["type"] == "style")
        self.assertEqual(style["key"], "general")

    async def test_slow_jev_does_not_block_translation(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(2)
            return httpx.Response(200, json=jev_reply("business", 0.9))

        jev = JevClassifier("typesafe", api_key="k", timeout=0.2, transport=httpx.MockTransport(handler))
        self.service = self.make_service(jev=jev)
        started = time.perf_counter()
        events = await collect(self.service, "빠르게", targets=["en"])

        self.assertLess(time.perf_counter() - started, 1.5)
        decision = next(event for event in events if event["type"] == "jev")
        self.assertEqual(decision["status"], "skipped")
        self.assertEqual(final_texts(events), {"en": "[en] 빠르게"})

    async def test_manual_style_skips_jev(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("Jev must not be called for a manual style")

        jev = JevClassifier("typesafe", api_key="k", transport=httpx.MockTransport(handler))
        self.service = self.make_service(jev=jev)
        events = await collect(self.service, "고객님 죄송합니다", style="support", targets=["zh"])

        self.assertFalse([event for event in events if event["type"] == "jev"])
        self.assertIn("Customer service", self.requests("thread/start")[0]["params"]["baseInstructions"])

    async def test_status_reports_account_and_usage(self) -> None:
        self.service = self.make_service()
        status = await self.service.status()

        self.assertTrue(status["codex"]["ok"])
        self.assertEqual(status["account"]["email"], "user@example.com")
        self.assertEqual([w["windowMinutes"] for w in status["usage"]["windows"]], [300, 10080])


class JevCloudflareTests(unittest.IsolatedAsyncioTestCase):
    async def test_cloudflare_payload_and_envelope(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"result": jev_reply("product", 0.8), "success": True, "errors": []})

        configured = JevClassifier.from_env(
            {"CLOUDFLARE_ACCOUNT_ID": "acc123", "CLOUDFLARE_API_TOKEN": "cf-token", "JEV_TIMEOUT": "2"}
        )
        assert configured is not None
        self.assertEqual((configured.provider, configured.timeout), ("cloudflare", 2.0))
        await configured.close()
        jev = JevClassifier(
            "cloudflare", api_key="cf-token", account_id="acc123", transport=httpx.MockTransport(handler)
        )
        decision = await jev.classify("신상 원피스 할인")
        await jev.close()

        self.assertEqual((decision.label, decision.confidence, decision.provider), ("product", 0.8, "cloudflare"))
        self.assertEqual(str(seen[0].url), "https://api.cloudflare.com/client/v4/accounts/acc123/ai/run")
        body = json.loads(seen[0].content)
        self.assertEqual(body["model"], "typesafe/jev")
        self.assertEqual(body["input"]["state"], "신상 원피스 할인")

    def test_from_env_prefers_typesafe_and_can_be_turned_off(self) -> None:
        env = {"TYPESAFE_API_KEY": "k", "CLOUDFLARE_ACCOUNT_ID": "a", "CLOUDFLARE_API_TOKEN": "t"}
        jev = JevClassifier.from_env(env)
        self.assertEqual(jev.provider if jev else None, "typesafe")
        asyncio.run(jev.close())
        self.assertIsNone(JevClassifier.from_env({**env, "JEV_PROVIDER": "off"}))
        self.assertIsNone(JevClassifier.from_env({}))


class TranslatorHttpTests(FakeCodexMixin, unittest.TestCase):
    def tearDown(self) -> None:
        self.cleanup_tmp()

    def client(self, **kwargs) -> TestClient:
        return TestClient(create_app(self.make_service(**kwargs)), base_url="http://127.0.0.1:8777")

    def test_translate_endpoint_streams_server_sent_events(self) -> None:
        with self.client() as client:
            response = client.post("/api/translate", json={"text": "감사합니다", "style": "auto"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertEqual(final_texts(events), {"en": "[en] 감사합니다", "zh": "[zh] 감사합니다"})

    def test_only_checked_languages_are_translated(self) -> None:
        with self.client() as client:
            languages = client.get("/api/status").json()["targets"]
            response = client.post("/api/translate", json={"text": "감사합니다", "targets": ["zh"]})
            nothing = client.post("/api/translate", json={"text": "감사합니다", "targets": []})
        self.assertEqual(
            [(t["code"], t["native"], t["htmlLang"]) for t in languages],
            [("en", "English", "en"), ("zh", "简体中文", "zh-CN")],
        )
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertEqual(final_texts(events), {"zh": "[zh] 감사합니다"})
        self.assertEqual(events[0], {"type": "start", "targets": ["zh"]})
        self.assertEqual(len(self.requests("thread/start")), 2)
        self.assertIn('"type": "end"', nothing.text)
        self.assertNotIn('"type": "start"', nothing.text)

    def test_login_returns_browser_url_and_status_follows(self) -> None:
        with self.client(logged_in=False) as client:
            self.assertIsNone(client.get("/api/status").json()["account"])
            login = client.post("/api/login", json={"method": "browser"}).json()
            self.assertTrue(login["authUrl"].startswith("https://auth.openai.com/"))
            device = client.post("/api/login", json={"method": "device"}).json()
            self.assertEqual(device["userCode"], "ABCD-1234")
            status = client.get("/api/status").json()
        self.assertEqual(status["account"]["type"], "chatgpt")

    def test_index_and_foreign_requests(self) -> None:
        with self.client() as client:
            self.assertIn("KR 즉시 번역기", client.get("/").text)
            cross_site = client.post(
                "/api/translate", json={"text": "x"}, headers={"Origin": "https://evil.example"}
            )
            self.assertEqual(cross_site.status_code, 403)
            rebinding = client.get("/api/status", headers={"Host": "evil.example"})
            self.assertEqual(rebinding.status_code, 400)

    def test_missing_codex_is_reported(self) -> None:
        self.tmp = tempfile.mkdtemp()
        service = TranslationService.from_env({"TRANSLATOR_CODEX_BIN": "/nonexistent/codex", "PATH": ""})
        with TestClient(create_app(service), base_url="http://127.0.0.1") as client:
            status = client.get("/api/status").json()
        self.assertFalse(status["codex"]["ok"])
        self.assertIn("npm install -g @openai/codex", status["codex"]["error"])


SECRET = "test-shared-secret"
PORTAL = "https://inventory.example/translator"
TUNNEL = "https://quiet-river-1234.trycloudflare.com"


def poll_job(client: TestClient, job_id: str) -> list[dict]:
    events: list[dict] = []
    after = 0
    for _ in range(200):
        page = client.get(f"/api/jobs/{job_id}", params={"after": after, "wait": 5}).json()
        events += page["events"]
        after = page["next"]
        if page["done"]:
            return events
    raise AssertionError("job did not finish")


class SingleSignOnTokenTests(unittest.TestCase):
    def test_inventory_site_tokens_open_a_translator_session(self) -> None:
        now = 1_800_000_000
        token = login_token(SECRET, now=now)

        self.assertTrue(translator_auth.verify(SECRET, translator_auth.LOGIN_PURPOSE, token, now=now))
        self.assertFalse(translator_auth.verify(SECRET, translator_auth.LOGIN_PURPOSE, token, now=now + 121))
        self.assertFalse(translator_auth.verify("other-secret", translator_auth.LOGIN_PURPOSE, token, now=now))
        self.assertFalse(translator_auth.verify(SECRET, translator_auth.SESSION_PURPOSE, token, now=now))
        expires, signature = token.split(".")
        self.assertFalse(translator_auth.verify(SECRET, translator_auth.LOGIN_PURPOSE, f"{int(expires) + 999}.{signature}", now=now))
        self.assertFalse(translator_auth.verify(SECRET, translator_auth.LOGIN_PURPOSE, "", now=now))

    def test_translator_url_lookup(self) -> None:
        gist = "https://gist.githubusercontent.com/beiko3444/abc/raw/monitor.json"
        self.assertEqual(translator_gist_url(gist), "https://gist.githubusercontent.com/beiko3444/abc/raw/translator.json")
        self.assertEqual(
            translator_gist_url("https://gist.githubusercontent.com/u/abc/raw/0123abcd/monitor.json?x=1"),
            "https://gist.githubusercontent.com/u/abc/raw/translator.json",
        )
        self.assertIsNone(translator_gist_url(""))

        asked: list[str] = []

        def resolve(url: str) -> str:
            asked.append(url)
            return TUNNEL + "/"

        self.assertEqual(resolve_translator_url(gist, resolve, env={}), TUNNEL)
        self.assertEqual(asked, ["https://gist.githubusercontent.com/beiko3444/abc/raw/translator.json"])
        self.assertEqual(resolve_translator_url(gist, resolve, env={"TRANSLATOR_URL": "https://t.example/"}), "https://t.example")
        self.assertIsNone(resolve_translator_url("", resolve, env={}))


class DeployedTranslatorTests(FakeCodexMixin, unittest.TestCase):
    def tearDown(self) -> None:
        self.cleanup_tmp()

    def client(self, **kwargs) -> TestClient:
        app = create_app(self.make_service(**kwargs), shared_secret=SECRET, portal_url=PORTAL)
        return TestClient(app, base_url=TUNNEL, follow_redirects=False)

    def sign_in(self, client: TestClient) -> None:
        response = client.get("/auth", params={"token": login_token(SECRET)})
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/"))
        cookie = response.headers["set-cookie"]
        for flag in ("HttpOnly", "Secure", "SameSite=lax"):
            self.assertIn(flag, cookie)

    def test_requires_a_session_from_the_inventory_site(self) -> None:
        with self.client() as client:
            self.assertEqual(client.get("/healthz").json(), {"ok": True})
            denied = client.get("/api/status")
            self.assertEqual((denied.status_code, denied.json()["portalUrl"]), (401, PORTAL))
            self.assertEqual(client.get("/").headers["location"], PORTAL)
            self.assertEqual(client.post("/api/jobs", json={"text": "안녕"}).status_code, 401)
            self.assertEqual(client.get("/auth", params={"token": "123.bad"}).status_code, 403)

            self.sign_in(client)
            status = client.get("/api/status").json()
            self.assertTrue(status["deployed"])
            self.assertEqual(status["portalUrl"], PORTAL)
            self.assertEqual(client.get("/").status_code, 200)
        self.assertEqual(self.requests("turn/start"), [])

    def test_public_visitors_cannot_change_or_read_server_account(self) -> None:
        with self.client() as client:
            self.sign_in(client)
            status = client.get("/api/status").json()
            self.assertTrue(status["ready"])
            for key in ("account", "usage", "login"):
                self.assertNotIn(key, status)
            self.assertEqual(client.post("/api/login", json={"method": "device"}).status_code, 403)
            self.assertEqual(client.post("/api/logout").status_code, 403)
            self.assertTrue(client.get("/api/status").json()["ready"])
        self.assertEqual(self.requests("account/logout"), [])
        self.assertEqual(self.requests("account/login/start"), [])

    def test_long_poll_job_streams_translation(self) -> None:
        with self.client() as client:
            self.sign_in(client)
            job = client.post("/api/jobs", json={"text": "견적 부탁드립니다", "targets": ["en", "zh"]}).json()
            events = poll_job(client, job["id"])
            self.assertEqual(client.get("/api/jobs/unknown").status_code, 404)

        self.assertEqual(final_texts(events), {"en": "[en] 견적 부탁드립니다", "zh": "[zh] 견적 부탁드립니다"})
        self.assertEqual(streamed_texts(events), final_texts(events))
        self.assertEqual(events[-1]["type"], "end")

    def test_new_job_replaces_the_running_one(self) -> None:
        with self.client() as client:
            self.sign_in(client)
            slow = client.post("/api/jobs", json={"text": "SLOW 아주 긴 문장", "targets": ["en"]}).json()
            first = client.get(f"/api/jobs/{slow['id']}", params={"after": 0, "wait": 5}).json()
            self.assertFalse(first["done"])
            after = first["next"]
            while not any(e["type"] == "delta" for e in first["events"]):
                first = client.get(f"/api/jobs/{slow['id']}", params={"after": after, "wait": 5}).json()
                after = first["next"]
                self.assertFalse(first["done"])
            fresh = client.post("/api/jobs", json={"text": "새 문장", "targets": ["en"], "replaces": slow["id"]}).json()
            events = poll_job(client, fresh["id"])
            old = client.get(f"/api/jobs/{slow['id']}", params={"after": first["next"], "wait": 5}).json()

        self.assertEqual(final_texts(events), {"en": "[en] 새 문장"})
        self.assertTrue(old["done"])
        self.assertNotIn("done", [event["type"] for event in old["events"]])
        for _ in range(50):
            if self.requests("turn/interrupt"):
                break
            time.sleep(0.05)
        self.assertEqual(len(self.requests("turn/interrupt")), 1)

    def test_server_account_management_and_foreign_requests_fail(self) -> None:
        with self.client(logged_in=False) as client:
            self.sign_in(client)
            self.assertEqual(client.post("/api/login", json={"method": "browser"}).status_code, 403)
            self.assertEqual(client.post("/api/login", json={"method": "device"}).status_code, 403)
            cross_site = client.post("/api/jobs", json={"text": "x"}, headers={"Origin": "https://evil.example"})
            self.assertEqual(cross_site.status_code, 403)
            self.assertEqual(client.get("/healthz", headers={"Host": "evil.example"}).status_code, 400)


class InventoryTranslatorMenuTests(unittest.TestCase):
    def client(self, **env: str) -> TestClient:
        from inventory_web.app import create_app as create_inventory_app

        base = {"VERCEL": "1", "SMARTINVENTORY_WEB_PASSWORD": "pw", "TRANSLATOR_SHARED_SECRET": SECRET, "TRANSLATOR_URL": TUNNEL}
        patcher = patch.dict(os.environ, {**base, **env})
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("SMARTINVENTORY_MONITOR_URL", "MONITOR_URL", "SMARTINVENTORY_CACHE_DB"):
            os.environ.pop(key, None)
        return TestClient(create_inventory_app(), follow_redirects=False)

    def test_menu_link_redirects_signed_in_users_with_a_token(self) -> None:
        client = self.client()
        self.assertEqual(client.post("/login", data={"password": "pw"}).status_code, 303)
        self.assertIn('href="/translator"', client.get("/").text)
        response = client.get("/translator")

        self.assertEqual(response.status_code, 303)
        location = response.headers["location"]
        self.assertTrue(location.startswith(f"{TUNNEL}/auth?token="))
        token = location.split("token=", 1)[1]
        self.assertTrue(translator_auth.verify(SECRET, translator_auth.LOGIN_PURPOSE, token))
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")

    def test_menu_needs_site_login_and_configuration(self) -> None:
        client = self.client()
        self.assertEqual(client.get("/translator").headers["location"], "/login")

        client = self.client(TRANSLATOR_SHARED_SECRET="")
        client.post("/login", data={"password": "pw"})
        self.assertEqual(client.get("/translator").status_code, 503)

        client = self.client(SMARTINVENTORY_WEB_PASSWORD="")
        self.assertIn("SMARTINVENTORY_WEB_PASSWORD", client.get("/translator").text)


class _FakeResponsesHandler(BaseHTTPRequestHandler):
    """Minimal OpenAI Responses API stream so the real Codex binary can run a turn offline."""

    protocol_version = "HTTP/1.1"
    bodies: list = []

    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        type(self).bodies.append(body)
        answer = "你好" if "Simplified Chinese" in json.dumps(body, ensure_ascii=False) else "Hello"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def event(name: str, data: dict) -> None:
            payload = json.dumps(dict(data, type=name))
            self.wfile.write(f"event: {name}\ndata: {payload}\n\n".encode())
            self.wfile.flush()

        item = {"type": "message", "role": "assistant", "id": "msg_1", "content": []}
        event("response.created", {"response": {"id": "resp_1"}})
        event("response.output_item.added", {"output_index": 0, "item": item})
        for char in answer:
            event("response.output_text.delta", {"item_id": "msg_1", "output_index": 0, "content_index": 0, "delta": char})
        done = dict(item, content=[{"type": "output_text", "text": answer, "annotations": []}])
        event("response.output_item.done", {"output_index": 0, "item": done})
        event("response.completed", {"response": {"id": "resp_1", "usage": {
            "input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
            "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}}})


@unittest.skipUnless(os.environ.get("CODEX_BIN"), "set CODEX_BIN to run against the real Codex CLI")
class RealCodexIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Runs the real `codex app-server`; only the model endpoint is faked."""

    async def test_translation_through_real_app_server(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = ThreadingHTTPServer(("127.0.0.1", port), _FakeResponsesHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        home = tempfile.mkdtemp(prefix="codex-home-")
        workdir = tempfile.mkdtemp(prefix="codex-work-")
        provider = [
            "-c", 'model_provider="fake"',
            "-c", 'model_providers.fake.name="fake"',
            "-c", f'model_providers.fake.base_url="http://127.0.0.1:{port}/v1"',
            "-c", 'model_providers.fake.wire_api="responses"',
            "-c", 'model_providers.fake.env_key="FAKE_KEY"',
            "-c", 'model="fake-model"',
        ]
        env = dict(os.environ, CODEX_HOME=home, FAKE_KEY="x", NO_PROXY="127.0.0.1", no_proxy="127.0.0.1")
        codex = CodexAppServer(build_app_server_command(os.environ["CODEX_BIN"], provider), cwd=workdir, env=env)
        service = TranslationService(codex, workdir=workdir, effort="low")
        try:
            events = await collect(service, "안녕하세요")
            status = await service.status()
        finally:
            await service.close()
            server.shutdown()
            server.server_close()
            shutil.rmtree(home, ignore_errors=True)
            shutil.rmtree(workdir, ignore_errors=True)

        self.assertEqual(final_texts(events), {"en": "Hello", "zh": "你好"})
        self.assertEqual(streamed_texts(events), {"en": "Hello", "zh": "你好"})
        self.assertIsNone(status["account"])
        tools = {tool.get("name") or tool.get("type") for body in _FakeResponsesHandler.bodies for tool in body.get("tools", [])}
        for blocked in ("exec_command", "web_search", "view_image"):
            self.assertNotIn(blocked, tools)


if __name__ == "__main__":
    unittest.main()
