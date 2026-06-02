import unittest
from typing import Any, Dict, Mapping, Sequence

from inventory_app.connectors.fassto import (
    FasstoConnector,
    build_warehousing_statement_rows,
)


class _CaptureConnector(FasstoConnector):
    def __init__(self) -> None:
        super().__init__(
            api_cd="api-cd",
            api_key="api-key",
            cst_cd="CST01",
            api_url="https://example.invalid",
            timeout_seconds=3,
        )
        self.last_call: Dict[str, Any] | None = None

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
    ) -> Dict[str, Any]:
        self.last_call = {"method": method, "path": path, "body": body}
        return {"header": {"code": "200", "msg": "ok"}, "data": []}


class FasstoCancelAndStatementTests(unittest.TestCase):
    def test_cancel_warehousing_uses_expected_endpoint(self) -> None:
        connector = _CaptureConnector()
        payload: Sequence[Mapping[str, Any]] = [{"slipNo": "SLIP-001", "remark": "cancel"}]
        connector.cancel_warehousing(payload)

        assert connector.last_call is not None
        self.assertEqual(connector.last_call["method"], "PATCH")
        self.assertEqual(connector.last_call["path"], "/api/v1/warehousing/cancel/CST01")
        self.assertEqual(connector.last_call["body"], list(payload))

    def test_build_warehousing_statement_rows_maps_items_and_total(self) -> None:
        model = build_warehousing_statement_rows(
            {
                "goods": [
                    {
                        "cstGodCd": "A1",
                        "godNm": "item A",
                        "ordQty": 2,
                        "godBarcd": "B-A1",
                        "distTermMgtYn": "Y",
                        "distTermDt": "20260630",
                    },
                    {
                        "cstGodCd": "B2",
                        "goodsNm": "item B",
                        "tarQty": 3,
                        "godBarcode": "B-B2",
                        "distTermYn": "N",
                        "expirationDt": "20260701",
                    },
                ]
            }
        )

        self.assertEqual(model["total_qty"], 5)
        self.assertEqual(len(model["items"]), 2)
        self.assertEqual(model["items"][0]["code"], "A1")
        self.assertEqual(model["items"][0]["barcode"], "B-A1")
        self.assertEqual(model["items"][0]["dist_term_mgt_yn"], "Y")
        self.assertEqual(model["items"][0]["dist_term_dt"], "20260630")
        self.assertEqual(model["items"][1]["code"], "B2")
        self.assertEqual(model["items"][1]["name"], "item B")
        self.assertEqual(model["items"][1]["barcode"], "B-B2")
        self.assertEqual(model["items"][1]["dist_term_mgt_yn"], "N")
        self.assertEqual(model["items"][1]["dist_term_dt"], "20260701")


if __name__ == "__main__":
    unittest.main()
