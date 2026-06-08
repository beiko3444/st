from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

os.environ["VERCEL"] = "1"

import httpx
from fastapi.testclient import TestClient

from inventory_app.models import ChannelProduct, PurchaseOrder, PurchaseRecord
from inventory_app.services.local_cache import ChannelProductCache
from inventory_web.app import create_app
from inventory_web.jobs import jobs


class InventoryWebTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["VERCEL"] = "1"
        os.environ.pop("SMARTINVENTORY_MONITOR_URL", None)
        os.environ.pop("MONITOR_URL", None)
        os.environ.pop("SMARTINVENTORY_CACHE_DB", None)
        os.environ.pop("CRON_SECRET", None)
        os.environ.pop("DISCORD_INVENTORY_WEBHOOK_URL", None)
        os.environ.pop("DISCORD_WEBHOOK_URL", None)
        os.environ.pop("SMARTINVENTORY_WEB_PASSWORD", None)
        os.environ.pop("SMARTINVENTORY_SESSION_SECRET", None)

    def test_root_renders_all_work_tabs(self) -> None:
        client = TestClient(create_app())
        response = client.get("/")

        self.assertEqual(response.status_code, 200)
        body = response.text
        for label in (
            "상품관리",
            "네이버",
            "쿠팡",
            "판매일보",
            "매출비교",
            "키워드매출",
            "구매내역",
            "카드사용내역",
            "파스토",
        ):
            self.assertIn(label, body)

    def test_health_reports_vercel_runtime(self) -> None:
        client = TestClient(create_app())
        payload = client.get("/api/health").json()

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["data"]["config"]["vercel"])

    def test_job_status_endpoint_returns_snapshot(self) -> None:
        job = jobs.create(
            "unit-test",
            lambda log, progress: (
                log("running"),
                progress(80),
                {"done": True},
            )[-1],
        )
        client = TestClient(create_app())

        for _ in range(30):
            payload = client.get(f"/api/jobs/{job.id}").json()
            if payload["data"]["status"] == "succeeded":
                break
            time.sleep(0.05)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["status"], "succeeded")
        self.assertEqual(payload["data"]["result"], {"done": True})

    def test_channel_endpoint_uses_monitor_backend_when_configured(self) -> None:
        os.environ["SMARTINVENTORY_MONITOR_URL"] = "https://backend.example"

        def fake_request(method: str, url: str, **_kwargs):
            self.assertEqual(method, "GET")
            self.assertEqual(url, "https://backend.example/inventory")
            return httpx.Response(
                200,
                json={
                    "naver": [
                        {
                            "product_id": "1001",
                            "item_id": None,
                            "name": "Alpha Product",
                            "image_url": "https://example.com/a.png",
                            "product_url": "https://example.com/a",
                            "stock": 5,
                            "today_sales": 1,
                            "sales": 12,
                            "price": 9900,
                            "recorded_at": "2026-06-06T00:00:00",
                        }
                    ],
                    "coupang": [],
                },
            )

        client = TestClient(create_app())
        with patch("inventory_web.app.httpx.request", side_effect=fake_request):
            payload = client.get("/api/channels/naver?q=Alpha").json()

        self.assertTrue(payload["ok"])
        rows = payload["data"]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["productId"], "1001")
        self.assertEqual(rows[0]["productKey"], "1001|")

    def test_vercel_blocks_fixed_ip_api_without_monitor_backend(self) -> None:
        client = TestClient(create_app())

        payload = client.get("/api/revenue").json()

        self.assertFalse(payload["ok"])
        self.assertIn("SMARTINVENTORY_MONITOR_URL", payload["error"])

    def test_revenue_endpoint_uses_monitor_backend_when_configured(self) -> None:
        os.environ["SMARTINVENTORY_MONITOR_URL"] = "https://backend.example"

        def fake_request(method: str, url: str, **kwargs):
            self.assertEqual(method, "GET")
            self.assertEqual(url, "https://backend.example/revenue")
            self.assertEqual(kwargs["params"], {"period_days": 30})
            return httpx.Response(
                200,
                json={
                    "snapshot": {
                        "period_days": 30,
                        "products": [{"product_id": "A", "net": 1000}],
                    },
                    "warnings": [],
                    "cached": True,
                },
            )

        client = TestClient(create_app())
        with patch("inventory_web.app.httpx.request", side_effect=fake_request):
            payload = client.get("/api/revenue?period_days=30").json()

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["data"]["cached"])
        self.assertEqual(payload["data"]["snapshot"]["products"][0]["product_id"], "A")

    def test_discord_inventory_report_dry_run_uses_master_rows_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["SMARTINVENTORY_CACHE_DB"] = str(Path(tmpdir) / "cache.sqlite3")
            cache = ChannelProductCache()
            master = cache.create_master("관리상품", unit_cost=1000)
            now = datetime(2026, 6, 6, 0, 0, 0)
            cache.save_rows(
                "naver",
                [
                    ChannelProduct(
                        serial=1,
                        product_id="linked",
                        item_id=None,
                        name="연결 네이버 상품",
                        image_url=None,
                        product_url="https://example.com/linked",
                        stock=5,
                        today_sales=2,
                        sales=10,
                        price=12000,
                        synced_at=now,
                    ),
                    ChannelProduct(
                        serial=2,
                        product_id="unlinked",
                        item_id=None,
                        name="미연결 채널 상품",
                        image_url=None,
                        product_url="https://example.com/unlinked",
                        stock=99,
                        today_sales=9,
                        sales=90,
                        price=9900,
                        synced_at=now,
                    ),
                ],
            )
            cache.link_channel_product("naver", "id:linked|item:", master.id)

            client = TestClient(create_app())
            payload = client.get("/api/reports/inventory/discord?dry_run=1").json()

        self.assertTrue(payload["ok"])
        messages = "\n".join(payload["data"]["messages"])
        self.assertEqual(payload["data"]["rows"], 1)
        self.assertIn("관리상품", messages)
        self.assertIn("네이버 5", messages)
        self.assertNotIn("미연결 채널 상품", messages)

    def test_discord_inventory_report_checks_cron_secret(self) -> None:
        os.environ["CRON_SECRET"] = "unit-secret"
        client = TestClient(create_app())

        unauthorized = client.get("/api/reports/inventory/discord?dry_run=1").json()
        authorized = client.get(
            "/api/reports/inventory/discord?dry_run=1",
            headers={"Authorization": "Bearer unit-secret"},
        ).json()

        self.assertFalse(unauthorized["ok"])
        self.assertEqual(unauthorized["error"], "unauthorized")
        self.assertTrue(authorized["ok"])

    def test_web_password_protects_app_and_login_sets_session(self) -> None:
        os.environ["SMARTINVENTORY_WEB_PASSWORD"] = "let-me-in"
        os.environ["SMARTINVENTORY_SESSION_SECRET"] = "unit-session-secret"
        client = TestClient(create_app(), follow_redirects=False)

        root = client.get("/")
        api_response = client.get("/api/health")
        login = client.post("/login", data={"password": "let-me-in"})

        self.assertEqual(root.status_code, 303)
        self.assertEqual(root.headers["location"], "/login")
        self.assertEqual(api_response.status_code, 401)
        self.assertEqual(login.status_code, 303)
        self.assertIn("smartinventory_session", login.headers.get("set-cookie", ""))
        self.assertEqual(client.get("/").status_code, 200)

    def test_discord_report_allows_cron_secret_without_web_session(self) -> None:
        os.environ["SMARTINVENTORY_WEB_PASSWORD"] = "let-me-in"
        os.environ["SMARTINVENTORY_SESSION_SECRET"] = "unit-session-secret"
        os.environ["CRON_SECRET"] = "cron-secret"
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["SMARTINVENTORY_CACHE_DB"] = str(Path(tmpdir) / "cache.sqlite3")
            cache = ChannelProductCache()
            master = cache.create_master("관리상품", unit_cost=1000)
            cache.save_rows(
                "naver",
                [
                    ChannelProduct(
                        serial=1,
                        product_id="linked",
                        item_id=None,
                        name="연결 네이버 상품",
                        image_url=None,
                        product_url=None,
                        stock=3,
                        today_sales=0,
                        sales=0,
                        price=1000,
                        synced_at=datetime(2026, 6, 6),
                    )
                ],
            )
            cache.link_channel_product("naver", "id:linked|item:", master.id)

            client = TestClient(create_app())
            payload = client.get(
                "/api/reports/inventory/discord?dry_run=1",
                headers={"Authorization": "Bearer cron-secret"},
            ).json()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["rows"], 1)

    def test_sync_all_uses_monitor_inventory_sync_job(self) -> None:
        os.environ["SMARTINVENTORY_MONITOR_URL"] = "https://backend.example"
        calls: list[tuple[str, str, dict]] = []

        def fake_request(method: str, url: str, **kwargs):
            calls.append((method, url, kwargs))
            return httpx.Response(200, json={"accepted": True, "source": "monitor"})

        client = TestClient(create_app())
        with patch("inventory_web.app.httpx.request", side_effect=fake_request):
            job = client.post("/api/sync/all").json()["data"]
            for _ in range(30):
                payload = client.get(f"/api/jobs/{job['id']}").json()
                if payload["data"]["status"] == "succeeded":
                    break
                time.sleep(0.05)

        self.assertEqual(payload["data"]["status"], "succeeded")
        self.assertEqual(calls[0][0], "POST")
        self.assertEqual(calls[0][1], "https://backend.example/sync/inventory")
        self.assertEqual(calls[0][2]["params"], {"wait": "1"})

    def test_sales_totals_endpoint_proxies_monitor(self) -> None:
        os.environ["SMARTINVENTORY_MONITOR_URL"] = "https://backend.example"

        def fake_request(method: str, url: str, **kwargs):
            self.assertEqual(method, "GET")
            self.assertEqual(url, "https://backend.example/sales/totals")
            self.assertEqual(kwargs["params"], {"days": 30})
            return httpx.Response(
                200,
                json={"totals": [{"channel": "naver", "product_key": "id:100|item:", "quantity": 7}]},
            )

        client = TestClient(create_app())
        with patch("inventory_web.app.httpx.request", side_effect=fake_request):
            payload = client.get("/api/sales/totals?days=30").json()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["totals"][0]["quantity"], 7)

    def test_purchase_crawler_status_endpoint_reports_runtime(self) -> None:
        client = TestClient(create_app())

        payload = client.get("/api/purchases/crawler/status").json()

        self.assertTrue(payload["ok"])
        self.assertIn("available", payload["data"])
        self.assertIn("playwrightInstalled", payload["data"])

    def test_purchase_crawl_job_saves_records_and_orders(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["SMARTINVENTORY_CACHE_DB"] = str(Path(tmpdir) / "cache.sqlite3")
            now = datetime(2026, 6, 6, 12, 0, 0)

            def fake_crawl_channel(*_args, **kwargs):
                channel = kwargs.get("channel") or (_args[0] if _args else "coupang")
                return type(
                    "FakeCrawlResult",
                    (),
                    {
                        "channel": channel,
                        "records": [
                            PurchaseRecord(
                                id=None,
                                channel=channel,
                                order_date="2026-06-06",
                                order_no="1000000001",
                                title="테스트 상품",
                                amount=12000,
                                payment_method="카드",
                                source_url="https://example.com/order",
                                raw_text="raw",
                                imported_at=now,
                            )
                        ],
                        "orders": [
                            PurchaseOrder(
                                channel=channel,
                                order_no="1000000001",
                                order_date="2026-06-06",
                                payment_total=12000,
                                item_count=1,
                                status="배송완료",
                                payment_method="카드",
                                source_url="https://example.com/order",
                                raw_text="raw",
                                imported_at=now,
                            )
                        ],
                        "error": None,
                    },
                )()

            client = TestClient(create_app())
            with patch("inventory_web.app.crawl_channel", side_effect=fake_crawl_channel):
                job = client.post("/api/purchases/crawl", json={"channel": "coupang", "max_pages": 1}).json()["data"]
                for _ in range(30):
                    payload = client.get(f"/api/jobs/{job['id']}").json()
                    if payload["data"]["status"] == "succeeded":
                        break
                    time.sleep(0.05)

            records = client.get("/api/purchases/records?channel=coupang").json()["data"]["records"]
            orders = client.get("/api/purchases/orders?channel=coupang").json()["data"]["orders"]

        self.assertEqual(payload["data"]["status"], "succeeded")
        self.assertEqual(records[0]["title"], "테스트 상품")
        self.assertEqual(orders[0]["order_no"], "1000000001")


if __name__ == "__main__":
    unittest.main()
