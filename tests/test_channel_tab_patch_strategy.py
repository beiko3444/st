from __future__ import annotations

import unittest
from datetime import datetime

from inventory_app.models import ChannelProduct
from inventory_app.ui.main_window import ChannelTab


def _row(
    product_id: str,
    item_id: str | None,
    stock: int,
    synced_at: datetime,
) -> ChannelProduct:
    return ChannelProduct(
        serial=0,
        product_id=product_id,
        item_id=item_id,
        name=f"상품-{product_id}-{item_id or 'none'}",
        image_url=None,
        product_url=f"https://example.com/{product_id}",
        stock=stock,
        sales=0,
        price=1000,
        synced_at=synced_at,
    )


class ChannelTabPatchStrategyTests(unittest.TestCase):
    def test_can_patch_when_identity_keys_are_same(self) -> None:
        now = datetime.now()
        previous = [_row("1001", "2001", 10, now), _row("1002", "2002", 20, now)]
        current = [_row("1001", "2001", 99, now), _row("1002", "2002", 20, now)]
        self.assertTrue(ChannelTab._can_patch_render(previous, current))

    def test_cannot_patch_when_row_order_changes(self) -> None:
        now = datetime.now()
        previous = [_row("1001", "2001", 10, now), _row("1002", "2002", 20, now)]
        current = [_row("1002", "2002", 20, now), _row("1001", "2001", 10, now)]
        self.assertFalse(ChannelTab._can_patch_render(previous, current))

    def test_changed_indexes_detect_object_replacement(self) -> None:
        now = datetime.now()
        unchanged = _row("1001", "2001", 10, now)
        replaced_before = _row("1002", "2002", 20, now)
        replaced_after = _row("1002", "2002", 21, now)

        previous = [unchanged, replaced_before]
        current = [unchanged, replaced_after]

        self.assertEqual(ChannelTab._changed_row_indexes(previous, current), [1])


if __name__ == "__main__":
    unittest.main()
