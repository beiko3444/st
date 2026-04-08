from __future__ import annotations

import math
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, List

import httpx
from PySide6.QtCore import QEvent, QObject, QThread, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QCloseEvent, QColor, QDesktopServices, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
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
from inventory_app.services.keyword_services import (
    KeywordRevenueRow,
    KeywordRevenueSnapshot,
    NaverKeywordRevenueService,
)
from inventory_app.services.local_cache import ChannelProductCache
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


@dataclass
class FavoriteInventoryRow:
    channel: str
    cost_key: str
    serial: int
    image_url: str | None
    name: str
    stock: int | None
    sales: int | None
    stockout_days: int | None
    price: int | None
    product_url: str | None


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


class EditableNameLabel(QLabel):
    double_clicked = Signal()

    def mouseDoubleClickEvent(self, event: Any) -> None:
        if event.button() == Qt.LeftButton:
            self.double_clicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class ChannelTab(QWidget):
    image_downloaded = Signal(str, object)
    sync_finished = Signal(str, bool)
    favorites_changed = Signal(str)

    def __init__(
        self,
        channel_name: str,
        sales_header: str,
        sales_period_days: int | None,
        fetch_fn: Callable[[], tuple[List[ChannelProduct], List[str]]],
        initial_fetch_fn: Callable[[], tuple[List[ChannelProduct], List[str]]] | None = None,
    ) -> None:
        super().__init__()
        self.channel_name = channel_name
        self.sales_header = sales_header
        self.sales_period_days = max(1, int(sales_period_days)) if sales_period_days else None
        self.fetch_fn = fetch_fn
        self.initial_fetch_fn = initial_fetch_fn

        self.worker: ChannelSyncWorker | None = None
        self.rows: List[ChannelProduct] = []
        self.filtered_rows: List[ChannelProduct] = []
        self.cache = ChannelProductCache()
        self.favorite_keys: set[str] = self.cache.load_favorite_keys(self.channel_name)
        self.name_overrides = self.cache.load_name_overrides(self.channel_name)

        self.image_cache: dict[str, QPixmap] = {}
        self._image_waiters: dict[str, list[tuple[QLabel, int]]] = {}
        self._image_pending: set[str] = set()
        self.image_executor = ThreadPoolExecutor(max_workers=8)
        self.render_token = 0
        self._force_full_render_next = False

        self.sort_column = 1
        self.sort_order = Qt.AscendingOrder

        self.sync_button = QPushButton("동기화")
        self.favorite_filter = QComboBox()
        self.search_input = QLineEdit()
        self.status_label = QLabel("준비 완료")
        self.table = QTableWidget(0, 10)

        self.image_downloaded.connect(self._on_image_downloaded)
        self._build_ui()
        self._load_initial_rows()
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
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
        self.search_input.setFocusPolicy(Qt.ClickFocus)
        self.search_input.textChanged.connect(self._apply_filters)

        top_bar.addWidget(self.sync_button)
        top_bar.addWidget(QLabel("필터"))
        top_bar.addWidget(self.favorite_filter)
        top_bar.addWidget(QLabel("검색"))
        top_bar.addWidget(self.search_input, 1)

        self.table.setHorizontalHeaderLabels(
            [
                "★",
                "연번",
                "상품이미지",
                "상품명",
                "재고",
                "오늘판매",
                self.sales_header,
                "품절예상(일)",
                "예측 한달매출",
                "가격",
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
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.setSectionResizeMode(3, QHeaderView.Interactive)
        header.setSectionResizeMode(4, QHeaderView.Fixed)
        header.setSectionResizeMode(5, QHeaderView.Fixed)
        header.setSectionResizeMode(6, QHeaderView.Fixed)
        header.setSectionResizeMode(7, QHeaderView.Fixed)
        header.setSectionResizeMode(8, QHeaderView.Fixed)
        header.setSectionResizeMode(9, QHeaderView.Fixed)

        self.table.setColumnWidth(0, 36)
        self.table.setColumnWidth(1, 44)
        self.table.setColumnWidth(2, 64)
        self.table.setColumnWidth(3, 360)
        self.table.setColumnWidth(4, 78)
        self.table.setColumnWidth(5, 72)
        self.table.setColumnWidth(6, 88)
        self.table.setColumnWidth(7, 90)
        self.table.setColumnWidth(8, 118)
        self.table.setColumnWidth(9, 98)

        self.status_label.setStyleSheet("color: #475569;")

        root_layout.addLayout(top_bar)
        root_layout.addWidget(self.table, 1)
        root_layout.addWidget(self.status_label)

        self.search_input.clearFocus()
        self.sync_button.setFocus(Qt.OtherFocusReason)

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
                alternate-background-color: #edf2f7;
                border: 1px solid #d8dee4;
                gridline-color: #e5e7eb;
                selection-background-color: #bbf7d0;
                selection-color: #111827;
            }
            QTableWidget::item:selected {
                background: #bbf7d0;
                color: #111827;
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
        migrated = self._migrate_legacy_favorite_keys()
        self._apply_filters()
        if migrated:
            self.favorites_changed.emit(self.channel_name)
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
        migrated = self._migrate_legacy_favorite_keys()

        if changed_count > 0 or migrated:
            self._apply_filters()
            self.favorites_changed.emit(self.channel_name)

        pi_source = "__pi__" in warning_messages
        real_warnings = [w for w in warning_messages if w != "__pi__"]

        prefix = "📡 라즈베리파이 | " if pi_source else ""
        summary = f"{prefix}{self.channel_name} 동기화 완료: {len(self.rows)}건"
        summary += f" | 변경 {changed_count}건"
        if real_warnings:
            summary += f" | 경고 {len(real_warnings)}건"
        self.status_label.setText(summary)
        self.status_label.setToolTip("\n".join(real_warnings) if real_warnings else "")
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
        if row.product_id:
            return f"id:{row.product_id}|item:{row.item_id or ''}"
        if row.product_url:
            return f"url:{row.product_url}"
        return f"name:{row.name}"

    @staticmethod
    def _legacy_favorite_key(row: ChannelProduct) -> str:
        return "|".join(
            [
                row.product_id,
                row.product_url or "",
                row.name,
                str(row.price) if row.price is not None else "",
            ]
        )

    def _find_legacy_favorite_key(self, row: ChannelProduct) -> str | None:
        legacy_exact = self._legacy_favorite_key(row)
        if legacy_exact in self.favorite_keys:
            return legacy_exact

        # Backward compatibility for old favorite keys that started with product_id.
        if row.product_id:
            prefix = f"{row.product_id}|"
            for key in self.favorite_keys:
                if key.startswith(prefix) and key.count("|") >= 3:
                    return key

        # Fallback for old keys that can be identified by product_url.
        if row.product_url:
            marker = f"|{row.product_url}|"
            for key in self.favorite_keys:
                if marker in key and key.count("|") >= 3:
                    return key
        return None

    def _migrate_legacy_favorite_keys(self) -> bool:
        if not self.favorite_keys or not self.rows:
            return False

        migrated = False
        for row in self.rows:
            canonical_key = self._favorite_key(row)
            if canonical_key in self.favorite_keys:
                continue

            legacy_key = self._find_legacy_favorite_key(row)
            if not legacy_key:
                continue

            self.favorite_keys.discard(legacy_key)
            self.favorite_keys.add(canonical_key)
            self.cache.save_favorite(self.channel_name, legacy_key, False)
            self.cache.save_favorite(self.channel_name, canonical_key, True)
            migrated = True
        return migrated

    def _is_favorite(self, row: ChannelProduct) -> bool:
        return self._favorite_key(row) in self.favorite_keys

    def _toggle_row_favorite(self, row: ChannelProduct) -> None:
        key = self._favorite_key(row)
        if key in self.favorite_keys:
            self.favorite_keys.remove(key)
            self.cache.save_favorite(self.channel_name, key, False)
        else:
            self.favorite_keys.add(key)
            self.cache.save_favorite(self.channel_name, key, True)
        self._apply_filters()
        self.favorites_changed.emit(self.channel_name)

    def toggle_current_row_favorite(self) -> None:
        current_index = self.table.currentRow()
        if current_index < 0 or current_index >= len(self.filtered_rows):
            return
        self._toggle_row_favorite(self.filtered_rows[current_index])

    def favorite_inventory_rows(self) -> List[FavoriteInventoryRow]:
        rows: List[FavoriteInventoryRow] = []
        for row in self.rows:
            if not self._is_favorite(row):
                continue
            rows.append(
                FavoriteInventoryRow(
                    channel=self.channel_name,
                    cost_key=self._name_override_key(row),
                    serial=row.serial,
                    image_url=row.image_url,
                    name=self._display_name(row),
                    stock=row.stock,
                    sales=row.sales,
                    stockout_days=self._stockout_days(row),
                    price=row.price,
                    product_url=row.product_url,
                )
            )
        return rows

    def _favorite_button(self, row: ChannelProduct) -> QPushButton:
        checked = self._is_favorite(row)
        button = QPushButton("★" if checked else "☆")
        button.setCheckable(True)
        button.setChecked(checked)
        button.setFixedSize(22, 22)
        button.setCursor(Qt.PointingHandCursor)
        button.setToolTip("즐겨찾기 토글 (` 단축키)")
        button.setStyleSheet(
            """
            QPushButton {
                background: transparent;
                border: none;
                font-size: 15px;
                color: #b6beca;
                padding: 0px;
            }
            QPushButton:checked {
                color: #f59e0b;
            }
            QPushButton:hover {
                background: #eef2f7;
                border-radius: 11px;
            }
            """
        )
        button.clicked.connect(lambda _checked=False, item=row: self._toggle_row_favorite(item))
        return button

    def _favorite_cell(self, row: ChannelProduct) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._favorite_button(row), 0, Qt.AlignCenter)
        return container

    @staticmethod
    def _name_override_key(row: ChannelProduct) -> str:
        if row.product_id:
            return f"id:{row.product_id}|item:{row.item_id or ''}"
        if row.product_url:
            return f"url:{row.product_url}"
        return f"name:{row.name}"

    def _display_name(self, row: ChannelProduct) -> str:
        key = self._name_override_key(row)
        return self.name_overrides.get(key, row.name)

    def _predicted_monthly_revenue(self, row: ChannelProduct) -> int | None:
        if row.sales is None or row.price is None:
            return None
        if not self.sales_period_days:
            return None

        period = max(1, int(self.sales_period_days))
        sales = max(0, int(row.sales))
        price = max(0, int(row.price))
        estimated_qty = sales * (30.0 / float(period))
        return int(round(estimated_qty * price))

    def _stockout_days(self, row: ChannelProduct) -> int | None:
        if row.stock is None or row.sales is None or not self.sales_period_days:
            return None
        stock = max(0, int(row.stock))
        sales = max(0, int(row.sales))
        if sales <= 0:
            return None
        period = max(1, int(self.sales_period_days))
        daily_sales = sales / float(period)
        if daily_sales <= 0:
            return None
        return int(math.ceil(stock / daily_sales))

    def _image_label(self) -> ProductImageLabel:
        label = ProductImageLabel()
        label.clicked.connect(self._open_product_page)
        return label

    def _image_cell(self, row: ChannelProduct) -> tuple[QWidget, ProductImageLabel]:
        container = QWidget()
        container.setFixedWidth(58)

        layout = QHBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(0)

        label = self._image_label()
        label.set_product_url(row.product_url)
        layout.addWidget(label, 0, Qt.AlignCenter)
        return container, label

    def _stock_cover_meta(self, row: ChannelProduct) -> tuple[int, str, str]:
        period_days = self.sales_period_days
        if period_days is None:
            return 0, "#94a3b8", "판매 기준일 정보 없음"

        stock = row.stock
        sales = row.sales
        if stock is None or sales is None:
            return 0, "#94a3b8", f"재고/판매 데이터 부족 ({period_days}일 기준)"

        safe_stock = max(0, int(stock))
        safe_sales = max(0, int(sales))
        if safe_sales <= 0:
            return 0, "#94a3b8", f"무판매 ({period_days}일 기준) | 재고 {safe_stock:,}"

        daily_sales = safe_sales / float(period_days)
        cover_days = safe_stock / daily_sales if daily_sales > 0 else 0.0

        if cover_days <= 7:
            color = "#ef4444"
        elif cover_days <= 21:
            color = "#f59e0b"
        else:
            color = "#22c55e"

        gauge_value = int(round((cover_days / 30.0) * 100))
        gauge_value = max(0, min(100, gauge_value))
        if gauge_value == 0 and safe_stock > 0:
            gauge_value = 1

        label = (
            f"커버 {cover_days:.1f}일 (재고 {safe_stock:,} / 판매 {safe_sales:,}, "
            f"{period_days}일 기준)"
        )
        return gauge_value, color, label

    def _serial_cell(self, row: ChannelProduct) -> QWidget:
        gauge_value, gauge_color, gauge_text = self._stock_cover_meta(row)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        serial_label = QLabel(str(row.serial))
        serial_label.setAlignment(Qt.AlignCenter)
        serial_label.setStyleSheet("color: #111827; font-weight: 600;")
        layout.addWidget(serial_label)

        gauge_bar = QProgressBar()
        gauge_bar.setRange(0, 100)
        gauge_bar.setValue(gauge_value)
        gauge_bar.setTextVisible(False)
        gauge_bar.setFixedHeight(4)
        gauge_bar.setFixedWidth(30)
        gauge_bar.setToolTip(gauge_text)
        gauge_bar.setStyleSheet(
            f"""
            QProgressBar {{
                border: 1px solid #cbd5e1;
                border-radius: 2px;
                background: #f1f5f9;
            }}
            QProgressBar::chunk {{
                border-radius: 2px;
                background: {gauge_color};
            }}
            """
        )
        layout.addWidget(gauge_bar, 0, Qt.AlignHCenter)
        return container

    def _name_cell(self, row: ChannelProduct) -> QWidget:
        display_name = self._display_name(row)
        original_name = row.name
        is_customized = display_name != original_name
        is_zero_stock = row.stock is not None and int(row.stock) == 0

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(6, 0, 6, 0)
        layout.setSpacing(0)

        name_label = EditableNameLabel(display_name)
        name_color = "#b91c1c" if is_zero_stock else "#0f172a"
        if is_customized:
            name_label.setToolTip(f"원래 상품명: {original_name}\n더블클릭: 표시 상품명 수정")
            name_label.setStyleSheet(
                f"color: {name_color}; font-weight: 700; padding: 0px; margin: 0px;"
            )
        else:
            name_label.setToolTip("더블클릭: 표시 상품명 수정")
            name_label.setStyleSheet(
                f"color: {name_color}; font-weight: 500; padding: 0px; margin: 0px;"
            )
        name_label.double_clicked.connect(lambda item=row: self._edit_row_name(item))
        layout.addWidget(name_label)
        if is_customized:
            original_label = QLabel(f"원상품명: {original_name}")
            original_color = "#dc2626" if is_zero_stock else "#64748b"
            original_label.setStyleSheet(
                f"color: {original_color}; font-size: 8px; padding: 0px; margin: 0px;"
            )
            original_label.setToolTip(original_name)
            layout.addWidget(original_label)
        return container

    def _edit_row_name(self, row: ChannelProduct) -> None:
        key = self._name_override_key(row)
        original_name = row.name
        current_display_name = self._display_name(row)
        value, ok = QInputDialog.getText(
            self,
            f"{self.channel_name} 상품명 설정",
            "표시할 상품명을 입력하세요. (빈칸: 원래 이름 복원)",
            QLineEdit.Normal,
            current_display_name,
        )
        if not ok:
            return

        updated = value.strip()
        if not updated or updated == original_name:
            self.name_overrides.pop(key, None)
            self.cache.save_name_override(self.channel_name, key, None)
        else:
            self.name_overrides[key] = updated
            self.cache.save_name_override(self.channel_name, key, updated)
        self._force_full_render_next = True
        self._apply_filters()
        self.favorites_changed.emit(self.channel_name)

    def _open_product_page(self, url: str) -> None:
        QDesktopServices.openUrl(QUrl(url))

    @Slot()
    def _apply_filters(self) -> None:
        force_full_render = self._force_full_render_next
        self._force_full_render_next = False

        keyword = self.search_input.text().strip().lower()
        fav_only = self.favorite_filter.currentText() == "즐겨찾기"

        filtered: List[ChannelProduct] = []
        for row in self.rows:
            if fav_only and not self._is_favorite(row):
                continue
            displayed = self._display_name(row).lower()
            original = row.name.lower()
            if keyword and keyword not in displayed and keyword not in original:
                continue
            filtered.append(row)

        previous_rows = self.filtered_rows
        next_rows = self._sort_rows(filtered)
        if not force_full_render and self._can_patch_render(previous_rows, next_rows):
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
            return sorted(rows, key=lambda row: self._display_name(row).lower(), reverse=descending)
        if col == 4:
            return self._sort_nullable_numeric(rows, lambda row: row.stock, descending)
        if col == 5:
            return self._sort_nullable_numeric(rows, lambda row: row.today_sales, descending)
        if col == 6:
            return self._sort_nullable_numeric(rows, lambda row: row.sales, descending)
        if col == 7:
            return self._sort_nullable_numeric(rows, self._stockout_days, descending)
        if col == 8:
            return self._sort_nullable_numeric(rows, self._predicted_monthly_revenue, descending)
        if col == 9:
            return self._sort_nullable_numeric(rows, lambda row: row.price, descending)
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
        self.table.setRowHeight(index, 66)
        self.table.setCellWidget(index, 0, self._favorite_cell(row))

        self.table.setCellWidget(index, 1, self._serial_cell(row))

        image_cell, image_label = self._image_cell(row)
        self.table.setCellWidget(index, 2, image_cell)

        self.table.setCellWidget(index, 3, self._name_cell(row))

        self.table.setItem(
            index,
            4,
            self._table_item(
                self._fmt_int(row.stock),
                Qt.AlignRight | Qt.AlignVCenter,
                sort_value=self._sortable_none_last(row.stock),
            ),
        )

        self.table.setItem(
            index,
            5,
            self._table_item(
                self._fmt_int(row.today_sales),
                Qt.AlignRight | Qt.AlignVCenter,
                sort_value=self._sortable_none_last(row.today_sales),
            ),
        )

        self.table.setItem(
            index,
            6,
            self._table_item(
                self._fmt_int(row.sales),
                Qt.AlignRight | Qt.AlignVCenter,
                sort_value=self._sortable_none_last(row.sales),
            ),
        )

        self.table.setItem(
            index,
            7,
            self._table_item(
                self._fmt_int(self._stockout_days(row)),
                Qt.AlignRight | Qt.AlignVCenter,
                sort_value=self._sortable_none_last(self._stockout_days(row)),
            ),
        )

        self.table.setItem(
            index,
            8,
            self._table_item(
                self._fmt_int(self._predicted_monthly_revenue(row), "원"),
                Qt.AlignRight | Qt.AlignVCenter,
                sort_value=self._sortable_none_last(self._predicted_monthly_revenue(row)),
            ),
        )

        self.table.setItem(
            index,
            9,
            self._table_item(
                self._fmt_int(row.price, "원"),
                Qt.AlignRight | Qt.AlignVCenter,
                sort_value=self._sortable_none_last(row.price),
            ),
        )

        self._queue_image(image_label, row.image_url, token)

        # 품절 행 빨간 배경
        is_soldout = row.stock is not None and int(row.stock) == 0
        bg = QColor("#fee2e2") if is_soldout else QColor(0, 0, 0, 0)
        for col in range(5, 10):
            item = self.table.item(index, col)
            if item:
                item.setBackground(bg)

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

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            event.type() == QEvent.MouseButtonPress
            and self.search_input.hasFocus()
            and watched is not self.search_input
        ):
            if isinstance(watched, QWidget) and self.search_input.isAncestorOf(watched):
                return super().eventFilter(watched, event)
            self.search_input.clearFocus()
        return super().eventFilter(watched, event)

    def shutdown(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        if self.worker and self.worker.isRunning():
            finished = self.worker.wait(60000)
            if not finished:
                self.worker.terminate()
                self.worker.wait(2000)
        self.image_executor.shutdown(wait=False, cancel_futures=True)


class InventoryManagementTab(QWidget):
    image_downloaded = Signal(str, object)

    def __init__(self) -> None:
        super().__init__()
        self.cache = ChannelProductCache()
        self.rows: List[FavoriteInventoryRow] = []
        self.filtered_rows: List[FavoriteInventoryRow] = []
        self.cost_overrides_by_channel: dict[str, dict[str, int]] = {}
        self.image_cache: dict[str, QPixmap] = {}
        self._image_waiters: dict[str, list[tuple[QLabel, int]]] = {}
        self._image_pending: set[str] = set()
        self.image_executor = ThreadPoolExecutor(max_workers=4)
        self.render_token = 0

        self.channel_filter = QComboBox()
        self.search_input = QLineEdit()
        self.table = QTableWidget(0, 12)
        self.status_label = QLabel("즐겨찾기한 상품이 없습니다.")
        self.note_label = QLabel("네이버/쿠팡 탭에서 ★를 눌러 즐겨찾기하면 여기에 재고가 표시됩니다.")
        self.note_label.setWordWrap(True)

        self.image_downloaded.connect(self._on_image_downloaded)
        self._build_ui()

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(10)

        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)

        self.channel_filter.addItems(["전체", "네이버", "쿠팡"])
        self.channel_filter.currentIndexChanged.connect(self._apply_filters)

        self.search_input.setPlaceholderText("즐겨찾기 상품명 검색")
        self.search_input.textChanged.connect(self._apply_filters)

        top_bar.addWidget(QLabel("채널"))
        top_bar.addWidget(self.channel_filter)
        top_bar.addWidget(QLabel("검색"))
        top_bar.addWidget(self.search_input, 1)

        self.table.setHorizontalHeaderLabels(
            [
                "채널",
                "연번",
                "상품이미지",
                "상품명",
                "재고",
                "판매량",
                "품절예상(일)",
                "판매가",
                "원가",
                "총원가",
                "총판매가",
                "예상이익",
            ]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setSortingEnabled(True)
        self.table.setColumnWidth(0, 80)
        self.table.setColumnWidth(1, 54)
        self.table.setColumnWidth(2, 68)
        self.table.setColumnWidth(3, 420)
        self.table.setColumnWidth(4, 82)
        self.table.setColumnWidth(5, 82)
        self.table.setColumnWidth(6, 106)
        self.table.setColumnWidth(7, 98)
        self.table.setColumnWidth(8, 98)
        self.table.setColumnWidth(9, 118)
        self.table.setColumnWidth(10, 128)
        self.table.setColumnWidth(11, 118)
        self.table.itemDoubleClicked.connect(self._on_item_double_clicked)

        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.setSectionResizeMode(3, QHeaderView.Interactive)
        header.setSectionResizeMode(4, QHeaderView.Fixed)
        header.setSectionResizeMode(5, QHeaderView.Fixed)
        header.setSectionResizeMode(6, QHeaderView.Fixed)
        header.setSectionResizeMode(7, QHeaderView.Fixed)
        header.setSectionResizeMode(8, QHeaderView.Fixed)
        header.setSectionResizeMode(9, QHeaderView.Fixed)
        header.setSectionResizeMode(10, QHeaderView.Fixed)
        header.setSectionResizeMode(11, QHeaderView.Fixed)

        self.status_label.setStyleSheet("color: #475569;")
        self.note_label.setStyleSheet("color: #64748b;")

        root_layout.addLayout(top_bar)
        root_layout.addWidget(self.table, 1)
        root_layout.addWidget(self.status_label)
        root_layout.addWidget(self.note_label)
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
            QTableWidget {
                background: #ffffff;
                alternate-background-color: #edf2f7;
                border: 1px solid #d8dee4;
                gridline-color: #e5e7eb;
                selection-background-color: #bbf7d0;
                selection-color: #111827;
            }
            QTableWidget::item:selected {
                background: #bbf7d0;
                color: #111827;
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

    @staticmethod
    def _fmt_int(value: int | None, suffix: str = "") -> str:
        if value is None:
            return "-"
        return f"{int(value):,}{suffix}"

    @staticmethod
    def _sortable_none_last(value: int | None) -> tuple[int, int]:
        if value is None:
            return (1, 0)
        return (0, int(value))

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

    @staticmethod
    def _channel_cell_colors(channel: str) -> tuple[QColor, QColor]:
        if channel == "쿠팡":
            return QColor("#dbeafe"), QColor("#1d4ed8")
        if channel == "네이버":
            return QColor("#dcfce7"), QColor("#15803d")
        return QColor("#f1f5f9"), QColor("#334155")

    def _ensure_cost_map(self, channel: str) -> dict[str, int]:
        channel_key = str(channel).strip()
        overrides = self.cost_overrides_by_channel.get(channel_key)
        if overrides is None:
            overrides = self.cache.load_cost_overrides(channel_key)
            self.cost_overrides_by_channel[channel_key] = overrides
        return overrides

    def _unit_cost(self, row: FavoriteInventoryRow) -> int | None:
        overrides = self._ensure_cost_map(row.channel)
        return overrides.get(row.cost_key)

    @staticmethod
    def _total_cost(stock: int | None, unit_cost: int | None) -> int | None:
        if stock is None or unit_cost is None:
            return None
        return int(stock) * int(unit_cost)

    @staticmethod
    def _total_sales_price(stock: int | None, sale_price: int | None) -> int | None:
        if stock is None or sale_price is None:
            return None
        return int(stock) * int(sale_price)

    @staticmethod
    def _expected_profit(total_sales_price: int | None, total_cost: int | None) -> int | None:
        if total_sales_price is None or total_cost is None:
            return None
        return int(total_sales_price) - int(total_cost)

    def _save_unit_cost(self, channel: str, cost_key: str, unit_cost: int | None) -> None:
        overrides = self._ensure_cost_map(channel)
        if unit_cost is None:
            overrides.pop(cost_key, None)
        else:
            overrides[cost_key] = int(unit_cost)
        self.cache.save_cost_override(channel, cost_key, unit_cost)

    def _edit_unit_cost(self, channel: str, cost_key: str, current_cost: int | None) -> None:
        initial = "" if current_cost is None else str(current_cost)
        value, ok = QInputDialog.getText(
            self,
            "원가 입력",
            "원가를 입력하세요. (빈칸 입력 시 원가 삭제)",
            QLineEdit.Normal,
            initial,
        )
        if not ok:
            return

        text = value.strip().replace(",", "")
        if not text:
            self._save_unit_cost(channel, cost_key, None)
            self._apply_filters()
            return

        try:
            parsed = int(text)
        except ValueError:
            QMessageBox.warning(self, "원가 입력 오류", "원가는 숫자만 입력할 수 있습니다.")
            return

        if parsed < 0:
            QMessageBox.warning(self, "원가 입력 오류", "원가는 0 이상이어야 합니다.")
            return

        self._save_unit_cost(channel, cost_key, parsed)
        self._apply_filters()

    def set_rows(self, rows: List[FavoriteInventoryRow]) -> None:
        channels = {row.channel for row in rows}
        for channel in channels:
            self._ensure_cost_map(channel)
        self.rows = list(rows)
        self._apply_filters()

    @Slot()
    def _apply_filters(self) -> None:
        selected_channel = self.channel_filter.currentText()
        keyword = self.search_input.text().strip().lower()

        filtered: List[FavoriteInventoryRow] = []
        for row in self.rows:
            if selected_channel != "전체" and row.channel != selected_channel:
                continue
            if keyword and keyword not in row.name.lower():
                continue
            filtered.append(row)

        filtered.sort(
            key=lambda row: (
                row.stock is None,
                int(row.stock or 0),
                row.channel,
                row.serial,
            )
        )
        self.filtered_rows = filtered
        self._render_rows(filtered)

        self.status_label.setText(f"즐겨찾기 재고 {len(filtered)}건 (전체 {len(self.rows)}건)")
        if filtered:
            self.note_label.setText("이미지 클릭/상품명 더블클릭으로 상품 페이지를 열고, 원가 칼럼 더블클릭으로 원가를 입력할 수 있습니다.")
        else:
            self.note_label.setText("네이버/쿠팡 탭에서 ★를 눌러 즐겨찾기한 상품만 표시됩니다.")

    def _image_cell(self, row: FavoriteInventoryRow) -> tuple[QWidget, ProductImageLabel]:
        container = QWidget()
        container.setFixedWidth(62)
        layout = QHBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(0)
        label = ProductImageLabel()
        label.set_product_url(row.product_url)
        label.clicked.connect(self._open_product_page)
        layout.addWidget(label, 0, Qt.AlignCenter)
        return container, label

    def _render_rows(self, rows: List[FavoriteInventoryRow]) -> None:
        self.render_token += 1
        token = self.render_token
        self._image_waiters.clear()

        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))

        for index, row in enumerate(rows):
            self.table.setRowHeight(index, 56)
            channel_bg, channel_fg = self._channel_cell_colors(row.channel)
            channel_item = self._table_item(row.channel, Qt.AlignCenter, row.channel)
            channel_item.setBackground(channel_bg)
            channel_item.setForeground(channel_fg)
            self.table.setItem(
                index,
                0,
                channel_item,
            )
            self.table.setItem(
                index,
                1,
                self._table_item(str(row.serial), Qt.AlignCenter, row.serial),
            )

            image_cell, image_label = self._image_cell(row)
            self.table.setCellWidget(index, 2, image_cell)
            self.table.setItem(index, 2, self._table_item("", Qt.AlignCenter, 0))
            self._queue_image(image_label, row.image_url, token)

            name_item = self._table_item(row.name, Qt.AlignVCenter | Qt.AlignLeft, row.name.lower())
            if row.product_url:
                name_item.setData(Qt.UserRole + 1, row.product_url)
                name_item.setToolTip("더블클릭: 상품 페이지 열기")
            self.table.setItem(index, 3, name_item)
            self.table.setItem(
                index,
                4,
                self._table_item(
                    self._fmt_int(row.stock),
                    Qt.AlignRight | Qt.AlignVCenter,
                    self._sortable_none_last(row.stock),
                ),
            )
            self.table.setItem(
                index,
                5,
                self._table_item(
                    self._fmt_int(row.sales),
                    Qt.AlignRight | Qt.AlignVCenter,
                    self._sortable_none_last(row.sales),
                ),
            )
            self.table.setItem(
                index,
                6,
                self._table_item(
                    self._fmt_int(row.stockout_days),
                    Qt.AlignRight | Qt.AlignVCenter,
                    self._sortable_none_last(row.stockout_days),
                ),
            )
            self.table.setItem(
                index,
                7,
                self._table_item(
                    self._fmt_int(row.price, "원"),
                    Qt.AlignRight | Qt.AlignVCenter,
                    self._sortable_none_last(row.price),
                ),
            )
            unit_cost = self._unit_cost(row)
            unit_cost_item = self._table_item(
                self._fmt_int(unit_cost, "원"),
                Qt.AlignRight | Qt.AlignVCenter,
                self._sortable_none_last(unit_cost),
            )
            unit_cost_item.setData(Qt.UserRole + 2, row.channel)
            unit_cost_item.setData(Qt.UserRole + 3, row.cost_key)
            unit_cost_item.setData(Qt.UserRole + 4, unit_cost)
            unit_cost_item.setToolTip("더블클릭: 원가 입력/수정")
            self.table.setItem(index, 8, unit_cost_item)

            total_cost = self._total_cost(row.stock, unit_cost)
            self.table.setItem(
                index,
                9,
                self._table_item(
                    self._fmt_int(total_cost, "원"),
                    Qt.AlignRight | Qt.AlignVCenter,
                    self._sortable_none_last(total_cost),
                ),
            )

            total_sales_price = self._total_sales_price(row.stock, row.price)
            self.table.setItem(
                index,
                10,
                self._table_item(
                    self._fmt_int(total_sales_price, "원"),
                    Qt.AlignRight | Qt.AlignVCenter,
                    self._sortable_none_last(total_sales_price),
                ),
            )

            expected_profit = self._expected_profit(total_sales_price, total_cost)
            self.table.setItem(
                index,
                11,
                self._table_item(
                    self._fmt_int(expected_profit, "원"),
                    Qt.AlignRight | Qt.AlignVCenter,
                    self._sortable_none_last(expected_profit),
                ),
            )

        self.table.setSortingEnabled(True)
        if rows:
            self.table.sortItems(4, Qt.AscendingOrder)

    def _queue_image(self, label: QLabel, image_url: str | None, token: int) -> None:
        normalized_url = ChannelTab._normalize_image_url(image_url)
        if not normalized_url:
            return

        cached = self.image_cache.get(normalized_url)
        if cached is not None:
            self._set_label_pixmap(label, cached)
            return

        waiters = self._image_waiters.setdefault(normalized_url, [])
        waiters.append((label, token))
        if normalized_url in self._image_pending:
            return

        self._image_pending.add(normalized_url)
        future = self.image_executor.submit(ChannelTab._download_image_bytes, normalized_url)
        future.add_done_callback(
            lambda f, url=normalized_url: self._emit_image_downloaded(url, f)
        )

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

    def _open_product_page(self, url: str) -> None:
        QDesktopServices.openUrl(QUrl(url))

    @Slot(QTableWidgetItem)
    def _on_item_double_clicked(self, item: QTableWidgetItem) -> None:
        if item.column() == 8:
            channel = item.data(Qt.UserRole + 2)
            cost_key = item.data(Qt.UserRole + 3)
            current_cost = item.data(Qt.UserRole + 4)
            if isinstance(channel, str) and isinstance(cost_key, str):
                self._edit_unit_cost(channel, cost_key, int(current_cost) if current_cost is not None else None)
            return

        url = item.data(Qt.UserRole + 1)
        if not isinstance(url, str) or not url.strip():
            return
        QDesktopServices.openUrl(QUrl(url))

    def shutdown(self) -> None:
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
    image_downloaded = Signal(str, object)
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
        self.product_image_cache: dict[str, QPixmap] = {}
        self._product_image_waiters: dict[str, list[tuple[QLabel, int]]] = {}
        self._product_image_pending: set[str] = set()
        self.product_image_executor = ThreadPoolExecutor(max_workers=6)
        self.product_render_token = 0

        self.sync_button = QPushButton("동기화")
        self.period_combo = QComboBox()
        self.summary_table = QTableWidget(0, 7)
        self.product_table = QTableWidget(0, 9)
        self.status_label = QLabel("준비 완료")
        self.note_label = QLabel("")
        self.image_downloaded.connect(self._on_product_image_downloaded)

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
        self.period_combo.currentIndexChanged.connect(lambda _idx: self.sync_now())

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
                "상품이미지",
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
        self.product_table.setColumnWidth(2, 78)
        self.product_table.setColumnWidth(3, 430)
        self.product_table.setColumnWidth(4, 100)
        self.product_table.setColumnWidth(5, 140)
        self.product_table.setColumnWidth(6, 130)
        self.product_table.setColumnWidth(7, 140)
        self.product_table.setColumnWidth(8, 110)

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
                alternate-background-color: #edf2f7;
                border: 1px solid #d8dee4;
                gridline-color: #e5e7eb;
                selection-background-color: #bbf7d0;
                selection-color: #111827;
            }
            QTableWidget::item:selected {
                background: #bbf7d0;
                color: #111827;
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
        self.product_render_token += 1
        token = self.product_render_token
        self._product_image_waiters.clear()

        self.product_table.setSortingEnabled(False)
        self.product_table.setRowCount(len(rows))

        for index, row in enumerate(rows):
            self.product_table.setRowHeight(index, 52)
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
            image_cell, image_label = self._product_image_cell()
            self.product_table.setCellWidget(index, 2, image_cell)
            self.product_table.setItem(index, 2, self._table_item("", Qt.AlignCenter, 0))
            self._queue_product_image(image_label, row.image_url, token)
            self.product_table.setItem(
                index,
                3,
                self._table_item(
                    row.name,
                    Qt.AlignVCenter | Qt.AlignLeft,
                    row.name.lower(),
                ),
            )
            self.product_table.setItem(
                index,
                4,
                self._table_item(self._fmt_int(row.orders), Qt.AlignCenter, row.orders),
            )
            self.product_table.setItem(
                index,
                5,
                self._table_item(
                    self._fmt_money(row.gross),
                    Qt.AlignRight | Qt.AlignVCenter,
                    row.gross,
                ),
            )
            self.product_table.setItem(
                index,
                6,
                self._table_item(
                    self._fmt_money(row.refund),
                    Qt.AlignRight | Qt.AlignVCenter,
                    row.refund,
                ),
            )
            self.product_table.setItem(
                index,
                7,
                self._table_item(
                    self._fmt_money(row.net),
                    Qt.AlignRight | Qt.AlignVCenter,
                    row.net,
                ),
            )
            self.product_table.setItem(
                index,
                8,
                self._table_item(dtype, Qt.AlignCenter, dtype),
            )

        self.product_table.setSortingEnabled(True)
        if rows:
            self.product_table.sortItems(7, Qt.DescendingOrder)

    def _product_image_cell(self) -> tuple[QWidget, ProductImageLabel]:
        container = QWidget()
        container.setFixedWidth(58)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(0)
        label = ProductImageLabel()
        label.set_product_url(None)
        layout.addWidget(label, 0, Qt.AlignCenter)
        return container, label

    def _queue_product_image(self, label: QLabel, image_url: str | None, token: int) -> None:
        normalized_url = ChannelTab._normalize_image_url(image_url)
        if not normalized_url:
            return

        cached = self.product_image_cache.get(normalized_url)
        if cached is not None:
            self._set_product_label_pixmap(label, cached)
            return

        waiters = self._product_image_waiters.setdefault(normalized_url, [])
        waiters.append((label, token))
        if normalized_url in self._product_image_pending:
            return

        self._product_image_pending.add(normalized_url)
        future = self.product_image_executor.submit(ChannelTab._download_image_bytes, normalized_url)
        future.add_done_callback(
            lambda f, url=normalized_url: self._emit_product_image_downloaded(url, f)
        )

    def _emit_product_image_downloaded(self, url: str, future: Future[bytes | None]) -> None:
        data: bytes | None
        try:
            data = future.result()
        except Exception:  # noqa: BLE001
            data = None
        self.image_downloaded.emit(url, data)

    @Slot(str, object)
    def _on_product_image_downloaded(self, url: str, data: object) -> None:
        self._product_image_pending.discard(url)
        waiters = self._product_image_waiters.pop(url, [])
        if not waiters:
            return

        pixmap: QPixmap | None = None
        if isinstance(data, (bytes, bytearray)):
            candidate = QPixmap()
            if candidate.loadFromData(bytes(data)):
                pixmap = candidate
                self.product_image_cache[url] = candidate
        if pixmap is None:
            return

        for label, token in waiters:
            if token != self.product_render_token:
                continue
            try:
                self._set_product_label_pixmap(label, pixmap)
            except RuntimeError:
                continue

    def _set_product_label_pixmap(self, label: QLabel, pixmap: QPixmap) -> None:
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
        self.product_image_executor.shutdown(wait=False, cancel_futures=True)


class KeywordSyncWorker(QThread):
    completed = Signal(object, object)
    failed = Signal(str)

    def __init__(
        self,
        fetch_fn: Callable[[int], tuple[KeywordRevenueSnapshot, List[str]]],
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


class KeywordRevenueTab(QWidget):
    sync_finished = Signal(str, bool)

    def __init__(
        self,
        fetch_fn: Callable[[int], tuple[KeywordRevenueSnapshot, List[str]]],
        default_days: int,
    ) -> None:
        super().__init__()
        self.fetch_fn = fetch_fn
        self.default_days = max(1, int(default_days))
        self.worker: KeywordSyncWorker | None = None

        self.sync_button = QPushButton("동기화")
        self.period_combo = QComboBox()
        self.table = QTableWidget(0, 8)
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
        self.period_combo.currentIndexChanged.connect(lambda _idx: self.sync_now())

        top_bar.addWidget(self.sync_button)
        top_bar.addWidget(QLabel("기준기간"))
        top_bar.addWidget(self.period_combo)
        top_bar.addStretch(1)

        self.table.setHorizontalHeaderLabels(
            [
                "연번",
                "키워드",
                "매출",
                "주문수",
                "유입수",
                "전환율",
                "객단가",
                "데이터출처",
            ]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setSortingEnabled(True)
        self.table.setColumnWidth(0, 54)
        self.table.setColumnWidth(1, 300)
        self.table.setColumnWidth(2, 140)
        self.table.setColumnWidth(3, 100)
        self.table.setColumnWidth(4, 100)
        self.table.setColumnWidth(5, 100)
        self.table.setColumnWidth(6, 130)
        self.table.setColumnWidth(7, 140)

        self.status_label.setStyleSheet("color: #475569;")
        self.note_label.setStyleSheet("color: #475569;")
        self.note_label.setWordWrap(True)

        root_layout.addLayout(top_bar)
        root_layout.addWidget(QLabel("네이버 키워드 매출"), 0)
        root_layout.addWidget(self.table, 1)
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
                alternate-background-color: #edf2f7;
                border: 1px solid #d8dee4;
                gridline-color: #e5e7eb;
                selection-background-color: #bbf7d0;
                selection-color: #14532d;
            }
            QTableWidget::item:selected {
                background: #bbf7d0;
                color: #14532d;
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
        for idx in range(self.period_combo.count()):
            value = int(self.period_combo.itemData(idx) or 0)
            if value == self.default_days:
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
        self._set_busy(True, f"네이버 키워드 매출 데이터를 동기화하는 중입니다... ({period_days}일)")
        self.worker = KeywordSyncWorker(self.fetch_fn, period_days)
        self.worker.completed.connect(self._on_sync_completed)
        self.worker.failed.connect(self._on_sync_failed)
        self.worker.start()
        return True

    @staticmethod
    def _fmt_int(value: int | None) -> str:
        if value is None:
            return "-"
        return f"{int(value):,}"

    @staticmethod
    def _fmt_money(value: float | int | None) -> str:
        if value is None:
            return "-"
        return f"{int(round(float(value))):,}원"

    @staticmethod
    def _fmt_rate(value: float | None) -> str:
        if value is None:
            return "-"
        return f"{value:.2f}%"

    @staticmethod
    def _risk_level(row: KeywordRevenueRow) -> str | None:
        if row.inflow is None or row.inflow <= 0:
            return None

        conversion = row.conversion_rate
        if conversion is None and row.inflow > 0:
            conversion = (float(row.orders) / float(row.inflow)) * 100.0
        if conversion is None:
            return None

        # 고유입 + 저전환 자동 탐지 기준
        if row.inflow >= 200 and conversion < 1.2:
            return "high"
        if row.inflow >= 80 and conversion < 2.5:
            return "medium"
        return None

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
        if not isinstance(snapshot_obj, KeywordRevenueSnapshot):
            self.status_label.setText("키워드 매출 동기화 실패: 응답 형식 오류")
            self.sync_finished.emit("키워드매출", False)
            return

        warnings = list(warnings_obj) if isinstance(warnings_obj, list) else []
        high_count, medium_count = self._render_rows(snapshot_obj.rows)

        self.status_label.setText(
            (
                f"키워드 동기화 완료: 최근 {snapshot_obj.period_days}일 "
                f"(키워드 {len(snapshot_obj.rows)}건, 고위험 {high_count}건, 주의 {medium_count}건) "
                f"| {snapshot_obj.generated_at.strftime('%Y-%m-%d %H:%M:%S')}"
            )
        )

        note_lines = list(snapshot_obj.notes)
        note_lines.append("")
        note_lines.append("[자동 하이라이트 기준]")
        note_lines.append("빨강: 유입수 200 이상 + 전환율 1.2% 미만")
        note_lines.append("주황: 유입수 80 이상 + 전환율 2.5% 미만")
        note_lines.append(f"탐지 결과: 고위험 {high_count}건 / 주의 {medium_count}건")
        if warnings:
            note_lines.append("")
            note_lines.append("[경고]")
            note_lines.extend(warnings)
        self.note_label.setText("\n".join(note_lines))
        self.note_label.setToolTip("\n".join(warnings) if warnings else "")
        self.sync_finished.emit("키워드매출", True)

    @Slot(str)
    def _on_sync_failed(self, error: str) -> None:
        self._set_busy(False)
        self.status_label.setText("키워드 매출 동기화 실패")
        QMessageBox.critical(self, "키워드 매출 동기화 실패", error)
        self.sync_finished.emit("키워드매출", False)

    def _render_rows(self, rows: List[KeywordRevenueRow]) -> tuple[int, int]:
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        high_count = 0
        medium_count = 0
        for index, row in enumerate(rows):
            self.table.setRowHeight(index, 34)
            self.table.setItem(
                index,
                0,
                self._table_item(str(index + 1), Qt.AlignCenter, index + 1),
            )
            self.table.setItem(
                index,
                1,
                self._table_item(row.keyword, Qt.AlignVCenter | Qt.AlignLeft, row.keyword.lower()),
            )
            self.table.setItem(
                index,
                2,
                self._table_item(
                    self._fmt_money(row.pay_amount),
                    Qt.AlignRight | Qt.AlignVCenter,
                    row.pay_amount,
                ),
            )
            self.table.setItem(
                index,
                3,
                self._table_item(self._fmt_int(row.orders), Qt.AlignRight | Qt.AlignVCenter, row.orders),
            )
            self.table.setItem(
                index,
                4,
                self._table_item(
                    self._fmt_int(row.inflow),
                    Qt.AlignRight | Qt.AlignVCenter,
                    -1 if row.inflow is None else row.inflow,
                ),
            )
            self.table.setItem(
                index,
                5,
                self._table_item(
                    self._fmt_rate(row.conversion_rate),
                    Qt.AlignRight | Qt.AlignVCenter,
                    -1.0 if row.conversion_rate is None else row.conversion_rate,
                ),
            )
            self.table.setItem(
                index,
                6,
                self._table_item(
                    self._fmt_money(row.avg_order_value),
                    Qt.AlignRight | Qt.AlignVCenter,
                    -1.0 if row.avg_order_value is None else row.avg_order_value,
                ),
            )
            self.table.setItem(
                index,
                7,
                self._table_item(row.source, Qt.AlignCenter, row.source),
            )

            risk = self._risk_level(row)
            if risk == "high":
                high_count += 1
                bg = QColor("#fee2e2")
                fg = QColor("#991b1b")
                tip = "고유입 저전환(고위험): 유입수 200+ / 전환율 1.2% 미만"
            elif risk == "medium":
                medium_count += 1
                bg = QColor("#ffedd5")
                fg = QColor("#9a3412")
                tip = "고유입 저전환(주의): 유입수 80+ / 전환율 2.5% 미만"
            else:
                bg = None
                fg = None
                tip = ""

            if bg is not None and fg is not None:
                for col in range(8):
                    item = self.table.item(index, col)
                    if item is None:
                        continue
                    item.setBackground(bg)
                    if col in (1, 5):
                        item.setForeground(fg)
                    if tip:
                        item.setToolTip(tip)

        self.table.setSortingEnabled(True)
        if rows:
            self.table.sortItems(2, Qt.DescendingOrder)
        return high_count, medium_count

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
        self.keyword_service = NaverKeywordRevenueService(config)

        self.setWindowTitle("스마트스토어 / 쿠팡 분리 재고 대시보드")
        self.resize(1780, 900)

        self.sync_all_button = QPushButton("전체 동기화")
        self.sync_all_button.setObjectName("primarySyncButton")
        self.pi_status_button = QPushButton("📡 라즈베리파이")
        self.pi_status_button.setObjectName("piStatusButton")
        self.pi_status_button.setVisible(bool(config.monitor_url))
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
            sales_period_days=sales_days,
            fetch_fn=self.naver_service.fetch,
            initial_fetch_fn=self.naver_service.fetch_cached,
        )
        self.coupang_tab = ChannelTab(
            channel_name="쿠팡",
            sales_header="판매량(30일)",
            sales_period_days=30,
            fetch_fn=self.coupang_service.fetch,
            initial_fetch_fn=self.coupang_service.fetch_cached,
        )
        self.inventory_tab = InventoryManagementTab()
        self.revenue_tab = RevenueTab(
            fetch_fn=self.revenue_service.fetch,
            default_days=sales_days,
        )
        self.keyword_tab = KeywordRevenueTab(
            fetch_fn=self.keyword_service.fetch,
            default_days=sales_days,
        )

        self._build_ui()

        self.favorite_shortcut = QShortcut(QKeySequence(Qt.Key_QuoteLeft), self)
        self.favorite_shortcut.setContext(Qt.ApplicationShortcut)
        self.favorite_shortcut.activated.connect(self._toggle_favorite_on_current_tab)

        self.tab_shortcut_1 = QShortcut(QKeySequence("1"), self)
        self.tab_shortcut_1.setContext(Qt.ApplicationShortcut)
        self.tab_shortcut_1.activated.connect(lambda: self._activate_tab_shortcut(0))

        self.tab_shortcut_2 = QShortcut(QKeySequence("2"), self)
        self.tab_shortcut_2.setContext(Qt.ApplicationShortcut)
        self.tab_shortcut_2.activated.connect(lambda: self._activate_tab_shortcut(1))

        self.tab_shortcut_3 = QShortcut(QKeySequence("3"), self)
        self.tab_shortcut_3.setContext(Qt.ApplicationShortcut)
        self.tab_shortcut_3.activated.connect(lambda: self._activate_tab_shortcut(2))

        self.tab_shortcut_4 = QShortcut(QKeySequence("4"), self)
        self.tab_shortcut_4.setContext(Qt.ApplicationShortcut)
        self.tab_shortcut_4.activated.connect(lambda: self._activate_tab_shortcut(3))

        self.tab_shortcut_5 = QShortcut(QKeySequence("5"), self)
        self.tab_shortcut_5.setContext(Qt.ApplicationShortcut)
        self.tab_shortcut_5.activated.connect(lambda: self._activate_tab_shortcut(4))

        self.naver_tab.sync_finished.connect(self._on_sub_sync_finished)
        self.coupang_tab.sync_finished.connect(self._on_sub_sync_finished)
        self.naver_tab.favorites_changed.connect(self._refresh_inventory_tab)
        self.coupang_tab.favorites_changed.connect(self._refresh_inventory_tab)
        self.revenue_tab.sync_finished.connect(self._on_sub_sync_finished)
        self.keyword_tab.sync_finished.connect(self._on_sub_sync_finished)
        self._refresh_inventory_tab()

    def _build_ui(self) -> None:
        root = QWidget(self)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(8)

        self.sync_all_button.clicked.connect(self.sync_now)
        self.sync_progress.setObjectName("syncProgressBar")
        self.sync_progress.setRange(0, 100)
        self.sync_progress.setValue(0)
        self.sync_progress.setTextVisible(False)
        self.sync_progress.setFormat("대기 중 0%")
        self.sync_progress.setVisible(False)

        self.tabs.addTab(self.naver_tab, "네이버")
        self.tabs.addTab(self.coupang_tab, "쿠팡")
        self.tabs.addTab(self.inventory_tab, "재고관리")
        self.tabs.addTab(self.revenue_tab, "매출비교")
        self.tabs.addTab(self.keyword_tab, "키워드매출")
        corner = QWidget()
        corner_layout = QHBoxLayout(corner)
        corner_layout.setContentsMargins(0, 0, 0, 0)
        corner_layout.setSpacing(0)
        self.pi_status_button.clicked.connect(self._check_pi_status)
        corner_layout.addWidget(self.pi_status_button)
        corner_layout.addWidget(self.sync_all_button)
        self.tabs.setCornerWidget(corner, Qt.TopRightCorner)

        root_layout.addWidget(self.tabs, 1)
        root_layout.addWidget(self.sync_progress, 0)

        self.setCentralWidget(root)
        self._apply_styles()

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow {
                background: #f3f6fb;
            }
            #piStatusButton {
                background: #16a34a;
                color: white;
                border: 1px solid #16a34a;
                border-radius: 9px;
                padding: 6px 12px;
                font-weight: 600;
                margin-right: 6px;
            }
            #piStatusButton:hover {
                background: #15803d;
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
                min-height: 6px;
                max-height: 6px;
                border: 1px solid #d8dee4;
                border-radius: 3px;
                background: #f8fafc;
            }
            #syncProgressBar::chunk {
                border-radius: 3px;
                background: #10b981;
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
            self.sync_progress.setValue(0)
            self.sync_progress.setFormat("대기 중 0%")
            self.sync_progress.setVisible(False)
            return
        self.sync_progress.setVisible(True)
        self.sync_progress.setValue(0)
        self.sync_progress.setFormat("동기화 진행 중... 0%")

    def _update_sync_progress(self) -> None:
        if not self._sync_session_active:
            return
        total = len(self._sync_expected_sources)
        done = len(self._sync_finished_sources)
        if total <= 0:
            self.sync_progress.setValue(0)
            self.sync_progress.setFormat("대기 중 0%")
            self._sync_session_active = False
            self.sync_progress.setVisible(False)
            return
        percent = int(round((done * 100) / total))
        self.sync_progress.setValue(percent)
        if done >= total:
            self.sync_progress.setValue(100)
            if self._sync_failed_sources:
                self.sync_progress.setFormat("동기화 완료 (일부 실패) 100%")
            else:
                self.sync_progress.setFormat("동기화 완료 100%")
            self._sync_session_active = False
            self.sync_progress.setVisible(False)
        else:
            self.sync_progress.setFormat(f"동기화 진행 중... {percent}%")

    def _collect_favorite_inventory_rows(self) -> List[FavoriteInventoryRow]:
        rows: List[FavoriteInventoryRow] = []
        rows.extend(self.naver_tab.favorite_inventory_rows())
        rows.extend(self.coupang_tab.favorite_inventory_rows())
        rows.sort(
            key=lambda row: (
                row.stock is None,
                int(row.stock or 0),
                row.channel,
                row.serial,
            )
        )
        return rows

    def _refresh_inventory_tab(self, *_args: object) -> None:
        self.inventory_tab.set_rows(self._collect_favorite_inventory_rows())

    @Slot()
    def _check_pi_status(self) -> None:
        import httpx as _httpx
        url = self.config.monitor_url
        if not url:
            return
        try:
            resp = _httpx.get(f"{url.rstrip('/')}/status", timeout=8)
            resp.raise_for_status()
            data = resp.json()
            naver_ts = data.get("naver_last_updated") or "-"
            coupang_ts = data.get("coupang_last_updated") or "-"
            records = data.get("records", 0)

            def _fmt(ts: str) -> str:
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(ts)
                    return dt.strftime("%m/%d %H:%M")
                except Exception:
                    return ts

            msg = (
                f"✅ 정상 작동 중\n\n"
                f"네이버 마지막 수집: {_fmt(naver_ts)}\n"
                f"쿠팡 마지막 수집:   {_fmt(coupang_ts)}\n"
                f"총 누적 레코드:     {records:,}건"
            )
        except Exception as e:
            msg = f"❌ 연결 실패\n\n{e}"
        QMessageBox.information(self, "📡 라즈베리파이 상태", msg)

    def sync_now(self) -> None:
        started_sources: set[str] = set()
        if self.naver_tab.sync_now():
            started_sources.add("네이버")
        if self.coupang_tab.sync_now():
            started_sources.add("쿠팡")
        if self.revenue_tab.sync_now():
            started_sources.add("매출비교")
        if self.keyword_tab.sync_now():
            started_sources.add("키워드매출")

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

    def _activate_tab_shortcut(self, index: int) -> None:
        focused = QApplication.focusWidget()
        if isinstance(focused, QLineEdit):
            return
        if 0 <= index < self.tabs.count():
            self.tabs.setCurrentIndex(index)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.naver_tab.shutdown()
        self.coupang_tab.shutdown()
        self.inventory_tab.shutdown()
        self.revenue_tab.shutdown()
        self.keyword_tab.shutdown()
        super().closeEvent(event)
