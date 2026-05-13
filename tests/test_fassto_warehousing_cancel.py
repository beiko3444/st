import unittest

from inventory_app.connectors.fassto import warehousing_cancel_check


class FasstoWarehousingCancelTests(unittest.TestCase):
    def test_requested_warehousing_can_be_cancelled(self) -> None:
        allowed, reason = warehousing_cancel_check("1", None)

        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_cancelled_warehousing_cannot_be_cancelled_again(self) -> None:
        allowed, reason = warehousing_cancel_check("5", None)

        self.assertFalse(allowed)
        self.assertIn("입고취소", reason)

    def test_completed_warehousing_cannot_be_cancelled(self) -> None:
        allowed, reason = warehousing_cancel_check("4", None)

        self.assertFalse(allowed)
        self.assertIn("입고완료", reason)

    def test_center_arrived_status_name_can_be_cancelled(self) -> None:
        allowed, reason = warehousing_cancel_check(None, "센터도착")

        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_inspection_status_cannot_be_cancelled(self) -> None:
        allowed, reason = warehousing_cancel_check("2", "검수중")

        self.assertFalse(allowed)
        self.assertIn("검수", reason)


if __name__ == "__main__":
    unittest.main()
