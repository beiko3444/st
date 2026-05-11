"""라즈베리파이 마스터 상품 API HTTP 클라이언트.

Pi 의 /masters, /master-links 엔드포인트를 호출해 MasterProduct/ChannelMasterLink 로 파싱.
네트워크/응답 오류는 MasterRemoteError 로 통일.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

import httpx

from inventory_app.models import (
    ChannelMasterLink,
    MasterProduct,
    StockInboundEntry,
    StockInboundSummary,
)


class MasterRemoteError(Exception):
    """Pi 마스터 API 호출 실패."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        path: str = "",
        method: str = "",
    ) -> None:
        super().__init__(message)
        self.status = int(status)
        self.path = path
        self.method = method


def _parse_datetime(value: Any) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except Exception:  # noqa: BLE001
        return datetime.now()


def _row_to_master(row: Dict[str, Any]) -> MasterProduct:
    return MasterProduct(
        id=int(row.get("id") or 0),
        name=str(row.get("name") or ""),
        unit_cost=(int(row["unit_cost"]) if row.get("unit_cost") is not None else None),
        memo=(str(row["memo"]) if row.get("memo") is not None else None),
        representative_channel=(
            str(row["representative_channel"])
            if row.get("representative_channel")
            else None
        ),
        representative_product_key=(
            str(row["representative_product_key"])
            if row.get("representative_product_key")
            else None
        ),
        created_at=_parse_datetime(row.get("created_at")),
        updated_at=_parse_datetime(row.get("updated_at")),
    )


def _row_to_link(row: Dict[str, Any]) -> ChannelMasterLink:
    return ChannelMasterLink(
        channel=str(row.get("channel") or ""),
        product_key=str(row.get("product_key") or ""),
        master_id=int(row.get("master_id") or 0),
        multiplier=max(1, int(row.get("multiplier") or 1)),
        created_at=_parse_datetime(row.get("created_at")),
        updated_at=_parse_datetime(row.get("updated_at")),
    )


def _row_to_stock_inbound(row: Dict[str, Any]) -> StockInboundEntry:
    return StockInboundEntry(
        id=(int(row["id"]) if row.get("id") is not None else None),
        receipt_date=str(row.get("receipt_date") or ""),
        master_id=int(row.get("master_id") or 0),
        channel=str(row.get("channel") or ""),
        input_qty=max(0, int(row.get("input_qty") or 0)),
        remaining_qty=max(0, int(row.get("remaining_qty") or 0)),
        last_consumed_at=_parse_datetime(row.get("last_consumed_at"))
        if row.get("last_consumed_at")
        else None,
        created_at=_parse_datetime(row.get("created_at")),
        updated_at=_parse_datetime(row.get("updated_at")),
    )


def _row_to_stock_inbound_summary(row: Dict[str, Any]) -> StockInboundSummary:
    return StockInboundSummary(
        master_id=int(row.get("master_id") or 0),
        channel=str(row.get("channel") or ""),
        pending_qty=max(0, int(row.get("pending_qty") or 0)),
        last_consumed_at=_parse_datetime(row.get("last_consumed_at"))
        if row.get("last_consumed_at")
        else None,
        updated_at=_parse_datetime(row.get("updated_at")),
    )


class MasterRemoteClient:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = float(timeout)
        self._client: Optional[httpx.Client] = None

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
        try:
            resp = self.client.request(method, path, json=json_body, params=params)
        except httpx.HTTPError as exc:
            raise MasterRemoteError(
                f"Pi 마스터 서버 통신 실패: {exc}",
                status=0,
                path=path,
                method=method,
            ) from exc

        payload: Any = None
        try:
            payload = resp.json()
        except ValueError:
            payload = None

        if resp.status_code < 200 or resp.status_code >= 300:
            msg = ""
            if isinstance(payload, dict):
                msg = str(payload.get("error") or "")
            if not msg:
                msg = f"HTTP {resp.status_code}"
            raise MasterRemoteError(
                msg,
                status=resp.status_code,
                path=path,
                method=method,
            )

        return payload if isinstance(payload, dict) else {}

    # -- masters --------------------------------------------------------

    def list_masters(self) -> List[MasterProduct]:
        data = self._request("GET", "/masters")
        rows = data.get("masters") or []
        return [_row_to_master(r) for r in rows if isinstance(r, dict)]

    def get_master(self, master_id: int) -> Optional[MasterProduct]:
        try:
            data = self._request("GET", f"/masters/{int(master_id)}")
        except MasterRemoteError as exc:
            if exc.status == 404:
                return None
            raise
        row = data.get("master")
        return _row_to_master(row) if isinstance(row, dict) else None

    def create_master(
        self,
        name: str,
        unit_cost: Optional[int] = None,
        memo: Optional[str] = None,
    ) -> MasterProduct:
        body: Dict[str, Any] = {"name": str(name or "").strip()}
        if unit_cost is not None:
            body["unit_cost"] = int(unit_cost)
        if memo is not None:
            body["memo"] = str(memo)
        data = self._request("POST", "/masters", json_body=body)
        row = data.get("master") or {}
        return _row_to_master(row)

    def update_master(
        self,
        master_id: int,
        *,
        name: Optional[str] = None,
        unit_cost: Optional[int] = None,
        memo: Optional[str] = None,
        clear_unit_cost: bool = False,
        clear_memo: bool = False,
    ) -> MasterProduct:
        body: Dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if clear_unit_cost:
            body["clear_unit_cost"] = True
        elif unit_cost is not None:
            body["unit_cost"] = int(unit_cost)
        if clear_memo:
            body["clear_memo"] = True
        elif memo is not None:
            body["memo"] = memo
        data = self._request("PATCH", f"/masters/{int(master_id)}", json_body=body)
        row = data.get("master") or {}
        return _row_to_master(row)

    def delete_master(self, master_id: int) -> None:
        self._request("DELETE", f"/masters/{int(master_id)}")

    def set_master_representative(
        self,
        master_id: int,
        channel: Optional[str],
        product_key: Optional[str],
    ) -> MasterProduct:
        body: Dict[str, Any] = {
            "channel": (channel if channel else None),
            "product_key": (product_key if product_key else None),
        }
        data = self._request(
            "PUT", f"/masters/{int(master_id)}/representative", json_body=body
        )
        row = data.get("master") or {}
        return _row_to_master(row)

    # -- links ----------------------------------------------------------

    def list_all_links(self) -> List[ChannelMasterLink]:
        data = self._request("GET", "/master-links")
        rows = data.get("links") or []
        return [_row_to_link(r) for r in rows if isinstance(r, dict)]

    def link(
        self,
        channel: str,
        product_key: str,
        master_id: int,
        multiplier: int = 1,
    ) -> ChannelMasterLink:
        body = {
            "channel": channel,
            "product_key": product_key,
            "master_id": int(master_id),
            "multiplier": max(1, int(multiplier)),
        }
        data = self._request("POST", "/master-links", json_body=body)
        row = data.get("link") or {}
        return _row_to_link(row)

    def unlink(self, channel: str, product_key: str) -> None:
        self._request(
            "DELETE",
            "/master-links",
            params={"channel": channel, "product_key": product_key},
        )

    def set_link_multiplier(
        self,
        channel: str,
        product_key: str,
        multiplier: int,
    ) -> ChannelMasterLink:
        body = {
            "channel": channel,
            "product_key": product_key,
            "multiplier": max(1, int(multiplier)),
        }
        data = self._request("PUT", "/master-links/multiplier", json_body=body)
        row = data.get("link") or {}
        return _row_to_link(row)

    # -- stock inbounds ------------------------------------------------

    def list_stock_inbounds(
        self,
        *,
        master_id: Optional[int] = None,
        channel: Optional[str] = None,
    ) -> List[StockInboundEntry]:
        params: Dict[str, Any] = {}
        if master_id is not None:
            params["master_id"] = int(master_id)
        if channel:
            params["channel"] = str(channel)
        data = self._request("GET", "/stock-inbounds", params=params or None)
        rows = data.get("items") or []
        return [_row_to_stock_inbound(r) for r in rows if isinstance(r, dict)]

    def list_stock_inbound_summaries(
        self,
        *,
        master_id: Optional[int] = None,
        channel: Optional[str] = None,
    ) -> List[StockInboundSummary]:
        params: Dict[str, Any] = {}
        if master_id is not None:
            params["master_id"] = int(master_id)
        if channel:
            params["channel"] = str(channel)
        data = self._request("GET", "/stock-inbounds", params=params or None)
        rows = data.get("summaries") or []
        return [_row_to_stock_inbound_summary(r) for r in rows if isinstance(r, dict)]

    def add_stock_inbound(
        self,
        *,
        master_id: int,
        channel: str,
        quantity: int,
    ) -> StockInboundEntry:
        body = {
            "receipt_date": date.today().isoformat(),
            "master_id": int(master_id),
            "channel": str(channel or "").strip(),
            "quantity": int(quantity),
        }
        data = self._request("POST", "/stock-inbounds", json_body=body)
        row = data.get("item") or {}
        return _row_to_stock_inbound(row)

    def reconcile_stock_inbounds(self, items: List[Dict[str, Any]]) -> List[StockInboundSummary]:
        data = self._request(
            "POST",
            "/stock-inbounds/reconcile",
            json_body={"items": items},
        )
        rows = data.get("summaries") or []
        return [_row_to_stock_inbound_summary(r) for r in rows if isinstance(r, dict)]
