from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class ChannelProduct:
    serial: int
    product_id: str
    name: str
    image_url: Optional[str]
    product_url: Optional[str]
    stock: Optional[int]
    sales: Optional[int]
    price: Optional[int]
    synced_at: datetime


@dataclass
class UnifiedProduct:
    serial: int
    name: str
    image_url: Optional[str]
    naver_url: Optional[str]
    coupang_url: Optional[str]
    naver_name: Optional[str]
    coupang_name: Optional[str]
    naver_stock: Optional[int]
    coupang_stock: Optional[int]
    stock_diff: Optional[int]
    naver_sales: Optional[int]
    coupang_sales: Optional[int]
    naver_price: Optional[int]
    coupang_price: Optional[int]
    match_type: str
    synced_at: datetime
