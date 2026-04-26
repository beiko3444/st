"""실제 크롤러 호출 테스트."""

import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from inventory_app.services.purchase_crawler import (
    CrawlerProgress,
    crawl_channel,
    ensure_browser_installed,
)


def main():
    progress = CrawlerProgress(
        on_log=lambda m: print(f"[LOG] {m}", flush=True),
        on_login_required=lambda m: print(f"[LOGIN] {m}", flush=True),
        cancelled=lambda: False,
    )
    print("[STEP] 브라우저 가용성 확인", flush=True)
    try:
        ensure_browser_installed(progress)
    except Exception as e:
        print(f"[ERROR] {e}", flush=True)
        return

    print("[STEP] 네이버 크롤링 시작", flush=True)
    result = crawl_channel(
        "naver",
        headless=False,
        max_pages=2,
        reset_session=False,
        progress=progress,
    )
    print(f"\n[RESULT] error={result.error}", flush=True)
    print(f"[RESULT] records={len(result.records)}", flush=True)
    for i, r in enumerate(result.records[:10], 1):
        print(
            f"  {i}. date={r.order_date} | order_no={r.order_no} | "
            f"title={r.title[:50]} | amount={r.amount} | pay={r.payment_method}",
            flush=True,
        )


if __name__ == "__main__":
    main()
