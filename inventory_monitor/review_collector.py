#!/usr/bin/env python3
"""
리뷰 수 수집기 — Playwright로 스마트스토어/쿠팡 상품 페이지에서 리뷰 수를 스크래핑.
cron으로 하루 1회 실행. 라즈베리파이에서 구동.

사용법:
    python3 inventory_monitor/review_collector.py

crontab 예시:
    0 3 * * * cd /home/beiko/st && /usr/bin/python3 inventory_monitor/review_collector.py >> /tmp/review_collector.log 2>&1
"""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

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


def _collect_naver_reviews(db: InventoryHistoryDB) -> int:
    """스마트스토어 리뷰 수 수집 — 비공개 API 시도 후 Playwright 폴백."""
    import httpx

    products = db.get_latest_snapshot("naver")
    if not products:
        log.info("네이버 상품 없음, 건너뜀")
        return 0

    results: list[dict] = []

    for p in products:
        product_url = p.get("product_url") or ""
        product_id = p.get("product_id", "")
        name = p.get("name", "")
        image_url = p.get("image_url")

        review_count = _naver_api_review_count(product_url)

        if review_count is None:
            review_count = _naver_playwright_review_count(product_url)

        if review_count is not None:
            results.append({
                "product_id": product_id,
                "name": name,
                "image_url": image_url,
                "review_count": review_count,
                "review_score": None,
            })
            log.info("  네이버 [%s] 리뷰: %d", name[:30], review_count)

    if results:
        n = db.insert_reviews("naver", results)
        log.info("네이버 리뷰 %d건 저장", n)
        return n
    return 0


def _naver_api_review_count(product_url: str) -> int | None:
    """네이버 비공개 API로 리뷰 수 조회 시도."""
    import httpx

    if not product_url:
        return None

    # URL에서 channel + product number 추출
    # 예: https://smartstore.naver.com/store_name/products/12345678
    match = re.search(r"/products/(\d+)", product_url)
    if not match:
        return None
    product_no = match.group(1)

    # 스마트스토어 비공개 API 시도
    urls_to_try = [
        f"https://smartstore.naver.com/i/v1/contents/reviews/total-count?originProductNo={product_no}",
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json",
        "Referer": product_url,
    }

    for url in urls_to_try:
        try:
            resp = httpx.get(url, headers=headers, timeout=10, follow_redirects=True)
            if resp.status_code == 200:
                data = resp.json()
                # 응답 형식에 따라 파싱
                if isinstance(data, dict):
                    for key in ("totalCount", "reviewCount", "count", "totalReviewCount"):
                        if key in data:
                            return int(data[key])
                    # 중첩 구조 탐색
                    if "data" in data and isinstance(data["data"], dict):
                        for key in ("totalCount", "reviewCount", "count"):
                            if key in data["data"]:
                                return int(data["data"][key])
        except Exception:
            continue
    return None


def _naver_playwright_review_count(product_url: str) -> int | None:
    """Playwright로 상품 페이지에서 리뷰 수 파싱."""
    if not product_url:
        return None
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(product_url, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(2000)

            # "리뷰 1,234" 또는 "구매후기(1,234)" 패턴
            content = page.content()
            patterns = [
                r'리뷰\s*[\(（]?\s*([\d,]+)\s*[\)）]?',
                r'구매후기\s*[\(（]\s*([\d,]+)\s*[\)）]',
                r'review.*?(\d[\d,]*)',
            ]
            for pattern in patterns:
                match = re.search(pattern, content, re.IGNORECASE)
                if match:
                    count = int(match.group(1).replace(",", ""))
                    browser.close()
                    return count
            browser.close()
    except Exception as e:
        log.warning("네이버 Playwright 실패 [%s]: %s", product_url[:60], e)
    return None


def _collect_coupang_reviews(db: InventoryHistoryDB) -> int:
    """쿠팡 리뷰 수 수집 — Playwright 사용."""
    products = db.get_latest_snapshot("coupang")
    if not products:
        log.info("쿠팡 상품 없음, 건너뜀")
        return 0

    # product_id 기준으로 중복 제거 (item_id별로 여러 행일 수 있음)
    seen_pids: set[str] = set()
    unique_products: list[dict] = []
    for p in products:
        pid = p.get("product_id", "")
        if pid not in seen_pids:
            seen_pids.add(pid)
            unique_products.append(p)

    results: list[dict] = []

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )

            for p in unique_products:
                product_url = p.get("product_url") or ""
                product_id = p.get("product_id", "")
                name = p.get("name", "")
                image_url = p.get("image_url")

                if not product_url:
                    continue

                review_count = _coupang_page_review_count(context, product_url)

                if review_count is not None:
                    results.append({
                        "product_id": product_id,
                        "name": name,
                        "image_url": image_url,
                        "review_count": review_count,
                        "review_score": None,
                    })
                    log.info("  쿠팡 [%s] 리뷰: %d", name[:30], review_count)

            browser.close()
    except Exception as e:
        log.exception("쿠팡 Playwright 초기화 실패: %s", e)

    if results:
        n = db.insert_reviews("coupang", results)
        log.info("쿠팡 리뷰 %d건 저장", n)
        return n
    return 0


def _coupang_page_review_count(context, product_url: str) -> int | None:
    """쿠팡 상품 페이지에서 리뷰 수 파싱."""
    try:
        page = context.new_page()
        page.goto(product_url, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(2000)

        content = page.content()
        page.close()

        # "상품평 (1,234)" 또는 "상품리뷰 1,234건" 패턴
        patterns = [
            r'상품평\s*[\(（]\s*([\d,]+)\s*[\)）]',
            r'상품리뷰\s*[\(（]?\s*([\d,]+)',
            r'count":\s*(\d+).*?review',
            r'review.*?"count":\s*(\d+)',
            r'(\d[\d,]*)\s*개의?\s*상품평',
        ]
        for pattern in patterns:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                return int(match.group(1).replace(",", ""))
    except Exception as e:
        log.warning("쿠팡 페이지 실패 [%s]: %s", product_url[:60], e)
    return None


def main() -> None:
    log.info("=== 리뷰 수집 시작 ===")
    db = InventoryHistoryDB()
    now = datetime.now()

    n_naver = _collect_naver_reviews(db)
    n_coupang = _collect_coupang_reviews(db)

    log.info("=== 리뷰 수집 완료: 네이버 %d건, 쿠팡 %d건 ===", n_naver, n_coupang)


if __name__ == "__main__":
    main()
