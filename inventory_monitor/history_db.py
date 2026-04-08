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
                    stock INTEGER,
                    sales INTEGER,
                    price INTEGER,
                    recorded_at TEXT NOT NULL
                )
                """
            )
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

    def insert_rows(
        self,
        channel: str,
        rows: List[dict],
        recorded_at: datetime | None = None,
    ) -> int:
        if recorded_at is None:
            recorded_at = datetime.now()
        ts = recorded_at.isoformat()

        with self._guard, self._connection() as conn:
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO inventory_history
                        (channel, product_id, item_id, name, stock, sales, price, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        channel,
                        str(row.get("product_id", "")),
                        row.get("item_id"),
                        str(row.get("name", "")),
                        row.get("stock"),
                        row.get("sales"),
                        row.get("price"),
                        ts,
                    ),
                )
            conn.commit()
        return len(rows)

    def get_latest_snapshot(
        self, channel: str
    ) -> List[Tuple[str, str, int | None, str]]:
        with self._guard, self._connection() as conn:
            cursor = conn.execute(
                """
                SELECT product_id, name, stock, recorded_at
                FROM inventory_history
                WHERE channel = ?
                  AND recorded_at = (
                      SELECT MAX(recorded_at) FROM inventory_history WHERE channel = ?
                  )
                ORDER BY name
                """,
                (channel, channel),
            )
            return cursor.fetchall()

    def count_records(self) -> int:
        with self._guard, self._connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM inventory_history")
            return cursor.fetchone()[0]
