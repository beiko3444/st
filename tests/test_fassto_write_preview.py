import unittest

from inventory_app.config import AppConfig
import inventory_app.ui.fassto_tab as fassto_tab_module
from PySide6.QtGui import QTextDocument
from inventory_app.ui.fassto_tab import (
    _code128_svg_data_uri,
    _json_preview_text,
    _show_statement_print_preview,
    _statement_table_html,
)


def _config(**overrides: str) -> AppConfig:
    values = {
        "smartstore_client_id": "id",
        "smartstore_client_secret": "secret",
        "smartstore_token_type": "Bearer",
        "smartstore_stats_client_id": "stats-id",
        "smartstore_stats_client_secret": "stats-secret",
        "smartstore_stats_token_type": "Bearer",
        "stats_lookback_days": 30,
        "coupang_vendor_id": "vendor",
        "coupang_access_key": "access",
        "coupang_secret_key": "secret",
        "timeout_seconds": 10,
        "max_products": 100,
        "statement_customer_name": "엑스트래커",
        "statement_supplier_biz_no": "",
        "statement_supplier_name": "미지정 공급사",
        "statement_supplier_ceo": "",
        "statement_supplier_addr": "",
        "statement_supplier_tel": "",
        "statement_buyer_biz_no": "",
        "statement_buyer_name": "",
        "statement_buyer_ceo": "",
        "statement_buyer_addr": "",
        "statement_buyer_tel": "",
    }
    values.update(overrides)
    return AppConfig(**values)


class FasstoWritePreviewTests(unittest.TestCase):
    def test_json_preview_text_is_capped_for_large_payload(self) -> None:
        payload = {
            "header": {"code": "200", "msg": "ok"},
            "data": [{"idx": i, "text": "x" * 500} for i in range(200)],
            "meta": {"nested": {"a": {"b": {"c": {"d": "too deep"}}}}},
        }
        preview = _json_preview_text(payload, max_chars=2000)

        self.assertLessEqual(len(preview), 2004)
        self.assertIn("...", preview)

    def test_code128_svg_data_uri(self) -> None:
        self.assertEqual(_code128_svg_data_uri(""), "")
        uri = _code128_svg_data_uri("YI21I0260518000316")
        self.assertTrue(uri.startswith("data:image/svg+xml;utf8,"))
        self.assertIn("%3Csvg", uri)

    def test_statement_table_html_matches_fassto_print_form(self) -> None:
        html = _statement_table_html(
            {
                "slipNo": "YI21I0260602000308",
                "remark": "입고 전 <검수> 필요",
                "goods": [
                    {
                        "cstGodCd": "2063926832706",
                        "godBarcd": "8809879791215",
                        "godNm": "럭베이트V3 상온보관 반건조 홍갯지렁이",
                        "ordQty": 100,
                        "distTermMgtYn": "N",
                    },
                    {
                        "cstGodCd": "2095142097233",
                        "godBarcd": "8809879791208",
                        "godNm": "럭베이트V3 상온보관 반건조 청갯지렁이",
                        "ordQty": 297,
                        "distTermMgtYn": "N",
                    },
                ],
            },
            _config(),
        )

        self.assertIn("<div class=\"title\">거래명세표</div>", html)
        self.assertIn("파스토 고객사명 : &nbsp; 엑스트래커", html)
        self.assertIn("공<br>급<br>자", html)
        self.assertIn("공<br>급<br>받<br>는<br>자", html)
        self.assertIn("372-81-00976", html)
        self.assertIn("주식회사 파스토", html)
        self.assertIn("홍종욱", html)
        self.assertIn("02-1566-3033", html)
        self.assertIn("data:image/svg+xml;utf8,", html)
        self.assertIn("YI21I0260602000308", html)
        self.assertIn("2063926832706", html)
        self.assertIn("8809879791215", html)
        self.assertIn("럭베이트V3 상온보관 반건조 홍갯지렁이", html)
        self.assertIn("합계 :</td><td class=\"num\">397</td>", html)
        self.assertIn("입고 전 &lt;검수&gt; 필요", html)
        self.assertNotIn("입고 전 <검수> 필요", html)

    def test_statement_customer_name_does_not_fall_back_to_supplier_name(self) -> None:
        html = _statement_table_html(
            {
                "slipNo": "YI21I0260602000308",
                "supNm": "상세응답 공급사",
                "goods": [],
            },
            _config(statement_customer_name=""),
        )

        self.assertIn("파스토 고객사명 : &nbsp; -", html)
        self.assertNotIn("파스토 고객사명 : &nbsp; 상세응답 공급사", html)

    def test_statement_print_preview_connects_document_print(self) -> None:
        calls: list[object] = []
        created: list["_FakePreviewDialog"] = []

        class _FakeSignal:
            def connect(self, callback: object) -> None:
                calls.append(callback)

        class _FakePreviewDialog:
            def __init__(self, printer: object, parent: object) -> None:
                self.printer = printer
                self.parent = parent
                self.paintRequested = _FakeSignal()
                self.title = ""
                created.append(self)

            def setWindowTitle(self, title: str) -> None:
                self.title = title

            def exec(self) -> int:
                return fassto_tab_module.QDialog.Accepted

        original = fassto_tab_module.QPrintPreviewDialog
        document = QTextDocument()
        try:
            fassto_tab_module.QPrintPreviewDialog = _FakePreviewDialog
            ok = _show_statement_print_preview(None, document, object())
        finally:
            fassto_tab_module.QPrintPreviewDialog = original

        self.assertTrue(ok)
        self.assertEqual(created[0].title, "거래명세표 미리보기")
        self.assertIs(calls[0].__self__, document)
        self.assertEqual(calls[0].__name__, "print_")


if __name__ == "__main__":
    unittest.main()

