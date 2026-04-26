from __future__ import annotations

import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from inventory_app.models import PurchaseOrder, PurchaseRecord
from inventory_app.services.purchase_history_service import PurchaseHistoryParser

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - handled at runtime for users without playwright
    PlaywrightTimeoutError = TimeoutError  # type: ignore[assignment]
    sync_playwright = None  # type: ignore[assignment]


ORDER_URLS = {
    "naver": "https://order.pay.naver.com/home",
    "coupang": "https://mc.coupang.com/ssr/desktop/order/list",
}

CHANNEL_LABELS = {
    "naver": "\ub124\uc774\ubc84",
    "coupang": "\ucfe0\ud321",
}

ORDER_HINTS = {
    "naver": ("\uc8fc\ubb38", "\ubc30\uc1a1", "\uacb0\uc81c", "\uad6c\ub9e4", "\uc6d0"),
    "coupang": ("\uc8fc\ubb38", "\ubc30\uc1a1", "\uacb0\uc81c", "\uad6c\ub9e4", "\uc6d0"),
}

LOGIN_HINTS = (
    "\ub85c\uadf8\uc778",
    "login",
    "\uc544\uc774\ub514",
    "\ube44\ubc00\ubc88\ud638",
    "\ud68c\uc6d0",
    "sign in",
)


@dataclass
class CrawlerProgress:
    on_log: Callable[[str], None] = field(default=lambda _msg: None)
    on_login_required: Callable[[str], None] = field(default=lambda _msg: None)
    cancelled: Callable[[], bool] = field(default=lambda: False)


@dataclass
class CrawlResult:
    channel: str
    records: List[PurchaseRecord]
    error: Optional[str] = None
    orders: List["PurchaseOrder"] = field(default_factory=list)


class PlaywrightUnavailable(RuntimeError):
    pass


def _profile_root() -> Path:
    return (Path.home() / ".smartinventory" / "browser_profiles").resolve()


def _profile_dir(channel: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", channel.lower()) or "channel"
    return _profile_root() / f"purchase_{safe}"


def _ensure_playwright() -> None:
    if sync_playwright is None:
        raise PlaywrightUnavailable(
            "playwright \ud328\ud0a4\uc9c0\uac00 \uc5c6\uc2b5\ub2c8\ub2e4. requirements.txt \uc124\uce58 \ud6c4 \ub2e4\uc2dc \ube4c\ub4dc\ud574\uc8fc\uc138\uc694."
        )


def ensure_browser_installed(progress: Optional[CrawlerProgress] = None) -> None:
    progress = progress or CrawlerProgress()
    _ensure_playwright()
    # The collector intentionally uses a normal visible browser profile. It does not
    # install evasion scripts or modify browser fingerprints. Edge/Chrome are tried
    # first because they are normally already present on Windows.
    progress.on_log("\ube0c\ub77c\uc6b0\uc800 \uc900\ube44 \ud655\uc778 \uc644\ub8cc")


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _launch_persistent_context(pw, channel: str, headless: bool, progress: CrawlerProgress):
    user_data_dir = _profile_dir(channel)
    user_data_dir.mkdir(parents=True, exist_ok=True)
    kwargs = {
        "user_data_dir": str(user_data_dir),
        "headless": headless,
        "locale": "ko-KR",
        "timezone_id": "Asia/Seoul",
        "viewport": {"width": 1360, "height": 900},
    }
    last_error: Exception | None = None
    for browser_channel, label in (("msedge", "Microsoft Edge"), ("chrome", "Google Chrome")):
        try:
            context = pw.chromium.launch_persistent_context(channel=browser_channel, **kwargs)
            progress.on_log(f"{label} \uc815\uc0c1 \uc138\uc158\uc73c\ub85c \uc5f4\uc5c8\uc2b5\ub2c8\ub2e4")
            return context
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    try:
        context = pw.chromium.launch_persistent_context(**kwargs)
        progress.on_log("Playwright Chromium \uc815\uc0c1 \uc138\uc158\uc73c\ub85c \uc5f4\uc5c8\uc2b5\ub2c8\ub2e4")
        return context
    except Exception as exc:  # noqa: BLE001
        hint = (
            "\n\n\uac1c\ubc1c \ud658\uacbd\uc5d0\uc11c\ub294 python -m playwright install chromium \uc744 \ud55c \ubc88 \uc2e4\ud589\ud558\uba74 \ud574\uacb0\ub429\ub2c8\ub2e4."
            if not _is_frozen()
            else "\n\nexe\uc5d0\uc11c\ub294 Microsoft Edge \ub610\ub294 Chrome\uc774 \uc124\uce58\ub418\uc5b4 \uc788\uc5b4\uc57c \ud569\ub2c8\ub2e4."
        )
        raise RuntimeError(f"\uc0ac\uc6a9 \uac00\ub2a5\ud55c \ube0c\ub77c\uc6b0\uc800\ub97c \ucc3e\uc9c0 \ubabb\ud588\uc2b5\ub2c8\ub2e4: {last_error or exc}{hint}") from exc


def _body_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=10_000)
    except Exception:  # noqa: BLE001
        try:
            return page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        except Exception:  # noqa: BLE001
            return ""


def _looks_like_logged_in_order_page(channel: str, text: str, url: str) -> bool:
    lowered = (text or "").lower()
    has_order_hint = sum(1 for hint in ORDER_HINTS[channel] if hint in text) >= 2
    has_login_hint = any(hint in lowered or hint in text for hint in LOGIN_HINTS)
    return has_order_hint and not (has_login_hint and not has_order_hint)


def _wait_for_user_login(page, channel: str, progress: CrawlerProgress, timeout_seconds: int = 360) -> None:
    label = CHANNEL_LABELS.get(channel, channel)
    deadline = time.time() + timeout_seconds
    notified = False
    while time.time() < deadline:
        if progress.cancelled():
            raise RuntimeError("\uc0ac\uc6a9\uc790\uac00 \uc218\uc9d1\uc744 \ucde8\uc18c\ud588\uc2b5\ub2c8\ub2e4.")
        text = _body_text(page)
        if _looks_like_logged_in_order_page(channel, text, page.url):
            progress.on_log(f"{label} \ub85c\uadf8\uc778/\uc8fc\ubb38\ub0b4\uc5ed \ud655\uc778 \uc644\ub8cc")
            return
        if not notified:
            progress.on_login_required(
                f"{label} \ube0c\ub77c\uc6b0\uc800 \ucc3d\uc5d0\uc11c \uc9c1\uc811 \ub85c\uadf8\uc778\ud558\uace0, \uc8fc\ubb38\ub0b4\uc5ed \ud654\uba74\uc774 \ubcf4\uc774\uba74 \uadf8\ub300\ub85c \ub450\uc138\uc694."
            )
            notified = True
        time.sleep(2.0)
    raise RuntimeError(f"{label} \ub85c\uadf8\uc778/\uc8fc\ubb38\ub0b4\uc5ed \ud655\uc778 \uc2dc\uac04\uc774 \ucd08\uacfc\ub418\uc5c8\uc2b5\ub2c8\ub2e4.")


def _click_next_if_available(page) -> bool:
    selectors = [
        "button:has-text('\\ub2e4\\uc74c')",
        "a:has-text('\\ub2e4\\uc74c')",
        "button:has-text('\\ub354\\ubcf4\\uae30')",
        "a:has-text('\\ub354\\ubcf4\\uae30')",
        "[aria-label*='Next']",
        "[aria-label*='\\ub2e4\\uc74c']",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() == 0:
                continue
            if locator.is_visible(timeout=1000) and locator.is_enabled(timeout=1000):
                locator.click(timeout=3000)
                try:
                    page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    time.sleep(1.0)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _collect_records(page, channel: str, max_pages: int, progress: CrawlerProgress) -> List[PurchaseRecord]:
    parser = PurchaseHistoryParser()
    records: List[PurchaseRecord] = []
    seen = set()
    label = CHANNEL_LABELS.get(channel, channel)
    for page_no in range(1, max(1, max_pages) + 1):
        if progress.cancelled():
            break
        text = _body_text(page)
        parsed = parser.parse_text(channel, text, source_url=page.url)
        added = 0
        for record in parsed:
            key = (record.order_date, record.order_no, record.title, record.amount)
            if key in seen:
                continue
            seen.add(key)
            records.append(record)
            added += 1
        progress.on_log(f"{label} {page_no}\ud398\uc774\uc9c0: {added}\uac74 \ucd94\ucd9c")
        if page_no >= max_pages:
            break
        if not _click_next_if_available(page):
            break
    return records


def crawl_channel(
    channel: str,
    *,
    headless: bool = False,
    max_pages: int = 5,
    reset_session: bool = False,
    progress: Optional[CrawlerProgress] = None,
) -> CrawlResult:
    progress = progress or CrawlerProgress()
    channel = channel.lower().strip()
    if channel not in ORDER_URLS:
        return CrawlResult(channel=channel, records=[], error=f"\uc9c0\uc6d0\ud558\uc9c0 \uc54a\ub294 \ucc44\ub110: {channel}")

    # \ucfe0\ud321\uc740 Akamai Bot Manager \uac00 \uc77c\ubc18 Playwright launch \ub97c \ucc28\ub2e8\ud558\ubbc0\ub85c,
    # NTFS junction + CDP attach \ub85c \uc0ac\uc6a9\uc790 \ud3c9\uc0c1\uc2dc Chrome \ud504\ub85c\ud30c\uc77c\uc744 \ud65c\uc6a9\ud55c\ub2e4.
    if channel == "coupang":
        return _crawl_coupang_via_cdp(
            max_pages=max_pages,
            reset_session=reset_session,
            progress=progress,
        )

    if reset_session:
        shutil.rmtree(_profile_dir(channel), ignore_errors=True)

    try:
        _ensure_playwright()
        with sync_playwright() as pw:  # type: ignore[misc]
            context = _launch_persistent_context(pw, channel, headless, progress)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                progress.on_log(f"{CHANNEL_LABELS[channel]} \uc8fc\ubb38\ub0b4\uc5ed \ud398\uc774\uc9c0\ub85c \uc774\ub3d9 \uc911")
                page.goto(ORDER_URLS[channel], wait_until="domcontentloaded", timeout=60_000)
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass
                _wait_for_user_login(page, channel, progress)
                records = _collect_records(page, channel, max_pages, progress)
                progress.on_log(f"{CHANNEL_LABELS[channel]} \uc218\uc9d1 \uc644\ub8cc: {len(records)}\uac74")
                return CrawlResult(channel=channel, records=records)
            finally:
                context.close()
    except PlaywrightUnavailable as exc:
        return CrawlResult(channel=channel, records=[], error=str(exc))
    except Exception as exc:  # noqa: BLE001
        progress.on_log(f"\uc624\ub958: {exc}")
        return CrawlResult(channel=channel, records=[], error=str(exc))


# ---------------------------------------------------------------------------
# 쿠팡 전용: CDP attach + NTFS junction 우회
# ---------------------------------------------------------------------------


_COUPANG_DEBUG_PORT = 9223
_COUPANG_ORDER_URL = "https://mc.coupang.com/ssr/desktop/order/list"


def _find_chrome_path() -> Optional[str]:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        str(Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "Application" / "chrome.exe"),
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return None


def _real_chrome_user_data() -> Path:
    return Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data"


def _coupang_junction_path() -> Path:
    return Path.home() / ".smartinventory" / "chrome_junction"


def _setup_chrome_junction(progress: CrawlerProgress) -> bool:
    """NTFS junction 으로 사용자 평상시 Chrome User Data 를 다른 경로로 보이게 함.

    이렇게 하면 Chrome 보안 정책 (default user-data-dir 에 디버깅 거부) 우회 가능.
    """
    junction = _coupang_junction_path()
    real = _real_chrome_user_data()
    if not real.exists():
        progress.on_log(f"사용자 Chrome User Data 폴더 없음: {real}")
        return False
    if junction.exists():
        return True
    junction.parent.mkdir(parents=True, exist_ok=True)
    import subprocess as _sp
    cmd = ["cmd", "/c", "mklink", "/J", str(junction), str(real)]
    progress.on_log(f"Junction 생성: {junction.name} → {real.name}")
    try:
        result = _sp.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            progress.on_log(f"Junction 실패: {result.stderr.strip()}")
            return False
    except Exception as exc:  # noqa: BLE001
        progress.on_log(f"Junction 예외: {exc}")
        return False
    return True


def _kill_chrome(progress: CrawlerProgress) -> None:
    """Chrome 모든 인스턴스 종료. 쿠팡 자동수집 전 필수."""
    import subprocess as _sp
    progress.on_log("Chrome 모든 인스턴스 종료 중...")
    try:
        _sp.run(
            ["taskkill", "/F", "/IM", "chrome.exe", "/T"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:  # noqa: BLE001
        pass
    time.sleep(2)


def _start_chrome_with_debug(
    chrome_path: str, junction: Path, port: int, target_url: str, progress: CrawlerProgress
) -> Optional[int]:
    import subprocess as _sp
    # SingletonLock 정리
    for f in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (junction / f).unlink(missing_ok=True)  # type: ignore[arg-type]
        except Exception:
            pass

    args = [
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={junction}",
        "--profile-directory=Default",
        target_url,
    ]
    try:
        proc = _sp.Popen(args)
        progress.on_log(f"Chrome 디버깅 모드 시작 (port {port}, PID {proc.pid})")
        return proc.pid
    except Exception as exc:  # noqa: BLE001
        progress.on_log(f"Chrome 시작 실패: {exc}")
        return None


def _crawl_coupang_via_cdp(
    *,
    max_pages: int,
    reset_session: bool,
    progress: CrawlerProgress,
) -> CrawlResult:
    """쿠팡 전용 CDP attach 방식.

    1) Chrome 모두 종료
    2) NTFS junction 으로 사용자 평상시 user-data-dir 를 다른 경로처럼 보이게 함
    3) Chrome 을 디버깅 포트로 시작 → 사용자 평상시 cookies/세션 그대로 활용
    4) Playwright connect_over_cdp 로 attach
    5) 사용자 로그인 대기
    6) 주문 데이터 추출
    """
    if reset_session:
        # 세션 초기화 = junction 만 삭제 (사용자 실제 Chrome 데이터는 절대 안 건드림)
        try:
            jp = _coupang_junction_path()
            if jp.exists():
                import subprocess as _sp
                _sp.run(["cmd", "/c", "rmdir", str(jp)], capture_output=True, timeout=5)
        except Exception:  # noqa: BLE001
            pass

    chrome = _find_chrome_path()
    if not chrome:
        return CrawlResult(
            channel="coupang", records=[],
            error="Google Chrome 을 찾을 수 없습니다.\nhttps://www.google.com/chrome/ 에서 설치 후 재시도하세요.",
        )

    _ensure_playwright()

    _kill_chrome(progress)
    if not _setup_chrome_junction(progress):
        return CrawlResult(
            channel="coupang", records=[],
            error="Chrome 프로파일 junction 생성 실패. 평상시 Chrome 을 한 번 실행한 적이 있어야 합니다.",
        )
    junction = _coupang_junction_path()
    if not _start_chrome_with_debug(chrome, junction, _COUPANG_DEBUG_PORT, _COUPANG_ORDER_URL, progress):
        return CrawlResult(channel="coupang", records=[], error="Chrome 시작 실패")
    time.sleep(3)

    records: List[PurchaseRecord] = []
    try:
        with sync_playwright() as pw:  # type: ignore[misc]
            try:
                browser = pw.chromium.connect_over_cdp(f"http://localhost:{_COUPANG_DEBUG_PORT}")
            except Exception as exc:  # noqa: BLE001
                return CrawlResult(
                    channel="coupang", records=[],
                    error=(
                        f"CDP 연결 실패: {exc}\n\n"
                        "다른 Chrome 인스턴스가 디버깅 포트를 가로챘을 수 있습니다.\n"
                        "Chrome 을 모두 종료하고 다시 시도하세요."
                    ),
                )
            progress.on_log(f"CDP 연결 OK (contexts={len(browser.contexts)})")
            ctx = browser.contexts[0]

            # 쿠팡 탭 찾기 (poll)
            target_page = None
            deadline = time.time() + 480  # 8분
            notified_login = False
            last_state = ""
            while time.time() < deadline:
                if progress.cancelled():
                    return CrawlResult(channel="coupang", records=[], error="사용자 취소")

                cp = None
                for p in ctx.pages:
                    try:
                        if "coupang.com" in p.url:
                            cp = p
                            break
                    except Exception:
                        continue

                if cp is None:
                    time.sleep(2)
                    continue

                try:
                    cur = cp.url
                    content = cp.content()
                except Exception:
                    time.sleep(2)
                    continue

                state = cur[:80]
                if state != last_state:
                    progress.on_log(f"탭: {state}")
                    last_state = state

                # Akamai 차단 감지
                if "Access Denied" in content and "edgesuite" in content.lower():
                    progress.on_log("⚠ Akamai 차단 페이지. 사용자가 페이지 새로고침 / 재로그인 필요")
                    time.sleep(3)
                    continue

                # 로그인 페이지인지
                if "이메일 로그인" in content and "회원가입" in content:
                    if not notified_login:
                        progress.on_login_required("쿠팡 로그인이 필요합니다. 브라우저 창에서 직접 로그인해주세요.")
                        notified_login = True
                    time.sleep(3)
                    continue

                # 주문 페이지인지 (로그인된 상태)
                if "주문목록" in content or "최근 6개월" in content or "주문 상세보기" in content:
                    target_page = cp
                    progress.on_log("주문 페이지 도달!")
                    break

                time.sleep(3)

            if target_page is None:
                return CrawlResult(
                    channel="coupang", records=[],
                    error="주문 페이지에 도달하지 못했습니다 (시간 초과 또는 차단).",
                )

            # 데이터 추출 — 1 ~ max_pages 페이지 순회
            try:
                target_page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            time.sleep(1.5)

            # 첫 페이지 추출
            seen_keys: set[str] = set()
            page1 = _extract_coupang_orders(target_page, progress)
            detail_urls: List[str] = []
            detail_urls.extend(_extract_coupang_order_detail_links(target_page))
            for rec in page1:
                key = (rec.order_date or "", rec.title or "", rec.amount or 0)
                if key in seen_keys:
                    continue
                seen_keys.add(str(key))
                records.append(rec)
            progress.on_log(f"쿠팡 페이지 1: {len(page1)}건 · 상세링크 {len(detail_urls)}개")

            # 2 ~ max_pages 페이지 (URL ?pageIndex=N-1)
            for page_no in range(2, max(2, max_pages) + 1):
                if progress.cancelled():
                    break
                ok = _navigate_coupang_to_page(target_page, page_no, progress)
                if not ok:
                    progress.on_log(f"쿠팡 페이지 {page_no} 이동 실패. 종료.")
                    break
                try:
                    target_page.wait_for_load_state("networkidle", timeout=4_000)
                except Exception:
                    pass
                time.sleep(0.5)
                page_recs = _extract_coupang_orders(target_page, progress)
                page_links = _extract_coupang_order_detail_links(target_page)
                for h in page_links:
                    if h not in detail_urls:
                        detail_urls.append(h)
                added = 0
                for rec in page_recs:
                    key = (rec.order_date or "", rec.title or "", rec.amount or 0)
                    if str(key) in seen_keys:
                        continue
                    seen_keys.add(str(key))
                    records.append(rec)
                    added += 1
                progress.on_log(
                    f"쿠팡 페이지 {page_no}: {len(page_recs)}건 추출, 신규 {added} · "
                    f"상세링크 +{len(page_links)} (누계 {len(detail_urls)})"
                )

                # 마지막 페이지 자동 감지 — 쿠팡 안내 메시지로 판별
                try:
                    page_content = target_page.content()
                except Exception:
                    page_content = ""
                is_last = (
                    "마지막 내역입니다" in page_content
                    or "주문하신 내역이 없습니다" in page_content
                    or "조회된 주문 내역이 없습니다" in page_content
                )
                if is_last:
                    progress.on_log("✓ '마지막 내역' 안내 감지 → 자동 종료")
                    break
                if added == 0 and len(page_recs) == 0:
                    progress.on_log("새 데이터 없음 → 마지막 페이지 도달")
                    break

            # ── 주문 상세 페이지 순회: order_no + payment_total 수집 ──
            orders: List[PurchaseOrder] = []
            if detail_urls and not progress.cancelled():
                progress.on_log(
                    f"주문 상세 페이지 {len(detail_urls)}개 순회 시작 (결제 합계금 추출)"
                )
                orders = _crawl_coupang_order_details(target_page, detail_urls, progress)
                _associate_orders_to_records(orders, records)
                progress.on_log(
                    f"주문 상세 추출 완료: {len(orders)}개 주문 (결제금 미상 "
                    f"{sum(1 for o in orders if o.payment_total is None)}개)"
                )

    except Exception as exc:  # noqa: BLE001
        progress.on_log(f"오류: {exc}")
        return CrawlResult(channel="coupang", records=[], error=str(exc))
    finally:
        # 수집 종료 시 Chrome 창 자동 닫음
        try:
            _kill_chrome(progress)
            progress.on_log("Chrome 창 자동 종료")
        except Exception:  # noqa: BLE001
            pass

    final_orders = locals().get("orders") or []
    progress.on_log(f"쿠팡 수집 완료: 품목 {len(records)}건 / 주문 {len(final_orders)}개")
    return CrawlResult(
        channel="coupang",
        records=records,
        orders=final_orders,
    )


# 페이지 텍스트 → 주문 블록 → PurchaseRecord 추출
_COUPANG_DATE_RE = re.compile(r"(20\d{2})\.\s*(\d{1,2})\.\s*(\d{1,2})\s*주문")
_AMOUNT_RE = re.compile(r"([0-9][0-9,]{2,})\s*원")
_COUPANG_ORDER_NO_RE = re.compile(r"주문번호[:\s]*([0-9]{6,})")
_COUPANG_PAYMENT_TOTAL_RE = re.compile(
    r"(?:총\s*결제\s*금액|결제\s*금액|총\s*결제|결제예정금액)\s*[:\s]*([0-9][0-9,]+)\s*원"
)
_COUPANG_PAYMENT_METHOD_RE = re.compile(
    r"(신용카드|체크카드|쿠페이머니|쿠페이|간편결제|계좌이체|토스페이|카카오페이|페이코)"
    r"(?:\s*\(?([0-9*\-]{4,})\)?)?"
)


def _extract_coupang_order_detail_links(page) -> List[str]:
    """주문 리스트 페이지에서 '주문 상세보기' 링크들의 href 수집."""
    try:
        hrefs = page.evaluate(
            """() => {
                const out = new Set();
                document.querySelectorAll('a').forEach(a => {
                    const t = (a.innerText || '').trim();
                    if (t.includes('주문 상세') || t.includes('상세보기')) {
                        if (a.href) out.add(a.href);
                    }
                });
                return Array.from(out);
            }"""
        ) or []
    except Exception:  # noqa: BLE001
        hrefs = []
    return [str(h) for h in hrefs if h]


def _parse_coupang_order_detail(text: str) -> dict:
    """주문 상세 페이지 본문 텍스트에서 order_no / payment_total / status 추출."""
    info: dict = {}
    m = _COUPANG_ORDER_NO_RE.search(text or "")
    if m:
        info["order_no"] = m.group(1)
    m = _COUPANG_PAYMENT_TOTAL_RE.search(text or "")
    if m:
        try:
            info["payment_total"] = int(m.group(1).replace(",", ""))
        except ValueError:
            pass
    m = _COUPANG_PAYMENT_METHOD_RE.search(text or "")
    if m:
        method = m.group(1)
        last4 = m.group(2)
        info["payment_method"] = f"{method} {last4}".strip() if last4 else method
    # 주문일 (yyyy. M. d 주문)
    md = _COUPANG_DATE_RE.search(text or "")
    if md:
        info["order_date"] = f"{int(md.group(1)):04d}-{int(md.group(2)):02d}-{int(md.group(3)):02d}"
    # 상태
    for st in ("배송완료", "배송중", "배송준비중", "주문취소", "취소완료", "반품완료", "반품", "결제완료", "구매확정"):
        if st in (text or ""):
            info["status"] = st
            break
    return info


def _crawl_coupang_order_details(
    page,
    detail_urls: List[str],
    progress: CrawlerProgress,
    *,
    max_details: int = 60,
) -> List[PurchaseOrder]:
    """각 주문 상세 페이지를 방문해 order_no + payment_total 수집.

    리스트 페이지에서 모은 href 들을 순회하며 같은 탭에서 navigate.
    완료 후 다시 list 로 돌아갈 수 있게 history.back() 시도.
    """
    out: List[PurchaseOrder] = []
    now = datetime.now()
    seen: set[str] = set()
    for idx, url in enumerate(detail_urls[:max_details]):
        if progress.cancelled():
            break
        if url in seen:
            continue
        seen.add(url)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            time.sleep(0.4)
            try:
                page.wait_for_load_state("networkidle", timeout=4_000)
            except Exception:
                pass
            try:
                body = page.evaluate("() => document.body.innerText") or ""
            except Exception:
                body = ""
            info = _parse_coupang_order_detail(body)
            order_no = info.get("order_no")
            if not order_no:
                progress.on_log(f"  detail {idx + 1}: 주문번호 추출 실패 ({url[:60]})")
                continue
            order = PurchaseOrder(
                channel="coupang",
                order_no=str(order_no),
                order_date=info.get("order_date"),
                payment_total=info.get("payment_total"),
                item_count=0,  # 호출부에서 records 와 매핑 후 채움
                status=info.get("status"),
                payment_method=info.get("payment_method"),
                source_url=url,
                raw_text=(body or "")[:4000],
                imported_at=now,
            )
            out.append(order)
            pt = order.payment_total
            progress.on_log(
                f"  detail {idx + 1}/{min(len(detail_urls), max_details)}: "
                f"주문 {order_no} · {pt:,}원" if pt is not None
                else f"  detail {idx + 1}/{min(len(detail_urls), max_details)}: 주문 {order_no} · 결제금 미상"
            )
        except Exception as exc:  # noqa: BLE001
            progress.on_log(f"  detail {idx + 1} 실패: {exc}")
    return out


def _associate_orders_to_records(
    orders: List[PurchaseOrder],
    records: List[PurchaseRecord],
) -> None:
    """orders[].order_date 와 records[].order_date 가 일치하면 item_count 채움 +
    record.order_no 채워서 다음 매칭에서 활용 가능하게."""
    by_date: dict[str, List[PurchaseOrder]] = {}
    for o in orders:
        if o.order_date:
            by_date.setdefault(o.order_date, []).append(o)
    # 같은 날짜에 여러 주문이 있으면 1:N 매칭이 모호 — 단순화: 날짜당 1개일 때만 link
    for d, lst in by_date.items():
        if len(lst) != 1:
            continue
        o = lst[0]
        cnt = 0
        for r in records:
            if r.order_date == d and r.channel == o.channel:
                r.order_no = o.order_no
                cnt += 1
        o.item_count = cnt


def _scroll_to_bottom(page, progress: CrawlerProgress, max_iter: int = 3) -> None:
    """페이지 끝까지 반복 스크롤. 쿠팡 주문페이지는 페이지당 5건 고정이라 단순화.

    더 이상 스크롤되지 않으면 즉시 종료.
    """
    last_height = 0
    for _ in range(max_iter):
        try:
            cur_height = page.evaluate("() => document.body.scrollHeight")
        except Exception:  # noqa: BLE001
            break
        if cur_height == last_height:
            break
        last_height = cur_height
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            break
        time.sleep(0.3)


def _navigate_coupang_to_page(page, page_no: int, progress: CrawlerProgress) -> bool:
    """쿠팡 주문페이지 N번째로 이동.

    쿠팡 URL 패턴: `mc.coupang.com/ssr/desktop/order/list?pageIndex=N`

    매핑 (쿠팡은 0-base):
    - page_no=1: default URL (첫 페이지에서 이미 처리)
    - page_no=2: ?pageIndex=1
    - page_no=3: ?pageIndex=2
    - page_no=N: ?pageIndex=N-1
    """
    base = "https://mc.coupang.com/ssr/desktop/order/list"
    page_index = max(0, page_no - 1)
    new_url = f"{base}?pageIndex={page_index}"
    try:
        page.goto(new_url, wait_until="domcontentloaded", timeout=20_000)
        progress.on_log(f"  pageIndex={page_index} (앱 페이지 {page_no}) 이동")
        time.sleep(0.5)
        return True
    except Exception as exc:  # noqa: BLE001
        progress.on_log(f"  pageIndex={page_index} 이동 실패: {exc}")
        return False


def _extract_coupang_orders(page, progress: CrawlerProgress) -> List[PurchaseRecord]:
    """쿠팡 주문페이지 본문에서 PurchaseRecord 추출.

    DOM 구조 (sc-XXX 클래스 hash 동적이라 텍스트 기반 파싱):
    - 주문 wrapper: 텍스트 "YYYY. M. D 주문" 으로 시작하는 div
    - 그 안 tbody tr 가 상품 1개씩
    - tr inner_text: 배송상태\\n도착일\\n상품명\\n금액원\\n수량개\\n버튼들
    """
    records: List[PurchaseRecord] = []
    now = datetime.now()

    # 무한 스크롤 lazy load 트리거 — 모든 데이터가 DOM 에 올라오게
    _scroll_to_bottom(page, progress)

    # 페이지 전체 body 텍스트
    try:
        body = page.evaluate("() => document.body.innerText") or ""
    except Exception:
        return records

    # "20YY. M. D 주문" 으로 split
    parts = re.split(r"(?=20\d{2}\.\s*\d{1,2}\.\s*\d{1,2}\s*주문)", body)
    progress.on_log(f"본문 split → {len(parts)} 블록")

    for block in parts:
        block = block.strip()
        m = _COUPANG_DATE_RE.search(block)
        if not m:
            continue
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        order_date = f"{y:04d}-{mo:02d}-{d:02d}"

        # 블록 내에서 라인별 파싱. 한 주문에 여러 상품일 수 있음.
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        # 첫 줄은 "20YY. M. D 주문". 두번째는 보통 "주문 상세보기"
        # 상품 블록 패턴: [배송완료/취소/...] [도착일?] [상품명] [금액 원] [수량개]
        i = 0
        while i < len(lines):
            line = lines[i]
            # 상품 블록 시작점: "배송완료" / "주문취소" 같은 상태 키워드
            if re.match(r"^(배송완료|배송중|주문취소|취소완료|반품|반품완료|결제완료|결제취소|구매확정)$", line):
                status = line
                # 다음 줄들에서 상품명 + 금액 + 수량 찾기
                product = None
                amount = None
                qty = None
                j = i + 1
                while j < len(lines) and j < i + 10:
                    nl = lines[j]
                    if amount is None:
                        am = _AMOUNT_RE.search(nl)
                        if am:
                            try:
                                amount = int(am.group(1).replace(",", ""))
                            except ValueError:
                                pass
                    if qty is None:
                        qm = re.match(r"^(\d+)\s*개$", nl)
                        if qm:
                            qty = int(qm.group(1))
                    if product is None:
                        # 상품명 후보: 길고, 가격/수량/UI 버튼 텍스트가 아닌 줄.
                        # 상품명이 숫자로 시작해도 OK ("7세대 23L 진공청소기" 등)
                        is_price = bool(re.match(r"^[\d,]+\s*원$", nl))
                        is_qty = bool(re.match(r"^\d+\s*개$", nl))
                        is_button_or_ui = any(
                            kw in nl for kw in (
                                "도착", "장바구니", "배송", "주문", "상세보기",
                                "상품 등급", "리뷰", "교환", "반품 신청",
                                "재구매", "구매 확정", "취소 요청",
                            )
                        )
                        if (
                            len(nl) > 8
                            and not is_price
                            and not is_qty
                            and not is_button_or_ui
                        ):
                            product = nl
                    # 다음 상태 라인이면 상품 끝
                    if j > i and re.match(
                        r"^(배송완료|배송중|주문취소|취소완료|반품|반품완료|결제완료|결제취소|구매확정)$", nl
                    ):
                        break
                    j += 1

                if amount is not None and amount > 0:
                    title = product or "쿠팡 주문"
                    if status:
                        title = f"[{status}] {title}"
                    records.append(
                        PurchaseRecord(
                            id=None,
                            channel="coupang",
                            order_date=order_date,
                            order_no=None,
                            title=title[:120],
                            amount=amount,
                            payment_method=None,
                            source_url=_COUPANG_ORDER_URL,
                            raw_text=block[:2000],
                            imported_at=now,
                        )
                    )
                i = j
                continue
            i += 1

    return records


__all__ = [
    "CrawlerProgress",
    "CrawlResult",
    "PlaywrightUnavailable",
    "crawl_channel",
    "ensure_browser_installed",
]
