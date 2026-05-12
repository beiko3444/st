import unittest

from inventory_app.connectors.fassto import normalize_fassto_warehousings


class FasstoWarehousingStatusTests(unittest.TestCase):
    def test_cancelled_warehousing_status_code_gets_readable_name(self) -> None:
        rows = normalize_fassto_warehousings(
            [
                {
                    "slipNo": "YI21IO260512000260",
                    "ordDt": "20260512",
                    "wrkStat": "5",
                    "wrkStatNm": None,
                }
            ]
        )

        self.assertEqual(rows[0].wrkStat, "5")
        self.assertEqual(rows[0].wrkStatNm, "입고취소")


if __name__ == "__main__":
    unittest.main()
