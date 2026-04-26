"""네이버/쿠팡 개인 구매내역 자동 크롤러.

사용 방식:
1) 최초 1회 — 사용자가 ``playwright install chromium`` 으로 브라우저 다운로드
   (앱이 자동 호출도 가능. ensure_browser() 참고)
2) ``crawl_naver()`` / ``crawl_coupang()`` 호출 → 별도 창에서 Chromium 이 뜸
3) 로그인 상태가 없으면 사용자가 직접 로그인 → 세션 정보 디스크에 저장
4) 로그인된 채로 주문 페이지 이동 → 데이터 추출 → ``PurchaseRecord`` 리스트 반환

저장 위치: ``~/.smartinventory/playwright_state_{channel}.json``
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, List, Optional

from inventory_app.models import PurchaseRecord


# ---------------------------------------------------------------------------
# 경로/유틸
# ---------------------------------------------------------------------------


def _state_dir() -> Path:
    return (Path.home() / ".smartinventory").resolve()


def _state_path(channel: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", channel.lower()) or "channel"
    return _state_dir() / f"playwright_state_{safe}.json"


def _purge_state(channel: str) -> None:
    try:
        _state_path(channel).unlink(missing_ok=True)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 진행 상황 콜백
# ---------------------------------------------------------------------------


@dataclass
class CrawlerProgress:
    on_log: Callable[[str], None] = field(default=lambda _msg: None)
    on_login_required: Callable[[str], None] = field(default=lambda _msg: None)
    cancelled: Callable[[], bool] = field(default=lambda: False)


# ---------------------------------------------------------------------------
# Playwright 사전 점검
# ---------------------------------------------------------------------------


class PlaywrightUnavailable(RuntimeError):
    pass


def _import_playwright():
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
        return sync_playwright
    except Exception as exc:  # noqa: BLE001
        raise PlaywrightUnavailable(
            "playwright 패키지가 없습니다. 'pip install playwright' 후 'playwright install chromium' 을 실행하세요."
        ) from exc


def _is_frozen() -> bool:
    """PyInstaller 빌드된 exe 안에서 실행 중인지."""
    return getattr(sys, "frozen", False)


def ensure_browser_installed(progress: Optional[CrawlerProgress] = None) -> None:
    """크롤링용 브라우저 가용성 확인.

    우선순위:
    1) 시스템에 설치된 Microsoft Edge (Windows 기본) → 추가 설치 없이 사용
    2) Playwright Chromium (개발 환경에서 'playwright install chromium' 한 경우)
    3) 둘 다 없고 frozen exe 가 아니면 → playwright install 자동 호출
    4) frozen exe 인데 둘 다 없으면 → 사용자에게 Edge 안내
    """
    progress = progress or CrawlerProgress()
    sync_playwright = _import_playwright()

    # 1) Edge 시도
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=True)
            browser.close()
        progress.on_log("브라우저 OK: 시스템 Microsoft Edge")
        return
    except Exception:  # noqa: BLE001
        pass

    # 2) Playwright Chromium 시도
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            browser.close()
        progress.on_log("브라우저 OK: Playwright Chromium")
        return
    except Exception:  # noqa: BLE001
        pass

    # 3) Dev 환경: 자동 설치 시도
    if not _is_frozen():
        progress.on_log("Chromium 브라우저 설치 중... (최초 1회, 수백 MB 다운로드)")
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                check=True,
                capture_output=True,
                text=True,
                timeout=600,
            )
            if proc.stdout:
                progress.on_log(proc.stdout.strip()[-400:])
            progress.on_log("Chromium 설치 완료")
            return
        except Exception as exc:  # noqa: BLE001
            raise PlaywrightUnavailable(
                f"브라우저 설치 실패. 수동으로 'playwright install chromium' 실행 요망: {exc}"
            ) from exc

    # 4) Frozen + 브라우저 없음
    raise PlaywrightUnavailable(
        "브라우저를 찾을 수 없습니다.\n\n"
        "Microsoft Edge 설치 후 재시도하거나, 개발자에게 'playwright install chromium' 을 별도로 패키징해달라고 요청하세요.\n"
        "Windows 11 은 보통 Edge 가 기본 설치되어 있습니다."
    )


# ---------------------------------------------------------------------------
# 채널별 추출 로직
# ---------------------------------------------------------------------------


_AMOUNT_RE = re.compile(r"([0-9][0-9,]{2,})\s*원")


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    cleaned = re.sub(r"[^0-9]", "", str(value))
    try:
        return int(cleaned) if cleaned else None
    except ValueError:
        return None


def _max_amount(text: str) -> int | None:
    values = []
    for raw in _AMOUNT_RE.findall(text or ""):
        try:
            values.append(int(raw.replace(",", "")))
        except ValueError:
            continue
    return max(values) if values else None


def _norm_date(text: str | None) -> str | None:
    if not text:
        return None
    m = re.search(r"(20\d{2})[.\-/년\s]+(\d{1,2})[.\-/월\s]+(\d{1,2})", text)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _crawl_naver(page, max_pages: int, progress: CrawlerProgress) -> List[PurchaseRecord]:
    """네이버 페이 주문내역 페이지 수집.

    URL 후보:
    - https://order.pay.naver.com/home  (로그인 후 메인)
    - https://order.pay.naver.com/orderList?  (목록)
    """
    records: List[PurchaseRecord] = []
    now = datetime.now()
    target = "https://order.pay.naver.com/home"

    progress.on_log(f"네이버 주문내역 페이지 이동: {target}")
    page.goto(target, wait_until="domcontentloaded")
    # 로그인 안 된 경우 nid.naver.com 으로 리다이렉트됨
    if "nid.naver.com" in page.url:
        progress.on_login_required("네이버 로그인이 필요합니다. 브라우저에서 로그인 후 다시 시도해주세요.")
        # 사용자 로그인 대기 (URL 변경 감지) — 최대 5분
        page.wait_for_url(re.compile(r"https?://order\.pay\.naver\.com/.*"), timeout=300_000)
        progress.on_log("로그인 감지됨, 주문내역 수집 시작")
        page.goto(target, wait_until="domcontentloaded")

    # 페이지가 SPA 라서 데이터 렌더 대기
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(1.5)

    # 주문 카드 selector — 페이지 구조 변경 감안해 여러 후보
    card_selectors = [
        'li[class*="OrderItem"]',
        'div[class*="order_item"]',
        'div[class*="OrderCard"]',
        'section[class*="order"] li',
    ]

    for page_idx in range(1, max_pages + 1):
        if progress.cancelled():
            progress.on_log("취소 요청됨")
            break
        cards = []
        for sel in card_selectors:
            try:
                handles = page.query_selector_all(sel)
            except Exception:  # noqa: BLE001
                handles = []
            if handles:
                cards = handles
                break
        if not cards:
            # fallback: 페이지 전체 텍스트에서 블록 분리
            full_text = page.evaluate("() => document.body && document.body.innerText || ''")
            blocks = _split_text_blocks(full_text)
            for block in blocks:
                rec = _block_to_record("naver", block, target, now)
                if rec is not None:
                    records.append(rec)
            progress.on_log(f"네이버 페이지 {page_idx}: 카드 selector 실패 → 텍스트 추출로 {len(blocks)}건")
            break

        added_in_page = 0
        for card in cards:
            try:
                text = (card.inner_text() or "").strip()
            except Exception:  # noqa: BLE001
                continue
            rec = _block_to_record("naver", text, target, now)
            if rec is None:
                continue
            records.append(rec)
            added_in_page += 1
        progress.on_log(f"네이버 페이지 {page_idx}: {added_in_page}건 추출")

        # 다음 페이지 버튼 탐색
        next_btn = None
        for sel in ['button[aria-label*="다음"]', 'a[aria-label*="다음"]', 'button:has-text("다음")']:
            try:
                btn = page.query_selector(sel)
            except Exception:  # noqa: BLE001
                btn = None
            if btn and btn.is_enabled():
                next_btn = btn
                break
        if not next_btn:
            break
        try:
            next_btn.click()
            time.sleep(1.5)
            page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:  # noqa: BLE001
            break

    return records


def _crawl_coupang(page, max_pages: int, progress: CrawlerProgress) -> List[PurchaseRecord]:
    """쿠팡 주문 목록 페이지 수집."""
    records: List[PurchaseRecord] = []
    now = datetime.now()
    target = "https://mc.coupang.com/ssr/desktop/order/list"

    progress.on_log(f"쿠팡 주문내역 페이지 이동: {target}")
    page.goto(target, wait_until="domcontentloaded")
    if "login.coupang.com" in page.url or "loginInputId" in (page.content() or ""):
        progress.on_login_required("쿠팡 로그인이 필요합니다. 브라우저에서 로그인 후 다시 시도해주세요.")
        page.wait_for_url(re.compile(r"https?://mc\.coupang\.com/.*"), timeout=300_000)
        progress.on_log("로그인 감지됨, 주문내역 수집 시작")
        page.goto(target, wait_until="domcontentloaded")

    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(1.5)

    card_selectors = [
        'div[class*="orderListItem"]',
        'div[class*="order-item"]',
        'div[class*="OrderItem"]',
        'tr[class*="order"]',
    ]

    for page_idx in range(1, max_pages + 1):
        if progress.cancelled():
            progress.on_log("취소 요청됨")
            break
        cards = []
        for sel in card_selectors:
            try:
                handles = page.query_selector_all(sel)
            except Exception:  # noqa: BLE001
                handles = []
            if handles:
                cards = handles
                break
        if not cards:
            full_text = page.evaluate("() => document.body && document.body.innerText || ''")
            blocks = _split_text_blocks(full_text)
            for block in blocks:
                rec = _block_to_record("coupang", block, target, now)
                if rec is not None:
                    records.append(rec)
            progress.on_log(f"쿠팡 페이지 {page_idx}: 카드 selector 실패 → 텍스트 추출 {len(blocks)}건")
            break

        added_in_page = 0
        for card in cards:
            try:
                text = (card.inner_text() or "").strip()
            except Exception:  # noqa: BLE001
                continue
            rec = _block_to_record("coupang", text, target, now)
            if rec is None:
                continue
            records.append(rec)
            added_in_page += 1
        progress.on_log(f"쿠팡 페이지 {page_idx}: {added_in_page}건 추출")

        next_btn = None
        for sel in ['button:has-text("다음")', 'a:has-text("다음")', 'button[aria-label*="다음"]']:
            try:
                btn = page.query_selector(sel)
            except Exception:  # noqa: BLE001
                btn = None
            if btn and btn.is_enabled():
                next_btn = btn
                break
        if not next_btn:
            break
        try:
            next_btn.click()
            time.sleep(1.5)
            page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:  # noqa: BLE001
            break

    return records


def _split_text_blocks(text: str) -> List[str]:
    if not text:
        return []
    # 날짜 패턴을 기준으로 블록 분리
    parts = re.split(r"(?=20\d{2}[.\-/년\s]+\d{1,2}[.\-/월\s]+\d{1,2})", text)
    return [p.strip() for p in parts if p.strip() and "원" in p]


def _block_to_record(
    channel: str,
    text: str,
    source_url: str,
    now: datetime,
) -> Optional[PurchaseRecord]:
    text = (text or "").strip()
    if not text or "원" not in text:
        return None
    amount = _max_amount(text)
    if amount is None:
        return None
    order_date = _norm_date(text)
    order_no = _extract_order_no(text)
    title = _guess_title(text)
    return PurchaseRecord(
        id=None,
        channel=channel,
        order_date=order_date,
        order_no=order_no,
        title=title,
        amount=amount,
        payment_method=_extract_payment_method(text),
        source_url=source_url,
        raw_text=text[:2000],
        imported_at=now,
    )


def _extract_order_no(text: str) -> Optional[str]:
    for pat in (
        r"(?:주문번호|주문\s*번호|order\s*no\.?)\s*[:：]?\s*([A-Za-z0-9\-]{6,})",
        r"\b([0-9]{10,})\b",
    ):
        m = re.search(pat, text, flags=re.I)
        if m:
            return m.group(1)
    return None


def _extract_payment_method(text: str) -> Optional[str]:
    # 흔한 결제수단 키워드
    for keyword in (
        "카드", "신용카드", "체크카드", "계좌이체", "무통장", "휴대폰결제",
        "네이버페이", "카카오페이", "토스페이", "쿠페이", "페이코",
        "삼성페이", "현금영수증", "포인트",
    ):
        if keyword in text:
            return keyword
    return None


def _guess_title(text: str) -> str:
    # 날짜/금액/공통 라벨 제거
    trimmed = re.sub(r"\b20\d{2}[.\-/년\s]+\d{1,2}[.\-/월\s]+\d{1,2}\b", " ", text)
    trimmed = re.sub(r"[0-9][0-9,]{2,}\s*원", " ", trimmed)
    trimmed = re.sub(
        r"(주문번호|주문\s*번호|배송완료|결제완료|구매확정|주문상세|배송중|취소|반품|교환)",
        " ",
        trimmed,
    )
    trimmed = re.sub(r"\s+", " ", trimmed).strip()
    if len(trimmed) > 120:
        trimmed = trimmed[:117].rstrip() + "..."
    return trimmed or "구매내역"


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------


@dataclass
class CrawlResult:
    channel: str
    records: List[PurchaseRecord]
    error: Optional[str] = None


def crawl_channel(
    channel: str,
    *,
    headless: bool = False,
    max_pages: int = 5,
    reset_session: bool = False,
    progress: Optional[CrawlerProgress] = None,
) -> CrawlResult:
    """네이버/쿠팡 자동 수집.

    headless=False 권장 — 최초 로그인 시 사용자가 입력해야 함.
    reset_session=True 면 저장된 세션 폐기 후 재로그인.
    """
    progress = progress or CrawlerProgress()
    channel = channel.lower()
    if channel not in {"naver", "coupang"}:
        return CrawlResult(channel=channel, records=[], error=f"지원 안 함: {channel}")

    if reset_session:
        _purge_state(channel)

    sync_playwright = _import_playwright()

    state_dir = _state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = _state_path(channel)

    records: List[PurchaseRecord] = []
    error: Optional[str] = None

    try:
        with sync_playwright() as pw:
            launch_kwargs = {
                "headless": headless,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
            }
            # 시스템 Edge 우선 (PyInstaller 빌드 환경에서 별도 다운로드 불필요)
            try:
                browser = pw.chromium.launch(channel="msedge", **launch_kwargs)
                progress.on_log("브라우저: 시스템 Edge")
            except Exception:  # noqa: BLE001
                browser = pw.chromium.launch(**launch_kwargs)
                progress.on_log("브라우저: Playwright Chromium")
            context_kwargs: dict = {
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0.0.0 Safari/537.36"
                ),
                "locale": "ko-KR",
                "timezone_id": "Asia/Seoul",
                "viewport": {"width": 1280, "height": 900},
            }
            if state_file.exists():
                try:
                    context_kwargs["storage_state"] = str(state_file)
                    progress.on_log(f"이전 로그인 세션 로드: {state_file.name}")
                except Exception:  # noqa: BLE001
                    pass

            context = browser.new_context(**context_kwargs)
            page = context.new_page()

            try:
                if channel == "naver":
                    records = _crawl_naver(page, max_pages=max_pages, progress=progress)
                else:
                    records = _crawl_coupang(page, max_pages=max_pages, progress=progress)

                # 로그인 성공 시 세션 저장
                try:
                    context.storage_state(path=str(state_file))
                    progress.on_log(f"세션 저장 완료: {state_file.name}")
                except Exception as exc:  # noqa: BLE001
                    progress.on_log(f"세션 저장 실패(무시): {exc}")
            finally:
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
        progress.on_log(f"오류: {error}")

    return CrawlResult(channel=channel, records=records, error=error)


__all__ = [
    "CrawlerProgress",
    "CrawlResult",
    "PlaywrightUnavailable",
    "crawl_channel",
    "ensure_browser_installed",
]
