from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import List

from inventory_app.models import ChannelProduct


def _default_cache_path() -> Path:
    from_env = os.environ.get("SMARTINVENTORY_CACHE_DB", "").strip()
    if from_env:
        return Path(from_env).expanduser().resolve()
    return (Path.home() / ".smartinventory" / "channel_cache.sqlite3").resolve()


class ChannelProductCache:
    _guard = threading.Lock()

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = (db_path or _default_cache_path()).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _ensure_schema(self) -> None:
        with self._guard, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_products (
                    channel TEXT NOT NULL,
                    row_no INTEGER NOT NULL,
                    serial INTEGER NOT NULL,
                    product_id TEXT NOT NULL,
                    item_id TEXT,
                    name TEXT NOT NULL,
                    image_url TEXT,
                    product_url TEXT,
                    stock INTEGER,
                    sales INTEGER,
                    price INTEGER,
                    synced_at TEXT NOT NULL,
                    PRIMARY KEY (channel, row_no)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_channel_products_channel_serial
                ON channel_products(channel, serial)
                """
            )
            conn.commit()

    def save_rows(self, channel: str, rows: List[ChannelProduct]) -> None:
        channel_key = str(channel).strip().lower()
        with self._guard, self._connect() as conn:
            conn.execute("DELETE FROM channel_products WHERE channel = ?", (channel_key,))
            for row_no, row in enumerate(rows, start=1):
                conn.execute(
                    """
                    INSERT INTO channel_products (
                        channel, row_no, serial, product_id, item_id, name, image_url, product_url,
                        stock, sales, price, synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        channel_key,
                        row_no,
                        int(row.serial),
                        row.product_id,
                        row.item_id,
                        row.name,
                        row.image_url,
                        row.product_url,
                        row.stock,
                        row.sales,
                        row.price,
                        row.synced_at.isoformat(),
                    ),
                )
            conn.commit()

    def load_rows(self, channel: str) -> List[ChannelProduct]:
        channel_key = str(channel).strip().lower()
        with self._guard, self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT
                    serial, product_id, item_id, name, image_url, product_url,
                    stock, sales, price, synced_at
                FROM channel_products
                WHERE channel = ?
                ORDER BY row_no ASC
                """,
                (channel_key,),
            )
            rows = cursor.fetchall()

        parsed: List[ChannelProduct] = []
        for serial, product_id, item_id, name, image_url, product_url, stock, sales, price, synced_at in rows:
            try:
                synced = datetime.fromisoformat(str(synced_at))
            except Exception:  # noqa: BLE001
                synced = datetime.now()
            parsed.append(
                ChannelProduct(
                    serial=int(serial),
                    product_id=str(product_id or ""),
                    item_id=(str(item_id) if item_id else None),
                    name=str(name or ""),
                    image_url=(str(image_url) if image_url else None),
                    product_url=(str(product_url) if product_url else None),
                    stock=(int(stock) if stock is not None else None),
                    sales=(int(sales) if sales is not None else None),
                    price=(int(price) if price is not None else None),
                    synced_at=synced,
                )
            )
        return parsed
