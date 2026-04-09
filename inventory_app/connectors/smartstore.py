from __future__ import annotations

import base64
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

import bcrypt
import httpx


class SmartStoreConnector:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_type: str,
        timeout_seconds: int,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_type = token_type
        self.timeout_seconds = max(3, int(timeout_seconds))
        self._api_client: Optional[httpx.Client] = None
        self._partner_client: Optional[httpx.Client] = None

        self._access_token: Optional[str] = None
        self._token_expire_at: Optional[datetime] = None
        self._channel_no_cache: Optional[str] = None

    @property
    def api_client(self) -> httpx.Client:
        if self._api_client is None:
            self._api_client = httpx.Client(
                base_url="https://api.commerce.naver.com/external",
                timeout=self.timeout_seconds,
            )
        return self._api_client

    @property
    def partner_client(self) -> httpx.Client:
        if self._partner_client is None:
            self._partner_client = httpx.Client(
                base_url="https://api.commerce.naver.com/partner",
                timeout=self.timeout_seconds,
            )
        return self._partner_client

    def _create_signature(self, timestamp_ms: int) -> str:
        password = f"{self.client_id}_{timestamp_ms}".encode("utf-8")
        hashed = bcrypt.hashpw(password, self.client_secret.encode("utf-8"))
        return base64.b64encode(hashed).decode("utf-8")

    @staticmethod
    def _sanitize_query_text(text: str | None) -> str:
        if not isinstance(text, str):
            return ""
        cleaned = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
        return " ".join(cleaned.split())

    @staticmethod
    def _extract_error_detail(response: httpx.Response) -> str:
        try:
            payload = response.json()
            if isinstance(payload, dict):
                code = payload.get("code")
                message = payload.get("message")
                if code and message:
                    return f"{code}: {message}"
                if message:
                    return str(message)
                return str(payload)
        except Exception:  # noqa: BLE001
            pass
        text = response.text.strip()
        if text:
            return text[:500]
        return f"HTTP {response.status_code}"

    @staticmethod
    def _extract_rows(payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            if payload.get("productId") is not None:
                return [payload]
            for key in (
                "rows",
                "data",
                "content",
                "items",
                "channels",
                "productUnitReport",
                "productReport",
            ):
                value = payload.get(key)
                if isinstance(value, list):
                    return [row for row in value if isinstance(row, dict)]
            if payload.get("channelNo") is not None:
                return [payload]
        return []

    def _try_issue_token(
        self,
        client: httpx.Client,
        use_query_params: bool,
    ) -> tuple[bool, str]:
        timestamp_ms = int(time.time() * 1000)
        client_secret_sign = self._create_signature(timestamp_ms)

        payload = {
            "client_id": self.client_id,
            "timestamp": str(timestamp_ms),
            "client_secret_sign": client_secret_sign,
            "grant_type": "client_credentials",
            "type": self.token_type,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        if use_query_params:
            response = client.post("/v1/oauth2/token", params=payload, headers=headers)
        else:
            response = client.post("/v1/oauth2/token", data=payload, headers=headers)

        if response.status_code >= 400:
            detail = self._extract_error_detail(response)
            return False, f"{response.request.url} -> HTTP {response.status_code} ({detail})"

        body = response.json()
        token = body.get("access_token")
        if not token:
            return False, f"{response.request.url} -> access_token 없음"

        expires_in = int(body.get("expires_in", 3600))
        self._access_token = str(token)
        self._token_expire_at = datetime.now() + timedelta(seconds=max(60, expires_in - 60))
        return True, ""

    def _issue_token(self) -> None:
        attempts = [
            ("partner/form", self.partner_client, False),
            ("partner/query", self.partner_client, True),
            ("external/form", self.api_client, False),
            ("external/query", self.api_client, True),
        ]
        errors: List[str] = []

        for name, client, use_query in attempts:
            ok, err = self._try_issue_token(client=client, use_query_params=use_query)
            if ok:
                return
            errors.append(f"[{name}] {err}")

        hint = (
            "스마트스토어 토큰 발급 실패. "
            "앱 설정의 API 호출 IP 화이트리스트(공인 IP)와 앱 권한(활성 상태)을 확인하세요."
        )
        raise RuntimeError(f"{hint}\n\n" + "\n".join(errors))

    def _get_token(self) -> str:
        if not self._access_token or not self._token_expire_at or datetime.now() >= self._token_expire_at:
            self._issue_token()
        return self._access_token or ""

    def _auth_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
        }

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
        return f"https://{text.lstrip('/')}"

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
    def _to_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _build_product_url(product_id: Any, product_name: str | None) -> Optional[str]:
        numeric_id = SmartStoreConnector._to_int(product_id)
        if numeric_id is not None:
            return f"https://smartstore.naver.com/main/products/{numeric_id}"
        query = SmartStoreConnector._sanitize_query_text(product_name)
        if query:
            return f"https://search.shopping.naver.com/search/all?query={quote_plus(query)}"
        return None

    def fetch_primary_channel_no(self) -> str:
        if self._channel_no_cache:
            return self._channel_no_cache

        response = self.api_client.get(
            "/v1/seller/channels",
            headers=self._auth_headers(),
        )
        response.raise_for_status()
        payload = response.json()

        channels = self._extract_rows(payload)
        if not channels:
            raise RuntimeError("채널 정보를 찾을 수 없습니다. /v1/seller/channels 응답을 확인하세요.")

        channels.sort(
            key=lambda row: (
                0 if str(row.get("channelType") or "").upper() == "STOREFARM" else 1,
                0 if str(row.get("channelType") or "").upper() == "WINDOW" else 1,
            )
        )
        channel_no = channels[0].get("channelNo")
        if channel_no is None:
            raise RuntimeError("채널 번호(channelNo)를 찾을 수 없습니다.")

        self._channel_no_cache = str(channel_no)
        return self._channel_no_cache

    def _fetch_product_sales_chunk(
        self,
        channel_no: str,
        start_date: date,
        end_date: date,
    ) -> Dict[str, int]:
        rows = self._fetch_product_sales_chunk_rows(channel_no, start_date, end_date)
        sales_map: Dict[str, int] = {}
        for row in rows:
            product_id = row.get("productId")
            if not product_id:
                continue
            purchases = self._to_int(row.get("numPurchases"))
            if purchases is None:
                continue
            key = str(product_id)
            sales_map[key] = sales_map.get(key, 0) + purchases
        return sales_map

    def _fetch_product_sales_chunk_rows(
        self,
        channel_no: str,
        start_date: date,
        end_date: date,
    ) -> List[Dict[str, Any]]:
        endpoint = f"/v1/bizdata-stats/channels/{channel_no}/sales/product/detail"
        date_formats = ["%Y-%m-%d", "%Y%m%d"]
        errors: List[str] = []

        for fmt in date_formats:
            params = {
                "startDate": start_date.strftime(fmt),
                "endDate": end_date.strftime(fmt),
            }
            response = self.api_client.get(
                endpoint,
                params=params,
                headers=self._auth_headers(),
            )

            if response.status_code >= 400:
                detail = self._extract_error_detail(response)
                errors.append(f"{response.request.url} -> HTTP {response.status_code} ({detail})")
                continue

            body = response.json()
            rows = self._extract_rows(body)
            if isinstance(body, dict) and body.get("code") and not rows:
                detail = f"{body.get('code')}: {body.get('message')}"
                errors.append(f"{response.request.url} -> {detail}")
                continue
            return rows

        if errors:
            raise RuntimeError("네이버 통계 API(판매량) 조회 실패\n" + "\n".join(errors))
        return []

    def _fetch_bizdata_stats_rows(
        self,
        endpoint: str,
        start_date: date,
        end_date: date,
        purpose: str,
    ) -> List[Dict[str, Any]]:
        date_formats = ["%Y-%m-%d", "%Y%m%d"]
        errors: List[str] = []

        for fmt in date_formats:
            params = {
                "startDate": start_date.strftime(fmt),
                "endDate": end_date.strftime(fmt),
            }
            response = self.api_client.get(
                endpoint,
                params=params,
                headers=self._auth_headers(),
            )

            if response.status_code >= 400:
                detail = self._extract_error_detail(response)
                errors.append(f"{response.request.url} -> HTTP {response.status_code} ({detail})")
                continue

            body = response.json()
            rows = self._extract_rows(body)
            if isinstance(body, dict) and body.get("code") and not rows:
                detail = f"{body.get('code')}: {body.get('message')}"
                errors.append(f"{response.request.url} -> {detail}")
                continue
            return rows

        if errors:
            raise RuntimeError(f"네이버 통계 API({purpose}) 조회 실패\n" + "\n".join(errors))
        return []

    @staticmethod
    def _iter_date_chunks(start_date: date, end_date: date, chunk_days: int = 14) -> List[tuple[date, date]]:
        chunks: List[tuple[date, date]] = []
        cursor = start_date
        while cursor <= end_date:
            chunk_end = min(cursor + timedelta(days=max(1, chunk_days) - 1), end_date)
            chunks.append((cursor, chunk_end))
            cursor = chunk_end + timedelta(days=1)
        return chunks

    def fetch_product_sales_counts(self, days: int = 30) -> Dict[str, int]:
        channel_no = self.fetch_primary_channel_no()

        lookback_days = max(1, int(days))
        final_end = datetime.now().date()
        final_start = final_end - timedelta(days=lookback_days - 1)

        cursor = final_start
        merged: Dict[str, int] = {}
        while cursor <= final_end:
            # API데이터솔루션 통계 API는 14일 초과 조회 시 E400S01을 반환할 수 있어 분할 조회한다.
            chunk_end = min(cursor + timedelta(days=13), final_end)
            chunk_sales = self._fetch_product_sales_chunk(channel_no, cursor, chunk_end)
            for key, count in chunk_sales.items():
                merged[key] = merged.get(key, 0) + count
            cursor = chunk_end + timedelta(days=1)

        return merged

    def fetch_search_channel_keyword_rows(self, days: int = 30) -> List[Dict[str, Any]]:
        channel_no = self.fetch_primary_channel_no()
        lookback_days = max(1, int(days))
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=lookback_days - 1)
        endpoint = f"/v1/bizdata-stats/channels/{channel_no}/marketing/search/keyword"

        rows: List[Dict[str, Any]] = []
        for chunk_start, chunk_end in self._iter_date_chunks(start_date, end_date, chunk_days=14):
            rows.extend(
                self._fetch_bizdata_stats_rows(
                    endpoint=endpoint,
                    start_date=chunk_start,
                    end_date=chunk_end,
                    purpose="검색 채널 키워드",
                )
            )
        return rows

    def fetch_product_search_keyword_rows(self, days: int = 30) -> List[Dict[str, Any]]:
        channel_no = self.fetch_primary_channel_no()
        lookback_days = max(1, int(days))
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=lookback_days - 1)
        endpoint = f"/v1/bizdata-stats/channels/{channel_no}/sales/product-search/keyword-by-product"

        rows: List[Dict[str, Any]] = []
        for chunk_start, chunk_end in self._iter_date_chunks(start_date, end_date, chunk_days=14):
            rows.extend(
                self._fetch_bizdata_stats_rows(
                    endpoint=endpoint,
                    start_date=chunk_start,
                    end_date=chunk_end,
                    purpose="상품/검색 채널 상품별 키워드",
                )
            )
        return rows

    def fetch_product_sales_revenue(self, days: int = 30) -> Dict[str, Dict[str, Any]]:
        channel_no = self.fetch_primary_channel_no()

        lookback_days = max(1, int(days))
        final_end = datetime.now().date()
        final_start = final_end - timedelta(days=lookback_days - 1)

        cursor = final_start
        merged: Dict[str, Dict[str, Any]] = {}
        while cursor <= final_end:
            chunk_end = min(cursor + timedelta(days=13), final_end)
            chunk_rows = self._fetch_product_sales_chunk_rows(channel_no, cursor, chunk_end)

            for row in chunk_rows:
                product_id = row.get("productId")
                if product_id is None:
                    continue
                key = str(product_id)
                existing = merged.setdefault(
                    key,
                    {
                        "product_id": key,
                        "product_name": str(row.get("productName") or ""),
                        "orders": 0,
                        "quantity": 0,
                        "pay_amount": 0.0,
                        "refund_amount": 0.0,
                    },
                )

                existing["orders"] += self._to_int(row.get("numPurchases")) or 0
                existing["quantity"] += self._to_int(row.get("productQuantity")) or 0
                existing["pay_amount"] += self._to_float(row.get("payAmount")) or 0.0
                existing["refund_amount"] += self._to_float(row.get("refundPayAmount")) or 0.0

            cursor = chunk_end + timedelta(days=1)

        return merged

    def fetch_products(self, max_items: int = 500) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        page = 1
        total_pages: Optional[int] = None

        while True:
            payload = {
                "page": page,
                "size": 100,
                "orderType": "NO",
            }
            response = self.api_client.post(
                "/v1/products/search",
                json=payload,
                headers=self._auth_headers(),
            )
            response.raise_for_status()
            body = response.json()

            groups = body.get("contents") or []
            for group in groups:
                channel_products = group.get("channelProducts") or []
                for product in channel_products:
                    image_data = product.get("representativeImage") or {}
                    origin_no = product.get("originProductNo")
                    channel_no = product.get("channelProductNo")
                    results.append(
                        {
                            "channel": "스마트스토어",
                            # stats API는 channelProductNo 기준 → 판매량 매칭을 위해 channelProductNo 우선
                            "product_id": str(channel_no or origin_no or ""),
                            "item_id": None,
                            "name": str(product.get("name") or ""),
                            "image_url": self._normalize_image_url(image_data.get("url")),
                            "product_url": self._build_product_url(
                                origin_no or channel_no,
                                str(product.get("name") or ""),
                            ),
                            "stock": self._to_int(product.get("stockQuantity")),
                            "price": self._to_int(
                                product.get("discountedPrice")
                                if product.get("discountedPrice") is not None
                                else product.get("salePrice")
                            ),
                        }
                    )

                    if len(results) >= max_items:
                        return results

            if total_pages is None:
                try:
                    total_pages = int(body.get("totalPages"))
                except (TypeError, ValueError):
                    total_pages = page

            if page >= total_pages:
                break

            page += 1

        return results
