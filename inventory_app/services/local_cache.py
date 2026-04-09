from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from inventory_app.models import ChannelProduct, SharedStockRule


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

    @contextmanager
    def _connection(self) -> sqlite3.Connection:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._guard, self._connection() as conn:
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
                    today_sales INTEGER,
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS product_name_overrides (
                    channel TEXT NOT NULL,
                    product_key TEXT NOT NULL,
                    custom_name TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (channel, product_key)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS product_cost_overrides (
                    channel TEXT NOT NULL,
                    product_key TEXT NOT NULL,
                    unit_cost INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (channel, product_key)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS product_favorites (
                    channel TEXT NOT NULL,
                    product_key TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (channel, product_key)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS shared_stock_rules (
                    channel TEXT NOT NULL,
                    product_key TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    pack_size INTEGER NOT NULL DEFAULT 1,
                    is_master INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (channel, product_key)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_shared_stock_group
                ON shared_stock_rules(group_id)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS shared_stock_pending_ops (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel TEXT NOT NULL,
                    product_key TEXT NOT NULL,
                    op_type TEXT NOT NULL,
                    group_id TEXT,
                    pack_size INTEGER,
                    is_master INTEGER,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_shared_stock_pending_created
                ON shared_stock_pending_ops(created_at, id)
                """
            )

            # Backward compatibility: add column for existing DBs.
            cols = {
                str(row[1]).strip().lower()
                for row in conn.execute("PRAGMA table_info(channel_products)")
            }
            if "today_sales" not in cols:
                conn.execute("ALTER TABLE channel_products ADD COLUMN today_sales INTEGER")
            conn.commit()

    def save_rows(self, channel: str, rows: List[ChannelProduct]) -> None:
        channel_key = str(channel).strip().lower()
        with self._guard, self._connection() as conn:
            conn.execute("DELETE FROM channel_products WHERE channel = ?", (channel_key,))
            for row_no, row in enumerate(rows, start=1):
                conn.execute(
                    """
                    INSERT INTO channel_products (
                        channel, row_no, serial, product_id, item_id, name, image_url, product_url,
                        stock, today_sales, sales, price, synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        row.today_sales,
                        row.sales,
                        row.price,
                        row.synced_at.isoformat(),
                    ),
                )
            conn.commit()

    def load_rows(self, channel: str) -> List[ChannelProduct]:
        channel_key = str(channel).strip().lower()
        with self._guard, self._connection() as conn:
            cursor = conn.execute(
                """
                SELECT
                    serial, product_id, item_id, name, image_url, product_url,
                    stock, today_sales, sales, price, synced_at
                FROM channel_products
                WHERE channel = ?
                ORDER BY row_no ASC
                """,
                (channel_key,),
            )
            rows = cursor.fetchall()

        parsed: List[ChannelProduct] = []
        for serial, product_id, item_id, name, image_url, product_url, stock, today_sales, sales, price, synced_at in rows:
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
                    today_sales=(int(today_sales) if today_sales is not None else None),
                    sales=(int(sales) if sales is not None else None),
                    price=(int(price) if price is not None else None),
                    synced_at=synced,
                )
            )
        return parsed

    def load_name_overrides(self, channel: str) -> Dict[str, str]:
        channel_key = str(channel).strip().lower()
        with self._guard, self._connection() as conn:
            cursor = conn.execute(
                """
                SELECT product_key, custom_name
                FROM product_name_overrides
                WHERE channel = ?
                """,
                (channel_key,),
            )
            rows = cursor.fetchall()

        overrides: Dict[str, str] = {}
        for product_key, custom_name in rows:
            key = str(product_key or "").strip()
            value = str(custom_name or "").strip()
            if key and value:
                overrides[key] = value
        return overrides

    def save_name_override(self, channel: str, product_key: str, custom_name: str | None) -> None:
        channel_key = str(channel).strip().lower()
        item_key = str(product_key).strip()
        value = str(custom_name or "").strip()
        if not item_key:
            return

        with self._guard, self._connection() as conn:
            if value:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO product_name_overrides (
                        channel, product_key, custom_name, updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        channel_key,
                        item_key,
                        value,
                        datetime.now().isoformat(),
                    ),
                )
            else:
                conn.execute(
                    """
                    DELETE FROM product_name_overrides
                    WHERE channel = ? AND product_key = ?
                    """,
                    (channel_key, item_key),
                )
            conn.commit()

    def load_favorite_keys(self, channel: str) -> set[str]:
        channel_key = str(channel).strip().lower()
        with self._guard, self._connection() as conn:
            cursor = conn.execute(
                """
                SELECT product_key
                FROM product_favorites
                WHERE channel = ?
                """,
                (channel_key,),
            )
            rows = cursor.fetchall()

        keys: set[str] = set()
        for (product_key,) in rows:
            key = str(product_key or "").strip()
            if key:
                keys.add(key)
        return keys

    def save_favorite(self, channel: str, product_key: str, is_favorite: bool) -> None:
        channel_key = str(channel).strip().lower()
        item_key = str(product_key).strip()
        if not item_key:
            return

        with self._guard, self._connection() as conn:
            if is_favorite:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO product_favorites (
                        channel, product_key, updated_at
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        channel_key,
                        item_key,
                        datetime.now().isoformat(),
                    ),
                )
            else:
                conn.execute(
                    """
                    DELETE FROM product_favorites
                    WHERE channel = ? AND product_key = ?
                    """,
                    (channel_key, item_key),
                )
            conn.commit()

    def load_cost_overrides(self, channel: str) -> Dict[str, int]:
        channel_key = str(channel).strip().lower()
        with self._guard, self._connection() as conn:
            cursor = conn.execute(
                """
                SELECT product_key, unit_cost
                FROM product_cost_overrides
                WHERE channel = ?
                """,
                (channel_key,),
            )
            rows = cursor.fetchall()

        overrides: Dict[str, int] = {}
        for product_key, unit_cost in rows:
            key = str(product_key or "").strip()
            if not key:
                continue
            try:
                overrides[key] = int(unit_cost)
            except (TypeError, ValueError):
                continue
        return overrides

    def save_cost_override(self, channel: str, product_key: str, unit_cost: int | None) -> None:
        channel_key = str(channel).strip().lower()
        item_key = str(product_key).strip()
        if not item_key:
            return

        with self._guard, self._connection() as conn:
            if unit_cost is None:
                conn.execute(
                    """
                    DELETE FROM product_cost_overrides
                    WHERE channel = ? AND product_key = ?
                    """,
                    (channel_key, item_key),
                )
            else:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO product_cost_overrides (
                        channel, product_key, unit_cost, updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        channel_key,
                        item_key,
                        int(unit_cost),
                        datetime.now().isoformat(),
                    ),
                )
            conn.commit()

    def load_shared_stock_rules(self, channel: str) -> Dict[str, SharedStockRule]:
        channel_key = str(channel).strip().lower()
        with self._guard, self._connection() as conn:
            cursor = conn.execute(
                """
                SELECT product_key, group_id, pack_size, is_master
                FROM shared_stock_rules
                WHERE channel = ?
                """,
                (channel_key,),
            )
            rows = cursor.fetchall()

        rules: Dict[str, SharedStockRule] = {}
        for product_key, group_id, pack_size, is_master in rows:
            key = str(product_key or "").strip()
            group = str(group_id or "").strip()
            if not key or not group:
                continue
            try:
                qty = max(1, int(pack_size))
            except (TypeError, ValueError):
                qty = 1
            rules[key] = SharedStockRule(
                group_id=group,
                pack_size=qty,
                is_master=bool(int(is_master or 0)),
            )
        return rules

    def replace_shared_stock_rules(self, channel: str, rules: Dict[str, SharedStockRule]) -> None:
        channel_key = str(channel).strip().lower()
        with self._guard, self._connection() as conn:
            conn.execute("DELETE FROM shared_stock_rules WHERE channel = ?", (channel_key,))
            for product_key, rule in rules.items():
                item_key = str(product_key or "").strip()
                if not item_key:
                    continue
                group_key = str(rule.group_id or "").strip()
                if not group_key:
                    continue
                qty = max(1, int(rule.pack_size))
                conn.execute(
                    """
                    INSERT INTO shared_stock_rules (
                        channel, product_key, group_id, pack_size, is_master, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        channel_key,
                        item_key,
                        group_key,
                        qty,
                        1 if rule.is_master else 0,
                        datetime.now().isoformat(),
                    ),
                )
            conn.commit()

    def save_shared_stock_rule(
        self,
        channel: str,
        product_key: str,
        group_id: str,
        pack_size: int,
        is_master: bool,
    ) -> None:
        channel_key = str(channel).strip().lower()
        item_key = str(product_key).strip()
        group_key = str(group_id).strip()
        if not item_key:
            return
        if not group_key:
            self.delete_shared_stock_rule(channel, item_key)
            return

        qty = max(1, int(pack_size))

        with self._guard, self._connection() as conn:
            if is_master:
                conn.execute(
                    """
                    UPDATE shared_stock_rules
                    SET is_master = 0, updated_at = ?
                    WHERE group_id = ?
                    """,
                    (datetime.now().isoformat(), group_key),
                )
            conn.execute(
                """
                INSERT OR REPLACE INTO shared_stock_rules (
                    channel, product_key, group_id, pack_size, is_master, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    channel_key,
                    item_key,
                    group_key,
                    qty,
                    1 if is_master else 0,
                    datetime.now().isoformat(),
                ),
            )
            conn.commit()

    def delete_shared_stock_rule(self, channel: str, product_key: str) -> None:
        channel_key = str(channel).strip().lower()
        item_key = str(product_key).strip()
        if not item_key:
            return

        with self._guard, self._connection() as conn:
            conn.execute(
                """
                DELETE FROM shared_stock_rules
                WHERE channel = ? AND product_key = ?
                """,
                (channel_key, item_key),
            )
            conn.commit()

    def enqueue_shared_stock_pending_op(
        self,
        channel: str,
        product_key: str,
        op_type: str,
        group_id: str | None = None,
        pack_size: int | None = None,
        is_master: bool | None = None,
    ) -> None:
        channel_key = str(channel).strip().lower()
        item_key = str(product_key).strip()
        op = str(op_type).strip().lower()
        if not item_key or op not in {"upsert", "delete"}:
            return

        with self._guard, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO shared_stock_pending_ops (
                    channel, product_key, op_type, group_id, pack_size, is_master, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel_key,
                    item_key,
                    op,
                    (str(group_id).strip() if group_id is not None else None),
                    (int(pack_size) if pack_size is not None else None),
                    (None if is_master is None else (1 if is_master else 0)),
                    datetime.now().isoformat(),
                ),
            )
            conn.commit()

    def load_shared_stock_pending_ops(
        self,
        channel: str,
        limit: int = 200,
    ) -> list[dict]:
        channel_key = str(channel).strip().lower()
        with self._guard, self._connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, product_key, op_type, group_id, pack_size, is_master, created_at
                FROM shared_stock_pending_ops
                WHERE channel = ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (channel_key, max(1, int(limit))),
            )
            rows = cursor.fetchall()

        pending: list[dict] = []
        for row_id, product_key, op_type, group_id, pack_size, is_master, created_at in rows:
            pending.append(
                {
                    "id": int(row_id),
                    "product_key": str(product_key or "").strip(),
                    "op_type": str(op_type or "").strip().lower(),
                    "group_id": (str(group_id).strip() if group_id is not None else None),
                    "pack_size": (int(pack_size) if pack_size is not None else None),
                    "is_master": (bool(int(is_master)) if is_master is not None else None),
                    "created_at": str(created_at or ""),
                }
            )
        return pending

    def delete_shared_stock_pending_op(self, op_id: int) -> None:
        with self._guard, self._connection() as conn:
            conn.execute(
                """
                DELETE FROM shared_stock_pending_ops
                WHERE id = ?
                """,
                (int(op_id),),
            )
            conn.commit()
