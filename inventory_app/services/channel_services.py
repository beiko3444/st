from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Tuple

import httpx

from inventory_app.config import AppConfig
from inventory_app.connectors.coupang import CoupangRocketConnector
from inventory_app.connectors.smartstore import SmartStoreConnector
from inventory_app.models import ChannelProduct
from inventory_app.services.local_cache import ChannelProductCache


def _fetch_from_monitor(url: str, channel: str, timeout: int) -> List[ChannelProduct] | None:
    """라즈베리파이 API에서 최신 재고 조회. 실패 시 None 반환."""
    try:
        resp = httpx.get(f"{url.rstrip('/')}/inventory", timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        rows = data.get(channel, [])
        now = datetime.now()
        result: List[ChannelProduct] = []
        for i, r in enumerate(rows, start=1):
            try:
                synced = datetime.fromisoformat(r["recorded_at"])
            except Exception:
                synced = now
            result.append(ChannelProduct(
                serial=i,
                product_id=str(r.get("product_id", "")),
                item_id=r.get("item_id"),
                name=str(r.get("name", "")),
                image_url=r.get("image_url"),
                product_url=r.get("product_url"),
                stock=r.get("stock"),
                sales=r.get("sales"),
                price=r.get("price"),
                synced_at=synced,
            ))
        return result
    except Exception:
        return None


def _to_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _assign_serial_by_sales(rows: List[ChannelProduct]) -> None:
    ranked = sorted(
        rows,
        key=lambda row: (
            1 if _to_int(row.sales) is None else 0,
            -(_to_int(row.sales) or 0),
            row.name.lower(),
            row.product_id,
        ),
    )
    for index, row in enumerate(ranked, start=1):
        row.serial = index


def _row_base_key(row: ChannelProduct) -> str:
    if row.product_id:
        return f"{row.product_id}|{row.item_id or ''}"
    if row.product_url:
        return f"url:{row.product_url}"
    return f"name:{row.name}"


def _row_cache_key(row: ChannelProduct, occurrence: int) -> str:
    return f"{_row_base_key(row)}#{occurrence}"


def _row_signature(row: ChannelProduct) -> tuple[object, ...]:
    return (
        row.product_id,
        row.item_id,
        row.name,
        row.image_url,
        row.product_url,
        _to_int(row.stock),
        _to_int(row.sales),
        _to_int(row.price),
    )


def _reconcile_rows_with_cache(
    cache: Dict[str, ChannelProduct],
    incoming: List[ChannelProduct],
) -> List[ChannelProduct]:
    next_cache: Dict[str, ChannelProduct] = {}
    base_seen: Dict[str, int] = {}
    reconciled: List[ChannelProduct] = []

    for row in incoming:
        base = _row_base_key(row)
        occurrence = base_seen.get(base, 0) + 1
        base_seen[base] = occurrence
        key = _row_cache_key(row, occurrence)

        previous = cache.get(key)
        if previous is not None and _row_signature(previous) == _row_signature(row):
            reconciled_row = previous
        else:
            reconciled_row = row

        next_cache[key] = reconciled_row
        reconciled.append(reconciled_row)

    cache.clear()
    cache.update(next_cache)
    return reconciled


def _summarize_naver_sales_error(error: str) -> str:
    text = str(error).strip()
    upper_text = text.upper()

    if "HTTP 403" in upper_text or "GW.AUTHN" in upper_text or "권한" in text:
        return (
            "네이버 판매량 조회 권한이 없습니다(HTTP 403). "
            "커머스 API 앱 권한/호출 IP 화이트리스트를 확인하세요. "
            "판매량은 0으로 표시됩니다."
        )

    compact = " ".join(part for part in text.splitlines() if part.strip())
    if len(compact) > 240:
        compact = compact[:237] + "..."
    return f"네이버 판매량 조회 실패: {compact}"


class NaverChannelService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.cache = ChannelProductCache()
        self.smartstore = SmartStoreConnector(
            client_id=config.smartstore_client_id,
            client_secret=config.smartstore_client_secret,
            token_type=config.smartstore_token_type,
            timeout_seconds=config.timeout_seconds,
        )
        self.smartstore_stats = SmartStoreConnector(
            client_id=config.smartstore_stats_client_id,
            client_secret=config.smartstore_stats_client_secret,
            token_type=config.smartstore_stats_token_type,
            timeout_seconds=config.timeout_seconds,
        )
        self._row_cache: Dict[str, ChannelProduct] = {}

    def fetch_cached(self) -> Tuple[List[ChannelProduct], List[str]]:
        try:
            rows = self.cache.load_rows("naver")
        except Exception:  # noqa: BLE001
            return [], []
        rows = _reconcile_rows_with_cache(self._row_cache, rows)
        _assign_serial_by_sales(rows)
        return rows, []

    def fetch(self) -> Tuple[List[ChannelProduct], List[str]]:
        # 라즈베리파이 API 우선 시도
        if self.config.monitor_url:
            rows = _fetch_from_monitor(self.config.monitor_url, "naver", self.config.timeout_seconds)
            if rows is not None:
                _assign_serial_by_sales(rows)
                return rows, ["__pi__"]

        synced_at = datetime.now()
        warnings: List[str] = []

        raw_rows = self.smartstore.fetch_products(max_items=self.config.max_products)
        rows: List[ChannelProduct] = []
        for raw in raw_rows:
            rows.append(
                ChannelProduct(
                    serial=0,
                    product_id=str(raw.get("product_id") or ""),
                    item_id=(str(raw.get("item_id")) if raw.get("item_id") else None),
                    name=str(raw.get("name") or ""),
                    image_url=raw.get("image_url"),
                    product_url=raw.get("product_url"),
                    stock=raw.get("stock"),
                    sales=None,
                    price=raw.get("price"),
                    synced_at=synced_at,
                )
            )

        sales_map = self._fetch_sales_map(warnings)
        if sales_map is not None:
            for row in rows:
                row.sales = sales_map.get(row.product_id, 0)

        rows = _reconcile_rows_with_cache(self._row_cache, rows)
        _assign_serial_by_sales(rows)
        try:
            self.cache.save_rows("naver", rows)
        except Exception:  # noqa: BLE001
            pass
        return rows, warnings

    def _fetch_sales_map(self, warnings: List[str]) -> dict[str, int] | None:
        connectors = [self.smartstore_stats]
        if (
            self.smartstore_stats.client_id != self.smartstore.client_id
            or self.smartstore_stats.client_secret != self.smartstore.client_secret
            or self.smartstore_stats.token_type != self.smartstore.token_type
        ):
            connectors.append(self.smartstore)

        last_error: str | None = None
        for connector in connectors:
            try:
                return connector.fetch_product_sales_counts(days=self.config.stats_lookback_days)
            except Exception as exc:  # noqa: BLE001
                last_error = _summarize_naver_sales_error(str(exc))
        if last_error:
            warnings.append(last_error)
        return None


class CoupangChannelService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.cache = ChannelProductCache()
        self.coupang = CoupangRocketConnector(
            vendor_id=config.coupang_vendor_id,
            access_key=config.coupang_access_key,
            secret_key=config.coupang_secret_key,
            timeout_seconds=config.timeout_seconds,
        )
        self._row_cache: Dict[str, ChannelProduct] = {}

    def fetch_cached(self) -> Tuple[List[ChannelProduct], List[str]]:
        try:
            rows = self.cache.load_rows("coupang")
        except Exception:  # noqa: BLE001
            return [], []
        rows = _reconcile_rows_with_cache(self._row_cache, rows)
        _assign_serial_by_sales(rows)
        return rows, []

    def fetch(self) -> Tuple[List[ChannelProduct], List[str]]:
        # 라즈베리파이 API 우선 시도
        if self.config.monitor_url:
            rows = _fetch_from_monitor(self.config.monitor_url, "coupang", self.config.timeout_seconds)
            if rows is not None:
                _assign_serial_by_sales(rows)
                return rows, ["__pi__"]

        synced_at = datetime.now()
        warnings: List[str] = []

        raw_rows = self.coupang.fetch_products(max_products=self.config.max_products)
        rows: List[ChannelProduct] = []
        for raw in raw_rows:
            rows.append(
                ChannelProduct(
                    serial=0,
                    product_id=str(raw.get("product_id") or ""),
                    item_id=(str(raw.get("item_id")) if raw.get("item_id") else None),
                    name=str(raw.get("name") or ""),
                    image_url=raw.get("image_url"),
                    product_url=raw.get("product_url"),
                    stock=raw.get("stock"),
                    sales=raw.get("sales"),
                    price=raw.get("price"),
                    synced_at=synced_at,
                )
            )

        rows = _reconcile_rows_with_cache(self._row_cache, rows)
        _assign_serial_by_sales(rows)
        try:
            self.cache.save_rows("coupang", rows)
        except Exception:  # noqa: BLE001
            pass
        return rows, warnings
