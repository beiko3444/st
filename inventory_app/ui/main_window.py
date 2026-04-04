from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, List

import httpx
from PySide6.QtCore import QThread, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QCloseEvent, QColor, QDesktopServices, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from inventory_app.config import AppConfig
from inventory_app.models import ChannelProduct
from inventory_app.services.channel_services import CoupangChannelService, NaverChannelService


class SortableTableItem(QTableWidgetItem):
    def __lt__(self, other: QTableWidgetItem) -> bool:
        left_value = self.data(Qt.UserRole)
        right_value = other.data(Qt.UserRole)
        if left_value is not None and right_value is not None:
            return left_value < right_value
        return super().__lt__(other)


class ChannelSyncWorker(QThread):
    completed = Signal(object, object)
    failed = Signal(str)

    def __init__(
        self,
        fetch_fn: Callable[[], tuple[List[ChannelProduct], List[str]]],
    ) -> None:
        super().__init__()
        self.fetch_fn = fetch_fn

    def run(self) -> None:
        try:
            rows, warnings = self.fetch_fn()
            self.completed.emit(rows, warnings)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class ProductImageLabel(QLabel):
    clicked = Signal(str)

    def __init__(self) -> None:
        super().__init__("이미지")
        self._product_url: str | None = None
        self.setAlignment(Qt.AlignCenter)
        self.setFixedSize(46, 46)
        self.setStyleSheet("border: 1px solid #d0d7de; color: #94a3b8; border-radius: 6px;")

    def set_product_url(self, url: str | None) -> None:
        self._product_url = url
        if url:
            self.setCursor(Qt.PointingHandCursor)
            self.setToolTip("이미지 클릭 시 상품페이지 열기")
        else:
            self.setCursor(Qt.ArrowCursor)
            self.setToolTip("")

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.LeftButton and self._product_url:
            self.clicked.emit(self._product_url)
            event.accept()
            return
        super().mousePressEvent(event)


class ChannelTab(QWidget):
    image_downloaded = Signal(str, object)

    def __init__(
        self,
        channel_name: str,
        sales_header: str,
        fetch_fn: Callable[[], tuple[List[ChannelProduct], List[str]]],
    ) -> None:
        super().__init__()
        self.channel_name = channel_name
        self.sales_header = sales_header
        self.fetch_fn = fetch_fn

        self.worker: ChannelSyncWorker | None = None
        self.rows: List[ChannelProduct] = []
        self.filtered_rows: List[ChannelProduct] = []
        self.favorite_keys: set[str] = set()

        self.image_cache: dict[str, QPixmap] = {}
        self._image_waiters: dict[str, list[tuple[QLabel, int]]] = {}
        self._image_pending: set[str] = set()
        self.image_executor = ThreadPoolExecutor(max_workers=8)
        self.render_token = 0

        self.sort_column = 1
        self.sort_order = Qt.AscendingOrder

        self.sync_button = QPushButton("동기화")
        self.favorite_filter = QComboBox()
        self.search_input = QLineEdit()
        self.status_label = QLabel("준비 완료")
        self.table = QTableWidget(0, 8)

        self.image_downloaded.connect(self._on_image_downloaded)
        self._build_ui()
        self._set_busy(False)

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(10)

        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)

        self.sync_button.clicked.connect(self.sync_now)

        self.favorite_filter.addItems(["전체", "즐겨찾기"])
        self.favorite_filter.currentIndexChanged.connect(self._apply_filters)

        self.search_input.setPlaceholderText(f"{self.channel_name} 상품명 검색")
        self.search_input.textChanged.connect(self._apply_filters)

        top_bar.addWidget(self.sync_button)
        top_bar.addWidget(QLabel("필터"))
        top_bar.addWidget(self.favorite_filter)
        top_bar.addWidget(QLabel("검색"))
        top_bar.addWidget(self.search_input, 1)

        self.table.setHorizontalHeaderLabels(
            [
                "즐겨찾기",
                "연번",
                "상품이미지",
                "상품명",
                "재고",
                self.sales_header,
                "가격",
                "마지막 동기화",
            ]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setSortingEnabled(False)

        header = self.table.horizontalHeader()
        header.setSortIndicatorShown(True)
        header.setSortIndicator(self.sort_column, self.sort_order)
        header.sectionClicked.connect(self._on_header_clicked)

        self.table.setColumnWidth(0, 52)
        self.table.setColumnWidth(1, 60)
        self.table.setColumnWidth(2, 78)
        self.table.setColumnWidth(3, 620)
        self.table.setColumnWidth(4, 110)
        self.table.setColumnWidth(5, 120)
        self.table.setColumnWidth(6, 130)
        self.table.setColumnWidth(7, 170)

        self.status_label.setStyleSheet("color: #475569;")

        root_layout.addLayout(top_bar)
        root_layout.addWidget(self.table, 1)
        root_layout.addWidget(self.status_label)

        self._apply_styles()

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QWidget {
                font-family: "Segoe UI";
                font-size: 12px;
            }
            QLineEdit, QComboBox, QPushButton {
                background: #ffffff;
                border: 1px solid #d0d7de;
                border-radius: 8px;
                padding: 4px 8px;
                color: #1f2937;
            }
            QLineEdit:focus, QComboBox:focus {
                border: 1px solid #7aa7e0;
            }
            QPushButton:hover {
                background: #f4f8fc;
            }
            QPushButton:pressed {
                background: #e7eff8;
            }
            QPushButton:disabled {
                background: #f8fafc;
                color: #9aa4b2;
            }
            QTableWidget {
                background: #ffffff;
                alternate-background-color: #f8fafc;
                border: 1px solid #d8dee4;
                gridline-color: #e5e7eb;
                selection-background-color: #dbeafe;
                selection-color: #111827;
            }
            QHeaderView::section {
                background: #f1f5f9;
                border: none;
                border-bottom: 1px solid #d8dee4;
                border-right: 1px solid #e5e7eb;
                color: #111827;
                font-weight: 600;
                padding: 6px;
            }
            """
        )

    def sync_now(self) -> None:
        if self.worker and self.worker.isRunning():
            return

        self._set_busy(True, f"{self.channel_name} 데이터를 동기화하는 중입니다...")
        self.worker = ChannelSyncWorker(self.fetch_fn)
        self.worker.completed.connect(self._on_sync_completed)
        self.worker.failed.connect(self._on_sync_failed)
        self.worker.start()

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.sync_button.setEnabled(not busy)
        self.sync_button.setText("동기화 중..." if busy else "동기화")
        if message:
            self.status_label.setText(message)

    @Slot(object, object)
    def _on_sync_completed(self, rows: object, warnings: object) -> None:
        self._set_busy(False)

        self.rows = list(rows) if isinstance(rows, list) else []
        warning_messages = list(warnings) if isinstance(warnings, list) else []

        self._apply_filters()

        summary = f"{self.channel_name} 동기화 완료: {len(self.rows)}건"
        if warning_messages:
            summary += f" | 경고 {len(warning_messages)}건"
        self.status_label.setText(summary)

        if warning_messages:
            QMessageBox.warning(self, f"{self.channel_name} 일부 경고", "\n".join(warning_messages))

    @Slot(str)
    def _on_sync_failed(self, error: str) -> None:
        self._set_busy(False)
        self.status_label.setText(f"{self.channel_name} 동기화 실패")
        QMessageBox.critical(self, f"{self.channel_name} 동기화 실패", error)

    @staticmethod
    def _fmt_int(value: int | None, suffix: str = "") -> str:
        if value is None:
            return "-"
        return f"{value:,}{suffix}"

    @staticmethod
    def _sortable_none_last(value: int | float | str | None) -> tuple[int, Any]:
        if value is None:
            return (1, 0)
        return (0, value)

    @staticmethod
    def _table_item(
        text: str,
        align: Qt.AlignmentFlag,
        color: QColor | None = None,
        sort_value: Any | None = None,
    ) -> QTableWidgetItem:
        item = SortableTableItem(text)
        item.setTextAlignment(align)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        if color is not None:
            item.setForeground(color)
        if sort_value is not None:
            item.setData(Qt.UserRole, sort_value)
        return item

    @staticmethod
    def _favorite_key(row: ChannelProduct) -> str:
        return "|".join(
            [
                row.product_id,
                row.product_url or "",
                row.name,
                str(row.price) if row.price is not None else "",
            ]
        )

    def _is_favorite(self, row: ChannelProduct) -> bool:
        return self._favorite_key(row) in self.favorite_keys

    def _toggle_row_favorite(self, row: ChannelProduct) -> None:
        key = self._favorite_key(row)
        if key in self.favorite_keys:
            self.favorite_keys.remove(key)
        else:
            self.favorite_keys.add(key)
        self._apply_filters()

    def toggle_current_row_favorite(self) -> None:
        current_index = self.table.currentRow()
        if current_index < 0 or current_index >= len(self.filtered_rows):
            return
        self._toggle_row_favorite(self.filtered_rows[current_index])

    def _favorite_button(self, row: ChannelProduct) -> QPushButton:
        checked = self._is_favorite(row)
        button = QPushButton("★" if checked else "☆")
        button.setCheckable(True)
        button.setChecked(checked)
        button.setFixedSize(26, 26)
        button.setCursor(Qt.PointingHandCursor)
        button.setToolTip("즐겨찾기 토글 (` 단축키)")
        button.setStyleSheet(
            """
            QPushButton {
                background: transparent;
                border: none;
                font-size: 16px;
                color: #b6beca;
                padding: 0px;
            }
            QPushButton:checked {
                color: #f59e0b;
            }
            QPushButton:hover {
                background: #eef2f7;
                border-radius: 13px;
            }
            """
        )
        button.clicked.connect(lambda _checked=False, item=row: self._toggle_row_favorite(item))
        return button

    def _image_label(self) -> ProductImageLabel:
        label = ProductImageLabel()
        label.clicked.connect(self._open_product_page)
        return label

    def _image_cell(self, product_url: str | None) -> tuple[QWidget, ProductImageLabel]:
        container = QWidget()
        container.setFixedWidth(58)

        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(0)

        label = self._image_label()
        label.set_product_url(product_url)
        layout.addWidget(label, 0, Qt.AlignCenter)

        return container, label

    def _open_product_page(self, url: str) -> None:
        QDesktopServices.openUrl(QUrl(url))

    @Slot()
    def _apply_filters(self) -> None:
        keyword = self.search_input.text().strip().lower()
        fav_only = self.favorite_filter.currentText() == "즐겨찾기"

        filtered: List[ChannelProduct] = []
        for row in self.rows:
            if fav_only and not self._is_favorite(row):
                continue
            if keyword and keyword not in row.name.lower():
                continue
            filtered.append(row)

        self.filtered_rows = self._sort_rows(filtered)
        self._render_table(self.filtered_rows)

    @Slot(int)
    def _on_header_clicked(self, column: int) -> None:
        if self.sort_column == column:
            self.sort_order = (
                Qt.DescendingOrder
                if self.sort_order == Qt.AscendingOrder
                else Qt.AscendingOrder
            )
        else:
            self.sort_column = column
            self.sort_order = Qt.AscendingOrder

        self.table.horizontalHeader().setSortIndicator(self.sort_column, self.sort_order)
        self._apply_filters()

    def _sort_nullable_numeric(
        self,
        rows: List[ChannelProduct],
        getter: Any,
        descending: bool,
    ) -> List[ChannelProduct]:
        present = [row for row in rows if getter(row) is not None]
        missing = [row for row in rows if getter(row) is None]
        present.sort(key=lambda row: int(getter(row) or 0), reverse=descending)
        return present + missing

    def _sort_rows(self, rows: List[ChannelProduct]) -> List[ChannelProduct]:
        descending = self.sort_order == Qt.DescendingOrder
        col = self.sort_column

        if col == 0:
            sorted_rows = sorted(
                rows,
                key=lambda row: (0 if self._is_favorite(row) else 1, row.serial),
            )
            if descending:
                sorted_rows.reverse()
            return sorted_rows
        if col == 1:
            return sorted(rows, key=lambda row: row.serial, reverse=descending)
        if col == 2:
            return list(rows)
        if col == 3:
            return sorted(rows, key=lambda row: row.name.lower(), reverse=descending)
        if col == 4:
            return self._sort_nullable_numeric(rows, lambda row: row.stock, descending)
        if col == 5:
            return self._sort_nullable_numeric(rows, lambda row: row.sales, descending)
        if col == 6:
            return self._sort_nullable_numeric(rows, lambda row: row.price, descending)
        if col == 7:
            return sorted(rows, key=lambda row: row.synced_at, reverse=descending)
        return list(rows)

    def _render_table(self, rows: List[ChannelProduct]) -> None:
        self.render_token += 1
        token = self.render_token

        self._image_waiters.clear()
        self.table.setRowCount(len(rows))

        for index, row in enumerate(rows):
            self.table.setRowHeight(index, 62)

            self.table.setCellWidget(index, 0, self._favorite_button(row))

            self.table.setItem(
                index,
                1,
                self._table_item(
                    str(row.serial),
                    Qt.AlignCenter,
                    sort_value=row.serial,
                ),
            )

            image_cell, image_label = self._image_cell(row.product_url)
            self.table.setCellWidget(index, 2, image_cell)

            self.table.setItem(
                index,
                3,
                self._table_item(
                    row.name,
                    Qt.AlignVCenter | Qt.AlignLeft,
                    sort_value=row.name.lower(),
                ),
            )

            self.table.setItem(
                index,
                4,
                self._table_item(
                    self._fmt_int(row.stock),
                    Qt.AlignCenter,
                    sort_value=self._sortable_none_last(row.stock),
                ),
            )

            self.table.setItem(
                index,
                5,
                self._table_item(
                    self._fmt_int(row.sales),
                    Qt.AlignCenter,
                    sort_value=self._sortable_none_last(row.sales),
                ),
            )

            self.table.setItem(
                index,
                6,
                self._table_item(
                    self._fmt_int(row.price, "원"),
                    Qt.AlignRight | Qt.AlignVCenter,
                    sort_value=self._sortable_none_last(row.price),
                ),
            )

            self.table.setItem(
                index,
                7,
                self._table_item(
                    row.synced_at.strftime("%Y-%m-%d %H:%M:%S"),
                    Qt.AlignCenter,
                    sort_value=row.synced_at.timestamp(),
                ),
            )

            self._queue_image(image_label, row.image_url, token)

        if rows and self.table.currentRow() < 0:
            self.table.setCurrentCell(0, 1)

    @staticmethod
    def _normalize_image_url(path_or_url: str | None) -> str | None:
        if not path_or_url:
            return None
        text = path_or_url.strip()
        if not text:
            return None
        if text.startswith("//"):
            return f"https:{text}"
        if text.startswith("http://") or text.startswith("https://"):
            return text
        return f"https://{text.lstrip('/')}"

    def _queue_image(self, label: QLabel, image_url: str | None, token: int) -> None:
        normalized_url = self._normalize_image_url(image_url)
        if not normalized_url:
            return

        cached = self.image_cache.get(normalized_url)
        if cached:
            self._set_label_pixmap(label, cached)
            return

        waiters = self._image_waiters.setdefault(normalized_url, [])
        waiters.append((label, token))

        if normalized_url in self._image_pending:
            return

        self._image_pending.add(normalized_url)
        future = self.image_executor.submit(self._download_image_bytes, normalized_url)
        future.add_done_callback(
            lambda f, url=normalized_url: self._emit_image_downloaded(url, f)
        )

    @staticmethod
    def _download_image_bytes(url: str) -> bytes | None:
        try:
            response = httpx.get(
                url,
                timeout=15,
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                },
            )
            if response.status_code != 200:
                return None
            content_type = (response.headers.get("content-type") or "").lower()
            if "image" not in content_type:
                return None
            return response.content
        except Exception:  # noqa: BLE001
            return None

    def _emit_image_downloaded(self, url: str, future: Future[bytes | None]) -> None:
        data: bytes | None
        try:
            data = future.result()
        except Exception:  # noqa: BLE001
            data = None
        self.image_downloaded.emit(url, data)

    @Slot(str, object)
    def _on_image_downloaded(self, url: str, data: object) -> None:
        self._image_pending.discard(url)

        waiters = self._image_waiters.pop(url, [])
        if not waiters:
            return

        pixmap: QPixmap | None = None
        if isinstance(data, (bytes, bytearray)):
            candidate = QPixmap()
            if candidate.loadFromData(bytes(data)):
                pixmap = candidate
                self.image_cache[url] = candidate

        if pixmap is None:
            return

        for label, token in waiters:
            if token != self.render_token:
                continue
            try:
                self._set_label_pixmap(label, pixmap)
            except RuntimeError:
                continue

    def _set_label_pixmap(self, label: QLabel, pixmap: QPixmap) -> None:
        scaled = pixmap.scaled(42, 42, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        label.setPixmap(scaled)
        label.setText("")
        label.setStyleSheet("border: 1px solid #d0d7de; border-radius: 6px;")

    def shutdown(self) -> None:
        if self.worker and self.worker.isRunning():
            finished = self.worker.wait(60000)
            if not finished:
                self.worker.terminate()
                self.worker.wait(2000)
        self.image_executor.shutdown(wait=False, cancel_futures=True)


class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config

        self.naver_service = NaverChannelService(config)
        self.coupang_service = CoupangChannelService(config)

        self.setWindowTitle("스마트스토어 / 쿠팡 분리 재고 대시보드")
        self.resize(1780, 900)

        self.sync_all_button = QPushButton("전체 동기화")
        self.sync_all_button.setObjectName("primarySyncButton")
        self.tabs = QTabWidget()

        sales_days = max(1, int(config.stats_lookback_days))
        self.naver_tab = ChannelTab(
            channel_name="네이버",
            sales_header=f"판매량({sales_days}일)",
            fetch_fn=self.naver_service.fetch,
        )
        self.coupang_tab = ChannelTab(
            channel_name="쿠팡",
            sales_header="판매량",
            fetch_fn=self.coupang_service.fetch,
        )

        self._build_ui()

        self.favorite_shortcut = QShortcut(QKeySequence(Qt.Key_QuoteLeft), self)
        self.favorite_shortcut.setContext(Qt.ApplicationShortcut)
        self.favorite_shortcut.activated.connect(self._toggle_favorite_on_current_tab)

    def _build_ui(self) -> None:
        root = QWidget(self)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(8)

        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)
        self.sync_all_button.clicked.connect(self.sync_now)
        top_bar.addWidget(self.sync_all_button)
        top_bar.addStretch(1)

        self.tabs.addTab(self.naver_tab, "네이버")
        self.tabs.addTab(self.coupang_tab, "쿠팡")

        root_layout.addLayout(top_bar)
        root_layout.addWidget(self.tabs, 1)

        self.setCentralWidget(root)
        self._apply_styles()

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow {
                background: #f3f6fb;
            }
            #primarySyncButton {
                background: #2f6fb2;
                color: white;
                border: 1px solid #2f6fb2;
                border-radius: 9px;
                padding: 6px 14px;
                font-weight: 600;
            }
            #primarySyncButton:hover {
                background: #2a639f;
            }
            #primarySyncButton:pressed {
                background: #245587;
            }
            QTabWidget::pane {
                border: 1px solid #d8dee4;
                border-radius: 10px;
                background: #ffffff;
                top: -1px;
            }
            QTabBar::tab {
                background: #e9eef4;
                border: 1px solid #d8dee4;
                border-bottom: none;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
                padding: 8px 14px;
                margin-right: 4px;
                color: #334155;
                font-weight: 600;
            }
            QTabBar::tab:selected {
                background: #ffffff;
                color: #0f172a;
            }
            """
        )

    @Slot()
    def sync_now(self) -> None:
        self.naver_tab.sync_now()
        self.coupang_tab.sync_now()

    @Slot()
    def _toggle_favorite_on_current_tab(self) -> None:
        current = self.tabs.currentWidget()
        if isinstance(current, ChannelTab):
            current.toggle_current_row_favorite()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.naver_tab.shutdown()
        self.coupang_tab.shutdown()
        super().closeEvent(event)
