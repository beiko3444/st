from __future__ import annotations

from pathlib import Path
from typing import Any, List

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from inventory_app.models import PurchaseRecord
from inventory_app.services.purchase_history_service import (
    PurchaseHistoryParser,
    PurchaseHistoryStore,
)


class _NumberItem(QTableWidgetItem):
    def __lt__(self, other: QTableWidgetItem) -> bool:
        left = self.data(Qt.UserRole)
        right = other.data(Qt.UserRole)
        if left is not None and right is not None:
            return left < right
        return super().__lt__(other)


class PurchaseHistoryTab(QWidget):
    CHANNEL_LABELS = {
        "all": "전체",
        "naver": "네이버",
        "coupang": "쿠팡",
    }
    ORDER_URLS = {
        "naver": "https://order.pay.naver.com/home",
        "coupang": "https://mc.coupang.com/ssr/desktop/order/list",
    }
    HEADERS = ("일자", "채널", "주문번호", "상품/내역", "결제금액", "결제수단", "가져온 시각")

    def __init__(self) -> None:
        super().__init__()
        self.store = PurchaseHistoryStore()
        self.parser = PurchaseHistoryParser()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        top = QHBoxLayout()
        self.channel_combo = QComboBox()
        for code, label in self.CHANNEL_LABELS.items():
            self.channel_combo.addItem(label, code)
        self.channel_combo.currentIndexChanged.connect(self.reload)

        self.open_naver_btn = QPushButton("네이버 주문내역 열기")
        self.open_naver_btn.clicked.connect(lambda: self._open_order_page("naver"))
        self.open_coupang_btn = QPushButton("쿠팡 주문내역 열기")
        self.open_coupang_btn.clicked.connect(lambda: self._open_order_page("coupang"))
        self.import_btn = QPushButton("HTML 가져오기")
        self.import_btn.clicked.connect(self._import_html)
        self.reload_btn = QPushButton("새로고침")
        self.reload_btn.clicked.connect(self.reload)
        self.status = QLabel("")

        top.addWidget(QLabel("채널"))
        top.addWidget(self.channel_combo)
        top.addWidget(self.open_naver_btn)
        top.addWidget(self.open_coupang_btn)
        top.addWidget(self.import_btn)
        top.addWidget(self.reload_btn)
        top.addWidget(self.status, 1)

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setStretchLastSection(True)

        layout.addLayout(top)
        layout.addWidget(self.table, 1)
        self.reload()

    def _selected_channel(self) -> str:
        return str(self.channel_combo.currentData() or "all")

    def _selected_import_channel(self) -> str:
        channel = self._selected_channel()
        if channel in {"naver", "coupang"}:
            return channel
        return "naver"

    def _open_order_page(self, channel: str) -> None:
        url = self.ORDER_URLS.get(channel)
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _import_html(self) -> None:
        channel = self._selected_import_channel()
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "구매내역 HTML 선택",
            "",
            "HTML files (*.html *.htm);;All files (*.*)",
        )
        if not path:
            return
        try:
            records = self.parser.parse_html_file(channel, Path(path))
            added = self.store.save_records(records)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "가져오기 실패", str(exc))
            return
        self.status.setText(f"{self.CHANNEL_LABELS[channel]} {len(records)}건 분석, {added}건 추가")
        self.reload()

    def reload(self) -> None:
        channel = self._selected_channel()
        rows = self.store.load_records(channel=channel)
        self._render(rows)
        total_amount = sum(int(row.amount or 0) for row in rows)
        self.status.setText(f"{len(rows):,}건 / {total_amount:,}원")

    def _render(self, rows: List[PurchaseRecord]) -> None:
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for row_idx, record in enumerate(rows):
            values: list[Any] = [
                record.order_date or "-",
                self.CHANNEL_LABELS.get(record.channel, record.channel),
                record.order_no or "-",
                record.title,
                record.amount,
                record.payment_method or "-",
                record.imported_at.strftime("%Y-%m-%d %H:%M"),
            ]
            for col_idx, value in enumerate(values):
                if col_idx == 4:
                    item = _NumberItem(f"{int(value or 0):,}원" if value else "-")
                    item.setData(Qt.UserRole, int(value or 0))
                else:
                    item = QTableWidgetItem(str(value))
                    item.setData(Qt.UserRole, str(value))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if col_idx == 3:
                    item.setToolTip(record.raw_text[:1000])
                self.table.setItem(row_idx, col_idx, item)
        self.table.resizeColumnsToContents()
        self.table.setSortingEnabled(True)

    def shutdown(self) -> None:
        return

