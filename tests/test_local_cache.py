from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from inventory_app.models import ChannelProduct
from inventory_app.services.local_cache import ChannelProductCache


class LocalCacheTests(unittest.TestCase):
    def test_save_and_load_channel_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = ChannelProductCache(Path(tmpdir) / "cache.sqlite3")
            now = datetime.now().replace(microsecond=0)
            rows = [
                ChannelProduct(
                    serial=1,
                    product_id="1001",
                    item_id=None,
                    name="네이버 A",
                    image_url="https://example.com/a.png",
                    product_url="https://example.com/a",
                    stock=11,
                    sales=2,
                    price=1000,
                    synced_at=now,
                ),
                ChannelProduct(
                    serial=2,
                    product_id="1002",
                    item_id="2002",
                    name="네이버 B",
                    image_url=None,
                    product_url="https://example.com/b",
                    stock=5,
                    sales=0,
                    price=2000,
                    synced_at=now,
                ),
            ]
            cache.save_rows("naver", rows)
            loaded = cache.load_rows("naver")

        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0].product_id, "1001")
        self.assertEqual(loaded[1].item_id, "2002")
        self.assertEqual(loaded[0].synced_at, now)
        self.assertEqual(loaded[1].price, 2000)

    def test_save_and_load_cost_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = ChannelProductCache(Path(tmpdir) / "cache.sqlite3")
            cache.save_cost_override("naver", "id:1001|item:", 3500)
            cache.save_cost_override("naver", "id:1002|item:2002", 4200)

            loaded = cache.load_cost_overrides("naver")
            self.assertEqual(loaded["id:1001|item:"], 3500)
            self.assertEqual(loaded["id:1002|item:2002"], 4200)

            cache.save_cost_override("naver", "id:1001|item:", None)
            after_delete = cache.load_cost_overrides("naver")
            self.assertNotIn("id:1001|item:", after_delete)
            self.assertEqual(after_delete["id:1002|item:2002"], 4200)

    def test_save_and_load_favorite_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = ChannelProductCache(Path(tmpdir) / "cache.sqlite3")

            cache.save_favorite("naver", "id:1001|item:", True)
            cache.save_favorite("naver", "id:1002|item:2002", True)
            self.assertEqual(
                cache.load_favorite_keys("naver"),
                {"id:1001|item:", "id:1002|item:2002"},
            )

            cache.save_favorite("naver", "id:1001|item:", False)
            self.assertEqual(cache.load_favorite_keys("naver"), {"id:1002|item:2002"})


if __name__ == "__main__":
    unittest.main()
