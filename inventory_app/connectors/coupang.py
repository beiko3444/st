from __future__ import annotations

import hashlib
import hmac
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urlencode

import httpx


class CoupangRocketConnector:
    _CACHE_TTL_SECONDS = 120.0
    _shared_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
    _shared_vendor_locks: Dict[str, threading.Lock] = {}
    _shared_guard = threading.Lock()

    def __init__(
        self,
        vendor_id: str,
        access_key: str,
        secret_key: str,
        timeout_seconds: int,
    ) -> None:
        self.vendor_id = vendor_id
        self.access_key = access_key
        self.secret_key = secret_key
        self.base_url = "https://api-gateway.coupang.com"
        self.client = httpx.Client(base_url=self.base_url, timeout=timeout_seconds)
        self.max_retry_attempts = 6
        self.detail_sleep_seconds = 0.15

    @staticmethod
    def _signed_date() -> str:
        return datetime.utcnow().strftime("%y%m%dT%H%M%SZ")

    def _authorization(self, method: str, path: str, query_string: str) -> str:
        signed_date = self._signed_date()
        message = f"{signed_date}{method.upper()}{path}{query_string}"
        signature = hmac.new(
            self.secret_key.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return (
            "CEA algorithm=HmacSHA256, "
            f"access-key={self.access_key}, "
            f"signed-date={signed_date}, "
            f"signature={signature}"
        )

    @staticmethod
    def _extract_error_detail(response: httpx.Response) -> str:
        try:
            payload = response.json()
            if isinstance(payload, dict):
                message = payload.get("message")
                code = payload.get("code")
                if code and message:
                    return f"{code}: {message}"
                if message:
                    return str(message)
                return str(payload)
        except Exception:  # noqa: BLE001
            pass
        text = response.text.strip()
        if text:
            return text[:400]
        return f"HTTP {response.status_code}"

    @staticmethod
    def _parse_retry_after_seconds(response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            seconds = float(value.strip())
            return max(0.0, min(seconds, 30.0))
        except (TypeError, ValueError):
            return None

    def _retry_delay_seconds(self, response: httpx.Response | None, attempt: int) -> float:
        if response is not None:
            retry_after = self._parse_retry_after_seconds(response)
            if retry_after is not None:
                return retry_after
        return min(8.0, 0.6 * (2 ** attempt))

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        params = params or {}
        query_string = urlencode(params, doseq=True)

        retry_statuses = {429, 500, 502, 503, 504}
        last_error: str | None = None

        for attempt in range(self.max_retry_attempts):
            headers = {
                "Authorization": self._authorization(method, path, query_string),
                "Content-Type": "application/json;charset=UTF-8",
                "X-EXTENDED-TIMEOUT": "90000",
            }

            try:
                response = self.client.request(method, path, params=params, headers=headers)
            except httpx.RequestError as exc:
                last_error = str(exc)
                if attempt < self.max_retry_attempts - 1:
                    time.sleep(self._retry_delay_seconds(None, attempt))
                    continue
                raise RuntimeError(f"쿠팡 API 요청 실패: {last_error}") from exc

            if response.status_code < 400:
                return response.json()

            status = response.status_code
            detail = self._extract_error_detail(response)
            last_error = f"{status}: {detail} ({response.request.url})"

            if status in retry_statuses and attempt < self.max_retry_attempts - 1:
                time.sleep(self._retry_delay_seconds(response, attempt))
                continue

            raise RuntimeError(f"쿠팡 API 오류: {last_error}")

        raise RuntimeError(f"쿠팡 API 요청 실패: {last_error or 'unknown error'}")

    @staticmethod
    def _to_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return None

    @staticmethod
    def _normalize_image_url(path_or_url: Any) -> Optional[str]:
        if not isinstance(path_or_url, str) or not path_or_url:
            return None
        text = path_or_url.strip()
        if not text:
            return None
        if text.startswith("//"):
            return f"https:{text}"
        if text.startswith("http://") or text.startswith("https://"):
            return text
        return f"https://image11.coupangcdn.com/image/{text.lstrip('/')}"

    @staticmethod
    def _pick_image(images: Any) -> Optional[str]:
        if not isinstance(images, list) or not images:
            return None

        preferred = None
        for image in images:
            if not isinstance(image, dict):
                continue
            image_type = str(image.get("imageType") or "").upper()
            candidate = (
                CoupangRocketConnector._normalize_image_url(image.get("cdnPath"))
                or CoupangRocketConnector._normalize_image_url(image.get("vendorPath"))
            )
            if image_type in {"REPRESENTATION", "MAIN"} and candidate:
                preferred = candidate
                break
            if not preferred and candidate:
                preferred = candidate

        return preferred

    @staticmethod
    def _extract_vendor_item_id(item: Dict[str, Any]) -> Optional[int]:
        direct = item.get("vendorItemId")
        if direct is not None:
            return CoupangRocketConnector._to_int(direct)

        candidates = [
            item.get("rocketGrowthItem"),
            item.get("rocketGrowthItemData"),
            item.get("marketPlaceItem"),
            item.get("marketplaceItem"),
            item.get("marketplaceItemData"),
        ]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("vendorItemId") is not None:
                return CoupangRocketConnector._to_int(candidate.get("vendorItemId"))

        return None

    @staticmethod
    def _extract_item_id(item: Dict[str, Any]) -> Optional[int]:
        direct = item.get("itemId")
        if direct is not None:
            return CoupangRocketConnector._to_int(direct)

        candidates = [
            item.get("rocketGrowthItem"),
            item.get("rocketGrowthItemData"),
            item.get("marketPlaceItem"),
            item.get("marketplaceItem"),
            item.get("marketplaceItemData"),
        ]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("itemId") is not None:
                return CoupangRocketConnector._to_int(candidate.get("itemId"))

        return None

    @staticmethod
    def _build_product_url(
        display_name: str,
        page_item_id: Optional[int],
        vendor_item_id: Optional[int],
    ) -> Optional[str]:
        if page_item_id is not None:
            if vendor_item_id is not None:
                return (
                    f"https://www.coupang.com/vp/products/{page_item_id}"
                    f"?itemId={page_item_id}&vendorItemId={vendor_item_id}"
                )
            return f"https://www.coupang.com/vp/products/{page_item_id}"
        if display_name:
            return f"https://www.coupang.com/np/search?q={quote_plus(display_name)}"
        return None

    @staticmethod
    def _extract_price(item: Dict[str, Any]) -> Optional[int]:
        if item.get("salePrice") is not None:
            return CoupangRocketConnector._to_int(item.get("salePrice"))

        price_data = item.get("priceData")
        if isinstance(price_data, dict) and price_data.get("salePrice") is not None:
            return CoupangRocketConnector._to_int(price_data.get("salePrice"))

        nested_candidates = [
            item.get("rocketGrowthItem"),
            item.get("rocketGrowthItemData"),
            item.get("marketPlaceItem"),
            item.get("marketplaceItem"),
            item.get("marketplaceItemData"),
        ]
        for candidate in nested_candidates:
            if not isinstance(candidate, dict):
                continue
            if candidate.get("salePrice") is not None:
                return CoupangRocketConnector._to_int(candidate.get("salePrice"))
            candidate_price_data = candidate.get("priceData")
            if isinstance(candidate_price_data, dict) and candidate_price_data.get("salePrice") is not None:
                return CoupangRocketConnector._to_int(candidate_price_data.get("salePrice"))

        return None

    @staticmethod
    def _extract_sales_count(sales_map: Any) -> Optional[int]:
        if not isinstance(sales_map, dict):
            return None

        preferred_keys = [
            "SALES_COUNT_LAST_THIRTY_DAYS",
            "SALES_COUNT_LAST_30_DAYS",
            "LAST_THIRTY_DAYS",
            "LAST_30_DAYS",
        ]
        for key in preferred_keys:
            if key in sales_map:
                return CoupangRocketConnector._to_int(sales_map.get(key))

        for value in sales_map.values():
            converted = CoupangRocketConnector._to_int(value)
            if converted is not None:
                return converted

        return None

    def _fetch_inventory_maps(self) -> Tuple[Dict[str, Optional[int]], Dict[str, Optional[int]]]:
        inventory_path = f"/v2/providers/rg_open_api/apis/api/v1/vendors/{self.vendor_id}/rg/inventory/summaries"
        inventory_map: Dict[str, Optional[int]] = {}
        sales_map: Dict[str, Optional[int]] = {}
        next_token: Optional[str] = None
        seen_tokens: set[str] = set()

        while True:
            params: Dict[str, Any] = {"vendorId": self.vendor_id}
            if next_token:
                params["nextToken"] = next_token

            payload = self._request("GET", inventory_path, params=params)
            data = payload.get("data") or []
            for row in data:
                if not isinstance(row, dict):
                    continue
                vendor_item_id = row.get("vendorItemId")
                if vendor_item_id is None:
                    continue
                key = str(vendor_item_id)

                inventory_details = row.get("inventoryDetails") or {}
                quantity = (
                    inventory_details.get("totalOrderableQuantity")
                    if isinstance(inventory_details, dict)
                    else None
                )
                inventory_map[key] = self._to_int(quantity)
                sales_map[key] = self._extract_sales_count(row.get("salesCountMap"))

            next_token = payload.get("nextToken")
            if not next_token:
                break
            if str(next_token) in seen_tokens:
                break
            seen_tokens.add(str(next_token))

        return inventory_map, sales_map

    def _fetch_rocket_seller_products(self, max_products: int) -> List[Dict[str, Any]]:
        path = "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products"
        rows: List[Dict[str, Any]] = []
        next_token: Optional[str] = None
        seen_tokens: set[str] = set()

        while True:
            params: Dict[str, Any] = {
                "vendorId": self.vendor_id,
                "maxPerPage": 100,
                "businessTypes": "rocketGrowth",
            }
            if next_token:
                params["nextToken"] = next_token

            payload = self._request("GET", path, params=params)
            data = payload.get("data") or []
            for row in data:
                if isinstance(row, dict):
                    rows.append(row)
                if len(rows) >= max_products:
                    return rows

            next_token = payload.get("nextToken")
            if not next_token:
                break
            if str(next_token) in seen_tokens:
                break
            seen_tokens.add(str(next_token))

        return rows

    def _fetch_product_detail(self, seller_product_id: Any) -> Dict[str, Any]:
        path = f"/v2/providers/seller_api/apis/api/v1/marketplace/seller-products/{seller_product_id}"
        payload = self._request("GET", path)
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        if isinstance(data, list) and data:
            head = data[0]
            if isinstance(head, dict):
                return head
        return {}

    @classmethod
    def _get_vendor_lock(cls, cache_key: str) -> threading.Lock:
        with cls._shared_guard:
            lock = cls._shared_vendor_locks.get(cache_key)
            if lock is None:
                lock = threading.Lock()
                cls._shared_vendor_locks[cache_key] = lock
            return lock

    @classmethod
    def _read_cache(cls, cache_key: str) -> Optional[List[Dict[str, Any]]]:
        with cls._shared_guard:
            cached = cls._shared_cache.get(cache_key)
        if not cached:
            return None

        cached_at, rows = cached
        if time.monotonic() - cached_at > cls._CACHE_TTL_SECONDS:
            return None
        return [dict(row) for row in rows]

    @classmethod
    def _write_cache(cls, cache_key: str, rows: List[Dict[str, Any]]) -> None:
        with cls._shared_guard:
            cls._shared_cache[cache_key] = (time.monotonic(), [dict(row) for row in rows])

    def _fetch_products_uncached(self, max_products: int = 500) -> List[Dict[str, Any]]:
        inventory_map, sales_map = self._fetch_inventory_maps()
        listed_products = self._fetch_rocket_seller_products(max_products=max_products)

        results: List[Dict[str, Any]] = []
        for listed in listed_products:
            seller_product_id = listed.get("sellerProductId")
            if seller_product_id is None:
                continue

            listed_items = listed.get("items")
            if not isinstance(listed_items, list):
                listed_items = []
            listed_image = self._pick_image(listed.get("images"))
            need_detail = not listed_items or listed_image is None

            detail: Dict[str, Any] = {}
            if need_detail:
                try:
                    if self.detail_sleep_seconds > 0:
                        time.sleep(self.detail_sleep_seconds)
                    detail = self._fetch_product_detail(seller_product_id)
                except Exception:  # noqa: BLE001
                    detail = {}

            product_name = (
                detail.get("displayProductName")
                or detail.get("sellerProductName")
                or listed.get("sellerProductName")
                or ""
            )
            image_url = self._pick_image(detail.get("images")) or listed_image

            items = detail.get("items")
            if not isinstance(items, list) or not items:
                items = listed_items

            if not items:
                row = {
                    "channel": "쿠팡로켓",
                    "product_id": str(seller_product_id),
                    "item_id": None,
                    "name": str(product_name),
                    "image_url": image_url,
                    "product_url": self._build_product_url(str(product_name), None, None),
                    "stock": None,
                    "sales": None,
                    "price": None,
                }
                if row["sales"] is None:
                    row["sales"] = 0
                results.append(row)
                continue

            for item in items:
                if not isinstance(item, dict):
                    continue
                vendor_item_id = self._extract_vendor_item_id(item)
                page_item_id = self._extract_item_id(item)
                item_name = item.get("itemName")
                display_name = str(product_name)
                if item_name:
                    display_name = f"{display_name} / {item_name}"
                item_image_url = self._pick_image(item.get("images")) or image_url

                stock = None
                sales = 0
                if vendor_item_id is not None:
                    key = str(vendor_item_id)
                    stock = inventory_map.get(key)
                    sales = sales_map.get(key) or 0

                results.append(
                    {
                        "channel": "쿠팡로켓",
                        "product_id": str(seller_product_id),
                        "item_id": str(vendor_item_id) if vendor_item_id is not None else None,
                        "name": display_name,
                        "image_url": item_image_url,
                        "product_url": self._build_product_url(display_name, page_item_id, vendor_item_id),
                        "stock": stock,
                        "sales": sales,
                        "price": self._extract_price(item),
                    }
                )

        return results

    def fetch_products(self, max_products: int = 500) -> List[Dict[str, Any]]:
        capped_products = max(1, int(max_products))
        cache_key = f"{self.vendor_id}:{capped_products}"

        cached = self._read_cache(cache_key)
        if cached is not None:
            return cached

        lock = self._get_vendor_lock(cache_key)
        with lock:
            cached = self._read_cache(cache_key)
            if cached is not None:
                return cached

            rows = self._fetch_products_uncached(max_products=capped_products)
            self._write_cache(cache_key, rows)
            return [dict(row) for row in rows]
