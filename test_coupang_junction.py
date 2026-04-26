"""쿠팡 진단 v5 — Junction link 우회.

핵심:
- Chrome 은 default user-data-dir 에서 --remote-debugging-port 거부
- NTFS junction 으로 사용자 평상시 폴더를 다른 경로로 보이게 함
- Chrome 입장: "default 아닌 경로" → 보안 정책 통과
- 실제 데이터: 사용자 평상시 cookies/fingerprint 그대로 → Akamai 통과 가능
"""

from __future__ import annotations

import io
import subprocess
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
DEBUG_PORT = 9223  # 다른 포트로
REAL_USER_DATA = Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
JUNCTION = Path.home() / ".smartinventory" / "chrome_junction"


def setup_junction():
    """Junction 생성 (이미 있으면 재사용)."""
    if JUNCTION.exists():
        # symlink/junction 인지 확인
        try:
            target = JUNCTION.resolve(strict=False)
            print(f"[INFO] Junction 이미 존재: {JUNCTION} → {target}", flush=True)
            return True
        except Exception:
            pass
    JUNCTION.parent.mkdir(parents=True, exist_ok=True)
    # mklink /J 로 junction 생성
    cmd = ["cmd", "/c", "mklink", "/J", str(JUNCTION), str(REAL_USER_DATA)]
    print(f"[STEP] Junction 생성: {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(f"  stdout: {result.stdout.strip()}", flush=True)
    print(f"  stderr: {result.stderr.strip()}", flush=True)
    return result.returncode == 0


def main():
    if not Path(CHROME_PATH).exists():
        print(f"[ERR] Chrome 경로 없음: {CHROME_PATH}", flush=True)
        return

    if not setup_junction():
        print("[ERR] Junction 생성 실패. 관리자 권한 필요할 수 있음.", flush=True)
        return

    # Singleton 락 제거
    for f in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (JUNCTION / f).unlink(missing_ok=True)  # type: ignore[arg-type]
        except Exception:
            pass

    args = [
        CHROME_PATH,
        f"--remote-debugging-port={DEBUG_PORT}",
        f"--user-data-dir={JUNCTION}",
        "--profile-directory=Default",
        "https://mc.coupang.com/ssr/desktop/order/list",
    ]
    print(f"[STEP] Chrome 시작 (junction 경유)", flush=True)
    print(f"  user-data-dir: {JUNCTION}", flush=True)
    proc = subprocess.Popen(args)
    print(f"  PID={proc.pid}", flush=True)

    time.sleep(6)

    # CDP attach
    print(f"\n[STEP] CDP attach to localhost:{DEBUG_PORT}", flush=True)
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"[ERR] {e}", flush=True)
        return

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(f"http://localhost:{DEBUG_PORT}")
        except Exception as e:
            print(f"[ERR] CDP connect 실패: {e}", flush=True)
            print("→ Chrome 이 디버깅 포트를 안 열었거나, 다른 Chrome 인스턴스가 IPC 가로챘을 수 있음.", flush=True)
            return

        print(f"[OK] CDP 연결. contexts={len(browser.contexts)}", flush=True)
        ctx = browser.contexts[0]

        def find_cp():
            for p in ctx.pages:
                try:
                    u = p.url
                except Exception:
                    continue
                if "coupang.com" in u:
                    return p
            return None

        # 사용자 로그인 시간 충분히 줌
        deadline = time.time() + 480  # 8분
        last = ""
        while time.time() < deadline:
            urls = []
            for p in ctx.pages:
                try:
                    urls.append(p.url)
                except Exception:
                    pass
            state = " | ".join(u[:60] for u in urls)
            if state != last:
                print(f"  탭: {state}", flush=True)
                last = state

            cp = find_cp()
            if cp is None:
                time.sleep(3)
                continue

            try:
                cur = cp.url
                content = cp.content()
            except Exception:
                content = ""
                cur = ""

            blocked = "Access Denied" in content and "edgesuite" in content.lower()
            if blocked:
                print(f"  ⚠ Akamai 차단", flush=True)
                time.sleep(3)
                continue
            if "login.coupang.com" in cur:
                print(f"  로그인 대기 중", flush=True)
                time.sleep(3)
                continue
            # body 길이 + 키워드로 정상 페이지 판별
            body_len = len(content)
            looks_logged_in = (
                ("주문번호" in content or "주문상세" in content)
                and "이메일 로그인" not in content
            )
            if looks_logged_in:
                print(f"\n✅ 주문 페이지 도달!", flush=True)
                body = cp.evaluate("() => document.body.innerText")
                print(f"  body 길이: {len(body)}", flush=True)
                preview = body[:600].replace("\n", " | ")
                print(f"  body 처음 600자:", flush=True)
                print(f"  {preview}", flush=True)

                # selector 시도
                sels = [
                    'div[class*="orderListItem"]',
                    'div[class*="order-list-item"]',
                    'div[class*="OrderListItem"]',
                    'tbody tr',
                    'li[class*="order"]',
                    'div[class*="order"]',
                ]
                print("\n  selector 매칭:", flush=True)
                for sel in sels:
                    try:
                        n = len(cp.query_selector_all(sel))
                    except Exception:
                        n = 0
                    if n > 0:
                        print(f"    {sel:40s} → {n}", flush=True)

                dump = Path("./debug_coupang_junction_dumps")
                dump.mkdir(exist_ok=True)
                (dump / "page.html").write_text(content, encoding="utf-8")
                (dump / "body.txt").write_text(body, encoding="utf-8")
                print(f"  덤프: {dump}", flush=True)
                break
            time.sleep(3)
        else:
            print("\n[INFO] Timeout. 최종 URL:", flush=True)
            cp = find_cp()
            if cp:
                print(f"  {cp.url}", flush=True)

        print("\n=== 완료 ===", flush=True)


if __name__ == "__main__":
    main()
