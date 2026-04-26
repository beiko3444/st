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


def _user_data_dir(channel: str) -> Path:
    """채널별 영구 user data directory.

    persistent_context 가 사용. 로그인/쿠키/캐시 모두 유지됨 →
    매번 깨끗한 프로파일이 아니라 진짜 일반 브라우저처럼 보여서 안티봇 우회 가능.
    """
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", channel.lower()) or "channel"
    return _state_dir() / f"browser_profile_{safe}"


def _purge_state(channel: str) -> None:
    try:
        _state_path(channel).unlink(missing_ok=True)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        pass
    # user_data_dir 도 같이 비움 (재로그인)
    import shutil
    udir = _user_data_dir(channel)
    if udir.exists():
        try:
            shutil.rmtree(udir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


# 봇 감지 우회용 init 스크립트 (Coupang Akamai 등 차단 회피)
_STEALTH_INIT_SCRIPT = """
// 1) navigator.webdriver 숨김 (Playwright 의 가장 확실한 마커)
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// 2) 일반 Chrome 처럼 plugins, languages 속성 제공
Object.defineProperty(navigator, 'languages', {
    get: () => ['ko-KR', 'ko', 'en-US', 'en']
});
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        { name: 'PDF Viewer' },
        { name: 'Chrome PDF Viewer' },
        { name: 'Chromium PDF Viewer' },
        { name: 'Microsoft Edge PDF Viewer' },
        { name: 'WebKit built-in PDF' }
    ]
});

// 3) chrome 객체 위장
window.chrome = window.chrome || { runtime: {} };

// 4) permissions API 응답 정상화 (자동화에선 default 라 의심받음)
const origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (origQuery) {
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : origQuery(parameters)
    );
}

// 5) WebGL vendor / renderer 정상화
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) return 'Intel Inc.';
    if (parameter === 37446) return 'Intel Iris OpenGL Engine';
    return getParameter.call(this, parameter);
};
"""


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


def _detect_browser_channel(progress: CrawlerProgress) -> Optional[str]:
    """시스템에 설치된 Chrome/Edge 가용성 확인. 사용 가능한 channel 반환.

    반환값:
    - "chrome": 시스템 Google Chrome
    - "msedge": 시스템 Microsoft Edge
    - None: Playwright 기본 Chromium (별도 다운로드 필요)
    """
    sync_playwright = _import_playwright()
    for channel in ("chrome", "msedge"):
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel=channel, headless=True)
                browser.close()
            return channel
        except Exception:  # noqa: BLE001
            continue
    return None


def ensure_browser_installed(progress: Optional[CrawlerProgress] = None) -> None:
    """크롤링용 브라우저 가용성 확인.

    우선순위:
    1) 시스템 Google Chrome (대부분 사용자)
    2) 시스템 Microsoft Edge (Windows 11 기본)
    3) Playwright Chromium (개발 환경에서 'playwright install chromium' 한 경우)
    4) 위 셋 다 없고 frozen exe 가 아니면 → playwright install 자동 호출
    5) frozen exe 인데 다 없으면 → 안내
    """
    progress = progress or CrawlerProgress()
    sync_playwright = _import_playwright()

    # 1, 2) 시스템 Chrome/Edge
    channel = _detect_browser_channel(progress)
    if channel == "chrome":
        progress.on_log("브라우저 OK: 시스템 Google Chrome")
        return
    if channel == "msedge":
        progress.on_log("브라우저 OK: 시스템 Microsoft Edge")
        return

    # 3) Playwright Chromium 시도
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            browser.close()
        progress.on_log("브라우저 OK: Playwright Chromium")
        return
    except Exception:  # noqa: BLE001
        pass

    # 4) Dev 환경: 자동 설치 시도
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

    # 5) Frozen + 브라우저 없음
    raise PlaywrightUnavailable(
        "브라우저를 찾을 수 없습니다.\n\n"
        "Google Chrome 또는 Microsoft Edge 를 설치한 뒤 재시도하세요.\n"
        "(Chrome 다운로드: https://www.google.com/chrome/)"
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


def _wait_off_login(page, host_keyword: str, timeout_sec: int = 240) -> bool:
    """CSP-safe polling: URL 에 host_keyword 가 있는 동안 대기.

    네이버 로그인 페이지는 CSP 로 wait_for_function/evaluate 의 `unsafe-eval` 을 막아
    Playwright wait_for_function 이 EvalError 로 즉시 실패한다.
    그래서 page.url 만 폴링.
    """
    start = time.time()
    while time.time() - start < timeout_sec:
        try:
            url = page.url
        except Exception:  # noqa: BLE001
            url = ""
        if host_keyword not in url:
            return True
        time.sleep(2)
    return False


def _crawl_naver(page, max_pages: int, progress: CrawlerProgress) -> List[PurchaseRecord]:
    """네이버 페이 결제내역 페이지 (https://pay.naver.com/pc/history) 수집.

    DOM 구조 (CSS Modules — 클래스에 hash suffix):
    - 결제 1건 컨테이너: [class*="PaymentItem_article"]
    - 상품명:           [class*="PaymentItem_product__"]
    - 금액:             [class*="PaymentItem_price"]
    - 시간:             [class*="PaymentItem_time"]
    - 주문상세:         [class*="PaymentItem_order-detail"]
    - 상태:             [class*="OrderStatus_article"]

    페이지네이션: URL ?page=N 으로 직접 이동.
    """
    records: List[PurchaseRecord] = []
    now = datetime.now()
    base_url = "https://pay.naver.com/pc/history"

    # 1) 진입
    try:
        page.goto(f"{base_url}?page=1", wait_until="domcontentloaded", timeout=30_000)
    except Exception as exc:  # noqa: BLE001
        progress.on_log(f"페이지 이동 오류(무시): {exc}")

    # 2) 로그인 처리 (네이버 ID 페이지)
    if "nid.naver.com" in page.url:
        progress.on_login_required(
            "네이버 로그인이 필요합니다. 브라우저 창에서 직접 로그인해주세요."
        )
        if not _wait_off_login(page, "nid.naver.com", timeout_sec=300):
            progress.on_log("로그인 대기 시간 초과")
            return records
        progress.on_log(f"로그인 감지됨 ({page.url[:80]}). 결제내역 페이지로 이동")
        try:
            page.goto(f"{base_url}?page=1", wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:  # noqa: BLE001
            progress.on_log(f"재이동 실패: {exc}")
            return records

    if "pay.naver.com" not in page.url:
        progress.on_log(f"결제내역 페이지 진입 실패. 현재: {page.url[:120]}")
        return records

    # 3) 페이지별 추출
    seen_keys: set[str] = set()  # (order_no or title+amount+date) 로 중복 방지
    for page_idx in range(1, max_pages + 1):
        if progress.cancelled():
            progress.on_log("취소 요청됨")
            break

        if page_idx > 1:
            try:
                page.goto(f"{base_url}?page={page_idx}", wait_until="domcontentloaded", timeout=30_000)
            except Exception as exc:  # noqa: BLE001
                progress.on_log(f"페이지 {page_idx} 이동 실패: {exc}")
                break

        # 데이터 렌더 대기
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:  # noqa: BLE001
            pass
        try:
            page.wait_for_selector('[class*="PaymentItem_article"]', timeout=10_000)
        except Exception:  # noqa: BLE001
            progress.on_log(f"페이지 {page_idx}: 결제 항목 못 찾음 → 종료")
            break
        time.sleep(1.0)

        articles = page.query_selector_all('[class*="PaymentItem_article"]')
        if not articles:
            progress.on_log(f"페이지 {page_idx}: 항목 0건 → 종료")
            break

        added = 0
        for art in articles:
            try:
                rec = _extract_naver_payment_item(art, base_url, now)
            except Exception as exc:  # noqa: BLE001
                progress.on_log(f"  항목 파싱 오류(skip): {exc}")
                continue
            if rec is None:
                continue
            key = rec.order_no or f"{rec.order_date}|{rec.title}|{rec.amount}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            records.append(rec)
            added += 1
        progress.on_log(f"네이버 페이지 {page_idx}: {added}건 추출 (총 {len(records)})")

        if added == 0:
            break

    return records


def _extract_naver_payment_item(article_handle, base_url: str, now: datetime) -> Optional[PurchaseRecord]:
    """PaymentItem_article 컨테이너 1개에서 PurchaseRecord 추출.

    실제 DOM 구조 (CSS Modules):
    - 상품명 정확 위치:  [class*="ProductName_name"]   (예: "허니콤보")
    - 가격:              [class*="PaymentItem_price"]
    - 결제시간:          [class*="PaymentItem_time"]
    - 상태:              [class*="OrderStatus_value"]
    - 주문상세 링크:     [class*="PaymentItem_view-detail"]  href 에 주문번호 포함
    - 상품 이미지 alt:   PaymentItem_product-detail 안의 img.alt — fallback 용
    """

    def _q_text(sel: str) -> str:
        try:
            el = article_handle.query_selector(sel)
            if el is None:
                return ""
            return (el.inner_text() or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _q_attr(sel: str, attr: str) -> str:
        try:
            el = article_handle.query_selector(sel)
            if el is None:
                return ""
            return (el.get_attribute(attr) or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    full_text = ""
    try:
        full_text = (article_handle.inner_text() or "").strip()
    except Exception:  # noqa: BLE001
        pass
    if not full_text:
        return None

    # 상품명: ProductName_name 안의 텍스트가 가장 정확
    product = _q_text('[class*="ProductName_name"]')
    if not product:
        # fallback 1: img alt (PaymentItem_product-detail 안)
        product = _q_attr('[class*="PaymentItem_product-detail"] img', "alt")
    if not product:
        product = _guess_title(full_text)

    price_text = _q_text('[class*="PaymentItem_price"]')
    time_text = _q_text('[class*="PaymentItem_time"]')
    status = _q_text('[class*="OrderStatus_value"]') or _q_text(
        '[class*="OrderStatus_article"]'
    )

    amount = _max_amount(price_text or full_text)
    if amount is None or amount <= 0:
        return None

    title = product.split("\n", 1)[0].strip() if "\n" in product else product
    title = title.strip() or "구매내역"
    if status:
        status_clean = status.split("\n", 1)[0].strip()[:20]
        if status_clean and status_clean not in title:
            title = f"[{status_clean}] {title}"

    order_date = _norm_naver_date(time_text or full_text, now)

    # 주문번호: view-detail 링크 href 에서 /detail/{slipNo} 추출
    detail_href = _q_attr('[class*="PaymentItem_view-detail"]', "href")
    order_no = _extract_naver_order_no_from_url(detail_href) or _extract_order_no(full_text)

    # source_url 도 detail href 가 있으면 그걸로 (개별 주문 페이지 직링크)
    src_url = detail_href or base_url

    return PurchaseRecord(
        id=None,
        channel="naver",
        order_date=order_date,
        order_no=order_no,
        title=title[:120],
        amount=amount,
        payment_method=_extract_payment_method(full_text),
        source_url=src_url,
        raw_text=full_text[:2000],
        imported_at=now,
    )


def _extract_naver_order_no_from_url(url: str) -> Optional[str]:
    """orders.pay.naver.com/.../detail/{slipNo} 형식에서 slipNo 추출."""
    if not url:
        return None
    m = re.search(r"/detail/([A-Za-z0-9]+)", url)
    if m:
        return m.group(1)
    return None


def _norm_naver_date(text: str, now: datetime) -> Optional[str]:
    """네이버 페이의 '4. 19. 17:49' 같은 표기에서 날짜 추출.

    연도 미표기 → now.year 로 가정 (12월 → 1월 같은 경계는 그대로 두되
    미래 날짜면 작년으로 보정).
    """
    if not text:
        return None
    # 표준 ISO/일반 패턴 먼저
    iso = _norm_date(text)
    if iso:
        return iso
    # "M. D." 또는 "M.D" 또는 "M월 D일"
    m = re.search(r"\b(\d{1,2})\.\s*(\d{1,2})\.", text)
    if not m:
        m = re.search(r"\b(\d{1,2})\s*월\s*(\d{1,2})", text)
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    year = now.year
    candidate = f"{year:04d}-{month:02d}-{day:02d}"
    # 미래 날짜면 작년
    try:
        cand_dt = datetime.strptime(candidate, "%Y-%m-%d")
        if cand_dt > now:
            year -= 1
            candidate = f"{year:04d}-{month:02d}-{day:02d}"
    except Exception:  # noqa: BLE001
        pass
    return candidate


def _crawl_coupang(page, max_pages: int, progress: CrawlerProgress) -> List[PurchaseRecord]:
    """쿠팡 주문 목록 페이지 수집."""
    records: List[PurchaseRecord] = []
    now = datetime.now()
    target = "https://mc.coupang.com/ssr/desktop/order/list"

    progress.on_log(f"쿠팡 주문내역 페이지 이동: {target}")
    try:
        page.goto(target, wait_until="domcontentloaded", timeout=30_000)
    except Exception as exc:  # noqa: BLE001
        progress.on_log(f"페이지 이동 오류(무시): {exc}")

    # 로그인 페이지로 갔는지 확인
    if "login.coupang.com" in page.url:
        progress.on_login_required(
            "쿠팡 로그인이 필요합니다. 브라우저 창에서 직접 로그인해주세요."
        )
        if not _wait_off_login(page, "login.coupang.com", timeout_sec=300):
            progress.on_log("로그인 대기 시간 초과")
            return records
        progress.on_log(f"로그인 감지됨 (현재 URL: {page.url[:80]}). 주문내역 페이지로 이동")
        try:
            page.goto(target, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:  # noqa: BLE001
            progress.on_log(f"주문내역 진입 실패: {exc}")
            return records

    if "login.coupang.com" in page.url:
        progress.on_log("아직 로그인 페이지. 사용자가 로그인 마치고 재시도하세요.")
        return records

    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(2.0)

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
    user_data_dir = _user_data_dir(channel)
    user_data_dir.mkdir(parents=True, exist_ok=True)

    records: List[PurchaseRecord] = []
    error: Optional[str] = None

    try:
        with sync_playwright() as pw:
            # persistent_context 사용:
            # - 영구 user_data_dir → 진짜 일반 브라우저처럼 동작 (Akamai 등 안티봇 우회)
            # - 로그인 상태도 자연스레 유지 (별도 storage_state 불필요)
            # - 첫 로그인은 사용자가 직접, 이후엔 쿠키/세션이 dir 안에 보관됨
            launch_kwargs = {
                "user_data_dir": str(user_data_dir),
                "headless": headless,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-features=IsolateOrigins,site-per-process",
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
                "extra_http_headers": {
                    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                },
            }
            # 시스템 Chrome → Edge → Playwright Chromium 순으로 시도.
            context = None
            last_err: Exception | None = None
            for channel_name, label in (("chrome", "Google Chrome"), ("msedge", "Microsoft Edge")):
                try:
                    context = pw.chromium.launch_persistent_context(
                        channel=channel_name,
                        **launch_kwargs,
                    )
                    progress.on_log(f"브라우저: 시스템 {label} (persistent profile)")
                    break
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    continue
            if context is None:
                try:
                    context = pw.chromium.launch_persistent_context(**launch_kwargs)
                    progress.on_log("브라우저: Playwright Chromium (persistent profile)")
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(
                        f"사용 가능한 브라우저가 없습니다. Chrome 또는 Edge 설치 후 재시도하세요.\n"
                        f"마지막 오류: {last_err or exc}"
                    ) from exc

            # 봇 감지 우회 init 스크립트 (모든 페이지에 적용)
            try:
                context.add_init_script(_STEALTH_INIT_SCRIPT)
            except Exception:  # noqa: BLE001
                pass

            # persistent context 는 기본 페이지가 있을 수 있음
            page = context.pages[0] if context.pages else context.new_page()

            try:
                if channel == "naver":
                    records = _crawl_naver(page, max_pages=max_pages, progress=progress)
                else:
                    records = _crawl_coupang(page, max_pages=max_pages, progress=progress)
                progress.on_log("크롤링 완료. 세션은 user_data_dir 에 자동 저장됨")
            finally:
                try:
                    context.close()
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
