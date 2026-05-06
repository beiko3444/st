from __future__ import annotations

import re
from datetime import datetime, date
from typing import Dict, List, Tuple

import httpx

from inventory_app.config import AppConfig
from inventory_app.connectors.coupang import CoupangRocketConnector
from inventory_app.connectors.smartstore import SmartStoreConnector
from inventory_app.models import ChannelProduct
from inventory_app.services.local_cache import ChannelProductCache
from inventory_app.services.shared_stock_grouping import product_identity_key


def _monitor_sales_key(product_id: str, item_id: str | None) -> str:
    return f"{product_id}|{item_id or ''}"


def _extract_naver_store_base(config: AppConfig) -> str | None:
    """credentials.smartstore.store_url 를 그대로 base URL 로 반환.

    예:
    - "https://brand.naver.com/xtracker"  → 그대로
    - "https://smartstore.naver.com/xtr"  → 그대로
    - "xtr" (slug 만)                      → "https://smartstore.naver.com/xtr" (옛 호환)
    - "https://smartstore.naver.com/main" → None (잘못된 slug 자리)
    """
    raw = str(getattr(config, "smartstore_store_url", "") or "").strip().rstrip("/")
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        # slug 만 온 경우 smartstore 기본값으로 감쌈 (이전 설정 호환)
        if "/" not in raw and raw.lower() != "main":
            return f"https://smartstore.naver.com/{raw}"
        return None
    # /main 같은 잘못된 slug 자리는 거부
    if re.search(r"/(main)/?$", raw, re.I):
        return None
    return raw


# 네이버 쪽 상품 URL 중 /products/{id} 부분에서 id 추출용
_NAVER_PRODUCT_ID_RE = re.compile(r"/products/(\d+)", re.I)
# 교정 대상 URL 판별용 (smartstore, brand, shopping 등)
_NAVER_URL_PREFIX_RE = re.compile(
    r"https?://(?:m\.)?(?:smartstore|brand|shopping)\.naver\.com/", re.I
)


def _fix_naver_product_urls(rows: List[ChannelProduct], base_url: str | None) -> None:
    """모든 네이버 계열 상품 URL 을 config 의 base_url 로 재구성.

    - 기존 URL 에서 product id 만 추출 → `{base_url}/products/{id}` 로 교체
    - /main/ 이든 /xtr/ 이든 다른 slug 든 전부 정규화됨
    - base_url 이 없으면 원본 유지
    """
    if not base_url:
        return
    base = base_url.rstrip("/")
    for row in rows:
        url = str(row.product_url or "")
        product_id = str(row.product_id or "").strip()
        if product_id.isdigit():
            row.product_url = f"{base}/products/{product_id}"
            continue
        if not url:
            continue
        # 네이버 계열 URL 이 아니면 건드리지 않음 (쿠팡 URL 보호)
        if not _NAVER_URL_PREFIX_RE.search(url):
            continue
        m = _NAVER_PRODUCT_ID_RE.search(url)
        if m:
            row.product_url = f"{base}/products/{m.group(1)}"


def _fetch_from_monitor(url: str, channel: str, timeout: int) -> List[ChannelProduct] | None:
    """라즈베리파이 API에서 최신 재고 조회. 실패 시 None 반환."""
    try:
        base_url = url.rstrip("/")
        resp = httpx.get(f"{base_url}/inventory", timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        today_sales_exact, today_sales_by_product = _fetch_today_sales_map_from_monitor(
            base_url,
            channel,
            timeout,
        )
        rows = data.get(channel, [])
        now = datetime.now()
        result: List[ChannelProduct] = []
        for i, r in enumerate(rows, start=1):
            try:
                synced = datetime.fromisoformat(r["recorded_at"])
            except Exception:
                synced = now
            product_id = str(r.get("product_id", ""))
            item_id = (str(r.get("item_id")) if r.get("item_id") else None)
            raw_today = r.get("today_sales")
            if raw_today is None:
                # Pi /sales 엔드포인트는 재고 감소만 카운트하므로 쿠팡도 정확.
                # exact 매칭(product_id+item_id)으로 옵션별 정확도 확보.
                exact_key = _monitor_sales_key(product_id, item_id)
                raw_today = today_sales_exact.get(exact_key)
                # 네이버는 item_id 없을 때 product 단위 fallback 허용.
                # 쿠팡은 옵션 변별이 중요하므로 fallback 없음.
                if raw_today is None and not item_id and channel.lower() != "coupang":
                    raw_today = today_sales_by_product.get(product_id, 0)
                if raw_today is None:
                    raw_today = 0
            today_sales = _to_int(raw_today)
            if today_sales is None:
                today_sales = 0
            result.append(ChannelProduct(
                serial=i,
                product_id=product_id,
                item_id=item_id,
                name=str(r.get("name", "")),
                image_url=r.get("image_url"),
                product_url=r.get("product_url"),
                stock=r.get("stock"),
                sales=r.get("sales"),
                price=r.get("price"),
                synced_at=synced,
                today_sales=today_sales,
            ))
        return result
    except Exception:
        return None


def _fetch_today_sales_map_from_monitor(
    base_url: str,
    channel: str,
    timeout: int,
) -> tuple[Dict[str, int], Dict[str, int]]:
    try:
        today = date.today().strftime("%Y-%m-%d")
        resp = httpx.get(f"{base_url}/sales?date={today}", timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
        sales = payload.get("sales") if isinstance(payload, dict) else []
        if not isinstance(sales, list):
            return {}, {}

        result_exact: Dict[str, int] = {}
        result_by_product: Dict[str, int] = {}
        for row in sales:
            if not isinstance(row, dict):
                continue
            if str(row.get("channel") or "").lower() != channel.lower():
                continue
            product_id = str(row.get("product_id") or "").strip()
            if not product_id:
                continue
            item_id = (str(row.get("item_id")) if row.get("item_id") else None)
            qty = _to_int(row.get("qty_sold")) or 0
            qty = max(0, qty)
            exact_key = _monitor_sales_key(product_id, item_id)
            result_exact[exact_key] = result_exact.get(exact_key, 0) + qty
            result_by_product[product_id] = result_by_product.get(product_id, 0) + qty
        return result_exact, result_by_product
    except Exception:
        return {}, {}


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


def _filter_rows_by_live_product_ids(
    rows: List[ChannelProduct],
    live_product_ids: set[str],
) -> tuple[List[ChannelProduct], int]:
    if not rows or not live_product_ids:
        return rows, 0
    filtered = [row for row in rows if str(row.product_id or "").strip() in live_product_ids]
    hidden_count = len(rows) - len(filtered)
    return filtered, max(0, hidden_count)


def _apply_naver_sales_map(
    rows: List[ChannelProduct],
    sales_map: Dict[str, int],
    origin_to_channel: Dict[str, str] | None = None,
) -> int:
    matched = 0
    origin_to_channel = origin_to_channel or {}
    for row in rows:
        product_id = str(row.product_id or "").strip()
        mapped_id = origin_to_channel.get(product_id, product_id)
        if mapped_id in sales_map:
            row.sales = sales_map.get(mapped_id, 0)
            matched += 1
        else:
            row.sales = 0
    return matched


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
        _to_int(row.today_sales),
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


def _migrate_cached_naver_urls(cache: ChannelProductCache, base_url: str | None) -> int:
    """캐시 DB 의 모든 네이버 상품 URL 을 config 의 base_url 로 일괄 정상화.

    이전에 `/main/` 또는 잘못된 slug(`/xtr/`) 로 저장됐던 URL 도 모두 한 번에 교체됨.
    """
    if not base_url:
        return 0
    try:
        rows = cache.load_rows("naver")
    except Exception:  # noqa: BLE001
        return 0
    base = base_url.rstrip("/")
    changed = 0
    for row in rows:
        url = str(row.product_url or "")
        product_id = str(row.product_id or "").strip()
        if product_id.isdigit():
            expected = f"{base}/products/{product_id}"
        else:
            if not url or not _NAVER_URL_PREFIX_RE.search(url):
                continue
            m = _NAVER_PRODUCT_ID_RE.search(url)
            if not m:
                continue
            expected = f"{base}/products/{m.group(1)}"
        if row.product_url != expected:
            row.product_url = expected
            changed += 1
    if changed > 0:
        try:
            cache.save_rows("naver", rows)
        except Exception:  # noqa: BLE001
            pass
    return changed


class NaverChannelService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.cache = ChannelProductCache()
        # 최초 1회: 캐시에 남아있던 옛 /main/ URL 을 정상 slug 로 일괄 교체
        _migrate_cached_naver_urls(self.cache, _extract_naver_store_base(config))
        self.smartstore = SmartStoreConnector(
            client_id=config.smartstore_client_id,
            client_secret=config.smartstore_client_secret,
            token_type=config.smartstore_token_type,
            timeout_seconds=config.timeout_seconds,
            store_url=config.smartstore_store_url,
        )
        self.smartstore_stats = SmartStoreConnector(
            client_id=config.smartstore_stats_client_id,
            client_secret=config.smartstore_stats_client_secret,
            token_type=config.smartstore_stats_token_type,
            timeout_seconds=config.timeout_seconds,
            store_url=config.smartstore_store_url,
        )
        self._row_cache: Dict[str, ChannelProduct] = {}

    def fetch_cached(self) -> Tuple[List[ChannelProduct], List[str]]:
        try:
            rows = self.cache.load_rows("naver")
        except Exception:  # noqa: BLE001
            return [], []
        rows = _reconcile_rows_with_cache(self._row_cache, rows)
        # 캐시에 남은 옛 /main/products/ URL 도 즉시 교체 → 다음 동기화 전에도 바로 동작
        _fix_naver_product_urls(rows, _extract_naver_store_base(self.config))
        _assign_serial_by_sales(rows)
        return rows, []

    def fetch(self) -> Tuple[List[ChannelProduct], List[str]]:
        naver_base = _extract_naver_store_base(self.config)
        # 라즈베리파이 API 우선 시도
        if self.config.monitor_url:
            rows = _fetch_from_monitor(self.config.monitor_url, "naver", self.config.timeout_seconds)
            if rows is not None:
                warnings: List[str] = ["__pi__"]
                for row in rows:
                    if row.today_sales is None:
                        row.today_sales = 0
                # monitor 가 내려준 /main/products/ 형식 URL 을 정상 slug 로 교체
                _fix_naver_product_urls(rows, naver_base)
                _assign_serial_by_sales(rows)
                # monitor 경로에서도 캐시에 저장 → 다음 시작 시 바로 표시됨
                try:
                    self.cache.save_rows("naver", rows)
                except Exception:  # noqa: BLE001
                    pass
                return rows, warnings
            cached, _ = self.fetch_cached()
            return cached, ["라즈베리파이 DB 조회 실패 — 로컬 캐시를 표시합니다."]

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
                    today_sales=0,
                )
            )

        sales_map = self._fetch_sales_map(warnings)
        if sales_map is not None:
            for row in rows:
                row.sales = sales_map.get(row.product_id, 0)

        try:
            today_map = self.smartstore_stats.fetch_product_sales_counts(days=1)
            for row in rows:
                row.today_sales = today_map.get(row.product_id, 0)
        except Exception:
            for row in rows:
                if row.today_sales is None:
                    row.today_sales = 0

        rows = _reconcile_rows_with_cache(self._row_cache, rows)
        _fix_naver_product_urls(rows, naver_base)
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
                # monitor 경로에서도 캐시에 저장 → 다음 시작 시 바로 표시됨
                try:
                    self.cache.save_rows("coupang", rows)
                except Exception:  # noqa: BLE001
                    pass
                return rows, ["__pi__"]
            cached, _ = self.fetch_cached()
            return cached, ["라즈베리파이 DB 조회 실패 — 로컬 캐시를 표시합니다."]

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
                    today_sales=0,
                )
            )

        rows = _reconcile_rows_with_cache(self._row_cache, rows)
        _assign_serial_by_sales(rows)
        try:
            self.cache.save_rows("coupang", rows)
        except Exception:  # noqa: BLE001
            pass
        return rows, warnings
