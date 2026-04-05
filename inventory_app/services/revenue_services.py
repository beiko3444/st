from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from inventory_app.config import AppConfig
from inventory_app.connectors.coupang import CoupangRocketConnector
from inventory_app.connectors.smartstore import SmartStoreConnector
from inventory_app.services.local_cache import ChannelProductCache


@dataclass
class RevenueChannelSummary:
    channel: str
    gross: float
    refund: float
    net: float
    orders: int
    estimated: bool
    note: str


@dataclass
class RevenueProductSummary:
    channel: str
    product_id: str
    name: str
    image_url: str | None
    orders: int
    gross: float
    refund: float
    net: float
    estimated: bool


@dataclass
class RevenueSnapshot:
    period_days: int
    generated_at: datetime
    summaries: List[RevenueChannelSummary]
    products: List[RevenueProductSummary]
    notes: List[str]


class RevenueComparisonService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.naver_stats = SmartStoreConnector(
            client_id=config.smartstore_stats_client_id,
            client_secret=config.smartstore_stats_client_secret,
            token_type=config.smartstore_stats_token_type,
            timeout_seconds=config.timeout_seconds,
        )
        self.naver_fallback = SmartStoreConnector(
            client_id=config.smartstore_client_id,
            client_secret=config.smartstore_client_secret,
            token_type=config.smartstore_token_type,
            timeout_seconds=config.timeout_seconds,
        )
        self.coupang = CoupangRocketConnector(
            vendor_id=config.coupang_vendor_id,
            access_key=config.coupang_access_key,
            secret_key=config.coupang_secret_key,
            timeout_seconds=config.timeout_seconds,
        )
        self.cache = ChannelProductCache()

    def _load_cached_image_map(self, channel: str) -> Dict[str, str]:
        image_map: Dict[str, str] = {}
        try:
            rows = self.cache.load_rows(channel)
        except Exception:  # noqa: BLE001
            return image_map

        for row in rows:
            key = str(row.product_id or "").strip()
            if not key or key in image_map:
                continue
            image_url = str(row.image_url or "").strip()
            if image_url:
                image_map[key] = image_url
        return image_map

    def fetch(self, period_days: int) -> Tuple[RevenueSnapshot, List[str]]:
        days = max(1, int(period_days))
        warnings: List[str] = []
        summaries: List[RevenueChannelSummary] = []
        products: List[RevenueProductSummary] = []
        notes: List[str] = []

        naver_summary, naver_products, naver_warning = self._fetch_naver_revenue(days)
        if naver_warning:
            warnings.append(naver_warning)
        else:
            summaries.append(naver_summary)
            products.extend(naver_products)
            notes.append(
                f"네이버 매출은 최근 {days}일 통계 API의 "
                "payAmount/refundPayAmount 실제값 기준입니다."
            )

        coupang_summary, coupang_products, coupang_warning = self._fetch_coupang_revenue()
        if coupang_warning:
            warnings.append(coupang_warning)
        else:
            summaries.append(coupang_summary)
            products.extend(coupang_products)
            notes.append(
                "쿠팡 매출은 인벤토리 API의 최근 30일 판매량 "
                "(SALES_COUNT_LAST_THIRTY_DAYS) x 판매가 기반 추정값입니다."
            )

        products.sort(key=lambda row: row.net, reverse=True)

        snapshot = RevenueSnapshot(
            period_days=days,
            generated_at=datetime.now(),
            summaries=summaries,
            products=products[:200],
            notes=notes,
        )
        return snapshot, warnings

    def _fetch_naver_revenue(
        self,
        days: int,
    ) -> Tuple[RevenueChannelSummary, List[RevenueProductSummary], str | None]:
        connectors = [self.naver_stats]
        if (
            self.naver_stats.client_id != self.naver_fallback.client_id
            or self.naver_stats.client_secret != self.naver_fallback.client_secret
            or self.naver_stats.token_type != self.naver_fallback.token_type
        ):
            connectors.append(self.naver_fallback)

        last_error: str | None = None
        naver_image_map = self._load_cached_image_map("naver")
        for connector in connectors:
            try:
                revenue_map = connector.fetch_product_sales_revenue(days=days)
                product_rows: List[RevenueProductSummary] = []
                total_gross = 0.0
                total_refund = 0.0
                total_orders = 0

                for product_id, item in revenue_map.items():
                    gross = float(item.get("pay_amount") or 0.0)
                    refund = float(item.get("refund_amount") or 0.0)
                    net = gross - refund
                    orders = int(item.get("orders") or 0)
                    total_gross += gross
                    total_refund += refund
                    total_orders += orders

                    product_rows.append(
                        RevenueProductSummary(
                            channel="네이버",
                            product_id=product_id,
                            name=str(item.get("product_name") or ""),
                            image_url=naver_image_map.get(product_id),
                            orders=orders,
                            gross=gross,
                            refund=refund,
                            net=net,
                            estimated=False,
                        )
                    )

                summary = RevenueChannelSummary(
                    channel="네이버",
                    gross=total_gross,
                    refund=total_refund,
                    net=total_gross - total_refund,
                    orders=total_orders,
                    estimated=False,
                    note="실제 매출",
                )
                return summary, product_rows, None
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)

        return (
            RevenueChannelSummary(
                channel="네이버",
                gross=0.0,
                refund=0.0,
                net=0.0,
                orders=0,
                estimated=False,
                note="조회 실패",
            ),
            [],
            f"네이버 매출 조회 실패: {last_error or 'unknown error'}",
        )

    def _fetch_coupang_revenue(
        self,
    ) -> Tuple[RevenueChannelSummary, List[RevenueProductSummary], str | None]:
        try:
            raw_rows = self.coupang.fetch_products(max_products=self.config.max_products)

            by_product: Dict[str, Dict[str, Any]] = {}
            for row in raw_rows:
                product_id = str(row.get("product_id") or "")
                if not product_id:
                    continue
                name = str(row.get("name") or "")
                sales = row.get("sales")
                price = row.get("price")
                if sales is None or price is None:
                    continue

                try:
                    sales_i = int(sales)
                    price_i = int(price)
                except (TypeError, ValueError):
                    continue

                gross = float(max(0, sales_i) * max(0, price_i))
                item = by_product.setdefault(
                    product_id,
                    {
                        "name": name,
                        "image_url": row.get("image_url"),
                        "orders": 0,
                        "gross": 0.0,
                    },
                )
                if not item["name"] and name:
                    item["name"] = name
                if not item.get("image_url") and row.get("image_url"):
                    item["image_url"] = row.get("image_url")
                item["orders"] += max(0, sales_i)
                item["gross"] += gross

            product_rows: List[RevenueProductSummary] = []
            total_gross = 0.0
            total_orders = 0
            for product_id, item in by_product.items():
                gross = float(item.get("gross") or 0.0)
                orders = int(item.get("orders") or 0)
                total_gross += gross
                total_orders += orders
                product_rows.append(
                    RevenueProductSummary(
                        channel="쿠팡",
                        product_id=product_id,
                        name=str(item.get("name") or ""),
                        image_url=(str(item.get("image_url")) if item.get("image_url") else None),
                        orders=orders,
                        gross=gross,
                        refund=0.0,
                        net=gross,
                        estimated=True,
                    )
                )

            summary = RevenueChannelSummary(
                channel="쿠팡",
                gross=total_gross,
                refund=0.0,
                net=total_gross,
                orders=total_orders,
                estimated=True,
                note="추정 매출(최근30일 판매량x가격)",
            )
            return summary, product_rows, None
        except Exception as exc:  # noqa: BLE001
            return (
                RevenueChannelSummary(
                    channel="쿠팡",
                    gross=0.0,
                    refund=0.0,
                    net=0.0,
                    orders=0,
                    estimated=True,
                    note="조회 실패",
                ),
                [],
                f"쿠팡 매출 조회 실패: {exc}",
            )
