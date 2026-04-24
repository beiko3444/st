from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from inventory_app.models import ChannelMasterLink, ChannelProduct, MasterProduct


def _row_content_hash(row: ChannelProduct) -> str:
    """\ubcc0\uacbd \uac10\uc9c0\uc6a9 \ud574\uc2dc. serial/synced_at\uc740 \uc81c\uc678 (\ub300\uc0c1 \ub370\uc774\ud130 \uc790\uccb4\uc758 \ubcc0\uacbd\ub9cc \ubcf4\uae30 \uc704\ud568)."""
    parts = (
        str(row.product_id or ""),
        str(row.item_id or ""),
        str(row.name or ""),
        str(row.image_url or ""),
        str(row.product_url or ""),
        str(row.stock) if row.stock is not None else "",
        str(row.today_sales) if row.today_sales is not None else "",
        str(row.sales) if row.sales is not None else "",
        str(row.price) if row.price is not None else "",
    )
    joined = "\x1f".join(parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


# 채널 키 정규화 — UI 는 한글명("네이버"/"쿠팡"), services 는 영문 코드("naver"/"coupang")를
# 섞어 써왔기 때문에 둘 다 같은 bucket 으로 매핑한다.
_CHANNEL_ALIASES = {
    "네이버": "naver",
    "naver": "naver",
    "smartstore": "naver",
    "쿠팡": "coupang",
    "coupang": "coupang",
}


def _normalize_channel(channel: str | None) -> str:
    text = str(channel or "").strip().lower()
    return _CHANNEL_ALIASES.get(text, text)


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
        conn.execute("PRAGMA foreign_keys=ON;")
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
                    content_hash TEXT,
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
            # shared_stock_rules / shared_stock_pending_ops 는 과거 자동매칭 기능 잔재.
            # 마스터 상품 연결(channel_master_links) 로 대체됐으므로 이전 DB 에 남아
            # 있을 경우 제거한다.
            conn.execute("DROP TABLE IF EXISTS shared_stock_rules")
            conn.execute("DROP TABLE IF EXISTS shared_stock_pending_ops")
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

            # Backward compatibility: add column for existing DBs.
            cols = {
                str(row[1]).strip().lower()
                for row in conn.execute("PRAGMA table_info(channel_products)")
            }
            if "today_sales" not in cols:
                conn.execute("ALTER TABLE channel_products ADD COLUMN today_sales INTEGER")
            if "content_hash" not in cols:
                conn.execute("ALTER TABLE channel_products ADD COLUMN content_hash TEXT")

            # 채널 키 마이그레이션: 과거 UI 는 한글명("네이버"/"쿠팡") 으로 저장해
            # services 의 영문 코드("naver"/"coupang") 조회와 불일치했음.
            # 모든 관련 테이블의 channel 값을 정규화된 코드로 이동.
            for old, new in [("네이버", "naver"), ("쿠팡", "coupang")]:
                for table in (
                    "channel_products",
                    "product_name_overrides",
                    "product_cost_overrides",
                    "product_favorites",
                ):
                    try:
                        conn.execute(
                            f"UPDATE {table} SET channel = ? WHERE channel = ?",
                            (new, old),
                        )
                    except sqlite3.OperationalError:
                        # 테이블 없거나 channel 컬럼 없으면 skip
                        pass
            conn.commit()

    def save_rows(self, channel: str, rows: List[ChannelProduct]) -> None:
        """\ubcc0\uacbd\ubd84\ub9cc DB\uc5d0 \ubc18\uc601\ud558\ub294 diff-upsert \uc800\uc7a5.

        - \uac01 row\uc758 content_hash\ub97c \uacc4\uc0b0\ud574 \uae30\uc874 \ud589\uacfc \ube44\uad50
        - \ud574\uc2dc \ub3d9\uc77c: \uac74\ub108\ub700 (DB I/O \uc5c6\uc74c)
        - \ud574\uc2dc \ub2e4\ub984: INSERT OR REPLACE
        - \uc0c8 incoming\uc5d0\uc11c \uc0ac\ub77c\uc9c4 row_no\ub294 DELETE
        - \uc804\uccb4 DELETE \ud6c4 \uc804\uccb4 INSERT \ubcf4\ub2e4 \ud6e8\uc52c \ube60\ub984 (\ubcc0\uacbd \uc5c6\uc73c\uba74 I/O 0)
        """
        channel_key = _normalize_channel(channel)
        incoming: List[tuple] = []
        incoming_row_nos: set[int] = set()
        for row_no, row in enumerate(rows, start=1):
            incoming_row_nos.add(row_no)
            incoming.append(
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
                    _row_content_hash(row),
                )
            )

        with self._guard, self._connection() as conn:
            # \uae30\uc874 row_no -> content_hash \uc2a4\ub0c5\uc0f7
            existing: Dict[int, str] = {}
            for rn, h in conn.execute(
                "SELECT row_no, content_hash FROM channel_products WHERE channel = ?",
                (channel_key,),
            ):
                existing[int(rn)] = str(h or "")

            existing_row_nos = set(existing.keys())

            # 1) incoming\uc5d0 \uc5c6\ub294 row_no\ub294 DELETE
            to_delete = existing_row_nos - incoming_row_nos
            if to_delete:
                conn.executemany(
                    "DELETE FROM channel_products WHERE channel = ? AND row_no = ?",
                    [(channel_key, rn) for rn in to_delete],
                )

            # 2) \ud574\uc2dc \ubcc0\uacbd\ub41c/\uc2e0\uaddc row\ub9cc upsert
            to_upsert: List[tuple] = []
            for payload in incoming:
                rn = payload[1]
                h = payload[-1]
                if existing.get(rn) != h:
                    to_upsert.append(payload)

            if to_upsert:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO channel_products (
                        channel, row_no, serial, product_id, item_id, name, image_url, product_url,
                        stock, today_sales, sales, price, synced_at, content_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    to_upsert,
                )

            conn.commit()

    def load_rows(self, channel: str) -> List[ChannelProduct]:
        channel_key = _normalize_channel(channel)
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
        channel_key = _normalize_channel(channel)
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
        channel_key = _normalize_channel(channel)
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
        channel_key = _normalize_channel(channel)
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
        channel_key = _normalize_channel(channel)
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
        channel_key = _normalize_channel(channel)
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
        channel_key = _normalize_channel(channel)
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

    # ------------------------------------------------------------------
    # Master products (user-curated SKU catalog)
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_master(row: tuple) -> MasterProduct:
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
        try:
            created = datetime.fromisoformat(str(created_at))
        except Exception:  # noqa: BLE001
            created = datetime.now()
        try:
            updated = datetime.fromisoformat(str(updated_at))
        except Exception:  # noqa: BLE001
            updated = created
        return MasterProduct(
            id=int(master_id),
            name=str(name or ""),
            unit_cost=(int(unit_cost) if unit_cost is not None else None),
            memo=(str(memo) if memo is not None else None),
            representative_channel=(
                _normalize_channel(rep_channel) if rep_channel else None
            ),
            representative_product_key=(str(rep_key) if rep_key else None),
            created_at=created,
            updated_at=updated,
        )

    def create_master(
        self,
        name: str,
        unit_cost: int | None = None,
        memo: str | None = None,
    ) -> MasterProduct:
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
        return self._row_to_master(row)

    def update_master(
        self,
        master_id: int,
        *,
        name: str | None = None,
        unit_cost: int | None = None,
        memo: str | None = None,
        clear_unit_cost: bool = False,
        clear_memo: bool = False,
    ) -> None:
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
            return
        updates.append("updated_at = ?")
        params.append(datetime.now().isoformat())
        params.append(int(master_id))

        with self._guard, self._connection() as conn:
            conn.execute(
                f"UPDATE master_products SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            conn.commit()

    def delete_master(self, master_id: int) -> None:
        with self._guard, self._connection() as conn:
            conn.execute(
                "DELETE FROM master_products WHERE id = ?",
                (int(master_id),),
            )
            conn.commit()

    def get_master(self, master_id: int) -> MasterProduct | None:
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
        return self._row_to_master(row) if row else None

    def list_masters(self) -> List[MasterProduct]:
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
        return [self._row_to_master(r) for r in rows]

    def set_master_representative(
        self,
        master_id: int,
        channel: str | None,
        product_key: str | None,
    ) -> None:
        with self._guard, self._connection() as conn:
            conn.execute(
                """
                UPDATE master_products
                SET representative_channel = ?, representative_product_key = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    (_normalize_channel(channel) if channel else None),
                    (str(product_key).strip() if product_key else None),
                    datetime.now().isoformat(),
                    int(master_id),
                ),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Channel-to-master links
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_link(row: tuple) -> ChannelMasterLink:
        channel, product_key, master_id, multiplier, created_at, updated_at = row
        try:
            created = datetime.fromisoformat(str(created_at))
        except Exception:  # noqa: BLE001
            created = datetime.now()
        try:
            updated = datetime.fromisoformat(str(updated_at))
        except Exception:  # noqa: BLE001
            updated = created
        return ChannelMasterLink(
            channel=_normalize_channel(channel),
            product_key=str(product_key or ""),
            master_id=int(master_id),
            multiplier=max(1, int(multiplier or 1)),
            created_at=created,
            updated_at=updated,
        )

    def link_channel_product(
        self,
        channel: str,
        product_key: str,
        master_id: int,
        multiplier: int = 1,
    ) -> None:
        channel_key = _normalize_channel(channel)
        item_key = str(product_key).strip()
        if not item_key:
            return
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

    def unlink_channel_product(self, channel: str, product_key: str) -> None:
        channel_key = _normalize_channel(channel)
        item_key = str(product_key).strip()
        if not item_key:
            return
        with self._guard, self._connection() as conn:
            conn.execute(
                """
                DELETE FROM channel_master_links
                WHERE channel = ? AND product_key = ?
                """,
                (channel_key, item_key),
            )
            # representative 가 이 링크를 가리키고 있었으면 해제
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
    ) -> None:
        channel_key = _normalize_channel(channel)
        item_key = str(product_key).strip()
        if not item_key:
            return
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

    def get_link(
        self,
        channel: str,
        product_key: str,
    ) -> ChannelMasterLink | None:
        channel_key = _normalize_channel(channel)
        item_key = str(product_key).strip()
        if not item_key:
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
        return self._row_to_link(row) if row else None

    def list_links_for_master(self, master_id: int) -> List[ChannelMasterLink]:
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
        return [self._row_to_link(r) for r in rows]

    def load_all_links(self) -> Dict[tuple[str, str], ChannelMasterLink]:
        """(channel, product_key) -> link 전체 스냅샷. 집계 파이프라인용."""
        with self._guard, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT channel, product_key, master_id, multiplier,
                    created_at, updated_at
                FROM channel_master_links
                """
            ).fetchall()
        result: Dict[tuple[str, str], ChannelMasterLink] = {}
        for row in rows:
            link = self._row_to_link(row)
            result[(link.channel, link.product_key)] = link
        return result
