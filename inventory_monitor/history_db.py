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
        conn.execute("PRAGMA foreign_keys=ON;")
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS master_products (
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
                CREATE TABLE IF NOT EXISTS channel_master_links (
                    channel TEXT NOT NULL,
                    product_key TEXT NOT NULL,
                    master_id INTEGER NOT NULL,
                    multiplier INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (channel, product_key),
                    FOREIGN KEY (master_id) REFERENCES master_products(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_channel_master_links_master
                ON channel_master_links(master_id)
                """
            )
            # 구매내역(쿠팡/네이버 주문) 캐시
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS purchase_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel TEXT NOT NULL,
                    order_date TEXT,
                    order_no TEXT,
                    title TEXT NOT NULL,
                    amount INTEGER,
                    payment_method TEXT,
                    source_url TEXT,
                    raw_text TEXT NOT NULL,
                    fingerprint TEXT NOT NULL UNIQUE,
                    imported_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_purchase_records_channel_date
                ON purchase_records(channel, order_date)
                """
            )
            # 주문 단위 요약 (카드 매칭용)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS purchase_orders (
                    channel TEXT NOT NULL,
                    order_no TEXT NOT NULL,
                    order_date TEXT,
                    payment_total INTEGER,
                    item_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT,
                    payment_method TEXT,
                    source_url TEXT,
                    raw_text TEXT,
                    imported_at TEXT NOT NULL,
                    PRIMARY KEY (channel, order_no)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_purchase_orders_channel_date
                ON purchase_orders(channel, order_date)
                """
            )
            # 캐시사용액/카드청구액 컬럼 마이그레이션
            order_cols = {row[1] for row in conn.execute("PRAGMA table_info(purchase_orders)").fetchall()}
            if "cash_used" not in order_cols:
                conn.execute("ALTER TABLE purchase_orders ADD COLUMN cash_used INTEGER")
            if "card_amount" not in order_cols:
                conn.execute("ALTER TABLE purchase_orders ADD COLUMN card_amount INTEGER")
            # 카드사용내역
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS card_usages (
                    use_key TEXT PRIMARY KEY,
                    corp_num TEXT,
                    card_num TEXT,
                    used_at TEXT,
                    store_name TEXT,
                    amount INTEGER,
                    category TEXT,
                    memo TEXT,
                    reviewed INTEGER NOT NULL DEFAULT 0,
                    coupang_purchase_id TEXT,
                    raw_json TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_card_usages_used_at
                ON card_usages(used_at)
                """
            )
            # 쿠팡 자동로그인 자격증명 (label = 사용자 지정 별칭)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS coupang_credentials (
                    label TEXT PRIMARY KEY,
                    email TEXT NOT NULL,
                    password_obf TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
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

    def get_sales_totals_rolling(self, days: int = 30) -> List[dict]:
        """최근 N일간 (오늘 포함) 재고차감 이벤트를 SKU 단위로 합산.

        반환: [{channel, product_id, item_id, qty_sold}, ...]
        - qty_sold 는 get_sales_for_date 와 동일한 diff 로직을 N일로 확장
        - qty_sold == 0 인 SKU 는 제외
        """
        n = max(1, int(days))
        with self._guard, self._connection() as conn:
            cursor = conn.execute(
                """
                WITH pairs AS (
                    SELECT
                        a.channel,
                        a.product_id,
                        a.item_id,
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
                    WHERE DATE(a.recorded_at) >= DATE('now', 'localtime', ?)
                )
                SELECT channel, product_id, item_id, SUM(qty_sold) AS qty
                FROM pairs
                WHERE qty_sold > 0
                GROUP BY channel, product_id, item_id
                HAVING qty > 0
                """,
                (f'-{n - 1} days',),
            )
            cols = ["channel", "product_id", "item_id", "qty_sold"]
            return [
                {
                    "channel": row[0],
                    "product_id": row[1],
                    "item_id": row[2],
                    "qty_sold": int(row[3] or 0),
                }
                for row in cursor.fetchall()
            ]

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

    def get_daily_sales_series(
        self, start_date: str, end_date: str
    ) -> List[dict]:
        """기간별 일자×(channel, product_id, item_id) 단위 판매량 합계.

        반환: [{date, channel, product_id, item_id, qty_sold, revenue}, ...]
        """
        with self._guard, self._connection() as conn:
            cursor = conn.execute(
                """
                WITH pairs AS (
                    SELECT
                        DATE(a.recorded_at) AS sale_date,
                        a.channel,
                        a.product_id,
                        a.item_id,
                        a.price,
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
                    WHERE DATE(a.recorded_at) BETWEEN ? AND ?
                )
                SELECT
                    sale_date,
                    channel,
                    product_id,
                    item_id,
                    SUM(qty_sold) AS qty_sold_sum,
                    SUM(CASE WHEN price IS NOT NULL THEN qty_sold * price ELSE 0 END) AS revenue_sum
                FROM pairs
                WHERE qty_sold > 0
                GROUP BY sale_date, channel, product_id, item_id
                ORDER BY sale_date ASC
                """,
                (start_date, end_date),
            )
            cols = [
                "date", "channel", "product_id", "item_id",
                "qty_sold", "revenue",
            ]
            result: List[dict] = []
            for row in cursor.fetchall():
                d = dict(zip(cols, row))
                d["qty_sold"] = int(d["qty_sold"] or 0)
                d["revenue"] = int(d["revenue"] or 0)
                result.append(d)
            return result

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

    # ── 마스터 상품 / 채널-마스터 링크 ──

    _CHANNEL_ALIASES = {
        "네이버": "naver",
        "naver": "naver",
        "smartstore": "naver",
        "쿠팡": "coupang",
        "coupang": "coupang",
    }

    @classmethod
    def _norm_channel(cls, channel: str | None) -> str:
        text = str(channel or "").strip().lower()
        return cls._CHANNEL_ALIASES.get(text, text)

    @staticmethod
    def _row_to_master_dict(row: tuple) -> dict:
        (
            master_id,
            name,
            unit_cost,
            memo,
            rep_channel,
            rep_key,
            created_at,
            updated_at,
        ) = row
        return {
            "id": int(master_id),
            "name": str(name or ""),
            "unit_cost": (int(unit_cost) if unit_cost is not None else None),
            "memo": (str(memo) if memo is not None else None),
            "representative_channel": (str(rep_channel) if rep_channel else None),
            "representative_product_key": (str(rep_key) if rep_key else None),
            "created_at": str(created_at),
            "updated_at": str(updated_at),
        }

    @staticmethod
    def _row_to_link_dict(row: tuple) -> dict:
        channel, product_key, master_id, multiplier, created_at, updated_at = row
        return {
            "channel": str(channel or ""),
            "product_key": str(product_key or ""),
            "master_id": int(master_id),
            "multiplier": max(1, int(multiplier or 1)),
            "created_at": str(created_at),
            "updated_at": str(updated_at),
        }

    def list_masters(self) -> List[dict]:
        with self._guard, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT id, name, unit_cost, memo,
                       representative_channel, representative_product_key,
                       created_at, updated_at
                FROM master_products
                ORDER BY name ASC, id ASC
                """
            ).fetchall()
        return [self._row_to_master_dict(r) for r in rows]

    def get_master(self, master_id: int) -> dict | None:
        with self._guard, self._connection() as conn:
            row = conn.execute(
                """
                SELECT id, name, unit_cost, memo,
                       representative_channel, representative_product_key,
                       created_at, updated_at
                FROM master_products WHERE id = ?
                """,
                (int(master_id),),
            ).fetchone()
        return self._row_to_master_dict(row) if row else None

    def create_master(
        self,
        name: str,
        unit_cost: int | None = None,
        memo: str | None = None,
    ) -> dict:
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("master name is required")
        now = datetime.now().isoformat()
        with self._guard, self._connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO master_products (
                    name, unit_cost, memo,
                    representative_channel, representative_product_key,
                    created_at, updated_at
                ) VALUES (?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    clean_name,
                    (int(unit_cost) if unit_cost is not None else None),
                    (str(memo).strip() if memo else None),
                    now,
                    now,
                ),
            )
            master_id = int(cursor.lastrowid)
            conn.commit()
            row = conn.execute(
                """
                SELECT id, name, unit_cost, memo,
                       representative_channel, representative_product_key,
                       created_at, updated_at
                FROM master_products WHERE id = ?
                """,
                (master_id,),
            ).fetchone()
        return self._row_to_master_dict(row)

    def update_master(
        self,
        master_id: int,
        *,
        name: str | None = None,
        unit_cost: int | None = None,
        memo: str | None = None,
        clear_unit_cost: bool = False,
        clear_memo: bool = False,
    ) -> dict | None:
        updates: list[str] = []
        params: list = []
        if name is not None:
            clean = str(name).strip()
            if not clean:
                raise ValueError("master name cannot be empty")
            updates.append("name = ?")
            params.append(clean)
        if clear_unit_cost:
            updates.append("unit_cost = NULL")
        elif unit_cost is not None:
            updates.append("unit_cost = ?")
            params.append(int(unit_cost))
        if clear_memo:
            updates.append("memo = NULL")
        elif memo is not None:
            updates.append("memo = ?")
            params.append(str(memo).strip() or None)
        if not updates:
            return self.get_master(master_id)
        updates.append("updated_at = ?")
        params.append(datetime.now().isoformat())
        params.append(int(master_id))

        with self._guard, self._connection() as conn:
            conn.execute(
                f"UPDATE master_products SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            conn.commit()
        return self.get_master(master_id)

    def delete_master(self, master_id: int) -> None:
        with self._guard, self._connection() as conn:
            conn.execute(
                "DELETE FROM master_products WHERE id = ?",
                (int(master_id),),
            )
            conn.commit()

    def set_master_representative(
        self,
        master_id: int,
        channel: str | None,
        product_key: str | None,
    ) -> dict | None:
        with self._guard, self._connection() as conn:
            conn.execute(
                """
                UPDATE master_products
                SET representative_channel = ?,
                    representative_product_key = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    (self._norm_channel(channel) if channel else None),
                    (str(product_key).strip() if product_key else None),
                    datetime.now().isoformat(),
                    int(master_id),
                ),
            )
            conn.commit()
        return self.get_master(master_id)

    def list_all_links(self) -> List[dict]:
        with self._guard, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT channel, product_key, master_id, multiplier,
                       created_at, updated_at
                FROM channel_master_links
                """
            ).fetchall()
        return [self._row_to_link_dict(r) for r in rows]

    def list_links_for_master(self, master_id: int) -> List[dict]:
        with self._guard, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT channel, product_key, master_id, multiplier,
                       created_at, updated_at
                FROM channel_master_links
                WHERE master_id = ?
                ORDER BY created_at ASC
                """,
                (int(master_id),),
            ).fetchall()
        return [self._row_to_link_dict(r) for r in rows]

    def get_link(self, channel: str, product_key: str) -> dict | None:
        channel_key = self._norm_channel(channel)
        item_key = str(product_key or "").strip()
        if not channel_key or not item_key:
            return None
        with self._guard, self._connection() as conn:
            row = conn.execute(
                """
                SELECT channel, product_key, master_id, multiplier,
                       created_at, updated_at
                FROM channel_master_links
                WHERE channel = ? AND product_key = ?
                """,
                (channel_key, item_key),
            ).fetchone()
        return self._row_to_link_dict(row) if row else None

    def link_channel_product(
        self,
        channel: str,
        product_key: str,
        master_id: int,
        multiplier: int = 1,
    ) -> dict:
        channel_key = self._norm_channel(channel)
        item_key = str(product_key or "").strip()
        if not channel_key or not item_key:
            raise ValueError("channel and product_key are required")
        qty = max(1, int(multiplier))
        now = datetime.now().isoformat()
        with self._guard, self._connection() as conn:
            existing = conn.execute(
                """
                SELECT created_at FROM channel_master_links
                WHERE channel = ? AND product_key = ?
                """,
                (channel_key, item_key),
            ).fetchone()
            created_at = str(existing[0]) if existing else now
            conn.execute(
                """
                INSERT OR REPLACE INTO channel_master_links (
                    channel, product_key, master_id, multiplier,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (channel_key, item_key, int(master_id), qty, created_at, now),
            )
            conn.commit()
        return self.get_link(channel_key, item_key)  # type: ignore[return-value]

    def unlink_channel_product(self, channel: str, product_key: str) -> None:
        channel_key = self._norm_channel(channel)
        item_key = str(product_key or "").strip()
        if not channel_key or not item_key:
            return
        with self._guard, self._connection() as conn:
            conn.execute(
                """
                DELETE FROM channel_master_links
                WHERE channel = ? AND product_key = ?
                """,
                (channel_key, item_key),
            )
            conn.execute(
                """
                UPDATE master_products
                SET representative_channel = NULL,
                    representative_product_key = NULL,
                    updated_at = ?
                WHERE representative_channel = ?
                    AND representative_product_key = ?
                """,
                (datetime.now().isoformat(), channel_key, item_key),
            )
            conn.commit()

    def set_link_multiplier(
        self,
        channel: str,
        product_key: str,
        multiplier: int,
    ) -> dict | None:
        channel_key = self._norm_channel(channel)
        item_key = str(product_key or "").strip()
        if not channel_key or not item_key:
            return None
        qty = max(1, int(multiplier))
        with self._guard, self._connection() as conn:
            conn.execute(
                """
                UPDATE channel_master_links
                SET multiplier = ?, updated_at = ?
                WHERE channel = ? AND product_key = ?
                """,
                (qty, datetime.now().isoformat(), channel_key, item_key),
            )
            conn.commit()
        return self.get_link(channel_key, item_key)

    # ── 구매내역 (쿠팡/네이버 주문) ──

    def upsert_purchase_records(self, records: List[dict]) -> int:
        """fingerprint 기준 INSERT OR IGNORE. 신규 row 수 반환."""
        rows: list[tuple] = []
        for r in records:
            fp = str(r.get("fingerprint") or "").strip()
            if not fp:
                continue
            rows.append(
                (
                    str(r.get("channel") or ""),
                    r.get("order_date"),
                    r.get("order_no"),
                    str(r.get("title") or ""),
                    self._opt_int(r.get("amount")),
                    r.get("payment_method"),
                    r.get("source_url"),
                    str(r.get("raw_text") or ""),
                    fp,
                    str(r.get("imported_at") or datetime.now().isoformat()),
                )
            )
        if not rows:
            return 0
        with self._guard, self._connection() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT OR IGNORE INTO purchase_records
                    (channel, order_date, order_no, title, amount, payment_method,
                     source_url, raw_text, fingerprint, imported_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
            return conn.total_changes - before

    def upsert_purchase_orders(self, orders: List[dict]) -> int:
        rows: list[tuple] = []
        for o in orders:
            order_no = str(o.get("order_no") or "").strip()
            channel = str(o.get("channel") or "").strip()
            if not order_no or not channel:
                continue
            rows.append(
                (
                    channel,
                    order_no,
                    o.get("order_date"),
                    self._opt_int(o.get("payment_total")),
                    int(o.get("item_count") or 0),
                    o.get("status"),
                    o.get("payment_method"),
                    o.get("source_url"),
                    o.get("raw_text") or "",
                    str(o.get("imported_at") or datetime.now().isoformat()),
                    self._opt_int(o.get("cash_used")),
                    self._opt_int(o.get("card_amount")),
                )
            )
        if not rows:
            return 0
        with self._guard, self._connection() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT INTO purchase_orders (
                    channel, order_no, order_date, payment_total, item_count,
                    status, payment_method, source_url, raw_text, imported_at,
                    cash_used, card_amount
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel, order_no) DO UPDATE SET
                    order_date     = excluded.order_date,
                    payment_total  = COALESCE(excluded.payment_total, purchase_orders.payment_total),
                    item_count     = excluded.item_count,
                    status         = excluded.status,
                    payment_method = COALESCE(excluded.payment_method, purchase_orders.payment_method),
                    source_url     = excluded.source_url,
                    raw_text       = excluded.raw_text,
                    imported_at    = excluded.imported_at,
                    cash_used      = COALESCE(excluded.cash_used, purchase_orders.cash_used),
                    card_amount    = COALESCE(excluded.card_amount, purchase_orders.card_amount)
                """,
                rows,
            )
            conn.commit()
            return conn.total_changes - before

    def list_purchase_orders(self, channel: str | None = None, limit: int = 2000) -> List[dict]:
        params: list = []
        where = ""
        if channel and channel != "all":
            where = "WHERE channel = ?"
            params.append(channel)
        params.append(max(1, int(limit)))
        with self._guard, self._connection() as conn:
            cursor = conn.execute(
                f"""
                SELECT channel, order_no, order_date, payment_total, item_count,
                       status, payment_method, source_url, raw_text, imported_at,
                       cash_used, card_amount
                FROM purchase_orders
                {where}
                ORDER BY COALESCE(order_date, imported_at) DESC
                LIMIT ?
                """,
                params,
            )
            cols = [
                "channel", "order_no", "order_date", "payment_total", "item_count",
                "status", "payment_method", "source_url", "raw_text", "imported_at",
                "cash_used", "card_amount",
            ]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def list_purchase_records(self, channel: str | None = None, limit: int = 1000) -> List[dict]:
        params: list = []
        where = ""
        if channel and channel != "all":
            where = "WHERE channel = ?"
            params.append(channel)
        params.append(max(1, int(limit)))
        with self._guard, self._connection() as conn:
            cursor = conn.execute(
                f"""
                SELECT id, channel, order_date, order_no, title, amount, payment_method,
                       source_url, raw_text, fingerprint, imported_at
                FROM purchase_records
                {where}
                ORDER BY COALESCE(order_date, imported_at) DESC, id DESC
                LIMIT ?
                """,
                params,
            )
            cols = [
                "id", "channel", "order_date", "order_no", "title", "amount",
                "payment_method", "source_url", "raw_text", "fingerprint", "imported_at",
            ]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def delete_purchase_records(
        self,
        channel: str | None = None,
        *,
        only_missing_order_no: bool = False,
    ) -> int:
        """purchase_records 삭제. only_missing_order_no=True 면 order_no 가 NULL/빈문자열인 행만."""
        clauses: list[str] = []
        params: list = []
        if channel and channel != "all":
            clauses.append("channel = ?")
            params.append(channel)
        if only_missing_order_no:
            clauses.append("(order_no IS NULL OR order_no = '')")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._guard, self._connection() as conn:
            cursor = conn.execute(
                f"DELETE FROM purchase_records {where}", params
            )
            conn.commit()
            return int(cursor.rowcount or 0)

    # ── 카드사용내역 ──

    def upsert_card_usages(self, items: List[dict]) -> int:
        """use_key PRIMARY KEY 로 UPSERT. 신규/갱신 수 반환."""
        rows: list[tuple] = []
        now = datetime.now().isoformat()
        for it in items:
            use_key = str(it.get("use_key") or it.get("id") or "").strip()
            if not use_key:
                continue
            raw = it.get("raw")
            if isinstance(raw, dict):
                import json as _json
                raw_json = _json.dumps(raw, ensure_ascii=False)
            elif isinstance(raw, str):
                raw_json = raw
            else:
                raw_json = None
            rows.append(
                (
                    use_key,
                    it.get("corp_num"),
                    it.get("card_num"),
                    it.get("used_at"),
                    it.get("store_name"),
                    self._opt_int(it.get("amount")),
                    it.get("category"),
                    it.get("memo"),
                    int(bool(it.get("reviewed"))),
                    it.get("coupang_purchase_id"),
                    raw_json,
                    now,
                )
            )
        if not rows:
            return 0
        with self._guard, self._connection() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT INTO card_usages
                    (use_key, corp_num, card_num, used_at, store_name, amount,
                     category, memo, reviewed, coupang_purchase_id, raw_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(use_key) DO UPDATE SET
                    corp_num            = excluded.corp_num,
                    card_num            = excluded.card_num,
                    used_at             = excluded.used_at,
                    store_name          = excluded.store_name,
                    amount              = excluded.amount,
                    category            = COALESCE(excluded.category, card_usages.category),
                    memo                = COALESCE(excluded.memo, card_usages.memo),
                    reviewed            = excluded.reviewed,
                    coupang_purchase_id = COALESCE(
                                              excluded.coupang_purchase_id,
                                              card_usages.coupang_purchase_id
                                          ),
                    raw_json            = excluded.raw_json,
                    updated_at          = excluded.updated_at
                """,
                rows,
            )
            conn.commit()
            return conn.total_changes - before

    def list_card_usages(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        card_num: str | None = None,
        limit: int = 5000,
    ) -> List[dict]:
        clauses: list[str] = []
        params: list = []
        if start_date:
            clauses.append("used_at >= ?")
            params.append(f"{start_date}T00:00:00")
        if end_date:
            clauses.append("used_at <= ?")
            params.append(f"{end_date}T23:59:59")
        if card_num:
            clauses.append("card_num = ?")
            params.append(card_num)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, int(limit)))
        with self._guard, self._connection() as conn:
            cursor = conn.execute(
                f"""
                SELECT use_key, corp_num, card_num, used_at, store_name, amount,
                       category, memo, reviewed, coupang_purchase_id, raw_json, updated_at
                FROM card_usages
                {where}
                ORDER BY used_at DESC
                LIMIT ?
                """,
                params,
            )
            cols = [
                "use_key", "corp_num", "card_num", "used_at", "store_name", "amount",
                "category", "memo", "reviewed", "coupang_purchase_id", "raw_json",
                "updated_at",
            ]
            out: list[dict] = []
            import json as _json
            for row in cursor.fetchall():
                d = dict(zip(cols, row))
                d["reviewed"] = bool(d.get("reviewed"))
                if d.get("raw_json"):
                    try:
                        d["raw"] = _json.loads(d["raw_json"])
                    except Exception:  # noqa: BLE001
                        d["raw"] = None
                else:
                    d["raw"] = None
                d.pop("raw_json", None)
                out.append(d)
            return out

    def update_card_usage_fields(
        self,
        use_key: str,
        *,
        memo: str | None = None,
        category: str | None = None,
        reviewed: bool | None = None,
        coupang_purchase_id: str | None = None,
        clear_memo: bool = False,
        clear_coupang_match: bool = False,
    ) -> dict | None:
        updates: list[str] = []
        params: list = []
        if clear_memo:
            updates.append("memo = NULL")
        elif memo is not None:
            updates.append("memo = ?")
            params.append(str(memo))
        if category is not None:
            updates.append("category = ?")
            params.append(str(category))
        if reviewed is not None:
            updates.append("reviewed = ?")
            params.append(int(bool(reviewed)))
        if clear_coupang_match:
            updates.append("coupang_purchase_id = NULL")
        elif coupang_purchase_id is not None:
            updates.append("coupang_purchase_id = ?")
            params.append(str(coupang_purchase_id))
        if not updates:
            return None
        updates.append("updated_at = ?")
        params.append(datetime.now().isoformat())
        params.append(str(use_key))
        with self._guard, self._connection() as conn:
            conn.execute(
                f"UPDATE card_usages SET {', '.join(updates)} WHERE use_key = ?",
                params,
            )
            conn.commit()
        rows = self.list_card_usages(limit=1)
        # 단순 반환: 변경된 row 1건 다시 조회
        for r in rows:
            if r.get("use_key") == use_key:
                return r
        return None

    # ---------------- 쿠팡 자격증명 ----------------

    def list_coupang_credentials(self) -> List[dict]:
        with self._guard, self._connection() as conn:
            cur = conn.execute(
                "SELECT label, email, password_obf, updated_at FROM coupang_credentials "
                "ORDER BY updated_at DESC"
            )
            return [
                {
                    "label": r[0],
                    "email": r[1],
                    "password_obf": r[2],
                    "updated_at": r[3],
                }
                for r in cur.fetchall()
            ]

    def upsert_coupang_credential(self, label: str, email: str, password_obf: str) -> None:
        if not label or not email or not password_obf:
            raise ValueError("label, email, password 모두 필요합니다.")
        with self._guard, self._connection() as conn:
            conn.execute(
                "INSERT INTO coupang_credentials (label, email, password_obf, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(label) DO UPDATE SET email=excluded.email, "
                "password_obf=excluded.password_obf, updated_at=excluded.updated_at",
                (str(label), str(email), str(password_obf), datetime.now().isoformat()),
            )
            conn.commit()

    def delete_coupang_credential(self, label: str) -> None:
        with self._guard, self._connection() as conn:
            conn.execute("DELETE FROM coupang_credentials WHERE label = ?", (str(label),))
            conn.commit()

    @staticmethod
    def _opt_int(value) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
