#!/usr/bin/env python3
"""
재고 모니터링 데몬 — 10분 간격으로 스마트스토어/쿠팡 재고를 조회하여 DB에 저장.
라즈베리파이에서 systemd 서비스로 실행.
"""
from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가 (inventory_app import를 위해)
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from inventory_app.config import load_config
from inventory_app.connectors.coupang import CoupangRocketConnector
from inventory_app.connectors.smartstore import SmartStoreConnector
from inventory_monitor.history_db import InventoryHistoryDB

INTERVAL_SECONDS = 600  # 10분

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("inventory-monitor")

_running = True


def _handle_signal(signum, frame):
    global _running
    log.info("종료 신호 수신 (signal=%s), 종료 중...", signum)
    _running = False


def _fetch_naver(
    connector: SmartStoreConnector,
    stats_connector: SmartStoreConnector,
    max_items: int,
) -> list[dict]:
    raw = connector.fetch_products(max_items=max_items)

    today_sales_map: dict[str, int] = {}
    try:
        today_sales_map = stats_connector.fetch_product_sales_counts(days=1)
    except Exception:
        log.warning("오늘 판매량 조회 실패 (무시)")

    return [
        {
            "product_id": str(r.get("product_id", "")),
            "item_id": r.get("item_id"),
            "name": str(r.get("name", "")),
            "image_url": r.get("image_url"),
            "product_url": r.get("product_url"),
            "stock": r.get("stock"),
            "sales": None,
            "today_sales": today_sales_map.get(str(r.get("product_id", ""))) if today_sales_map else None,
            "price": r.get("price"),
        }
        for r in raw
    ]


def _fetch_coupang(connector: CoupangRocketConnector, max_items: int) -> list[dict]:
    raw = connector.fetch_products(max_products=max_items)
    return [
        {
            "product_id": str(r.get("product_id", "")),
            "item_id": (str(r.get("item_id")) if r.get("item_id") else None),
            "name": str(r.get("name", "")),
            "image_url": r.get("image_url"),
            "product_url": r.get("product_url"),
            "stock": r.get("stock"),
            "sales": r.get("sales"),
            "today_sales": None,
            "price": r.get("price"),
        }
        for r in raw
    ]


def _collect_once(
    ss: SmartStoreConnector,
    ss_stats: SmartStoreConnector,
    cp: CoupangRocketConnector,
    db: InventoryHistoryDB,
    max_items: int,
) -> None:
    now = datetime.now()

    # 스마트스토어
    try:
        naver_rows = _fetch_naver(ss, ss_stats, max_items)
        n_naver = db.insert_rows("naver", naver_rows, recorded_at=now)
        log.info("스마트스토어: %d개 변동 저장 (전체 %d개)", n_naver, len(naver_rows))
    except Exception:
        log.exception("스마트스토어 조회 실패")

    # 쿠팡
    try:
        coupang_rows = _fetch_coupang(cp, max_items)
        n_coupang = db.insert_rows("coupang", coupang_rows, recorded_at=now)
        log.info("쿠팡: %d개 변동 저장 (전체 %d개)", n_coupang, len(coupang_rows))
    except Exception:
        log.exception("쿠팡 조회 실패")

    log.info("총 DB 레코드 수: %d", db.count_records())


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("=== 재고 모니터링 시작 ===")
    log.info("수집 간격: %d초 (%d분)", INTERVAL_SECONDS, INTERVAL_SECONDS // 60)

    cfg = load_config()

    ss = SmartStoreConnector(
        client_id=cfg.smartstore_client_id,
        client_secret=cfg.smartstore_client_secret,
        token_type=cfg.smartstore_token_type,
        timeout_seconds=cfg.timeout_seconds,
    )
    ss_stats = SmartStoreConnector(
        client_id=cfg.smartstore_stats_client_id,
        client_secret=cfg.smartstore_stats_client_secret,
        token_type=cfg.smartstore_stats_token_type,
        timeout_seconds=cfg.timeout_seconds,
    )
    cp = CoupangRocketConnector(
        vendor_id=cfg.coupang_vendor_id,
        access_key=cfg.coupang_access_key,
        secret_key=cfg.coupang_secret_key,
        timeout_seconds=cfg.timeout_seconds,
    )
    db = InventoryHistoryDB()

    # 시작 즉시 1회 수집
    _collect_once(ss, ss_stats, cp, db, cfg.max_products)

    while _running:
        for _ in range(INTERVAL_SECONDS):
            if not _running:
                break
            time.sleep(1)

        if _running:
            _collect_once(ss, ss_stats, cp, db, cfg.max_products)

    log.info("=== 재고 모니터링 종료 ===")


if __name__ == "__main__":
    main()
