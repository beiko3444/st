"""쿠팡 크롤러 진단 v3 — Akamai 우회 강화.

핵심 우회 전략 (블로그 글 참고):
1. 홈페이지(www.coupang.com) 먼저 방문 → JS 정상 실행 → _abck 쿠키 발급
2. 자연스러운 대기 (3~6초) + 마우스 움직임
3. 그 후에 주문내역 페이지 이동
4. headed Chrome + stealth + persistent profile
"""

from __future__ import annotations

import io
import random
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright

import shutil
USER_DATA_DIR = str(Path.home() / ".smartinventory" / "browser_profile_coupang")

# 첫 실행 시 사용자 평상시 Chrome 의 Default 프로파일을 복사 (cookies/login 그대로 가져옴)
def _ensure_seeded_profile() -> None:
    src = Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
    dst = Path(USER_DATA_DIR)
    marker = dst / ".seeded_from_chrome"
    if marker.exists():
        return  # 이미 한번 복사함
    if not (src / "Default").exists():
        return
    print(f"[SEED] 평상시 Chrome 프로파일 복사 중... ({src})", flush=True)
    dst.mkdir(parents=True, exist_ok=True)
    # 핵심 폴더만 복사 (전체 복사는 너무 무거움)
    for sub in ["Default", "Local State"]:
        s = src / sub
        d = dst / sub
        if s.exists():
            try:
                if s.is_dir():
                    shutil.copytree(s, d, dirs_exist_ok=True)
                else:
                    shutil.copy2(s, d)
            except Exception as e:
                print(f"  복사 실패({sub}): {e}", flush=True)
    marker.write_text("seeded", encoding="utf-8")
    print("[SEED] 복사 완료", flush=True)

_ensure_seeded_profile()

ORDER_URLS = [
    "https://mc.coupang.com/ssr/desktop/order/list",
    "https://mc.coupang.com/order/list",
]

LAUNCH_KWARGS = {
    "user_data_dir": USER_DATA_DIR,
    "headless": False,
    "channel": "chrome",
    "args": [
        "--disable-blink-features=AutomationControlled",
    ],
    # 봇 탐지 마커 플래그만 골라서 제거 (Chrome 정상 동작에 필요한 건 유지)
    "ignore_default_args": [
        "--enable-automation",
        "--disable-extensions",
    ],
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "locale": "ko-KR",
    "timezone_id": "Asia/Seoul",
    "viewport": {"width": 1920, "height": 1080},
    "extra_http_headers": {
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    },
}


def human_pause(min_s: float = 1.5, max_s: float = 4.0) -> None:
    time.sleep(random.uniform(min_s, max_s))


def random_mouse_moves(page, count: int = 5) -> None:
    """자연스러운 마우스 궤적 시뮬레이션."""
    width, height = 1280, 800
    for _ in range(count):
        x = random.randint(100, width - 100)
        y = random.randint(100, height - 100)
        try:
            page.mouse.move(x, y, steps=random.randint(8, 20))
        except Exception:
            return
        time.sleep(random.uniform(0.1, 0.4))


def random_scroll(page) -> None:
    """랜덤 스크롤."""
    for _ in range(random.randint(1, 3)):
        try:
            page.mouse.wheel(0, random.randint(150, 500))
        except Exception:
            return
        time.sleep(random.uniform(0.3, 0.8))


def is_blocked(page) -> bool:
    """Akamai Access Denied 페이지 감지."""
    try:
        content = page.content()
    except Exception:
        return False
    return "Access Denied" in content and "edgesuite" in content.lower()


def wait_until_off_login(page, timeout_sec: int = 240) -> bool:
    start = time.time()
    while time.time() - start < timeout_sec:
        try:
            url = page.url
        except Exception:
            url = ""
        if "login.coupang.com" not in url:
            return True
        time.sleep(2)
    return False


def main():
    print("=== 쿠팡 진단 v3 (Akamai 우회 강화) ===", flush=True)

    try:
        from playwright_stealth import stealth_sync  # type: ignore
        print("[OK] playwright-stealth 로드", flush=True)
    except Exception as e:
        stealth_sync = None
        print(f"[WARN] stealth 로드 실패: {e}", flush=True)

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(**LAUNCH_KWARGS)
        # 추가 stealth init script
        ctx.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'languages', { get: () => ['ko-KR', 'ko', 'en-US'] });
        Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
        window.chrome = window.chrome || { runtime: {} };
        const oQuery = window.navigator.permissions && window.navigator.permissions.query;
        if (oQuery) {
            window.navigator.permissions.query = (p) => p.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : oQuery(p);
        }
        """)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if stealth_sync is not None:
            try:
                stealth_sync(page)
                print("[OK] stealth 적용", flush=True)
            except Exception as e:
                print(f"[WARN] stealth 실패: {e}", flush=True)

        # ===== 1단계: 홈페이지 방문 (Akamai _abck 쿠키 발급용) =====
        print("\n[STEP 1] 쿠팡 홈 방문 → _abck 발급 + 자연스러운 행동", flush=True)
        try:
            page.goto("https://www.coupang.com/", wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            print(f"  goto 실패: {e}", flush=True)

        if is_blocked(page):
            print("  ⚠ 홈 방문에서 이미 Access Denied", flush=True)
        else:
            print(f"  홈 도달: {page.url[:80]}", flush=True)
        human_pause(3.5, 6.0)
        random_mouse_moves(page, count=6)
        random_scroll(page)
        human_pause(2.0, 4.0)

        # ===== 2단계: 로그인 페이지 또는 마이페이지로 천천히 이동 =====
        print("\n[STEP 2] 마이페이지/로그인", flush=True)
        try:
            page.goto("https://mc.coupang.com/", wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            print(f"  mc 이동 실패: {e}", flush=True)
        human_pause(2.0, 4.0)

        if "login.coupang.com" in page.url:
            if is_blocked(page):
                print("  ⚠ 로그인 페이지에서도 Akamai 차단", flush=True)
                print("  → 우회 실패. 다른 전략 필요.", flush=True)
                # 그래도 사용자 로그인 시도해볼 수 있게 일단 대기
            else:
                print("  로그인 페이지. 사용자 로그인 대기 (4분)", flush=True)
                if not wait_until_off_login(page, 240):
                    print("  로그인 대기 시간 초과", flush=True)
                else:
                    print(f"  로그인 후 URL: {page.url[:80]}", flush=True)
        else:
            print(f"  현재 URL: {page.url[:80]}", flush=True)

        human_pause(2.0, 4.0)
        random_mouse_moves(page, count=4)

        # ===== 3단계: 주문내역 페이지로 이동 =====
        for url in ORDER_URLS:
            print(f"\n[STEP 3] 주문내역 시도: {url}", flush=True)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as e:
                print(f"  goto 실패: {e}", flush=True)
                continue
            print(f"  최종 URL: {page.url[:120]}", flush=True)

            if is_blocked(page):
                print("  ⚠ Akamai Access Denied 페이지", flush=True)
                continue

            if "login.coupang.com" in page.url:
                print("  로그인 다시 요구됨. 대기...", flush=True)
                if not wait_until_off_login(page, 240):
                    continue
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                except Exception:
                    continue
                if is_blocked(page):
                    print("  ⚠ 재이동 후 Access Denied", flush=True)
                    continue

            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            human_pause(2.0, 4.0)

            try:
                title = page.title()
            except Exception:
                title = "(?)"
            try:
                body_text = page.evaluate("() => document.body && document.body.innerText || ''")
            except Exception:
                body_text = ""
            print(f"  title: {title}", flush=True)
            print(f"  body 길이: {len(body_text)}", flush=True)

            if len(body_text) < 200:
                print("  body 텍스트 너무 짧음. 다음 URL 시도.", flush=True)
                continue

            # selector 시도
            selectors_to_test = [
                'div[class*="order"]',
                'li[class*="order"]',
                'tr[class*="order"]',
                'div[class*="OrderListItem"]',
                'div[class*="orderListItem"]',
                'tbody tr',
                'div[class*="orderItem"]',
                'section[class*="order"]',
                'article[class*="order"]',
                'li:has-text("원")',
            ]
            print("  매칭된 selector:", flush=True)
            best = None
            for sel in selectors_to_test:
                try:
                    n = len(page.query_selector_all(sel))
                except Exception:
                    n = 0
                if n > 0:
                    print(f"    {sel:40s} → {n}", flush=True)
                    if not best or n > best[1]:
                        best = (sel, n)

            # 덤프
            try:
                html = page.content()
            except Exception:
                html = ""
            dump_dir = Path("./debug_coupang_dumps")
            dump_dir.mkdir(exist_ok=True)
            safe = url.replace("https://", "").replace("/", "_")[:60]
            (dump_dir / f"{safe}.html").write_text(html, encoding="utf-8")
            (dump_dir / f"{safe}.txt").write_text(body_text, encoding="utf-8")
            print(f"  덤프: debug_coupang_dumps/{safe}.*", flush=True)
            preview = (body_text[:600] or "(빈)").replace("\n", " | ")
            print(f"  body 처음 600자: {preview}", flush=True)
            break

        print("\n=== 완료 ===", flush=True)
        time.sleep(8)
        try:
            ctx.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
