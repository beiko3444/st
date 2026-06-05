from __future__ import annotations

import os
import time
import unittest
from unittest.mock import patch

os.environ["VERCEL"] = "1"

import httpx
from fastapi.testclient import TestClient

from inventory_web.app import create_app
from inventory_web.jobs import jobs


class InventoryWebTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["VERCEL"] = "1"
        os.environ.pop("SMARTINVENTORY_MONITOR_URL", None)
        os.environ.pop("MONITOR_URL", None)

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


if __name__ == "__main__":
    unittest.main()
