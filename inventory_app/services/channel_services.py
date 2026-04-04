from __future__ import annotations

from datetime import datetime
from typing import List, Tuple

from inventory_app.config import AppConfig
from inventory_app.connectors.coupang import CoupangRocketConnector
from inventory_app.connectors.smartstore import SmartStoreConnector
from inventory_app.models import ChannelProduct


class NaverChannelService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
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

    def fetch(self) -> Tuple[List[ChannelProduct], List[str]]:
        synced_at = datetime.now()
        warnings: List[str] = []

        raw_rows = self.smartstore.fetch_products(max_items=self.config.max_products)
        rows: List[ChannelProduct] = []
        for raw in raw_rows:
            rows.append(
                ChannelProduct(
                    serial=0,
                    product_id=str(raw.get("product_id") or ""),
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

        for index, row in enumerate(rows, start=1):
            row.serial = index
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
                last_error = f"Naver sales fetch failed: {exc}"
        if last_error:
            warnings.append(last_error)
        return None


class CoupangChannelService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.coupang = CoupangRocketConnector(
            vendor_id=config.coupang_vendor_id,
            access_key=config.coupang_access_key,
            secret_key=config.coupang_secret_key,
            timeout_seconds=config.timeout_seconds,
        )

    def fetch(self) -> Tuple[List[ChannelProduct], List[str]]:
        synced_at = datetime.now()
        warnings: List[str] = []

        raw_rows = self.coupang.fetch_products(max_products=self.config.max_products)
        rows: List[ChannelProduct] = []
        for raw in raw_rows:
            rows.append(
                ChannelProduct(
                    serial=0,
                    product_id=str(raw.get("product_id") or ""),
                    name=str(raw.get("name") or ""),
                    image_url=raw.get("image_url"),
                    product_url=raw.get("product_url"),
                    stock=raw.get("stock"),
                    sales=raw.get("sales"),
                    price=raw.get("price"),
                    synced_at=synced_at,
                )
            )

        for index, row in enumerate(rows, start=1):
            row.serial = index
        return rows, warnings
