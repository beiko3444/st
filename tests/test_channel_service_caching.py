from __future__ import annotations

import unittest

from inventory_app.config import AppConfig
from inventory_app.services.channel_services import CoupangChannelService, NaverChannelService


def _test_config() -> AppConfig:
    return AppConfig(
        smartstore_client_id="naver-client",
        smartstore_client_secret="naver-secret",
        smartstore_token_type="SELF",
        smartstore_stats_client_id="naver-stats-client",
        smartstore_stats_client_secret="naver-stats-secret",
        smartstore_stats_token_type="SELF",
        stats_lookback_days=30,
        coupang_vendor_id="vendor",
        coupang_access_key="access",
        coupang_secret_key="secret",
        timeout_seconds=5,
        max_products=100,
    )


class NaverChannelServiceCachingTests(unittest.TestCase):
    def test_reuses_row_instances_when_unchanged(self) -> None:
        service = NaverChannelService(_test_config())
        raw_rows = [
            {
                "product_id": "1001",
                "item_id": None,
                "name": "상품 A",
                "image_url": "https://example.com/a.png",
                "product_url": "https://example.com/a",
                "stock": 10,
                "price": 1000,
            },
            {
                "product_id": "1002",
                "item_id": None,
                "name": "상품 B",
                "image_url": "https://example.com/b.png",
                "product_url": "https://example.com/b",
                "stock": 5,
                "price": 2000,
            },
        ]

        service.smartstore.fetch_products = lambda max_items: [dict(row) for row in raw_rows]
        service._fetch_sales_map = lambda warnings: {"1001": 3, "1002": 1}

        first_rows, _ = service.fetch()
        second_rows, _ = service.fetch()

        first_by_id = {row.product_id: row for row in first_rows}
        second_by_id = {row.product_id: row for row in second_rows}

        self.assertIs(first_by_id["1001"], second_by_id["1001"])
        self.assertIs(first_by_id["1002"], second_by_id["1002"])
        self.assertEqual(first_by_id["1001"].synced_at, second_by_id["1001"].synced_at)
        self.assertEqual(first_by_id["1002"].synced_at, second_by_id["1002"].synced_at)

    def test_updates_only_changed_naver_rows(self) -> None:
        service = NaverChannelService(_test_config())
        datasets = [
            [
                {
                    "product_id": "1001",
                    "item_id": None,
                    "name": "상품 A",
                    "image_url": "https://example.com/a.png",
                    "product_url": "https://example.com/a",
                    "stock": 10,
                    "price": 1000,
                },
                {
                    "product_id": "1002",
                    "item_id": None,
                    "name": "상품 B",
                    "image_url": "https://example.com/b.png",
                    "product_url": "https://example.com/b",
                    "stock": 5,
                    "price": 2000,
                },
            ],
            [
                {
                    "product_id": "1001",
                    "item_id": None,
                    "name": "상품 A",
                    "image_url": "https://example.com/a.png",
                    "product_url": "https://example.com/a",
                    "stock": 99,  # changed
                    "price": 1000,
                },
                {
                    "product_id": "1002",
                    "item_id": None,
                    "name": "상품 B",
                    "image_url": "https://example.com/b.png",
                    "product_url": "https://example.com/b",
                    "stock": 5,
                    "price": 2000,
                },
            ],
        ]
        cursor = {"idx": 0}

        def _fetch_products(max_items: int) -> list[dict[str, object]]:
            index = min(cursor["idx"], len(datasets) - 1)
            cursor["idx"] += 1
            return [dict(row) for row in datasets[index]]

        service.smartstore.fetch_products = _fetch_products
        service._fetch_sales_map = lambda warnings: {"1001": 3, "1002": 1}

        first_rows, _ = service.fetch()
        second_rows, _ = service.fetch()

        first_by_id = {row.product_id: row for row in first_rows}
        second_by_id = {row.product_id: row for row in second_rows}

        self.assertIsNot(first_by_id["1001"], second_by_id["1001"])
        self.assertIs(first_by_id["1002"], second_by_id["1002"])
        self.assertNotEqual(first_by_id["1001"].synced_at, second_by_id["1001"].synced_at)


class CoupangChannelServiceCachingTests(unittest.TestCase):
    def test_updates_only_changed_coupang_rows(self) -> None:
        service = CoupangChannelService(_test_config())
        datasets = [
            [
                {
                    "product_id": "2001",
                    "item_id": "3001",
                    "name": "쿠팡 A",
                    "image_url": "https://example.com/c1.png",
                    "product_url": "https://example.com/c1",
                    "stock": 3,
                    "sales": 7,
                    "price": 1500,
                },
                {
                    "product_id": "2002",
                    "item_id": "3002",
                    "name": "쿠팡 B",
                    "image_url": "https://example.com/c2.png",
                    "product_url": "https://example.com/c2",
                    "stock": 8,
                    "sales": 2,
                    "price": 2500,
                },
            ],
            [
                {
                    "product_id": "2001",
                    "item_id": "3001",
                    "name": "쿠팡 A",
                    "image_url": "https://example.com/c1.png",
                    "product_url": "https://example.com/c1",
                    "stock": 3,
                    "sales": 11,  # changed
                    "price": 1500,
                },
                {
                    "product_id": "2002",
                    "item_id": "3002",
                    "name": "쿠팡 B",
                    "image_url": "https://example.com/c2.png",
                    "product_url": "https://example.com/c2",
                    "stock": 8,
                    "sales": 2,
                    "price": 2500,
                },
            ],
        ]
        cursor = {"idx": 0}

        def _fetch_products(max_products: int) -> list[dict[str, object]]:
            index = min(cursor["idx"], len(datasets) - 1)
            cursor["idx"] += 1
            return [dict(row) for row in datasets[index]]

        service.coupang.fetch_products = _fetch_products

        first_rows, _ = service.fetch()
        second_rows, _ = service.fetch()

        first_by_item = {row.item_id: row for row in first_rows}
        second_by_item = {row.item_id: row for row in second_rows}

        self.assertIsNot(first_by_item["3001"], second_by_item["3001"])
        self.assertIs(first_by_item["3002"], second_by_item["3002"])
        self.assertNotEqual(first_by_item["3001"].synced_at, second_by_item["3001"].synced_at)


if __name__ == "__main__":
    unittest.main()
