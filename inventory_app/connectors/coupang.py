from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urlencode

import httpx


class CoupangRocketConnector:
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

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        params = params or {}
        query_string = urlencode(params, doseq=True)
        headers = {
            "Authorization": self._authorization(method, path, query_string),
            "Content-Type": "application/json;charset=UTF-8",
            "X-EXTENDED-TIMEOUT": "90000",
        }
        response = self.client.request(method, path, params=params, headers=headers)
        response.raise_for_status()
        return response.json()

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

    def fetch_products(self, max_products: int = 500) -> List[Dict[str, Any]]:
        inventory_map, sales_map = self._fetch_inventory_maps()
        listed_products = self._fetch_rocket_seller_products(max_products=max_products)

        results: List[Dict[str, Any]] = []
        for listed in listed_products:
            seller_product_id = listed.get("sellerProductId")
            if seller_product_id is None:
                continue

            detail = self._fetch_product_detail(seller_product_id)
            product_name = (
                detail.get("displayProductName")
                or detail.get("sellerProductName")
                or listed.get("sellerProductName")
                or ""
            )
            image_url = self._pick_image(detail.get("images"))

            items = detail.get("items")
            if not isinstance(items, list) or not items:
                results.append(
                    {
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
                )
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
                sales = None
                if vendor_item_id is not None:
                    key = str(vendor_item_id)
                    stock = inventory_map.get(key)
                    sales = sales_map.get(key)
                    if sales is None:
                        sales = 0

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
