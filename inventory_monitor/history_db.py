from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Tuple


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
                    price INTEGER,
                    recorded_at TEXT NOT NULL
                )
                """
            )
            # 기존 DB 마이그레이션 (image_url, product_url 컬럼 추가)
            existing = {row[1] for row in conn.execute("PRAGMA table_info(inventory_history)")}
            if "image_url" not in existing:
                conn.execute("ALTER TABLE inventory_history ADD COLUMN image_url TEXT")
            if "product_url" not in existing:
                conn.execute("ALTER TABLE inventory_history ADD COLUMN product_url TEXT")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_history_recorded
                ON inventory_history(recorded_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_history_product
                ON inventory_history(channel, product_id, recorded_at)
                """
            )
            conn.commit()

    def _load_last_stocks(self, conn: sqlite3.Connection, channel: str) -> dict[str, int | None]:
        cursor = conn.execute(
            """
            SELECT product_id, stock
            FROM inventory_history h1
            WHERE channel = ?
              AND id = (
                  SELECT MAX(id) FROM inventory_history h2
                  WHERE h2.channel = h1.channel AND h2.product_id = h1.product_id
              )
            """,
            (channel,),
        )
        return {row[0]: row[1] for row in cursor.fetchall()}

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
            last_stocks = self._load_last_stocks(conn, channel)

            for row in rows:
                product_id = str(row.get("product_id", ""))
                current_stock = row.get("stock")
                last_stock = last_stocks.get(product_id, "__NEW__")

                if last_stock == "__NEW__" or last_stock != current_stock:
                    conn.execute(
                        """
                        INSERT INTO inventory_history
                            (channel, product_id, item_id, name, image_url, product_url,
                             stock, sales, price, recorded_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            channel,
                            product_id,
                            row.get("item_id"),
                            str(row.get("name", "")),
                            row.get("image_url"),
                            row.get("product_url"),
                            current_stock,
                            row.get("sales"),
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
                       stock, sales, price, recorded_at
                FROM inventory_history h1
                WHERE channel = ?
                  AND id = (
                      SELECT MAX(id) FROM inventory_history h2
                      WHERE h2.channel = h1.channel AND h2.product_id = h1.product_id
                  )
                ORDER BY name
                """,
                (channel,),
            )
            cols = ["product_id", "item_id", "name", "image_url", "product_url",
                    "stock", "sales", "price", "recorded_at"]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def count_records(self) -> int:
        with self._guard, self._connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM inventory_history")
            return cursor.fetchone()[0]
