import unittest

from inventory_app.connectors.fassto import build_warehousing_payload


class FasstoWarehousingPayloadTests(unittest.TestCase):
    def test_warehousing_payload_uses_swagger_god_cds_field(self) -> None:
        payload = build_warehousing_payload(
            {
                "ordNo": "IN-TEST",
                "ordDt": "20260512",
                "inWay": "02",
                "whCd": "YI21",
                "supCd": "99999999",
                "goods": [
                    {
                        "cstGodCd": "2003834450610",
                        "ordQty": 2,
                        "goodsSerialNo": ["S1"],
                    }
                ],
                "remark": "memo",
            }
        )

        self.assertEqual(
            payload,
            {
                "ordNo": "IN-TEST",
                "ordDt": "20260512",
                "inWay": "02",
                "remark": "memo",
                "godCds": [
                    {
                        "cstGodCd": "2003834450610",
                        "ordQty": 2,
                    }
                ],
            },
        )


if __name__ == "__main__":
    unittest.main()
