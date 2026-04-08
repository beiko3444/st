from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import List


def _default_db_path() -> Path:
    return (Path.home() / ".smartinventory" / "inventory_history.sqlite3").resolve()


class InventoryHistoryDB:
    _guard = threading.Lock()

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = (db_path or _default_db_path()).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._guard, self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inventory_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    item_id TEXT,
                    name TEXT NOT NULL,
                    image_url TEXT,
                    product_url TEXT,
                    stock INTEGER,
                    sales INTEGER,
                    today_sales INTEGER,
                    price INTEGER,
                    recorded_at TEXT NOT NULL
                )
                """
            )
            # 기존 DB 마이그레이션
            existing = {row[1] for row in conn.execute("PRAGMA table_info(inventory_history)")}
            for col, typedef in [
                ("image_url", "TEXT"),
                ("product_url", "TEXT"),
                ("today_sales", "INTEGER"),
            ]:
                if col not in existing:
                    conn.execute(f"ALTER TABLE inventory_history ADD COLUMN {col} {typedef}")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_history_recorded ON inventory_history(recorded_at)"
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_history_product
                   ON inventory_history(channel, product_id, item_id, recorded_at)"""
            )
            conn.commit()

    def _load_last_data(self, conn: sqlite3.Connection, channel: str) -> dict[tuple, dict]:
        """상품별 최신 데이터 반환. 키: (product_id, item_id)"""
        cursor = conn.execute(
            """
            SELECT product_id, item_id, stock, image_url
            FROM inventory_history h1
            WHERE channel = ?
              AND id = (
                  SELECT MAX(id) FROM inventory_history h2
                  WHERE h2.channel = h1.channel
                    AND h2.product_id = h1.product_id
                    AND (h2.item_id = h1.item_id OR (h2.item_id IS NULL AND h1.item_id IS NULL))
              )
            """,
            (channel,),
        )
        return {
            (row[0], row[1]): {"stock": row[2], "image_url": row[3]}
            for row in cursor.fetchall()
        }

    def insert_rows(
        self,
        channel: str,
        rows: List[dict],
        recorded_at: datetime | None = None,
    ) -> int:
        if recorded_at is None:
            recorded_at = datetime.now()
        ts = recorded_at.isoformat()
        inserted = 0

        with self._guard, self._connection() as conn:
            last_data = self._load_last_data(conn, channel)

            for row in rows:
                product_id = str(row.get("product_id", ""))
                item_id = row.get("item_id")
                key = (product_id, item_id)
                current_stock = row.get("stock")
                current_image = row.get("image_url")

                last = last_data.get(key)
                is_new = last is None
                stock_changed = (not is_new) and (last["stock"] != current_stock)
                image_missing = (not is_new) and (not last["image_url"]) and current_image

                if is_new or stock_changed or image_missing:
                    conn.execute(
                        """
                        INSERT INTO inventory_history
                            (channel, product_id, item_id, name, image_url, product_url,
                             stock, sales, today_sales, price, recorded_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            channel,
                            product_id,
                            item_id,
                            str(row.get("name", "")),
                            current_image,
                            row.get("product_url"),
                            current_stock,
                            row.get("sales"),
                            row.get("today_sales"),
                            row.get("price"),
                            ts,
                        ),
                    )
                    inserted += 1

            conn.commit()
        return inserted

    def get_latest_snapshot(self, channel: str) -> List[dict]:
        with self._guard, self._connection() as conn:
            cursor = conn.execute(
                """
                SELECT product_id, item_id, name, image_url, product_url,
                       stock, sales, today_sales, price, recorded_at
                FROM inventory_history h1
                WHERE channel = ?
                  AND id = (
                      SELECT MAX(id) FROM inventory_history h2
                      WHERE h2.channel = h1.channel
                        AND h2.product_id = h1.product_id
                        AND (h2.item_id = h1.item_id OR (h2.item_id IS NULL AND h1.item_id IS NULL))
                  )
                ORDER BY name
                """,
                (channel,),
            )
            cols = ["product_id", "item_id", "name", "image_url", "product_url",
                    "stock", "sales", "today_sales", "price", "recorded_at"]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def get_last_updated(self, channel: str) -> str | None:
        with self._guard, self._connection() as conn:
            cursor = conn.execute(
                "SELECT MAX(recorded_at) FROM inventory_history WHERE channel = ?",
                (channel,),
            )
            result = cursor.fetchone()
            return result[0] if result else None

    def get_collection_count(self, channel: str) -> int:
        with self._guard, self._connection() as conn:
            cursor = conn.execute(
                "SELECT COUNT(DISTINCT recorded_at) FROM inventory_history WHERE channel = ?",
                (channel,),
            )
            return cursor.fetchone()[0]

    def count_records(self) -> int:
        with self._guard, self._connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM inventory_history")
            return cursor.fetchone()[0]
