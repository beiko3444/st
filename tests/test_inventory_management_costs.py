from __future__ import annotations

import unittest

from inventory_app.ui.main_window import InventoryManagementTab


class InventoryManagementCostTests(unittest.TestCase):
    def test_total_cost_requires_stock_and_unit_cost(self) -> None:
        self.assertEqual(InventoryManagementTab._total_cost(10, 2500), 25000)
        self.assertIsNone(InventoryManagementTab._total_cost(None, 2500))
        self.assertIsNone(InventoryManagementTab._total_cost(10, None))

    def test_total_sales_price_requires_stock_and_sale_price(self) -> None:
        self.assertEqual(InventoryManagementTab._total_sales_price(8, 12000), 96000)
        self.assertIsNone(InventoryManagementTab._total_sales_price(None, 12000))
        self.assertIsNone(InventoryManagementTab._total_sales_price(8, None))

    def test_expected_profit_requires_total_cost_and_total_sales_price(self) -> None:
        self.assertEqual(InventoryManagementTab._expected_profit(96000, 25000), 71000)
        self.assertEqual(InventoryManagementTab._expected_profit(10000, 12000), -2000)
        self.assertIsNone(InventoryManagementTab._expected_profit(None, 25000))
        self.assertIsNone(InventoryManagementTab._expected_profit(96000, None))


if __name__ == "__main__":
    unittest.main()
