"""production 쿠팡 크롤러 테스트."""

import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from inventory_app.services.purchase_crawler import (
    CrawlerProgress,
    crawl_channel,
)


def main():
    progress = CrawlerProgress(
        on_log=lambda m: print(f"[LOG] {m}", flush=True),
        on_login_required=lambda m: print(f"[LOGIN] {m}", flush=True),
        cancelled=lambda: False,
    )
    result = crawl_channel(
        "coupang",
        headless=False,
        max_pages=3,
        reset_session=False,
        progress=progress,
    )
    print(f"\n[RESULT] error={result.error}", flush=True)
    print(f"[RESULT] records={len(result.records)}", flush=True)
    for i, r in enumerate(result.records[:10], 1):
        print(
            f"  {i}. date={r.order_date} | title={r.title[:60]} | amount={r.amount}",
            flush=True,
        )


if __name__ == "__main__":
    main()
