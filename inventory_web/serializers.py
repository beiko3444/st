from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from typing import Any


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return to_jsonable(asdict(value))
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    return value


def channel_product_to_dict(row: Any) -> dict[str, Any]:
    item_id = getattr(row, "item_id", None)
    product_id = str(getattr(row, "product_id", "") or "")
    product_key = f"{product_id}|{item_id or ''}"
    return {
        "serial": getattr(row, "serial", None),
        "productKey": product_key,
        "productId": product_id,
        "itemId": item_id,
        "name": getattr(row, "name", ""),
        "imageUrl": getattr(row, "image_url", None),
        "productUrl": getattr(row, "product_url", None),
        "stock": getattr(row, "stock", None),
        "todaySales": getattr(row, "today_sales", None),
        "sales": getattr(row, "sales", None),
        "price": getattr(row, "price", None),
        "syncedAt": to_jsonable(getattr(row, "synced_at", None)),
    }


def monitor_inventory_row(channel: str, row: dict[str, Any], index: int) -> dict[str, Any]:
    product_id = str(row.get("product_id") or "")
    item_id = str(row.get("item_id")) if row.get("item_id") else None
    return {
        "serial": index,
        "channel": channel,
        "productKey": f"{product_id}|{item_id or ''}",
        "productId": product_id,
        "itemId": item_id,
        "name": row.get("name") or "",
        "imageUrl": row.get("image_url"),
        "productUrl": row.get("product_url"),
        "stock": row.get("stock"),
        "todaySales": row.get("today_sales"),
        "sales": row.get("sales"),
        "price": row.get("price"),
        "syncedAt": row.get("recorded_at"),
    }
