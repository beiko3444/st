"""라즈베리파이 통합 데이터 API HTTP 클라이언트.

Pi 의 /purchase-records, /card-usages 엔드포인트를 호출.
구매내역(쿠팡/네이버 주문)과 카드사용내역을 Pi DB 에 저장/조회.

monitor_url 이 비어있으면 모든 메서드는 안전하게 빈 결과 반환 (호출부에서 fallback).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from inventory_app.models import CardUsage, PurchaseOrder, PurchaseRecord


class PiDataError(Exception):
    """Pi 데이터 API 호출 실패."""


class PiDataClient:
    def __init__(self, base_url: Optional[str], timeout: float = 10.0) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = float(timeout)
        self._client: Optional[httpx.Client] = None

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url)

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Any = None,
    ) -> Dict[str, Any]:
        if not self.is_configured:
            raise PiDataError("Pi monitor_url 미설정")
        try:
            resp = self.client.request(method, path, json=json_body, params=params)
        except httpx.HTTPError as exc:
            raise PiDataError(f"Pi 통신 실패: {exc}") from exc
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if resp.status_code < 200 or resp.status_code >= 300:
            msg = (payload or {}).get("error") if isinstance(payload, dict) else None
            raise PiDataError(msg or f"HTTP {resp.status_code}")
        return payload if isinstance(payload, dict) else {}

    # ── 구매내역 ─────────────────────────────────────────────

    def upload_purchase_records(self, records: List[PurchaseRecord], fingerprint_of) -> int:
        """records 를 Pi 로 업로드. fingerprint_of(record) → str 콜백.

        UNIQUE(fingerprint) 제약으로 중복은 자동 ignore.
        반환: 신규 INSERT 수
        """
        if not records:
            return 0
        body_records = []
        for r in records:
            body_records.append({
                "channel": r.channel,
                "order_date": r.order_date,
                "order_no": r.order_no,
                "title": r.title,
                "amount": r.amount,
                "payment_method": r.payment_method,
                "source_url": r.source_url,
                "raw_text": r.raw_text,
                "fingerprint": fingerprint_of(r),
                "imported_at": r.imported_at.isoformat() if r.imported_at else datetime.now().isoformat(),
            })
        data = self._request("POST", "/purchase-records", json_body={"records": body_records})
        return int(data.get("inserted") or 0)

    def list_purchase_records(self, channel: str = "all", limit: int = 2000) -> List[PurchaseRecord]:
        params = {"limit": int(limit)}
        if channel and channel != "all":
            params["channel"] = channel
        data = self._request("GET", "/purchase-records", params=params)
        rows = data.get("records") or []
        out: List[PurchaseRecord] = []
        for row in rows:
            try:
                imported_at = datetime.fromisoformat(row.get("imported_at"))
            except Exception:  # noqa: BLE001
                imported_at = datetime.now()
            out.append(PurchaseRecord(
                id=int(row.get("id")) if row.get("id") is not None else None,
                channel=str(row.get("channel") or ""),
                order_date=row.get("order_date"),
                order_no=row.get("order_no"),
                title=str(row.get("title") or ""),
                amount=row.get("amount"),
                payment_method=row.get("payment_method"),
                source_url=row.get("source_url"),
                raw_text=str(row.get("raw_text") or ""),
                imported_at=imported_at,
            ))
        return out

    # ── 주문 단위 (카드 매칭용) ──────────────────────────────

    def upload_purchase_orders(self, orders: List[PurchaseOrder]) -> int:
        if not orders:
            return 0
        body_orders = []
        for o in orders:
            body_orders.append({
                "channel": o.channel,
                "order_no": o.order_no,
                "order_date": o.order_date,
                "payment_total": o.payment_total,
                "item_count": o.item_count,
                "status": o.status,
                "payment_method": o.payment_method,
                "source_url": o.source_url,
                "raw_text": o.raw_text,
                "imported_at": o.imported_at.isoformat() if o.imported_at else datetime.now().isoformat(),
            })
        data = self._request("POST", "/purchase-orders", json_body={"orders": body_orders})
        return int(data.get("changed") or 0)

    def list_purchase_orders(self, channel: str = "all", limit: int = 2000) -> List[PurchaseOrder]:
        params: Dict[str, Any] = {"limit": int(limit)}
        if channel and channel != "all":
            params["channel"] = channel
        data = self._request("GET", "/purchase-orders", params=params)
        rows = data.get("orders") or []
        out: List[PurchaseOrder] = []
        for row in rows:
            try:
                imported_at = datetime.fromisoformat(row.get("imported_at"))
            except Exception:  # noqa: BLE001
                imported_at = datetime.now()
            out.append(PurchaseOrder(
                channel=str(row.get("channel") or ""),
                order_no=str(row.get("order_no") or ""),
                order_date=row.get("order_date"),
                payment_total=row.get("payment_total"),
                item_count=int(row.get("item_count") or 0),
                status=row.get("status"),
                payment_method=row.get("payment_method"),
                source_url=row.get("source_url"),
                raw_text=str(row.get("raw_text") or ""),
                imported_at=imported_at,
            ))
        return out

    # ── 카드사용내역 ─────────────────────────────────────────

    def upload_card_usages(self, items: List[CardUsage]) -> int:
        if not items:
            return 0
        body_items = []
        for it in items:
            body_items.append({
                "use_key": it.use_key or it.id,
                "id": it.id,
                "corp_num": it.corp_num,
                "card_num": it.card_num,
                "used_at": it.used_at,
                "store_name": it.store_name,
                "amount": it.amount,
                "category": it.category,
                "memo": it.memo,
                "reviewed": it.reviewed,
                "coupang_purchase_id": it.coupang_purchase_id,
                "raw": it.raw,
            })
        data = self._request("POST", "/card-usages", json_body={"items": body_items})
        return int(data.get("changed") or 0)

    def list_card_usages(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        card_num: Optional[str] = None,
        limit: int = 5000,
    ) -> List[CardUsage]:
        params: Dict[str, Any] = {"limit": int(limit)}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        if card_num:
            params["card_num"] = card_num
        data = self._request("GET", "/card-usages", params=params)
        rows = data.get("items") or []
        out: List[CardUsage] = []
        for row in rows:
            raw = row.get("raw")
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except Exception:  # noqa: BLE001
                    raw = None
            out.append(CardUsage(
                id=row.get("use_key"),
                corp_num=row.get("corp_num"),
                card_num=row.get("card_num"),
                use_key=row.get("use_key"),
                used_at=row.get("used_at"),
                store_name=row.get("store_name"),
                amount=row.get("amount"),
                category=row.get("category"),
                memo=row.get("memo"),
                reviewed=bool(row.get("reviewed")),
                coupang_purchase_id=row.get("coupang_purchase_id"),
                raw=raw if isinstance(raw, dict) else None,
            ))
        return out

    def patch_card_usage(
        self,
        use_key: str,
        *,
        memo: Optional[str] = None,
        category: Optional[str] = None,
        reviewed: Optional[bool] = None,
        coupang_purchase_id: Optional[str] = None,
        clear_memo: bool = False,
        clear_coupang_match: bool = False,
    ) -> None:
        body: Dict[str, Any] = {}
        if clear_memo:
            body["clear_memo"] = True
        elif memo is not None:
            body["memo"] = memo
        if category is not None:
            body["category"] = category
        if reviewed is not None:
            body["reviewed"] = bool(reviewed)
        if clear_coupang_match:
            body["clear_coupang_match"] = True
        elif coupang_purchase_id is not None:
            body["coupang_purchase_id"] = coupang_purchase_id
        if not body:
            return
        self._request("PATCH", f"/card-usages/{use_key}", json_body=body)


__all__ = ["PiDataClient", "PiDataError"]
