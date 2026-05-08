from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class ChannelProduct:
    serial: int
    product_id: str
    item_id: Optional[str]
    name: str
    image_url: Optional[str]
    product_url: Optional[str]
    stock: Optional[int]
    sales: Optional[int]
    price: Optional[int]
    synced_at: datetime
    today_sales: Optional[int] = None


@dataclass
class MasterProduct:
    id: int
    name: str
    unit_cost: Optional[int]
    memo: Optional[str]
    representative_channel: Optional[str]
    representative_product_key: Optional[str]
    created_at: datetime
    updated_at: datetime


@dataclass
class ChannelMasterLink:
    channel: str
    product_key: str
    master_id: int
    multiplier: int
    created_at: datetime
    updated_at: datetime


@dataclass
class StockInboundEntry:
    id: Optional[int]
    receipt_date: str
    master_id: int
    channel: str
    quantity: int
    created_at: datetime
    updated_at: datetime


@dataclass
class PurchaseRecord:
    id: Optional[int]
    channel: str
    order_date: Optional[str]
    order_no: Optional[str]
    title: str
    amount: Optional[int]
    payment_method: Optional[str]
    source_url: Optional[str]
    raw_text: str
    imported_at: datetime
    account_label: Optional[str] = None


@dataclass
class PurchaseOrder:
    """주문(결제) 단위 요약. 카드사용내역과 매칭하기 위한 핵심 entity.

    쿠팡: 한 주문 = 카드명세서 한 줄. payment_total 은 배송비/할인/쿠폰 적용 후
    실제 카드에 청구된 금액이며 품목 합산과는 다를 수 있다.
    """
    channel: str
    order_no: str                       # 주문번호 (e.g., "12345678")
    order_date: Optional[str]           # ISO yyyy-mm-dd
    payment_total: Optional[int]        # 실제 결제 합계 (원)
    item_count: int                     # 주문에 포함된 품목 수
    status: Optional[str]               # 배송완료 / 배송중 / 취소 / 반품 등
    payment_method: Optional[str]       # 결제수단 (카드 4자리 등)
    source_url: Optional[str]           # 주문 상세 URL
    raw_text: str                       # 주문 상세 페이지 본문 (디버깅용)
    imported_at: datetime
    cash_used: Optional[int] = None     # 쿠페이캐시/쿠폰/적립금 등 카드 외 차감 합계 (원)
    card_amount: Optional[int] = None   # payment_total - cash_used (실 카드 청구액)
    account_label: Optional[str] = None  # 크롤링 시 사용한 계정 별칭


@dataclass
class CardUsage:
    """외부 카드 API 의 한 카드 사용 건.

    docs/card-api-relocation-design.md §5/§7 의 응답 스키마와 호환.
    필드는 외부 서비스가 주는 그대로를 보존하되, snake_case 로 변환해 저장.
    """
    id: Optional[str]                       # 외부 API 의 id 또는 (corpNum, cardNum, useKey) 합성키
    corp_num: Optional[str]                 # 사업자번호
    card_num: Optional[str]                 # 카드번호 (마스킹된 형태)
    use_key: Optional[str]                  # 외부 useKey
    used_at: Optional[str]                  # ISO datetime (예: "2026-04-26T12:34:56")
    store_name: Optional[str]               # 가맹점명
    amount: Optional[int]                   # 결제금액 (원, 음수는 취소)
    category: Optional[str]                 # 카테고리
    memo: Optional[str]                     # 메모
    reviewed: bool = False                   # 검토 완료 여부
    coupang_purchase_id: Optional[str] = None  # 매칭된 쿠팡 구매내역 id
    raw: Optional[dict] = None               # 원본 응답 (디버깅용)
