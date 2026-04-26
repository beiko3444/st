from __future__ import annotations

import hashlib
import html
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable, List

from inventory_app.models import PurchaseRecord


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
    matches = re.findall(r"([0-9][0-9,]{2,})\s*원", text)
    if not matches:
        return None
    values: list[int] = []
    for raw in matches:
        try:
            values.append(int(raw.replace(",", "")))
        except ValueError:
            continue
    return max(values) if values else None


def _parse_date(text: str) -> str | None:
    patterns = (
        r"(20\d{2})[.\-/년\s]+(\d{1,2})[.\-/월\s]+(\d{1,2})",
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
        r"(?:주문번호|주문\s*번호|order\s*no\.?)\s*[:：]?\s*([A-Za-z0-9\-]{6,})",
        r"\b([0-9]{10,})\b",
    )
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.I)
        if m:
            return m.group(1)
    return None


def _guess_title(text: str) -> str:
    trimmed = re.sub(r"\b20\d{2}[.\-/년\s]+\d{1,2}[.\-/월\s]+\d{1,2}\b", " ", text)
    trimmed = re.sub(r"[0-9][0-9,]{2,}\s*원", " ", trimmed)
    trimmed = re.sub(r"(주문번호|주문\s*번호|배송완료|결제완료|구매확정|주문상세)", " ", trimmed)
    trimmed = re.sub(r"\s+", " ", trimmed).strip()
    if len(trimmed) > 120:
        trimmed = trimmed[:117].rstrip() + "..."
    return trimmed or "구매내역"


def _split_order_like_blocks(text: str) -> list[str]:
    date_pattern = re.compile(r"20\d{2}[.\-/년\s]+\d{1,2}[.\-/월\s]+\d{1,2}")
    starts = [m.start() for m in date_pattern.finditer(text)]
    parts: list[str] = []
    if starts:
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else len(text)
            parts.append(text[start:end].strip())
    else:
        parts = [p.strip() for p in re.split(r"(?=(?:주문번호|주문\s*번호))", text) if p.strip()]
    blocks = [p for p in parts if "원" in p and len(p) >= 20]
    if blocks:
        return blocks
    return [text] if "원" in text else []


class PurchaseHistoryStore:
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
            conn.commit()

    @staticmethod
    def _fingerprint(record: PurchaseRecord) -> str:
        joined = "\x1f".join(
            [
                record.channel,
                record.order_date or "",
                record.order_no or "",
                record.title,
                str(record.amount or ""),
                record.raw_text[:500],
            ]
        )
        return hashlib.sha1(joined.encode("utf-8")).hexdigest()

    def save_records(self, records: Iterable[PurchaseRecord]) -> int:
        rows = []
        for record in records:
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
            return conn.total_changes - before

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
            result.append(
                PurchaseRecord(
                    id=int(row[0]),
                    channel=str(row[1]),
                    order_date=row[2],
                    order_no=row[3],
                    title=str(row[4]),
                    amount=row[5],
                    payment_method=row[6],
                    source_url=row[7],
                    raw_text=str(row[8]),
                    imported_at=datetime.fromisoformat(str(row[9])),
                )
            )
        return result


class PurchaseHistoryParser:
    def parse_html_file(self, channel: str, path: Path) -> List[PurchaseRecord]:
        raw = path.read_text(encoding="utf-8", errors="ignore")
        text = _clean_text(raw)
        now = datetime.now()
        records: List[PurchaseRecord] = []
        for block in _split_order_like_blocks(text):
            title = _guess_title(block)
            amount = _parse_amount(block)
            if amount is None:
                continue
            records.append(
                PurchaseRecord(
                    id=None,
                    channel=channel,
                    order_date=_parse_date(block),
                    order_no=_parse_order_no(block),
                    title=title,
                    amount=amount,
                    payment_method=None,
                    source_url=None,
                    raw_text=block[:2000],
                    imported_at=now,
                )
            )
        return records
