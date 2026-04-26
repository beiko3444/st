"""외부 card-api-service 클라이언트.

docs/card-api-relocation-design.md §5 의 API 계약 그대로 호출.

- GET    /v1/card-usages
- PATCH  /v1/card-usages/{id}
- POST   /v1/card-usages/sync
- GET    /v1/coupang-purchases
- POST   /v1/coupang-purchases/sync
- POST   /v1/coupang-purchases/match

인증:
- HTTP Header `Authorization: Bearer {service_token}` (디자인 §6)

사용:
    client = CardApiClient.from_config(config)
    if not client.is_configured():
        raise CardApiUnavailable(...)
    res = client.list_card_usages(start="2026-01-01", end="2026-04-30")
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence

import httpx

from inventory_app.models import CardUsage


class CardApiError(RuntimeError):
    """카드 API 호출 실패."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        path: str = "",
        method: str = "",
        body: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.path = path
        self.method = method
        self.body = body

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message": str(self),
            "status": self.status,
            "path": self.path,
            "method": self.method,
            "body": (self.body or "")[:500],
        }


class CardApiUnavailable(CardApiError):
    """설정 누락 등 호출 자체가 불가능한 경우."""


@dataclass
class CardListPage:
    items: List[CardUsage]
    page: int
    page_size: int
    total_count: int
    total_pages: int
    summary: Dict[str, Any]
    raw: Dict[str, Any]


def _to_int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    text = str(v).strip().replace(",", "")
    try:
        return int(text)
    except ValueError:
        try:
            return int(float(text))
        except ValueError:
            return None


def _to_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    text = str(v).strip()
    return text or None


def _normalize_card_usage(raw: Mapping[str, Any]) -> CardUsage:
    """API 응답 row → CardUsage. 필드명 snake_case 변환."""

    def first(*keys: str) -> Any:
        for k in keys:
            if k in raw and raw[k] is not None and raw[k] != "":
                return raw[k]
        return None

    return CardUsage(
        id=_to_str(first("id", "_id", "rowId")),
        corp_num=_to_str(first("corpNum", "corp_num")),
        card_num=_to_str(first("cardNum", "card_num", "cardNumber")),
        use_key=_to_str(first("useKey", "use_key")),
        used_at=_to_str(first("usedAt", "used_at", "usedDateTime")),
        store_name=_to_str(first("storeName", "store_name", "merchantName")),
        amount=_to_int(first("amount", "totalAmount", "approvedAmount")),
        category=_to_str(first("category", "categoryName")),
        memo=_to_str(first("memo", "note")),
        reviewed=bool(first("reviewed", "isReviewed") or False),
        coupang_purchase_id=_to_str(first("coupangPurchaseId", "coupang_purchase_id")),
        raw=dict(raw),
    )


def _extract_list(payload: Any) -> List[Mapping[str, Any]]:
    """{items: [...]} 또는 [...] 둘 다 지원."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, Mapping)]
    if isinstance(payload, Mapping):
        for key in ("items", "data", "rows", "list", "content"):
            v = payload.get(key)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, Mapping)]
    return []


class CardApiClient:
    """카드 API 호출 클라이언트.

    config 의 `card_api.base_url`, `card_api.service_token` 사용.
    base_url 비어있으면 is_configured()=False.
    """

    def __init__(
        self,
        base_url: str,
        service_token: str,
        *,
        timeout_seconds: int = 30,
    ) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.service_token = (service_token or "").strip()
        self.timeout_seconds = max(3, int(timeout_seconds))
        self._client: Optional[httpx.Client] = None

    @classmethod
    def from_config(cls, config: Any) -> "CardApiClient":
        return cls(
            base_url=str(getattr(config, "card_api_base_url", "") or ""),
            service_token=str(getattr(config, "card_api_service_token", "") or ""),
            timeout_seconds=int(getattr(config, "timeout_seconds", 30) or 30),
        )

    def is_configured(self) -> bool:
        return bool(self.base_url)

    def config_summary(self) -> Dict[str, Any]:
        return {
            "baseUrl": self.base_url,
            "configured": self.is_configured(),
            "hasServiceToken": bool(self.service_token),
        }

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout_seconds)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    def __enter__(self) -> "CardApiClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ----- 공통 -----

    def _headers(self) -> Dict[str, str]:
        h: Dict[str, str] = {"Accept": "application/json"}
        if self.service_token:
            h["Authorization"] = f"Bearer {self.service_token}"
        return h

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        body: Any = None,
    ) -> Any:
        if not self.is_configured():
            raise CardApiUnavailable(
                "card_api 설정이 비어 있습니다. credentials.json 의 card_api.base_url 을 지정하세요.",
                path=path, method=method,
            )
        try:
            resp = self.client.request(
                method.upper(),
                path,
                headers=self._headers(),
                params=dict(params) if params else None,
                json=body if body is not None else None,
            )
        except httpx.RequestError as exc:
            raise CardApiError(
                f"카드 API 요청 실패: {exc}",
                status=0, path=path, method=method.upper(),
            ) from exc

        text = resp.text or ""
        if resp.status_code >= 400:
            raise CardApiError(
                f"카드 API 오류 ({method.upper()} {path}, status {resp.status_code})",
                status=resp.status_code, path=path, method=method.upper(),
                body=text,
            )
        if not text:
            return {}
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            raise CardApiError(
                f"카드 API JSON 파싱 실패: {text[:200]}",
                status=resp.status_code, path=path, method=method.upper(),
                body=text,
            ) from exc

    # ----- 카드 사용내역 -----

    def list_card_usages(
        self,
        *,
        page: int = 1,
        page_size: int = 100,
        card_num: Optional[str] = None,
        store_name: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> CardListPage:
        params: Dict[str, Any] = {"page": page, "pageSize": page_size}
        if card_num:
            params["cardNum"] = card_num
        if store_name:
            params["storeName"] = store_name
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date

        payload = self._request("GET", "/v1/card-usages", params=params)
        rows = _extract_list(payload)
        items = [_normalize_card_usage(r) for r in rows]

        meta: Dict[str, Any] = payload if isinstance(payload, dict) else {}
        return CardListPage(
            items=items,
            page=int(meta.get("page", page) or page),
            page_size=int(meta.get("pageSize", page_size) or page_size),
            total_count=int(meta.get("totalCount", len(items)) or len(items)),
            total_pages=int(meta.get("totalPages", 1) or 1),
            summary=dict(meta.get("summary", {}) or {}),
            raw=meta,
        )

    def update_card_usage(
        self,
        usage_id: str,
        *,
        memo: Optional[str] = None,
        category: Optional[str] = None,
        reviewed: Optional[bool] = None,
        coupang_purchase_id: Optional[str] = None,
    ) -> CardUsage:
        body: Dict[str, Any] = {}
        if memo is not None:
            body["memo"] = memo
        if category is not None:
            body["category"] = category
        if reviewed is not None:
            body["reviewed"] = reviewed
        if coupang_purchase_id is not None:
            body["coupangPurchaseId"] = coupang_purchase_id

        payload = self._request("PATCH", f"/v1/card-usages/{usage_id}", body=body)
        if isinstance(payload, dict) and any(payload.values()):
            return _normalize_card_usage(payload)
        return _normalize_card_usage({"id": usage_id, **body})

    def sync_card_usages(
        self,
        *,
        start_date: str,
        end_date: str,
        card_num: Optional[str] = None,
        refresh_before_fetch: bool = False,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "startDate": start_date,
            "endDate": end_date,
            "refreshBeforeFetch": bool(refresh_before_fetch),
        }
        if card_num:
            body["cardNum"] = card_num
        return self._request("POST", "/v1/card-usages/sync", body=body) or {}

    # ----- 쿠팡 구매내역 (디자인 §5.4) -----

    def list_coupang_purchases(
        self,
        *,
        page: int = 1,
        page_size: int = 100,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"page": page, "pageSize": page_size}
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date
        return self._request("GET", "/v1/coupang-purchases", params=params) or {}

    def sync_coupang_purchases(self, *, start_date: str, end_date: str) -> Dict[str, Any]:
        return self._request(
            "POST", "/v1/coupang-purchases/sync",
            body={"startDate": start_date, "endDate": end_date},
        ) or {}

    def match_coupang_purchases(self, *, start_date: str, end_date: str) -> Dict[str, Any]:
        return self._request(
            "POST", "/v1/coupang-purchases/match",
            body={"startDate": start_date, "endDate": end_date},
        ) or {}


__all__ = [
    "CardApiClient",
    "CardApiError",
    "CardApiUnavailable",
    "CardListPage",
]
