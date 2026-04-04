from __future__ import annotations

import unittest

from inventory_app.services.channel_services import _summarize_naver_sales_error


class NaverWarningSummaryTests(unittest.TestCase):
    def test_403_auth_error_is_summarized(self) -> None:
        raw = (
            "https://api.commerce.naver.com/external/v1/bizdata-stats/channels/123/sales/product/detail"
            "?startDate=2026-03-07&endDate=2026-03-20 -> HTTP 403 "
            "(GW.AUTHN: 요청을 보낼 권한이 없습니다.)"
        )
        summary = _summarize_naver_sales_error(raw)
        self.assertIn("HTTP 403", summary)
        self.assertIn("판매량은 0으로 표시됩니다", summary)
        self.assertNotIn("bizdata-stats/channels/123", summary)

    def test_generic_error_is_compacted(self) -> None:
        raw = "line1\nline2\nline3"
        summary = _summarize_naver_sales_error(raw)
        self.assertTrue(summary.startswith("네이버 판매량 조회 실패:"))
        self.assertIn("line1 line2 line3", summary)


if __name__ == "__main__":
    unittest.main()
