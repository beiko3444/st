"""쿠팡 주문 페이지의 연도 필터 element 찾기."""

import io
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from inventory_app.services.purchase_crawler import (
    _find_chrome_path, _setup_chrome_junction, _kill_chrome,
    _start_chrome_with_debug, _coupang_junction_path,
    CrawlerProgress, _COUPANG_DEBUG_PORT, _COUPANG_ORDER_URL,
)

progress = CrawlerProgress(on_log=lambda m: print(f"[LOG] {m}", flush=True))
_kill_chrome(progress)
_setup_chrome_junction(progress)
chrome = _find_chrome_path()
junction = _coupang_junction_path()
_start_chrome_with_debug(chrome, junction, _COUPANG_DEBUG_PORT, _COUPANG_ORDER_URL, progress)
time.sleep(8)

from playwright.sync_api import sync_playwright
with sync_playwright() as pw:
    browser = pw.chromium.connect_over_cdp(f"http://localhost:{_COUPANG_DEBUG_PORT}")
    ctx = browser.contexts[0]
    cp = None
    for p in ctx.pages:
        try:
            if "coupang.com" in p.url:
                cp = p
                break
        except Exception:
            continue
    if cp is None:
        print("[ERR] no coupang tab", flush=True)
        sys.exit()

    # 페이지 도달까지 대기
    for _ in range(20):
        try:
            content = cp.content()
            if "주문목록" in content or "최근 6개월" in content:
                break
        except Exception:
            pass
        time.sleep(2)

    # 연도/필터 element 찾기
    print("\n=== 연도 텍스트 element 분석 ===", flush=True)
    for year in (2025, 2024, 2023):
        target = str(year)
        # JS 로 모든 element 중 정확히 그 텍스트 가진 것 찾기
        try:
            results = cp.evaluate(f"""
                (() => {{
                    const all = document.querySelectorAll('*');
                    const matches = [];
                    for (const el of all) {{
                        // 직접 자식 텍스트 노드만 보기 (자식 내부 element 제외)
                        const text = Array.from(el.childNodes)
                            .filter(n => n.nodeType === Node.TEXT_NODE)
                            .map(n => n.textContent.trim())
                            .join('').trim();
                        if (text === '{target}') {{
                            matches.push({{
                                tag: el.tagName,
                                cls: el.className,
                                outer: el.outerHTML.substring(0, 200),
                                parent_tag: el.parentElement ? el.parentElement.tagName : null,
                                parent_cls: el.parentElement ? el.parentElement.className : null,
                                clickable: el.tagName === 'BUTTON' || el.tagName === 'A' || (el.parentElement && (el.parentElement.tagName === 'BUTTON' || el.parentElement.tagName === 'A')),
                            }});
                        }}
                    }}
                    return matches.slice(0, 5);
                }})()
            """)
            print(f"\n--- 연도 '{target}' 매칭 ---", flush=True)
            for r in results:
                print(f"  tag={r['tag']}, parent={r['parent_tag']}, clickable={r['clickable']}", flush=True)
                print(f"  cls: {r['cls']}", flush=True)
                print(f"  outer: {r['outer']}", flush=True)
        except Exception as e:
            print(f"  [ERR] {e}", flush=True)

    # 페이지네이션/더보기 등 키워드 가진 element
    print("\n=== '더보기' / pagination ===", flush=True)
    try:
        results = cp.evaluate("""
            (() => {
                const candidates = [];
                document.querySelectorAll('button, a').forEach(el => {
                    const t = (el.innerText || '').trim();
                    if (/더보기|더 보기|다음|next|이전|prev/i.test(t) && t.length < 30) {
                        candidates.push({tag: el.tagName, text: t, cls: el.className});
                    }
                });
                return candidates;
            })()
        """)
        for r in results[:10]:
            print(f"  {r['tag']} '{r['text']}' cls={r['cls'][:60]}", flush=True)
    except Exception as e:
        print(f"  [ERR] {e}", flush=True)

    print("\n=== 완료 ===", flush=True)
