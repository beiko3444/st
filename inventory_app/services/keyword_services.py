from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Tuple

from inventory_app.config import AppConfig
from inventory_app.connectors.smartstore import SmartStoreConnector


@dataclass
class KeywordRevenueRow:
    keyword: str
    pay_amount: float
    orders: int
    inflow: int | None
    conversion_rate: float | None
    avg_order_value: float | None
    source: str


@dataclass
class KeywordRevenueSnapshot:
    period_days: int
    generated_at: datetime
    rows: List[KeywordRevenueRow]
    notes: List[str]


class NaverKeywordRevenueService:
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

    @staticmethod
    def _to_int(value: Any) -> int | None:
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
    def _to_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _pick_first_str(row: Dict[str, Any], keys: List[str]) -> str | None:
        for key in keys:
            raw = row.get(key)
            if raw is None:
                continue
            text = str(raw).strip()
            if text:
                return text
        return None

    @classmethod
    def _pick_first_int(cls, row: Dict[str, Any], keys: List[str]) -> int | None:
        for key in keys:
            converted = cls._to_int(row.get(key))
            if converted is not None:
                return converted
        return None

    @classmethod
    def _pick_first_float(cls, row: Dict[str, Any], keys: List[str]) -> float | None:
        for key in keys:
            converted = cls._to_float(row.get(key))
            if converted is not None:
                return converted
        return None

    @staticmethod
    def _keyword_candidates(row: Dict[str, Any]) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        for value in row.values():
            if isinstance(value, dict):
                candidates.append(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        candidates.append(item)
        return candidates

    @classmethod
    def _aggregate_keyword_metrics(
        cls,
        rows: List[Dict[str, Any]],
        source: str,
    ) -> List[KeywordRevenueRow]:
        keyword_map: Dict[str, Dict[str, Any]] = {}

        keyword_keys = [
            "keyword",
            "refKeyword",
            "searchKeyword",
            "keywordName",
            "query",
            "searchTerm",
            "term",
        ]
        pay_keys = ["payAmount", "paymentAmount", "salesAmount", "orderAmount", "revenue"]
        orders_keys = ["numPurchases", "purchaseCount", "orderCount", "paymentCount", "orders"]
        inflow_keys = ["numInteractions", "inflowCount", "visitCount", "clickCount", "impressionClickCount"]
        refund_keys = ["refundPayAmount", "refundAmount"]
        conversion_keys = ["conversionRate", "purchaseRate", "cvr"]

        def upsert(metric_row: Dict[str, Any]) -> None:
            keyword = cls._pick_first_str(metric_row, keyword_keys)
            if not keyword:
                return

            gross = cls._pick_first_float(metric_row, pay_keys) or 0.0
            refund = cls._pick_first_float(metric_row, refund_keys) or 0.0
            pay_amount = max(0.0, gross - refund)
            orders = cls._pick_first_int(metric_row, orders_keys) or 0
            inflow = cls._pick_first_int(metric_row, inflow_keys)
            conversion = cls._pick_first_float(metric_row, conversion_keys)

            item = keyword_map.setdefault(
                keyword,
                {
                    "pay_amount": 0.0,
                    "orders": 0,
                    "inflow": 0,
                    "has_inflow": False,
                    "conversion_rates": [],
                },
            )
            item["pay_amount"] += pay_amount
            item["orders"] += max(0, int(orders))
            if inflow is not None:
                item["inflow"] += max(0, int(inflow))
                item["has_inflow"] = True
            if conversion is not None:
                item["conversion_rates"].append(float(conversion))

        for row in rows:
            upsert(row)
            for child in cls._keyword_candidates(row):
                upsert(child)

        aggregated: List[KeywordRevenueRow] = []
        for keyword, item in keyword_map.items():
            pay_amount = float(item["pay_amount"])
            orders = int(item["orders"])
            inflow = int(item["inflow"]) if bool(item["has_inflow"]) else None
            conversion_rate: float | None
            if item["conversion_rates"]:
                conversion_rate = float(sum(item["conversion_rates"]) / len(item["conversion_rates"]))
            elif inflow and inflow > 0:
                conversion_rate = (orders / inflow) * 100.0
            else:
                conversion_rate = None
            avg_order_value = (pay_amount / orders) if orders > 0 else None

            aggregated.append(
                KeywordRevenueRow(
                    keyword=keyword,
                    pay_amount=pay_amount,
                    orders=orders,
                    inflow=inflow,
                    conversion_rate=conversion_rate,
                    avg_order_value=avg_order_value,
                    source=source,
                )
            )

        aggregated.sort(key=lambda row: (row.pay_amount, row.orders), reverse=True)
        return aggregated[:200]

    def fetch(self, period_days: int) -> Tuple[KeywordRevenueSnapshot, List[str]]:
        days = max(1, int(period_days))
        warnings: List[str] = []
        notes: List[str] = []

        connectors = [self.naver_stats]
        if (
            self.naver_stats.client_id != self.naver_fallback.client_id
            or self.naver_stats.client_secret != self.naver_fallback.client_secret
            or self.naver_stats.token_type != self.naver_fallback.token_type
        ):
            connectors.append(self.naver_fallback)

        last_error: str | None = None
        for connector in connectors:
            try:
                rows = connector.fetch_search_channel_keyword_rows(days=days)
                parsed = self._aggregate_keyword_metrics(rows, source="검색 채널 키워드")
                notes.append(
                    f"검색 채널 키워드 API 기준 최근 {days}일 데이터를 집계했습니다."
                )
                return (
                    KeywordRevenueSnapshot(
                        period_days=days,
                        generated_at=datetime.now(),
                        rows=parsed,
                        notes=notes,
                    ),
                    warnings,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)

        for connector in connectors:
            try:
                rows = connector.fetch_product_search_keyword_rows(days=days)
                parsed = self._aggregate_keyword_metrics(rows, source="상품별 키워드(보조)")
                notes.append(
                    "검색 채널 키워드 API 조회 실패로 상품별 키워드 API 데이터를 보조로 사용했습니다."
                )
                return (
                    KeywordRevenueSnapshot(
                        period_days=days,
                        generated_at=datetime.now(),
                        rows=parsed,
                        notes=notes,
                    ),
                    warnings,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)

        warnings.append(f"네이버 키워드 매출 조회 실패: {last_error or 'unknown error'}")
        return (
            KeywordRevenueSnapshot(
                period_days=days,
                generated_at=datetime.now(),
                rows=[],
                notes=["키워드 매출 데이터를 가져오지 못했습니다."],
            ),
            warnings,
        )
