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
            # 리뷰 이력 테이블
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS review_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    name TEXT,
                    image_url TEXT,
                    review_count INTEGER,
                    review_score REAL,
                    recorded_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_review_product
                   ON review_history(channel, product_id, recorded_at)"""
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
            conn.commit()

    def _load_last_data(self, conn: sqlite3.Connection, channel: str) -> dict[tuple, dict]:
        """상품별 최신 데이터 반환. 키: (product_id, item_id)"""
        cursor = conn.execute(
            """
            SELECT product_id, item_id, stock, image_url, product_url
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
            (row[0], row[1]): {"stock": row[2], "image_url": row[3], "product_url": row[4]}
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
                current_product_url = row.get("product_url")

                last = last_data.get(key)
                is_new = last is None
                stock_changed = (not is_new) and (last["stock"] != current_stock)
                image_missing = (not is_new) and (not last["image_url"]) and current_image
                product_url_missing = (not is_new) and (not last["product_url"]) and current_product_url

                if is_new or stock_changed or image_missing or product_url_missing:
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

    # ── 판매일보 관련 ──

    def get_sales_for_date(self, date_str: str) -> List[dict]:
        """특정 날짜의 판매 내역 (재고 감소분) 반환.
        date_str: 'YYYY-MM-DD'
        """
        with self._guard, self._connection() as conn:
            cursor = conn.execute(
                """
                WITH pairs AS (
                    SELECT
                        a.channel,
                        a.product_id,
                        a.item_id,
                        a.name,
                        a.image_url,
                        a.product_url,
                        a.price,
                        a.recorded_at,
                        b.recorded_at AS prev_recorded_at,
                        b.stock AS stock_before,
                        a.stock AS stock_after,
                        b.today_sales AS today_sales_before,
                        a.today_sales AS today_sales_after,
                        b.sales AS sales_before,
                        a.sales AS sales_after,
                        CASE
                            WHEN a.channel = 'naver'
                                 AND a.today_sales IS NOT NULL
                            THEN CASE
                                WHEN b.today_sales IS NOT NULL
                                     AND DATE(b.recorded_at) = DATE(a.recorded_at)
                                THEN MAX(0, a.today_sales - b.today_sales)
                                ELSE MAX(0, a.today_sales)
                            END
                            WHEN a.channel = 'coupang'
                                 AND a.sales IS NOT NULL
                                 AND b.sales IS NOT NULL
                            THEN MAX(0, a.sales - b.sales)
                            WHEN a.stock IS NOT NULL
                                 AND b.stock IS NOT NULL
                            THEN (b.stock - a.stock)
                            ELSE 0
                        END AS qty_sold
                    FROM inventory_history a
                    JOIN inventory_history b
                        ON a.channel = b.channel
                        AND a.product_id = b.product_id
                        AND (a.item_id = b.item_id OR (a.item_id IS NULL AND b.item_id IS NULL))
                        AND b.id = (
                            SELECT MAX(id) FROM inventory_history c
                            WHERE c.channel = a.channel
                              AND c.product_id = a.product_id
                              AND (c.item_id = a.item_id OR (c.item_id IS NULL AND a.item_id IS NULL))
                              AND c.id < a.id
                        )
                    WHERE DATE(a.recorded_at) = ?
                )
                SELECT
                    channel,
                    product_id,
                    item_id,
                    name,
                    image_url,
                    product_url,
                    price,
                    recorded_at,
                    stock_before,
                    stock_after,
                    qty_sold
                FROM pairs
                WHERE qty_sold > 0
                ORDER BY recorded_at DESC
                """,
                (date_str,),
            )
            cols = [
                "channel", "product_id", "item_id", "name", "image_url",
                "product_url", "price", "recorded_at",
                "stock_before", "stock_after", "qty_sold",
            ]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def get_sales_dates(self) -> dict[str, int]:
        """판매(재고 감소)가 있었던 날짜별 건수 반환. {'2026-04-08': 5, ...}"""
        with self._guard, self._connection() as conn:
            cursor = conn.execute(
                """
                WITH pairs AS (
                    SELECT
                        DATE(a.recorded_at) AS sale_date,
                        CASE
                            WHEN a.channel = 'naver'
                                 AND a.today_sales IS NOT NULL
                            THEN CASE
                                WHEN b.today_sales IS NOT NULL
                                     AND DATE(b.recorded_at) = DATE(a.recorded_at)
                                THEN MAX(0, a.today_sales - b.today_sales)
                                ELSE MAX(0, a.today_sales)
                            END
                            WHEN a.channel = 'coupang'
                                 AND a.sales IS NOT NULL
                                 AND b.sales IS NOT NULL
                            THEN MAX(0, a.sales - b.sales)
                            WHEN a.stock IS NOT NULL
                                 AND b.stock IS NOT NULL
                            THEN (b.stock - a.stock)
                            ELSE 0
                        END AS qty_sold
                    FROM inventory_history a
                    JOIN inventory_history b
                        ON a.channel = b.channel
                        AND a.product_id = b.product_id
                        AND (a.item_id = b.item_id OR (a.item_id IS NULL AND b.item_id IS NULL))
                        AND b.id = (
                            SELECT MAX(id) FROM inventory_history c
                            WHERE c.channel = a.channel
                              AND c.product_id = a.product_id
                              AND (c.item_id = a.item_id OR (c.item_id IS NULL AND a.item_id IS NULL))
                              AND c.id < a.id
                        )
                )
                SELECT sale_date, COUNT(*) AS cnt
                FROM pairs
                WHERE qty_sold > 0
                GROUP BY sale_date
                ORDER BY sale_date DESC
                """
            )
            return {row[0]: row[1] for row in cursor.fetchall()}

    def get_daily_summary(self, date_str: str) -> dict:
        """특정 날짜의 판매 요약 (총 건수, 추정매출)."""
        rows = self.get_sales_for_date(date_str)
        total_qty = sum(r["qty_sold"] for r in rows)
        total_revenue = sum(
            r["qty_sold"] * r["price"]
            for r in rows
            if r["price"] is not None
        )
        channels = set(r["channel"] for r in rows)
        return {
            "date": date_str,
            "total_events": len(rows),
            "total_qty": total_qty,
            "total_revenue": total_revenue,
            "channels": sorted(channels),
        }

    # ── 리뷰 이력 관련 ──

    def insert_reviews(self, channel: str, rows: List[dict], recorded_at: datetime | None = None) -> int:
        if recorded_at is None:
            recorded_at = datetime.now()
        ts = recorded_at.isoformat()
        inserted = 0
        with self._guard, self._connection() as conn:
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO review_history
                        (channel, product_id, name, image_url, review_count, review_score, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        channel,
                        str(row.get("product_id", "")),
                        row.get("name"),
                        row.get("image_url"),
                        row.get("review_count"),
                        row.get("review_score"),
                        ts,
                    ),
                )
                inserted += 1
            conn.commit()
        return inserted

    def get_latest_reviews(self, channel: str) -> List[dict]:
        with self._guard, self._connection() as conn:
            cursor = conn.execute(
                """
                SELECT product_id, name, image_url, review_count, review_score, recorded_at
                FROM review_history r1
                WHERE channel = ?
                  AND id = (
                      SELECT MAX(id) FROM review_history r2
                      WHERE r2.channel = r1.channel
                        AND r2.product_id = r1.product_id
                  )
                ORDER BY review_count DESC
                """,
                (channel,),
            )
            cols = ["product_id", "name", "image_url", "review_count", "review_score", "recorded_at"]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]

    # ── 공유재고 규칙 관련 ──

    def get_shared_stock_rules(self, channel: str | None = None) -> List[dict]:
        with self._guard, self._connection() as conn:
            if channel:
                cursor = conn.execute(
                    """
                    SELECT channel, product_key, group_id, pack_size, is_master, updated_at
                    FROM shared_stock_rules
                    WHERE channel = ?
                    ORDER BY updated_at DESC, product_key ASC
                    """,
                    (str(channel).strip().lower(),),
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT channel, product_key, group_id, pack_size, is_master, updated_at
                    FROM shared_stock_rules
                    ORDER BY updated_at DESC, product_key ASC
                    """
                )
            rows = cursor.fetchall()

        cols = ["channel", "product_key", "group_id", "pack_size", "is_master", "updated_at"]
        return [dict(zip(cols, row)) for row in rows]

    def upsert_shared_stock_rule(
        self,
        channel: str,
        product_key: str,
        group_id: str,
        pack_size: int,
        is_master: bool,
    ) -> None:
        channel_key = str(channel or "").strip().lower()
        item_key = str(product_key or "").strip()
        group_key = str(group_id or "").strip()
        if not channel_key or not item_key or not group_key:
            raise ValueError("channel/product_key/group_id are required")
        qty = max(1, int(pack_size))
        now = datetime.now().isoformat()

        with self._guard, self._connection() as conn:
            if is_master:
                conn.execute(
                    """
                    UPDATE shared_stock_rules
                    SET is_master = 0, updated_at = ?
                    WHERE group_id = ?
                    """,
                    (now, group_key),
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
                    now,
                ),
            )
            conn.commit()

    def delete_shared_stock_rule(self, channel: str, product_key: str) -> None:
        channel_key = str(channel or "").strip().lower()
        item_key = str(product_key or "").strip()
        if not channel_key or not item_key:
            raise ValueError("channel and product_key are required")

        with self._guard, self._connection() as conn:
            conn.execute(
                """
                DELETE FROM shared_stock_rules
                WHERE channel = ? AND product_key = ?
                """,
                (channel_key, item_key),
            )
            conn.commit()
