from __future__ import annotations

import hashlib
import html
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

from inventory_app.models import PurchaseOrder, PurchaseRecord


@dataclass
class PurchaseGroup:
    """주문/결제 단위 묶음.

    쿠팡 주문 N개가 한 번에 결제되면 카드 명세서에는 합계 1건으로 찍히므로
    금액 매칭을 위해 동일 주문 묶음을 만들어 합계를 계산한다.
    """
    channel: str
    order_date: Optional[str]
    title: str            # 대표 제목 ("외 N건")
    total_amount: int     # 양수만 합계 (취소건 별도)
    item_count: int
    items: List[PurchaseRecord] = field(default_factory=list)
    group_key: str = ""


def group_records_by_order(records: List[PurchaseRecord]) -> List[PurchaseGroup]:
    """raw_text 가 동일한 항목들을 한 주문(결제) 묶음으로 본다.

    쿠팡 크롤러는 한 주문 블록 전체를 모든 line item 의 raw_text 에 동일하게 저장하므로
    raw_text 의 앞부분 해시 + order_date 로 묶을 수 있다.
    """
    groups: dict[str, list[PurchaseRecord]] = {}
    for r in records:
        key_text = (r.raw_text or "")[:200].strip()
        h = hashlib.sha1(key_text.encode("utf-8")).hexdigest()[:12]
        key = f"{r.channel}|{r.order_date or ''}|{h}"
        groups.setdefault(key, []).append(r)

    out: List[PurchaseGroup] = []
    for key, items in groups.items():
        # 음수(취소)는 합계에서 제외 (카드 매칭은 양수 결제건만)
        total = sum(int(i.amount or 0) for i in items if int(i.amount or 0) > 0)
        if total <= 0:
            continue
        first_title = (items[0].title or "").strip()
        clean = re.sub(r"^\[[^\]]+\]\s*", "", first_title).strip()
        if len(items) > 1:
            display = f"{clean[:40]} 외 {len(items) - 1}건"
        else:
            display = clean[:60] or "(제목 없음)"
        out.append(
            PurchaseGroup(
                channel=items[0].channel,
                order_date=items[0].order_date,
                title=display,
                total_amount=total,
                item_count=len(items),
                items=items,
                group_key=key,
            )
        )
    out.sort(key=lambda g: (g.order_date or ""), reverse=True)
    return out

WON = "\uc6d0"
ORDER_NO = "\uc8fc\ubb38\ubc88\ud638"
ORDER = "\uc8fc\ubb38"
NO = "\ubc88\ud638"
DEFAULT_TITLE = "\uad6c\ub9e4\ub0b4\uc5ed"

STATUS_WORDS = (
    "\ubc30\uc1a1\uc644\ub8cc",
    "\ubc30\uc1a1\uc911",
    "\uacb0\uc81c\uc644\ub8cc",
    "\uad6c\ub9e4\ud655\uc815",
    "\uc8fc\ubb38\uc0c1\uc138",
    "\ucde8\uc18c\uc644\ub8cc",
    "\uc8fc\ubb38\ucde8\uc18c",
    "\uad6c\ub9e4\ucde8\uc18c",
    "\ubc18\ud488\uc644\ub8cc",
    "\ubc18\ud488",
    "\uad50\ud658",
)


def _default_db_path() -> Path:
    from_env = os.environ.get("SMARTINVENTORY_CACHE_DB", "").strip()
    if from_env:
        return Path(from_env).expanduser().resolve()
    return (Path.home() / ".smartinventory" / "channel_cache.sqlite3").resolve()


def _clean_text(value: str | None) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_amount(text: str) -> int | None:
    matches = re.findall(rf"([0-9][0-9,]{{2,}})\s*{WON}", text)
    values: list[int] = []
    for raw in matches:
        try:
            values.append(int(raw.replace(",", "")))
        except ValueError:
            continue
    return max(values) if values else None


def _parse_date(text: str) -> str | None:
    patterns = (
        r"(20\d{2})[.\-/\s\ub144]+(\d{1,2})[.\-/\s\uc6d4]+(\d{1,2})",
        r"(\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})",
    )
    for pattern in patterns:
        m = re.search(pattern, text)
        if not m:
            continue
        year = int(m.group(1))
        if year < 100:
            year += 2000
        month = int(m.group(2))
        day = int(m.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def _parse_order_no(text: str) -> str | None:
    patterns = (
        rf"(?:{ORDER_NO}|{ORDER}\s*{NO}|order\s*no\.?)\s*[:\uff1a]?\s*([A-Za-z0-9\-]{{6,}})",
        r"\b([0-9]{10,})\b",
    )
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.I)
        if m:
            return m.group(1)
    return None


def _guess_title(text: str) -> str:
    trimmed = re.sub(r"\b20\d{2}[.\-/\s\ub144]+\d{1,2}[.\-/\s\uc6d4]+\d{1,2}\b", " ", text)
    trimmed = re.sub(rf"[0-9][0-9,]{{2,}}\s*{WON}", " ", trimmed)
    trimmed = re.sub(r"\b[A-Za-z0-9-]{10,}\b", " ", trimmed)
    for word in (ORDER_NO, f"{ORDER} {NO}", *STATUS_WORDS):
        trimmed = trimmed.replace(word, " ")
    trimmed = re.sub(r"\s+", " ", trimmed).strip()
    if len(trimmed) > 140:
        trimmed = trimmed[:137].rstrip() + "..."
    return trimmed or DEFAULT_TITLE


def _split_order_like_blocks(text: str) -> list[str]:
    date_pattern = re.compile(r"20\d{2}[.\-/\s\ub144]+\d{1,2}[.\-/\s\uc6d4]+\d{1,2}")
    starts = [m.start() for m in date_pattern.finditer(text)]
    parts: list[str] = []
    if starts:
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else len(text)
            parts.append(text[start:end].strip())
    else:
        parts = [p.strip() for p in re.split(rf"(?=(?:{ORDER_NO}|{ORDER}\s*{NO}))", text) if p.strip()]
    blocks = [p for p in parts if WON in p and len(p) >= 20]
    if blocks:
        return blocks
    return [text] if WON in text else []


class PurchaseHistoryStore:
    _guard = threading.Lock()

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        pi_client: "object | None" = None,
    ) -> None:
        self.db_path = (db_path or _default_db_path()).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()
        # Pi write-through: monitor_url 이 설정된 경우 Pi 에도 동기화 (best-effort)
        self._pi = pi_client

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
            # 캐시사용액/카드청구액 컬럼 마이그레이션 (없으면 추가)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(purchase_orders)").fetchall()}
            if "cash_used" not in cols:
                conn.execute("ALTER TABLE purchase_orders ADD COLUMN cash_used INTEGER")
            if "card_amount" not in cols:
                conn.execute("ALTER TABLE purchase_orders ADD COLUMN card_amount INTEGER")
            conn.commit()

    @staticmethod
    def _fingerprint(record: PurchaseRecord) -> str:
        """안정된 fingerprint — raw_text 제외.

        raw_text 는 크롤러 버전마다 포맷이 바뀌므로 fingerprint 에서 제외.
        같은 주문/같은 상품이면 channel+order_date+order_no+title+amount 가 동일하므로
        중복 INSERT 안 됨.
        """
        # title 의 status prefix '[배송완료]' 등은 시간이 지나면 바뀔 수 있어 제거
        clean_title = re.sub(r"^\s*\[[^\]]+\]\s*", "", str(record.title or "")).strip()
        joined = "\x1f".join(
            [
                record.channel,
                record.order_date or "",
                record.order_no or "",
                clean_title[:200],
                str(record.amount or ""),
            ]
        )
        return hashlib.sha1(joined.encode("utf-8")).hexdigest()

    def save_records(self, records: Iterable[PurchaseRecord]) -> int:
        records_list = list(records)
        rows = []
        for record in records_list:
            rows.append(
                (
                    record.channel,
                    record.order_date,
                    record.order_no,
                    record.title,
                    record.amount,
                    record.payment_method,
                    record.source_url,
                    record.raw_text,
                    self._fingerprint(record),
                    record.imported_at.isoformat(),
                )
            )
        if not rows:
            return 0
        with self._guard, self._connection() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT OR IGNORE INTO purchase_records (
                    channel, order_date, order_no, title, amount, payment_method,
                    source_url, raw_text, fingerprint, imported_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
            inserted = conn.total_changes - before

        # Pi write-through (best-effort, 실패는 로컬 저장에 영향 없음)
        if self._pi is not None:
            try:
                if getattr(self._pi, "is_configured", False):
                    self._pi.upload_purchase_records(records_list, self._fingerprint)
            except Exception:  # noqa: BLE001
                pass
        return inserted

    def delete_records(self, channel: str, *, only_missing_order_no: bool = False) -> int:
        """채널의 구매내역 삭제. 신규/잘못된 데이터 정리용."""
        clauses = ["channel = ?"]
        params: list = [channel]
        if only_missing_order_no:
            clauses.append("(order_no IS NULL OR order_no = '')")
        where = " AND ".join(clauses)
        with self._guard, self._connection() as conn:
            before = conn.total_changes
            conn.execute(f"DELETE FROM purchase_records WHERE {where}", params)
            conn.commit()
            return conn.total_changes - before

    def delete_orders(self, channel: str) -> int:
        """채널의 주문 단위 데이터 삭제."""
        with self._guard, self._connection() as conn:
            before = conn.total_changes
            conn.execute("DELETE FROM purchase_orders WHERE channel = ?", (channel,))
            conn.commit()
            return conn.total_changes - before

    # ── 주문 단위 (카드 매칭용) ──

    def save_orders(self, orders: Iterable[PurchaseOrder]) -> int:
        rows = []
        orders_list = list(orders)
        for o in orders_list:
            if not o.order_no:
                continue
            rows.append(
                (
                    o.channel,
                    o.order_no,
                    o.order_date,
                    int(o.payment_total) if o.payment_total is not None else None,
                    int(o.item_count or 0),
                    o.status,
                    o.payment_method,
                    o.source_url,
                    o.raw_text,
                    o.imported_at.isoformat(),
                    int(o.cash_used) if o.cash_used is not None else None,
                    int(o.card_amount) if o.card_amount is not None else None,
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
            inserted = conn.total_changes - before
        # Pi write-through
        if self._pi is not None and getattr(self._pi, "is_configured", False):
            try:
                self._pi.upload_purchase_orders(orders_list)
            except Exception:  # noqa: BLE001
                pass
        return inserted

    def load_orders(self, channel: str = "all", limit: int = 2000) -> List[PurchaseOrder]:
        params: list = []
        where = ""
        if channel and channel != "all":
            where = "WHERE channel = ?"
            params.append(channel)
        params.append(max(1, int(limit)))
        with self._connection() as conn:
            rows = conn.execute(
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
            ).fetchall()
        result: List[PurchaseOrder] = []
        for row in rows:
            try:
                imported_at = datetime.fromisoformat(row[9])
            except Exception:  # noqa: BLE001
                imported_at = datetime.now()
            result.append(
                PurchaseOrder(
                    channel=row[0],
                    order_no=row[1],
                    order_date=row[2],
                    payment_total=row[3],
                    item_count=int(row[4] or 0),
                    status=row[5],
                    payment_method=row[6],
                    source_url=row[7],
                    raw_text=row[8] or "",
                    imported_at=imported_at,
                    cash_used=row[10] if len(row) > 10 else None,
                    card_amount=row[11] if len(row) > 11 else None,
                )
            )
        return result

    def load_remote_orders(self, channel: str = "all", limit: int = 2000) -> List[PurchaseOrder]:
        if self._pi is not None and getattr(self._pi, "is_configured", False):
            try:
                return self._pi.list_purchase_orders(channel=channel, limit=limit)
            except Exception:  # noqa: BLE001
                pass
        return self.load_orders(channel=channel, limit=limit)

    def load_remote_records(self, channel: str = "all", limit: int = 2000) -> List[PurchaseRecord]:
        """Pi 가 설정돼 있으면 Pi 에서, 아니면 로컬에서 읽기."""
        if self._pi is not None and getattr(self._pi, "is_configured", False):
            try:
                return self._pi.list_purchase_records(channel=channel, limit=limit)
            except Exception:  # noqa: BLE001
                # Pi 실패 시 로컬 fallback
                pass
        return self.load_records(channel=channel, limit=limit)

    def pull_from_pi(self, limit: int = 5000) -> tuple[int, int]:
        """Pi 에서 구매내역+주문을 다운로드해 로컬 DB 에 저장.

        Returns: (records_inserted, orders_inserted_or_updated). Pi 미설정 시 (0,0).
        write-through 무한루프를 피하려고 raw INSERT OR IGNORE 사용.
        """
        if self._pi is None or not getattr(self._pi, "is_configured", False):
            return (0, 0)

        # 1) records
        try:
            remote_records = self._pi.list_purchase_records(channel="all", limit=limit)
        except Exception:  # noqa: BLE001
            remote_records = []

        rec_rows = []
        for r in remote_records:
            rec_rows.append((
                r.channel,
                r.order_date,
                r.order_no,
                r.title,
                r.amount,
                r.payment_method,
                r.source_url,
                r.raw_text,
                self._fingerprint(r),
                r.imported_at.isoformat() if r.imported_at else datetime.now().isoformat(),
            ))

        records_inserted = 0
        if rec_rows:
            with self._guard, self._connection() as conn:
                before = conn.total_changes
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO purchase_records (
                        channel, order_date, order_no, title, amount, payment_method,
                        source_url, raw_text, fingerprint, imported_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rec_rows,
                )
                conn.commit()
                records_inserted = conn.total_changes - before

        # 2) orders
        try:
            remote_orders = self._pi.list_purchase_orders(channel="all", limit=limit)
        except Exception:  # noqa: BLE001
            remote_orders = []

        order_rows = []
        for o in remote_orders:
            order_rows.append((
                o.channel,
                o.order_no,
                o.order_date,
                o.payment_total,
                o.item_count,
                o.status,
                o.payment_method,
                o.source_url,
                o.raw_text,
                o.imported_at.isoformat() if o.imported_at else datetime.now().isoformat(),
                o.cash_used,
                o.card_amount,
            ))

        orders_changed = 0
        if order_rows:
            with self._guard, self._connection() as conn:
                before = conn.total_changes
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO purchase_orders (
                        channel, order_no, order_date, payment_total, item_count,
                        status, payment_method, source_url, raw_text, imported_at,
                        cash_used, card_amount
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    order_rows,
                )
                conn.commit()
                orders_changed = conn.total_changes - before

        return (records_inserted, orders_changed)

    def load_records(self, channel: str = "all", limit: int = 1000) -> List[PurchaseRecord]:
        params: list[object] = []
        where = ""
        if channel != "all":
            where = "WHERE channel = ?"
            params.append(channel)
        params.append(max(1, int(limit)))
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT id, channel, order_date, order_no, title, amount, payment_method,
                       source_url, raw_text, imported_at
                FROM purchase_records
                {where}
                ORDER BY COALESCE(order_date, imported_at) DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        result: List[PurchaseRecord] = []
        for row in rows:
            try:
                imported_at = datetime.fromisoformat(row[9])
            except Exception:
                imported_at = datetime.now()
            result.append(
                PurchaseRecord(
                    id=row[0],
                    channel=row[1],
                    order_date=row[2],
                    order_no=row[3],
                    title=row[4],
                    amount=row[5],
                    payment_method=row[6],
                    source_url=row[7],
                    raw_text=row[8],
                    imported_at=imported_at,
                )
            )
        return result


class PurchaseHistoryParser:
    def parse_text(self, channel: str, text: str, source_url: str | None = None) -> List[PurchaseRecord]:
        clean = _clean_text(text)
        now = datetime.now()
        records: List[PurchaseRecord] = []
        for block in _split_order_like_blocks(clean):
            amount = _parse_amount(block)
            if amount is None:
                continue
            records.append(
                PurchaseRecord(
                    id=None,
                    channel=channel,
                    order_date=_parse_date(block),
                    order_no=_parse_order_no(block),
                    title=_guess_title(block),
                    amount=amount,
                    payment_method=self._guess_payment_method(block),
                    source_url=source_url,
                    raw_text=block[:2000],
                    imported_at=now,
                )
            )
        return records

    def parse_html_file(self, channel: str, path: Path) -> List[PurchaseRecord]:
        raw = path.read_text(encoding="utf-8", errors="ignore")
        return self.parse_text(channel, raw, source_url=str(path))

    @staticmethod
    def _guess_payment_method(text: str) -> str | None:
        keywords = (
            "\uce74\ub4dc",
            "\uc2e0\uc6a9\uce74\ub4dc",
            "\uccb4\ud06c\uce74\ub4dc",
            "\uacc4\uc88c\uc774\uccb4",
            "\ubb34\ud1b5\uc7a5",
            "\ub124\uc774\ubc84\ud398\uc774",
            "\uce74\uce74\uc624\ud398\uc774",
            "\ucfe0\ud398\uc774",
            "\ud1a0\uc2a4\ud398\uc774",
            "\ud604\uae08\uc601\uc218\uc99d",
        )
        for keyword in keywords:
            if keyword in text:
                return keyword
        return None
