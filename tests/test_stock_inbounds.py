from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from inventory_monitor.history_db import InventoryHistoryDB


class StockInboundTests(unittest.TestCase):
    def test_legacy_stock_inbounds_schema_is_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "inventory_history.sqlite3"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE master_products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    unit_cost INTEGER,
                    memo TEXT,
                    representative_channel TEXT,
                    representative_product_key TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE stock_inbounds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_date TEXT NOT NULL,
                    master_id INTEGER NOT NULL,
                    channel TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(receipt_date, master_id, channel)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO master_products (
                    id, name, unit_cost, memo,
                    representative_channel, representative_product_key,
                    created_at, updated_at
                ) VALUES (1, '테스트', NULL, NULL, NULL, NULL, '2026-05-11T00:00:00', '2026-05-11T00:00:00')
                """
            )
            conn.execute(
                """
                INSERT INTO stock_inbounds (
                    receipt_date, master_id, channel, quantity, created_at, updated_at
                ) VALUES ('2026-05-11', 1, 'naver', 7, '2026-05-11T10:00:00', '2026-05-11T10:00:00')
                """
            )
            conn.commit()
            conn.close()

            db = InventoryHistoryDB(db_path)
            rows = db.list_stock_inbounds()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["input_qty"], 7)
        self.assertEqual(rows[0]["remaining_qty"], 7)
        self.assertIsNone(rows[0]["last_consumed_at"])

    def test_first_reconcile_only_stores_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = InventoryHistoryDB(Path(tmpdir) / "inventory_history.sqlite3")
            master = db.create_master("테스트상품")
            db.add_stock_inbound("2026-05-11", master["id"], "naver", 5)

            result = db.reconcile_stock_inbounds(
                [{"master_id": master["id"], "channel": "naver", "current_stock": 12}]
            )
            summaries = db.list_stock_inbound_summaries(master_id=master["id"], channel="naver")

        self.assertEqual(result["baselines"], 1)
        self.assertEqual(result["consumed"], [])
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["pending_qty"], 5)
        self.assertIsNone(summaries[0]["last_consumed_at"])

    def test_reconcile_consumes_fifo_per_channel_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = InventoryHistoryDB(Path(tmpdir) / "inventory_history.sqlite3")
            master = db.create_master("테스트상품")
            first = db.add_stock_inbound("2026-05-10", master["id"], "naver", 5)
            second = db.add_stock_inbound("2026-05-11", master["id"], "naver", 3)
            coupang = db.add_stock_inbound("2026-05-11", master["id"], "coupang", 4)

            db.reconcile_stock_inbounds(
                [
                    {"master_id": master["id"], "channel": "naver", "current_stock": 10},
                    {"master_id": master["id"], "channel": "coupang", "current_stock": 20},
                ]
            )
            db.reconcile_stock_inbounds(
                [
                    {"master_id": master["id"], "channel": "naver", "current_stock": 17},
                    {"master_id": master["id"], "channel": "coupang", "current_stock": 22},
                ]
            )
            rows = db.list_stock_inbounds(master_id=master["id"])
            row_by_id = {row["id"]: row for row in rows}
            summaries = {
                row["channel"]: row
                for row in db.list_stock_inbound_summaries(master_id=master["id"])
            }

        self.assertEqual(row_by_id[first["id"]]["remaining_qty"], 0)
        self.assertEqual(row_by_id[second["id"]]["remaining_qty"], 1)
        self.assertEqual(row_by_id[coupang["id"]]["remaining_qty"], 2)
        self.assertIsNotNone(row_by_id[first["id"]]["last_consumed_at"])
        self.assertIsNotNone(row_by_id[second["id"]]["last_consumed_at"])
        self.assertIsNotNone(row_by_id[coupang["id"]]["last_consumed_at"])
        self.assertEqual(summaries["naver"]["pending_qty"], 1)
        self.assertEqual(summaries["coupang"]["pending_qty"], 2)


if __name__ == "__main__":
    unittest.main()
