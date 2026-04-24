#!/usr/bin/env python3
"""
리뷰 수 수집기 — 모바일 페이지 meta 태그 또는 Selenium으로 리뷰 수를 스크래핑.
cron으로 하루 1회 실행. 라즈베리파이에서 구동.

사용법:
    python3 inventory_monitor/review_collector.py

crontab:
    0 3 * * * cd /home/beiko/st && /usr/bin/python3 inventory_monitor/review_collector.py >> /tmp/review_collector.log 2>&1
"""
from __future__ import annotations

import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from inventory_monitor.history_db import InventoryHistoryDB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("review-collector")

_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)
_DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _collect_coupang_reviews(db: InventoryHistoryDB) -> int:
    """쿠팡 리뷰 수 수집 — 모바일 페이지 또는 Selenium."""
    products = db.get_latest_snapshot("coupang")
    if not products:
        log.info("쿠팡 상품 없음, 건너뜀")
        return 0

    # product_id 기준 중복 제거
    seen_pids: set[str] = set()
    unique_products: list[dict] = []
    for p in products:
        pid = p.get("product_id", "")
        if pid not in seen_pids:
            seen_pids.add(pid)
            unique_products.append(p)

    results: list[dict] = []
    driver = None

    for p in unique_products:
        product_url = p.get("product_url") or ""
        product_id = p.get("product_id", "")
        name = p.get("name", "")
        image_url = p.get("image_url")

        if not product_url:
            continue

        # httpx로 먼저 시도
        review_count = _coupang_httpx_review_count(product_url)

        # Selenium 폴백
        if review_count is None:
            if driver is None:
                try:
                    driver = _create_driver()
                except Exception as e:
                    log.error("Selenium 드라이버 생성 실패: %s", e)
                    break
            review_count = _selenium_review_count(driver, product_url, [
                r'상품평\s*[\(（]\s*([\d,]+)\s*[\)）]',
                r'상품리뷰\s*[\(（]?\s*([\d,]+)',
                r'(\d[\d,]*)\s*개의?\s*상품평',
            ])

        if review_count is not None:
            results.append({
                "product_id": product_id,
                "name": name,
                "image_url": image_url,
                "review_count": review_count,
                "review_score": None,
            })
            log.info("  쿠팡 [%s] 리뷰: %d", name[:30], review_count)

        time.sleep(5)

    if driver:
        driver.quit()

    if results:
        n = db.insert_reviews("coupang", results)
        log.info("쿠팡 리뷰 %d건 저장", n)
        return n
    return 0


def _coupang_httpx_review_count(product_url: str) -> int | None:
    """쿠팡 모바일 페이지에서 리뷰 수 파싱."""
    mobile_url = product_url.replace(
        "://www.coupang.com/", "://m.coupang.com/"
    )
    try:
        resp = httpx.get(
            mobile_url,
            headers={
                "User-Agent": _MOBILE_UA,
                "Accept-Language": "ko-KR,ko;q=0.9",
            },
            follow_redirects=True,
            timeout=15,
        )
        if resp.status_code != 200:
            return None

        html = resp.text
        # 상품평 (1,234) 패턴
        patterns = [
            r'상품평\s*[\(（]\s*([\d,]+)\s*[\)）]',
            r'(\d[\d,]*)\s*개의?\s*상품평',
            r'"ratingCount":\s*(\d+)',
            r'"reviewCount":\s*(\d+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                return int(match.group(1).replace(",", ""))
    except Exception as e:
        log.warning("쿠팡 httpx 실패 [%s]: %s", product_url[:50], e)
    return None


def _create_driver():
    """Selenium headless Chromium 드라이버 생성."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument(f"user-agent={_DESKTOP_UA}")
    service = Service("/usr/bin/chromedriver")
    return webdriver.Chrome(service=service, options=options)


def _selenium_review_count(driver, url: str, patterns: list[str]) -> int | None:
    """Selenium으로 페이지 HTML에서 리뷰 수 파싱."""
    try:
        driver.get(url)
        time.sleep(4)
        content = driver.page_source

        for pattern in patterns:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                return int(match.group(1).replace(",", ""))
    except Exception as e:
        log.warning("Selenium 실패 [%s]: %s", url[:60], e)
    return None


def main() -> None:
    log.info("=== 리뷰 수집 시작 ===")
    db = InventoryHistoryDB()

    n_coupang = _collect_coupang_reviews(db)

    log.info("=== 리뷰 수집 완료: 쿠팡 %d건 ===", n_coupang)


if __name__ == "__main__":
    main()
