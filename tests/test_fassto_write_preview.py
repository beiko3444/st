import unittest

from inventory_app.ui.fassto_tab import _json_preview_text


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


if __name__ == "__main__":
    unittest.main()

