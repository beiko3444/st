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
