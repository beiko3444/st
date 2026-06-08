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


def master_product_row_to_dict(row: Any) -> dict[str, Any]:
    master = row.master
    total_stock = row.total_stock
    unit_cost = getattr(master, "unit_cost", None)
    stock_cost = None
    if unit_cost is not None and total_stock is not None:
        stock_cost = int(unit_cost) * int(total_stock)

    today_revenue = 0
    for link in getattr(row, "linked", []) or []:
        qty = getattr(link, "today_sales", None)
        price = getattr(link, "price", None)
        if qty is None or price is None:
            continue
        today_revenue += int(qty) * int(price)

    linked = []
    for link in getattr(row, "linked", []) or []:
        linked.append(
            {
                "channel": getattr(link, "channel", ""),
                "productKey": getattr(link, "product_key", ""),
                "name": getattr(link, "name", ""),
                "imageUrl": getattr(link, "image_url", None),
                "productUrl": getattr(link, "product_url", None),
                "stock": getattr(link, "stock", None),
                "sales": getattr(link, "sales", None),
                "todaySales": getattr(link, "today_sales", None),
                "price": getattr(link, "price", None),
                "multiplier": getattr(link, "multiplier", 1),
                "syncedAt": to_jsonable(getattr(link, "synced_at", None)),
            }
        )

    return {
        "id": getattr(master, "id", None),
        "imageUrl": getattr(row, "image_url", None),
        "name": getattr(master, "name", ""),
        "unitCost": unit_cost,
        "naverPrice": getattr(row, "naver_price", None),
        "coupangPrice": getattr(row, "coupang_price", None),
        "naverStock": getattr(row, "naver_stock", None),
        "coupangStock": getattr(row, "coupang_stock", None),
        "totalStock": total_stock,
        "stockCost": stock_cost,
        "naverTodaySales": getattr(row, "naver_today_sales", None),
        "coupangTodaySales": getattr(row, "coupang_today_sales", None),
        "totalTodaySales": row.total_today_sales,
        "todayRevenue": today_revenue or None,
        "naverSales": getattr(row, "naver_sales", None),
        "coupangSales": getattr(row, "coupang_sales", None),
        "totalSales": row.total_sales,
        "linkCount": len(linked),
        "naverUrl": getattr(row, "naver_url", None),
        "coupangUrl": getattr(row, "coupang_url", None),
        "representativeChannel": getattr(master, "representative_channel", None),
        "representativeProductKey": getattr(master, "representative_product_key", None),
        "memo": getattr(master, "memo", None),
        "updatedAt": to_jsonable(getattr(master, "updated_at", None)),
        "linked": linked,
    }
