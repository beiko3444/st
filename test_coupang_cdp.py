"""쿠팡 진단 v4 — CDP attach 방식.

핵심 차이:
- 이전: Playwright 가 직접 Chrome 실행 → Akamai 가 자동화 마커로 감지
- 지금: subprocess 로 일반 Chrome 실행 + remote-debugging-port → Playwright 는
  단순히 connect_over_cdp 로 붙어서 DOM 만 읽음. Chrome 자체는 정상 사용자 Chrome.
"""

from __future__ import annotations

import io
import subprocess
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
DEBUG_PORT = 9222
# 사용자 평상시 Chrome User Data 그대로 사용 + Default 프로파일.
# 일반적인 Akamai 우회는 cookies/세션 history 가 충분해야 통과 가능.
USE_REAL_PROFILE = True
if USE_REAL_PROFILE:
    USER_DATA_DIR = str(Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data")
    PROFILE_DIR = "Default"
else:
    USER_DATA_DIR = str(Path.home() / ".smartinventory" / "chrome_cdp_coupang")
    PROFILE_DIR = None


def find_chrome() -> str | None:
    for p in CHROME_PATHS:
        if Path(p).exists():
            return p
    return None


def main():
    chrome = find_chrome()
    if not chrome:
        print("[ERR] Chrome 실행파일을 찾을 수 없습니다.", flush=True)
        return
    print(f"[OK] Chrome 발견: {chrome}", flush=True)

    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)
    print(f"[INFO] user-data-dir: {USER_DATA_DIR}", flush=True)

    # 1) 일반 Chrome 을 remote debugging port 로 시작
    args = [
        chrome,
        f"--remote-debugging-port={DEBUG_PORT}",
        f"--user-data-dir={USER_DATA_DIR}",
    ]
    if PROFILE_DIR:
        args.append(f"--profile-directory={PROFILE_DIR}")
    args.append("https://mc.coupang.com/ssr/desktop/order/list")
    print(f"[STEP 1] Chrome 시작: --remote-debugging-port={DEBUG_PORT}", flush=True)
    proc = subprocess.Popen(args)
    print(f"  PID={proc.pid}", flush=True)

    # Chrome 부팅 대기
    time.sleep(5)

    # 2) Playwright 로 CDP attach
    print("[STEP 2] CDP attach", flush=True)
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"[ERR] playwright import: {e}", flush=True)
        return

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(f"http://localhost:{DEBUG_PORT}")
        except Exception as e:
            print(f"[ERR] CDP connect 실패: {e}", flush=True)
            return
        print(f"  연결 OK. contexts={len(browser.contexts)}", flush=True)

        ctx = browser.contexts[0]

        def find_coupang_page():
            """모든 탭을 훑어서 쿠팡 도메인의 페이지 찾기."""
            for p in ctx.pages:
                try:
                    u = p.url
                except Exception:
                    continue
                if "coupang.com" in u:
                    return p
            return None

        # 3) 사용자가 로그인 + 페이지 도달까지 대기 (모든 탭 polling)
        print("\n[STEP 3] 모든 탭 polling 으로 쿠팡 페이지 추적 (5분)", flush=True)
        deadline = time.time() + 300
        last_state = ""
        target_page = None
        while time.time() < deadline:
            # 현재 모든 탭 URL
            all_urls = []
            for p in ctx.pages:
                try:
                    all_urls.append(p.url)
                except Exception:
                    continue
            state = " | ".join(u[:80] for u in all_urls)
            if state != last_state:
                print(f"  탭들: {state}", flush=True)
                last_state = state

            # 쿠팡 탭 찾기
            cp = find_coupang_page()
            if cp is None:
                time.sleep(3)
                continue

            try:
                cur = cp.url
                content = cp.content()
            except Exception:
                content = ""
                cur = ""

            # Access Denied 감지
            if "Access Denied" in content and "edgesuite" in content.lower():
                print(f"  ⚠ Akamai 차단: {cur[:80]}", flush=True)
                # 다른 탭이 정상일 수 있으니 계속 polling
                time.sleep(3)
                continue

            # 로그인 필요 페이지인지 확인
            if "login.coupang.com" in cur:
                print(f"  로그인 페이지: {cur[:80]} (사용자 로그인 대기)", flush=True)
                time.sleep(3)
                continue

            # 주문 페이지 패턴
            if "주문번호" in content or "order" in cur.lower() or "Order" in content:
                print(f"  ✓ 주문 페이지 도달: {cur[:120]}", flush=True)
                target_page = cp
                break

            time.sleep(3)

        if target_page is None:
            print("[INFO] 주문 페이지 도달 못 함. 마지막 쿠팡 탭 사용", flush=True)
            target_page = find_coupang_page()
        page = target_page or (ctx.pages[0] if ctx.pages else None)
        if page is None:
            print("[ERR] 분석할 페이지가 없습니다.", flush=True)
            return

        # 4) 페이지 분석
        print("\n[STEP 4] 페이지 분석", flush=True)
        try:
            content = page.content()
        except Exception:
            content = ""
        try:
            body_text = page.evaluate("() => document.body && document.body.innerText || ''")
        except Exception:
            body_text = ""
        print(f"  body 길이: {len(body_text)}", flush=True)

        if "Access Denied" in content and "edgesuite" in content.lower():
            print("  ❌ 최종 결과: Akamai 차단 (CDP 로도 안 됨)", flush=True)
        elif body_text and "주문" in body_text:
            print("  ✅ 최종 결과: 정상 페이지 도달!", flush=True)
            # selector 시도
            sels = [
                'div[class*="orderListItem"]',
                'div[class*="OrderListItem"]',
                'div[class*="order-list-item"]',
                'tbody tr',
                'li[class*="order"]',
                'div[class*="order"]',
            ]
            for sel in sels:
                try:
                    n = len(page.query_selector_all(sel))
                except Exception:
                    n = 0
                if n > 0:
                    print(f"    {sel:40s} → {n}", flush=True)
            # 덤프
            dump_dir = Path("./debug_coupang_cdp_dumps")
            dump_dir.mkdir(exist_ok=True)
            (dump_dir / "page.html").write_text(content, encoding="utf-8")
            (dump_dir / "body.txt").write_text(body_text, encoding="utf-8")
            print(f"  덤프: debug_coupang_cdp_dumps/", flush=True)
            preview = body_text[:500].replace("\n", " | ")
            print(f"  body 처음 500자: {preview}", flush=True)
        else:
            print(f"  ? 알수없는 상태. URL: {page.url[:120]}, body 길이: {len(body_text)}", flush=True)

        print("\n=== 완료. Chrome 창은 그대로 두세요 ===", flush=True)


if __name__ == "__main__":
    main()
