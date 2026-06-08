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

from inventory_app.models import ChannelProduct
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


if __name__ == "__main__":
    unittest.main()
