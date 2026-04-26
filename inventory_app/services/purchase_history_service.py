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
