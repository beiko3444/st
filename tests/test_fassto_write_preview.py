import unittest

from inventory_app.ui.fassto_tab import _code128_svg_data_uri, _json_preview_text


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


if __name__ == "__main__":
    unittest.main()

