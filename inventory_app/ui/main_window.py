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
    QProgressBar,
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
from inventory_app.services.revenue_services import (
    RevenueChannelSummary,
    RevenueComparisonService,
    RevenueProductSummary,
    RevenueSnapshot,
)


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
    sync_finished = Signal(str, bool)

    def __init__(
        self,
        channel_name: str,
        sales_header: str,
        fetch_fn: Callable[[], tuple[List[ChannelProduct], List[str]]],
        initial_fetch_fn: Callable[[], tuple[List[ChannelProduct], List[str]]] | None = None,
    ) -> None:
        super().__init__()
        self.channel_name = channel_name
        self.sales_header = sales_header
        self.fetch_fn = fetch_fn
        self.initial_fetch_fn = initial_fetch_fn

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
        self._load_initial_rows()
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

    def _load_initial_rows(self) -> None:
        if self.initial_fetch_fn is None:
            return
        try:
            rows, warnings = self.initial_fetch_fn()
        except Exception:  # noqa: BLE001
            return
        if not rows:
            return
        self.rows = list(rows)
        self._apply_filters()
        summary = f"{self.channel_name} 캐시 로드: {len(self.rows)}건"
        if warnings:
            summary += f" | 경고 {len(warnings)}건"
        self.status_label.setText(summary)
        self.status_label.setToolTip("\n".join(warnings) if warnings else "")

    def sync_now(self) -> bool:
        if self.worker and self.worker.isRunning():
            return False

        self._set_busy(True, f"{self.channel_name} 데이터를 동기화하는 중입니다...")
        self.worker = ChannelSyncWorker(self.fetch_fn)
        self.worker.completed.connect(self._on_sync_completed)
        self.worker.failed.connect(self._on_sync_failed)
        self.worker.start()
        return True

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.sync_button.setEnabled(not busy)
        self.sync_button.setText("동기화 중..." if busy else "동기화")
        if message:
            self.status_label.setText(message)

    @staticmethod
    def _changed_row_count(previous_rows: List[ChannelProduct], current_rows: List[ChannelProduct]) -> int:
        overlap = min(len(previous_rows), len(current_rows))
        changed = sum(1 for idx in range(overlap) if previous_rows[idx] is not current_rows[idx])
        return changed + abs(len(previous_rows) - len(current_rows))

    @Slot(object, object)
    def _on_sync_completed(self, rows: object, warnings: object) -> None:
        self._set_busy(False)

        previous_rows = self.rows
        self.rows = list(rows) if isinstance(rows, list) else []
        warning_messages = list(warnings) if isinstance(warnings, list) else []
        changed_count = self._changed_row_count(previous_rows, self.rows)

        if changed_count > 0:
            self._apply_filters()

        summary = f"{self.channel_name} 동기화 완료: {len(self.rows)}건"
        summary += f" | 변경 {changed_count}건"
        if warning_messages:
            summary += f" | 경고 {len(warning_messages)}건"
        self.status_label.setText(summary)
        self.status_label.setToolTip("\n".join(warning_messages) if warning_messages else "")
        self.sync_finished.emit(self.channel_name, True)

    @Slot(str)
    def _on_sync_failed(self, error: str) -> None:
        self._set_busy(False)
        self.status_label.setText(f"{self.channel_name} 동기화 실패")
        QMessageBox.critical(self, f"{self.channel_name} 동기화 실패", error)
        self.sync_finished.emit(self.channel_name, False)

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

        previous_rows = self.filtered_rows
        next_rows = self._sort_rows(filtered)
        if self._can_patch_render(previous_rows, next_rows):
            changed_indexes = self._changed_row_indexes(previous_rows, next_rows)
            self.filtered_rows = next_rows
            if changed_indexes:
                self._patch_table_rows(self.filtered_rows, changed_indexes)
        else:
            self.filtered_rows = next_rows
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

    @staticmethod
    def _row_identity_key(row: ChannelProduct) -> tuple[str, str]:
        if row.product_id:
            return (f"id:{row.product_id}", row.item_id or "")
        if row.product_url:
            return (f"url:{row.product_url}", "")
        return (f"name:{row.name}", "")

    @classmethod
    def _can_patch_render(
        cls,
        previous_rows: List[ChannelProduct],
        current_rows: List[ChannelProduct],
    ) -> bool:
        if len(previous_rows) != len(current_rows):
            return False
        return all(
            cls._row_identity_key(previous) == cls._row_identity_key(current)
            for previous, current in zip(previous_rows, current_rows)
        )

    @staticmethod
    def _changed_row_indexes(
        previous_rows: List[ChannelProduct],
        current_rows: List[ChannelProduct],
    ) -> list[int]:
        return [
            index
            for index, (previous, current) in enumerate(zip(previous_rows, current_rows))
            if previous is not current
        ]

    def _render_row(self, index: int, row: ChannelProduct, token: int) -> None:
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

    def _render_table(self, rows: List[ChannelProduct]) -> None:
        self.render_token += 1
        token = self.render_token

        self._image_waiters.clear()
        self.table.setRowCount(len(rows))

        for index, row in enumerate(rows):
            self._render_row(index, row, token)

        if rows and self.table.currentRow() < 0:
            self.table.setCurrentCell(0, 1)

    def _patch_table_rows(self, rows: List[ChannelProduct], changed_indexes: list[int]) -> None:
        token = self.render_token
        for index in changed_indexes:
            if 0 <= index < len(rows):
                self._render_row(index, rows[index], token)

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


class RevenueSyncWorker(QThread):
    completed = Signal(object, object)
    failed = Signal(str)

    def __init__(
        self,
        fetch_fn: Callable[[int], tuple[RevenueSnapshot, List[str]]],
        period_days: int,
    ) -> None:
        super().__init__()
        self.fetch_fn = fetch_fn
        self.period_days = max(1, int(period_days))

    def run(self) -> None:
        try:
            snapshot, warnings = self.fetch_fn(self.period_days)
            self.completed.emit(snapshot, warnings)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class RevenueTab(QWidget):
    sync_finished = Signal(str, bool)

    def __init__(
        self,
        fetch_fn: Callable[[int], tuple[RevenueSnapshot, List[str]]],
        default_days: int,
    ) -> None:
        super().__init__()
        self.fetch_fn = fetch_fn
        self.default_days = max(1, int(default_days))
        self.worker: RevenueSyncWorker | None = None

        self.sync_button = QPushButton("동기화")
        self.period_combo = QComboBox()
        self.summary_table = QTableWidget(0, 7)
        self.product_table = QTableWidget(0, 8)
        self.status_label = QLabel("준비 완료")
        self.note_label = QLabel("")

        self._build_ui()
        self._set_busy(False)

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(10)

        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)
        self.sync_button.clicked.connect(self.sync_now)

        self.period_combo.addItem("7일", 7)
        self.period_combo.addItem("14일", 14)
        self.period_combo.addItem("30일", 30)
        self.period_combo.addItem("60일", 60)
        self._select_default_period()

        top_bar.addWidget(self.sync_button)
        top_bar.addWidget(QLabel("기준기간"))
        top_bar.addWidget(self.period_combo)
        top_bar.addStretch(1)

        self.summary_table.setHorizontalHeaderLabels(
            [
                "채널",
                "총매출",
                "환불",
                "순매출",
                "주문수",
                "데이터유형",
                "비고",
            ]
        )
        self.summary_table.verticalHeader().setVisible(False)
        self.summary_table.setAlternatingRowColors(True)
        self.summary_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.summary_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.summary_table.setSelectionMode(QTableWidget.SingleSelection)
        self.summary_table.setSortingEnabled(True)
        self.summary_table.setColumnWidth(0, 90)
        self.summary_table.setColumnWidth(1, 140)
        self.summary_table.setColumnWidth(2, 130)
        self.summary_table.setColumnWidth(3, 140)
        self.summary_table.setColumnWidth(4, 100)
        self.summary_table.setColumnWidth(5, 110)
        self.summary_table.setColumnWidth(6, 260)

        self.product_table.setHorizontalHeaderLabels(
            [
                "채널",
                "상품ID",
                "상품명",
                "주문수",
                "총매출",
                "환불",
                "순매출",
                "데이터유형",
            ]
        )
        self.product_table.verticalHeader().setVisible(False)
        self.product_table.setAlternatingRowColors(True)
        self.product_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.product_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.product_table.setSelectionMode(QTableWidget.SingleSelection)
        self.product_table.setSortingEnabled(True)
        self.product_table.setColumnWidth(0, 80)
        self.product_table.setColumnWidth(1, 120)
        self.product_table.setColumnWidth(2, 560)
        self.product_table.setColumnWidth(3, 100)
        self.product_table.setColumnWidth(4, 140)
        self.product_table.setColumnWidth(5, 130)
        self.product_table.setColumnWidth(6, 140)
        self.product_table.setColumnWidth(7, 110)

        self.status_label.setStyleSheet("color: #475569;")
        self.note_label.setStyleSheet("color: #475569;")
        self.note_label.setWordWrap(True)

        root_layout.addLayout(top_bar)
        root_layout.addWidget(QLabel("채널 합계"), 0)
        root_layout.addWidget(self.summary_table, 0)
        root_layout.addWidget(QLabel("상품별 매출 (순매출 상위)"), 0)
        root_layout.addWidget(self.product_table, 1)
        root_layout.addWidget(self.status_label, 0)
        root_layout.addWidget(self.note_label, 0)

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

    def _select_default_period(self) -> None:
        target = self.default_days
        for idx in range(self.period_combo.count()):
            value = self.period_combo.itemData(idx)
            if int(value) == target:
                self.period_combo.setCurrentIndex(idx)
                return
        self.period_combo.setCurrentIndex(2)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.sync_button.setEnabled(not busy)
        self.sync_button.setText("동기화 중..." if busy else "동기화")
        self.period_combo.setEnabled(not busy)
        if message:
            self.status_label.setText(message)

    @Slot()
    def sync_now(self) -> bool:
        if self.worker and self.worker.isRunning():
            return False
        period_days = int(self.period_combo.currentData() or 30)
        self._set_busy(True, f"매출 데이터를 동기화하는 중입니다... ({period_days}일)")
        self.worker = RevenueSyncWorker(self.fetch_fn, period_days)
        self.worker.completed.connect(self._on_sync_completed)
        self.worker.failed.connect(self._on_sync_failed)
        self.worker.start()
        return True

    @staticmethod
    def _fmt_int(value: int | None) -> str:
        return f"{int(value or 0):,}"

    @staticmethod
    def _fmt_money(value: float | int | None) -> str:
        return f"{int(round(float(value or 0.0))):,}원"

    @staticmethod
    def _table_item(
        text: str,
        align: Qt.AlignmentFlag,
        sort_value: Any | None = None,
    ) -> QTableWidgetItem:
        item = SortableTableItem(text)
        item.setTextAlignment(align)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        if sort_value is not None:
            item.setData(Qt.UserRole, sort_value)
        return item

    @Slot(object, object)
    def _on_sync_completed(self, snapshot_obj: object, warnings_obj: object) -> None:
        self._set_busy(False)
        if not isinstance(snapshot_obj, RevenueSnapshot):
            self.status_label.setText("매출 동기화 실패: 응답 형식 오류")
            self.sync_finished.emit("매출비교", False)
            return

        warnings = list(warnings_obj) if isinstance(warnings_obj, list) else []

        self._render_summary(snapshot_obj.summaries)
        self._render_products(snapshot_obj.products)

        self.status_label.setText(
            (
                f"매출 동기화 완료: 네이버 {snapshot_obj.period_days}일 / 쿠팡 최근30일 기준 "
                f"(채널 {len(snapshot_obj.summaries)}개, 상품 {len(snapshot_obj.products)}건) "
                f"| {snapshot_obj.generated_at.strftime('%Y-%m-%d %H:%M:%S')}"
            )
        )
        note_lines = list(snapshot_obj.notes)
        if warnings:
            note_lines.append("")
            note_lines.append("[경고]")
            note_lines.extend(warnings)
        note_text = "\n".join(note_lines)
        self.note_label.setText(note_text)
        self.note_label.setToolTip("\n".join(warnings) if warnings else "")
        self.sync_finished.emit("매출비교", True)

    @Slot(str)
    def _on_sync_failed(self, error: str) -> None:
        self._set_busy(False)
        self.status_label.setText("매출 동기화 실패")
        QMessageBox.critical(self, "매출 동기화 실패", error)
        self.sync_finished.emit("매출비교", False)

    def _render_summary(self, rows: List[RevenueChannelSummary]) -> None:
        self.summary_table.setSortingEnabled(False)
        self.summary_table.setRowCount(len(rows))

        for index, row in enumerate(rows):
            self.summary_table.setRowHeight(index, 34)
            dtype = "추정" if row.estimated else "실측"

            self.summary_table.setItem(
                index,
                0,
                self._table_item(row.channel, Qt.AlignCenter, row.channel),
            )
            self.summary_table.setItem(
                index,
                1,
                self._table_item(
                    self._fmt_money(row.gross),
                    Qt.AlignRight | Qt.AlignVCenter,
                    row.gross,
                ),
            )
            self.summary_table.setItem(
                index,
                2,
                self._table_item(
                    self._fmt_money(row.refund),
                    Qt.AlignRight | Qt.AlignVCenter,
                    row.refund,
                ),
            )
            self.summary_table.setItem(
                index,
                3,
                self._table_item(
                    self._fmt_money(row.net),
                    Qt.AlignRight | Qt.AlignVCenter,
                    row.net,
                ),
            )
            self.summary_table.setItem(
                index,
                4,
                self._table_item(self._fmt_int(row.orders), Qt.AlignCenter, row.orders),
            )
            self.summary_table.setItem(
                index,
                5,
                self._table_item(dtype, Qt.AlignCenter, dtype),
            )
            self.summary_table.setItem(
                index,
                6,
                self._table_item(row.note, Qt.AlignLeft | Qt.AlignVCenter, row.note),
            )

        self.summary_table.setSortingEnabled(True)
        if rows:
            self.summary_table.sortItems(3, Qt.DescendingOrder)

    def _render_products(self, rows: List[RevenueProductSummary]) -> None:
        self.product_table.setSortingEnabled(False)
        self.product_table.setRowCount(len(rows))

        for index, row in enumerate(rows):
            self.product_table.setRowHeight(index, 32)
            dtype = "추정" if row.estimated else "실측"

            self.product_table.setItem(
                index,
                0,
                self._table_item(row.channel, Qt.AlignCenter, row.channel),
            )
            self.product_table.setItem(
                index,
                1,
                self._table_item(row.product_id, Qt.AlignCenter, row.product_id),
            )
            self.product_table.setItem(
                index,
                2,
                self._table_item(
                    row.name,
                    Qt.AlignVCenter | Qt.AlignLeft,
                    row.name.lower(),
                ),
            )
            self.product_table.setItem(
                index,
                3,
                self._table_item(self._fmt_int(row.orders), Qt.AlignCenter, row.orders),
            )
            self.product_table.setItem(
                index,
                4,
                self._table_item(
                    self._fmt_money(row.gross),
                    Qt.AlignRight | Qt.AlignVCenter,
                    row.gross,
                ),
            )
            self.product_table.setItem(
                index,
                5,
                self._table_item(
                    self._fmt_money(row.refund),
                    Qt.AlignRight | Qt.AlignVCenter,
                    row.refund,
                ),
            )
            self.product_table.setItem(
                index,
                6,
                self._table_item(
                    self._fmt_money(row.net),
                    Qt.AlignRight | Qt.AlignVCenter,
                    row.net,
                ),
            )
            self.product_table.setItem(
                index,
                7,
                self._table_item(dtype, Qt.AlignCenter, dtype),
            )

        self.product_table.setSortingEnabled(True)
        if rows:
            self.product_table.sortItems(6, Qt.DescendingOrder)

    def shutdown(self) -> None:
        if self.worker and self.worker.isRunning():
            finished = self.worker.wait(60000)
            if not finished:
                self.worker.terminate()
                self.worker.wait(2000)


class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config

        self.naver_service = NaverChannelService(config)
        self.coupang_service = CoupangChannelService(config)
        self.revenue_service = RevenueComparisonService(config)

        self.setWindowTitle("스마트스토어 / 쿠팡 분리 재고 대시보드")
        self.resize(1780, 900)

        self.sync_all_button = QPushButton("전체 동기화")
        self.sync_all_button.setObjectName("primarySyncButton")
        self.sync_progress = QProgressBar()
        self.tabs = QTabWidget()
        self._sync_expected_sources: set[str] = set()
        self._sync_finished_sources: set[str] = set()
        self._sync_failed_sources: set[str] = set()
        self._sync_session_active = False

        sales_days = max(1, int(config.stats_lookback_days))
        self.naver_tab = ChannelTab(
            channel_name="네이버",
            sales_header=f"판매량({sales_days}일)",
            fetch_fn=self.naver_service.fetch,
            initial_fetch_fn=self.naver_service.fetch_cached,
        )
        self.coupang_tab = ChannelTab(
            channel_name="쿠팡",
            sales_header="판매량",
            fetch_fn=self.coupang_service.fetch,
            initial_fetch_fn=self.coupang_service.fetch_cached,
        )
        self.revenue_tab = RevenueTab(
            fetch_fn=self.revenue_service.fetch,
            default_days=sales_days,
        )

        self._build_ui()

        self.favorite_shortcut = QShortcut(QKeySequence(Qt.Key_QuoteLeft), self)
        self.favorite_shortcut.setContext(Qt.ApplicationShortcut)
        self.favorite_shortcut.activated.connect(self._toggle_favorite_on_current_tab)

        self.naver_tab.sync_finished.connect(self._on_sub_sync_finished)
        self.coupang_tab.sync_finished.connect(self._on_sub_sync_finished)
        self.revenue_tab.sync_finished.connect(self._on_sub_sync_finished)

    def _build_ui(self) -> None:
        root = QWidget(self)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(8)

        self.sync_all_button.clicked.connect(self.sync_now)
        self.sync_progress.setObjectName("syncProgressBar")
        self.sync_progress.setRange(0, 100)
        self.sync_progress.setValue(0)
        self.sync_progress.setTextVisible(True)
        self.sync_progress.setFormat("대기 중")
        self.sync_progress.setVisible(False)

        self.tabs.addTab(self.naver_tab, "네이버")
        self.tabs.addTab(self.coupang_tab, "쿠팡")
        self.tabs.addTab(self.revenue_tab, "매출비교")
        corner = QWidget()
        corner_layout = QHBoxLayout(corner)
        corner_layout.setContentsMargins(0, 0, 0, 0)
        corner_layout.setSpacing(0)
        corner_layout.addWidget(self.sync_all_button)
        self.tabs.setCornerWidget(corner, Qt.TopRightCorner)

        root_layout.addWidget(self.sync_progress, 0)
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
            #syncProgressBar {
                min-height: 18px;
                max-height: 18px;
                border: 1px solid #d8dee4;
                border-radius: 7px;
                background: #f8fafc;
                text-align: center;
                color: #334155;
                font-size: 11px;
            }
            #syncProgressBar::chunk {
                border-radius: 6px;
                background: #3b82f6;
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

    def _start_sync_session(self, sources: set[str]) -> None:
        self._sync_expected_sources = set(sources)
        self._sync_finished_sources.clear()
        self._sync_failed_sources.clear()
        self._sync_session_active = bool(self._sync_expected_sources)
        if not self._sync_session_active:
            self.sync_progress.setVisible(False)
            return
        total = len(self._sync_expected_sources)
        self.sync_progress.setVisible(True)
        self.sync_progress.setValue(0)
        self.sync_progress.setFormat(f"동기화 진행 중... (0/{total})")

    def _update_sync_progress(self) -> None:
        if not self._sync_session_active:
            return
        total = len(self._sync_expected_sources)
        done = len(self._sync_finished_sources)
        if total <= 0:
            self.sync_progress.setVisible(False)
            self._sync_session_active = False
            return
        percent = int((done * 100) / total)
        self.sync_progress.setValue(percent)
        if done >= total:
            if self._sync_failed_sources:
                self.sync_progress.setFormat(f"동기화 완료 (일부 실패: {done}/{total})")
            else:
                self.sync_progress.setFormat(f"동기화 완료 ({done}/{total})")
            self._sync_session_active = False
        else:
            self.sync_progress.setFormat(f"동기화 진행 중... ({done}/{total})")

    @Slot()
    def sync_now(self) -> None:
        started_sources: set[str] = set()
        if self.naver_tab.sync_now():
            started_sources.add("네이버")
        if self.coupang_tab.sync_now():
            started_sources.add("쿠팡")
        if self.revenue_tab.sync_now():
            started_sources.add("매출비교")

        self._start_sync_session(started_sources)

    @Slot(str, bool)
    def _on_sub_sync_finished(self, source: str, succeeded: bool) -> None:
        if not self._sync_session_active:
            return
        if source not in self._sync_expected_sources:
            return
        if source in self._sync_finished_sources:
            return

        self._sync_finished_sources.add(source)
        if not succeeded:
            self._sync_failed_sources.add(source)
        self._update_sync_progress()

    @Slot()
    def _toggle_favorite_on_current_tab(self) -> None:
        current = self.tabs.currentWidget()
        if isinstance(current, ChannelTab):
            current.toggle_current_row_favorite()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.naver_tab.shutdown()
        self.coupang_tab.shutdown()
        self.revenue_tab.shutdown()
        super().closeEvent(event)
