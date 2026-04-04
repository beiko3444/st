from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from typing import List, Tuple

from inventory_app.config import AppConfig
from inventory_app.connectors.coupang import CoupangRocketConnector
from inventory_app.connectors.smartstore import SmartStoreConnector
from inventory_app.models import UnifiedProduct


@dataclass
class _ChannelProductRow:
    name: str
    image_url: str | None
    product_url: str | None
    stock: int | None
    sales: int | None
    price: int | None
    product_id: str
    item_id: str | None


class InventoryAggregator:
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
        self.coupang = CoupangRocketConnector(
            vendor_id=config.coupang_vendor_id,
            access_key=config.coupang_access_key,
            secret_key=config.coupang_secret_key,
            timeout_seconds=config.timeout_seconds,
        )

    @staticmethod
    def _normalize_text(text: str) -> str:
        value = text.lower()
        value = re.sub(r"\s+", " ", value).strip()
        value = value.replace("베이코", "")
        value = value.replace("스마트스토어", "")
        value = value.replace("쿠팡", "")
        value = value.replace("로켓", "")
        value = re.sub(r"[^0-9a-z가-힣]+", "", value)
        return value

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        words = re.findall(r"[0-9a-z가-힣]+", text.lower())
        stopwords = {
            "베이코",
            "스마트스토어",
            "쿠팡",
            "로켓",
            "낚시",
            "미끼",
            "상온보관",
            "반건조",
            "개",
            "입",
        }
        return {word for word in words if word and word not in stopwords}

    def _name_similarity(self, left_name: str, right_name: str) -> float:
        left_normalized = self._normalize_text(left_name)
        right_normalized = self._normalize_text(right_name)
        if not left_normalized or not right_normalized:
            return 0.0

        direct_ratio = SequenceMatcher(None, left_normalized, right_normalized).ratio()

        left_tokens = self._tokenize(left_name)
        right_tokens = self._tokenize(right_name)
        if not left_tokens or not right_tokens:
            return direct_ratio

        intersection = len(left_tokens & right_tokens)
        union = len(left_tokens | right_tokens)
        jaccard = intersection / union if union else 0.0
        min_cover = intersection / max(1, min(len(left_tokens), len(right_tokens)))
        token_ratio = (jaccard * 0.7) + (min_cover * 0.3)

        return max(direct_ratio, token_ratio)

    def _collect_smartstore_rows(self) -> List[_ChannelProductRow]:
        rows: List[_ChannelProductRow] = []
        raw_rows = self.smartstore.fetch_products(max_items=self.config.max_products)
        for row in raw_rows:
            rows.append(
                _ChannelProductRow(
                    name=str(row.get("name") or ""),
                    image_url=row.get("image_url"),
                    product_url=row.get("product_url"),
                    stock=row.get("stock"),
                    sales=row.get("sales"),
                    price=row.get("price"),
                    product_id=str(row.get("product_id") or ""),
                    item_id=(str(row.get("item_id")) if row.get("item_id") else None),
                )
            )
        return rows

    def _enrich_smartstore_sales(self, rows: List[_ChannelProductRow]) -> str | None:
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
                sales_map = connector.fetch_product_sales_counts(days=self.config.stats_lookback_days)
                for row in rows:
                    if row.product_id:
                        row.sales = sales_map.get(row.product_id, 0)
                return None
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)

        return last_error

    def _collect_coupang_rows(self) -> List[_ChannelProductRow]:
        rows: List[_ChannelProductRow] = []
        raw_rows = self.coupang.fetch_products(max_products=self.config.max_products)
        for row in raw_rows:
            rows.append(
                _ChannelProductRow(
                    name=str(row.get("name") or ""),
                    image_url=row.get("image_url"),
                    product_url=row.get("product_url"),
                    stock=row.get("stock"),
                    sales=row.get("sales"),
                    price=row.get("price"),
                    product_id=str(row.get("product_id") or ""),
                    item_id=(str(row.get("item_id")) if row.get("item_id") else None),
                )
            )
        return rows

    @staticmethod
    def _build_unified_row(
        synced_at: datetime,
        naver_row: _ChannelProductRow | None,
        coupang_row: _ChannelProductRow | None,
    ) -> UnifiedProduct:
        if naver_row and coupang_row:
            match_type = "matched"
            display_name = naver_row.name or coupang_row.name
        elif naver_row:
            match_type = "naver_only"
            display_name = naver_row.name
        else:
            match_type = "coupang_only"
            display_name = coupang_row.name if coupang_row else ""

        naver_stock = naver_row.stock if naver_row else None
        coupang_stock = coupang_row.stock if coupang_row else None
        stock_diff: int | None = None
        if naver_stock is not None and coupang_stock is not None:
            stock_diff = naver_stock - coupang_stock

        image_url = None
        if naver_row and naver_row.image_url:
            image_url = naver_row.image_url
        elif coupang_row and coupang_row.image_url:
            image_url = coupang_row.image_url

        return UnifiedProduct(
            serial=0,
            name=display_name,
            image_url=image_url,
            naver_url=(naver_row.product_url if naver_row else None),
            coupang_url=(coupang_row.product_url if coupang_row else None),
            naver_name=(naver_row.name if naver_row else None),
            coupang_name=(coupang_row.name if coupang_row else None),
            naver_stock=naver_stock,
            coupang_stock=coupang_stock,
            stock_diff=stock_diff,
            naver_sales=(naver_row.sales if naver_row else None),
            coupang_sales=(coupang_row.sales if coupang_row else None),
            naver_price=(naver_row.price if naver_row else None),
            coupang_price=(coupang_row.price if coupang_row else None),
            match_type=match_type,
            synced_at=synced_at,
        )

    def fetch_all(self) -> Tuple[List[UnifiedProduct], List[str]]:
        synced_at = datetime.now()
        merged: List[UnifiedProduct] = []
        errors: List[str] = []

        naver_rows: List[_ChannelProductRow] = []
        coupang_rows: List[_ChannelProductRow] = []

        try:
            naver_rows = self._collect_smartstore_rows()
            sales_error = self._enrich_smartstore_sales(naver_rows)
            if sales_error:
                errors.append(f"스마트스토어 판매량 조회 실패: {sales_error}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"스마트스토어 동기화 실패: {exc}")

        try:
            coupang_rows = self._collect_coupang_rows()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"쿠팡로켓 동기화 실패: {exc}")

        # Greedy 1:1 matching based on product name similarity.
        score_candidates: List[tuple[float, int, int]] = []
        for n_index, naver_row in enumerate(naver_rows):
            for c_index, coupang_row in enumerate(coupang_rows):
                score = self._name_similarity(naver_row.name, coupang_row.name)
                if score >= 0.56:
                    score_candidates.append((score, n_index, c_index))

        score_candidates.sort(reverse=True, key=lambda item: item[0])

        used_naver: set[int] = set()
        used_coupang: set[int] = set()

        for _, n_index, c_index in score_candidates:
            if n_index in used_naver or c_index in used_coupang:
                continue
            merged.append(
                self._build_unified_row(
                    synced_at=synced_at,
                    naver_row=naver_rows[n_index],
                    coupang_row=coupang_rows[c_index],
                )
            )
            used_naver.add(n_index)
            used_coupang.add(c_index)

        for n_index, naver_row in enumerate(naver_rows):
            if n_index in used_naver:
                continue
            merged.append(
                self._build_unified_row(
                    synced_at=synced_at,
                    naver_row=naver_row,
                    coupang_row=None,
                )
            )

        for c_index, coupang_row in enumerate(coupang_rows):
            if c_index in used_coupang:
                continue
            merged.append(
                self._build_unified_row(
                    synced_at=synced_at,
                    naver_row=None,
                    coupang_row=coupang_row,
                )
            )

        type_order = {"matched": 0, "naver_only": 1, "coupang_only": 2}
        merged.sort(key=lambda item: (type_order.get(item.match_type, 9), item.name))
        for index, item in enumerate(merged, start=1):
            item.serial = index

        return merged, errors
