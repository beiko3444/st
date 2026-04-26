"""네이버 크롤러 진단 스크립트 v2 — CSP-safe polling."""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

# 콘솔 한글 출력 보장
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright

USER_DATA_DIR = str(Path.home() / ".smartinventory" / "browser_profile_naver")

URLS_TO_TRY = [
    "https://pay.naver.com/pc/history",          # PC 버전 (가장 확실)
    "https://order.pay.naver.com/orderList",     # 구버전 주문목록
    "https://order.pay.naver.com/home",          # 구버전 홈
]

LAUNCH_KWARGS = {
    "user_data_dir": USER_DATA_DIR,
    "headless": False,
    "channel": "chrome",
    "args": [
        "--disable-blink-features=AutomationControlled",
    ],
    "ignore_default_args": ["--enable-automation"],
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "locale": "ko-KR",
    "timezone_id": "Asia/Seoul",
    "viewport": {"width": 1280, "height": 900},
}

STEALTH = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
"""


def wait_until_off_login(page, timeout_sec: int = 240) -> bool:
    """nid.naver.com 에서 벗어날 때까지 polling. CSP 안전."""
    start = time.time()
    while time.time() - start < timeout_sec:
        try:
            url = page.url
        except Exception:
            url = ""
        if "nid.naver.com" not in url:
            return True
        time.sleep(2)
    return False


def main():
    print("=== 진단 시작 ===", flush=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(**LAUNCH_KWARGS)
        ctx.add_init_script(STEALTH)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        for url in URLS_TO_TRY:
            print(f"\n=== 시도: {url} ===", flush=True)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as e:
                print(f"  goto 실패: {e}", flush=True)
                continue

            print(f"  최종 URL: {page.url[:120]}", flush=True)

            if "nid.naver.com" in page.url:
                print("  → 로그인 페이지. 폴링으로 사용자 로그인 대기 (최대 4분)", flush=True)
                if not wait_until_off_login(page, timeout_sec=240):
                    print("  로그인 안 됨, 다음 URL 시도", flush=True)
                    continue
                print(f"  로그인 후 URL: {page.url[:120]}", flush=True)
                # 원하는 페이지로 명시적 이동
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                    time.sleep(2.0)
                except Exception as e:
                    print(f"  재이동 실패: {e}", flush=True)
                    continue
                print(f"  재이동 후 URL: {page.url[:120]}", flush=True)
                if "nid.naver.com" in page.url:
                    print("  → 다시 로그인 요구. 다음 URL 시도", flush=True)
                    continue

            # SPA 데이터 렌더 대기
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                pass
            time.sleep(3.0)

            try:
                title = page.title()
            except Exception:
                title = "(?)"
            try:
                body_text = page.evaluate("() => document.body && document.body.innerText || ''")
            except Exception:
                body_text = ""
            print(f"  title: {title}", flush=True)
            print(f"  body 텍스트 길이: {len(body_text)}", flush=True)

            selectors_to_test = [
                'li[class*="OrderItem"]',
                'div[class*="order_item"]',
                'div[class*="OrderCard"]',
                'div[class*="orderCard"]',
                'div[class*="HistoryCard"]',
                'div[class*="historyCard"]',
                'div[class*="history_item"]',
                'div[class*="historyItem"]',
                'section[class*="order"] li',
                'ul[class*="order"] > li',
                'ul[class*="history"] > li',
                'article[class*="order"]',
                'article[class*="history"]',
                '[data-testid*="order"]',
                '[data-testid*="history"]',
                # 텍스트 기반
                'li:has-text("원")',
                'div:has-text("주문번호")',
                # 일반 카드 컨테이너
                'div[class*="card"]',
                'li[class*="card"]',
            ]
            print("  selector 매칭 카운트:", flush=True)
            for sel in selectors_to_test:
                try:
                    n = len(page.query_selector_all(sel))
                except Exception:
                    n = 0
                if n > 0:
                    print(f"    {sel:50s} → {n}", flush=True)

            # 덤프 저장
            try:
                html = page.content()
            except Exception:
                html = ""
            dump_dir = Path("./debug_naver_dumps")
            dump_dir.mkdir(exist_ok=True)
            safe_name = url.replace("https://", "").replace("/", "_")[:60]
            (dump_dir / f"{safe_name}.html").write_text(html, encoding="utf-8")
            (dump_dir / f"{safe_name}.txt").write_text(body_text, encoding="utf-8")
            print(f"  덤프 저장: debug_naver_dumps/{safe_name}.*", flush=True)
            print(f"  body innerText 처음 600자:", flush=True)
            preview = (body_text[:600] or "(빈 페이지)").replace("\n", " | ")
            print(f"    {preview}", flush=True)

            if len(body_text) > 200:
                # 충분한 데이터가 있는 페이지 → 종료
                break

        print("\n=== 완료 ===", flush=True)
        print("창은 8초 뒤 닫힙니다.", flush=True)
        time.sleep(8)
        try:
            ctx.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
