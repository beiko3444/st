from __future__ import annotations

import math
import re
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Dict, List
from urllib.parse import quote_plus

import httpx
from PySide6.QtCore import QDate, QEvent, QObject, QSettings, QThread, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QDesktopServices,
    QFont,
    QKeySequence,
    QPixmap,
    QShortcut,
    QTextCharFormat,
)
from PySide6.QtWidgets import (
    QApplication,
    QCalendarWidget,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFontDialog,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
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
from inventory_app.ui.fassto_tab import FasstoTab
from inventory_app.ui.product_master_tab import ProductMasterTab
from inventory_app.services.channel_services import CoupangChannelService, NaverChannelService
from inventory_app.services.keyword_services import (
    KeywordRevenueRow,
    KeywordRevenueSnapshot,
    NaverKeywordRevenueService,
)
from inventory_app.services.local_cache import ChannelProductCache
from inventory_app.services.master_product_service import (
    MasterProductService,
    build_master_service,
)
from inventory_app.services.master_remote_client import MasterRemoteError
from inventory_app.services.shared_stock_grouping import product_identity_key
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


SEARCH_DEBOUNCE_MS = 180
APP_SETTINGS_ORG = "SmartInventory"
APP_SETTINGS_APP = "SmartInventory"
APP_FONT_KEY = "ui/font"


def _base_widget_font_css() -> str:
    app = QApplication.instance()
    if app is None:
        return ""
    font = app.font()
    family = str(font.family() or "").replace("\\", "\\\\").replace('"', '\\"')
    point_size = font.pointSizeF()
    if point_size <= 0:
        point_size = float(font.pointSize()) if font.pointSize() > 0 else 10.0
    parts: list[str] = []
    if family:
        parts.append(f'font-family: "{family}";')
    if point_size > 0:
        parts.append(f"font-size: {point_size:g}pt;")
    return " ".join(parts)


def _with_base_widget_font(stylesheet: str) -> str:
    return stylesheet.replace("__BASE_WIDGET_FONT_CSS__", _base_widget_font_css())


def _normalize_web_url(url: str | None) -> str | None:
    if not isinstance(url, str):
        return None
    text = url.strip()
    if not text:
        return None
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"(?i)%0d|%0a", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if text.startswith("//"):
        text = f"https:{text}"
    elif text.startswith("/"):
        return None
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", text):
        text = f"https://{text.lstrip('/')}"
    qurl = QUrl.fromUserInput(text)
    if not qurl.isValid():
        return None
    if qurl.scheme() not in {"http", "https"}:
        if not qurl.host():
            return None
        qurl.setScheme("https")
    if not qurl.host():
        return None
    return qurl.toString()


def _build_search_url(channel_name: str, product_name: str) -> str | None:
    query = re.sub(r"\s+", " ", str(product_name or "").strip())
    if not query:
        return None
    channel = str(channel_name).strip().lower()
    if channel in {"naver", "네이버"}:
        return f"https://search.shopping.naver.com/search/all?query={quote_plus(query)}"
    if channel in {"coupang", "쿠팡"}:
        return f"https://www.coupang.com/np/search?q={quote_plus(query)}"
    return None


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
        self._product_url = _normalize_web_url(url)
        if self._product_url:
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


class ClickableImageContainer(QWidget):
    clicked = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._product_url: str | None = None

    def set_product_url(self, url: str | None) -> None:
        self._product_url = _normalize_web_url(url)
        if self._product_url:
            self.setCursor(Qt.PointingHandCursor)
            # 툴팁에 실제 URL 을 노출 → 유저가 클릭 전에 검증 가능
            self.setToolTip(
                f"좌클릭: 상품페이지 열기\n"
                f"우클릭: 이 URL 복사\n\n{self._product_url}"
            )
        else:
            self.setCursor(Qt.ArrowCursor)
            self.setToolTip("")

    def mousePressEvent(self, event: Any) -> None:
        # 좌클릭: 상품 페이지 열기 (QTableWidget 셀 위젯에선 release 이벤트 유실 사례가
        # 있어서 press 시점에 즉시 emit).
        if event.button() == Qt.LeftButton and self._product_url:
            self.clicked.emit(self._product_url)
            event.accept()
            return
        # 우클릭: URL 을 클립보드에 복사 (링크가 이상할 때 유저가 직접 브라우저에
        # 붙여넣어 확인할 수 있도록 진단 편의 제공)
        if event.button() == Qt.RightButton and self._product_url:
            try:
                QApplication.clipboard().setText(self._product_url)
            except Exception:  # noqa: BLE001
                pass
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


class ClickableLabel(QLabel):
    """클릭 가능한 QLabel."""

    clicked = Signal()

    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


def _pick_master_with_search(
    parent: QWidget,
    title: str,
    label_text: str,
    options: List[str],
    *,
    create_label: str | None = None,
) -> str | None:
    """검색 가능한 마스터 선택 다이얼로그.

    options 의 첫 번째 항목이 항상 보이도록 하고 (예: "+ 새 마스터 만들기"),
    그 외 항목은 검색어로 필터링.
    """
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.resize(420, 360)

    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(12, 12, 12, 12)
    layout.setSpacing(8)

    layout.addWidget(QLabel(label_text))

    search_edit = QLineEdit()
    search_edit.setPlaceholderText("마스터 이름/번호로 검색…")
    search_edit.setClearButtonEnabled(True)
    layout.addWidget(search_edit)

    list_widget = QListWidget()
    list_widget.setUniformItemSizes(True)
    layout.addWidget(list_widget, 1)

    button_box = QDialogButtonBox(
        QDialogButtonBox.Ok | QDialogButtonBox.Cancel
    )
    button_box.accepted.connect(dialog.accept)
    button_box.rejected.connect(dialog.reject)
    layout.addWidget(button_box)

    def _populate(filter_text: str) -> None:
        list_widget.clear()
        q = filter_text.strip().lower()
        for opt in options:
            is_create = create_label is not None and opt == create_label
            if not is_create and q and q not in opt.lower():
                continue
            item = QListWidgetItem(opt)
            list_widget.addItem(item)
        if list_widget.count() > 0:
            list_widget.setCurrentRow(0)

    _populate("")
    search_edit.textChanged.connect(_populate)

    # Enter 누르면 선택 확정, ↓ 누르면 리스트로 포커스 이동
    def _on_search_return() -> None:
        if list_widget.count() > 0:
            dialog.accept()

    search_edit.returnPressed.connect(_on_search_return)
    list_widget.itemDoubleClicked.connect(lambda _it: dialog.accept())

    if dialog.exec() != QDialog.Accepted:
        return None
    item = list_widget.currentItem()
    return item.text() if item else None


class ChannelTab(QWidget):
    image_downloaded = Signal(str, object)
    sync_finished = Signal(str, bool)
    favorites_changed = Signal(str)
    masters_changed = Signal(str)  # 마스터/링크가 이 탭에서 변경됨

    @staticmethod
    def _resolve_channel_code(channel_name: str) -> str:
        text = str(channel_name or "").strip().lower()
        if text in {"네이버", "naver"}:
            return "naver"
        if text in {"쿠팡", "coupang"}:
            return "coupang"
        return text

    def __init__(
        self,
        channel_name: str,
        sales_header: str,
        sales_period_days: int | None,
        fetch_fn: Callable[[], tuple[List[ChannelProduct], List[str]]],
        initial_fetch_fn: Callable[[], tuple[List[ChannelProduct], List[str]]] | None = None,
        monitor_url: str | None = None,
        timeout_seconds: int = 30,
    ) -> None:
        super().__init__()
        self.channel_name = channel_name
        self.sales_header = sales_header
        self.sales_period_days = max(1, int(sales_period_days)) if sales_period_days else None
        self.fetch_fn = fetch_fn
        self.initial_fetch_fn = initial_fetch_fn
        self.monitor_url = str(monitor_url).strip() if monitor_url else None
        self.timeout_seconds = max(3, int(timeout_seconds))
        self.channel_code = self._resolve_channel_code(self.channel_name)

        self.worker: ChannelSyncWorker | None = None
        self._initial_rows_loaded = False
        self.rows: List[ChannelProduct] = []
        self.filtered_rows: List[ChannelProduct] = []
        self.cache = ChannelProductCache()
        self.master_service = build_master_service(
            cache=self.cache, monitor_url=self.monitor_url
        )
        self.favorite_keys: set[str] = self.cache.load_favorite_keys(self.channel_name)
        self.name_overrides = self.cache.load_name_overrides(self.channel_name)
        # product_key -> master_id (이 채널에 속한 링크만)
        self.master_link_map: Dict[str, int] = {}
        self._reload_master_links()

        self.image_cache: dict[str, QPixmap] = {}
        self._image_waiters: dict[str, list[tuple[QLabel, int]]] = {}
        self._image_pending: set[str] = set()
        self.image_executor = ThreadPoolExecutor(max_workers=8)
        self.render_token = 0
        self._force_full_render_next = False

        # 기본 정렬: 30일 누적 판매량 내림차순 (연번 순서와 일치)
        self.sort_column = 6
        self.sort_order = Qt.DescendingOrder
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(SEARCH_DEBOUNCE_MS)
        self._filter_timer.timeout.connect(self._apply_filters)

        self.sync_button = QPushButton("동기화")
        self.favorite_filter = QComboBox()
        self.search_input = QLineEdit()
        self.today_sales_amount_label = ClickableLabel("오늘 총 판매금액: 0원")
        self.today_sales_amount_label.setToolTip("클릭하면 오른쪽에 오늘 판매된 상품 목록을 표시합니다")
        self.today_sales_amount_label.clicked.connect(self._show_today_all_sales_detail)
        self.status_label = QLabel("준비 완료")
        self.table = QTableWidget(0, 10)
        self.detail_title_label = QLabel("선택 상품 판매 로그")
        self.detail_meta_label = QLabel("상품을 클릭하면 오늘 판매 이력이 표시됩니다.")
        self.detail_table = QTableWidget(0, 3)
        self._today_detail_date: str | None = None
        self._today_sales_events_by_exact: Dict[str, List[dict]] = {}
        self._today_sales_events_by_product: Dict[str, List[dict]] = {}
        self._defer_detail_fetch_until_focus = True

        self.image_downloaded.connect(self._on_image_downloaded)
        self._build_ui()
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self._set_busy(False)

    def load_initial_rows_if_needed(self) -> None:
        if self._initial_rows_loaded:
            return
        self._initial_rows_loaded = True
        self._load_initial_rows()

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
        self.search_input.setFixedWidth(220)
        self.search_input.textChanged.connect(self._schedule_filter_refresh)
        self.today_sales_amount_label.setStyleSheet("color: #065f46; font-weight: 600;")

        top_bar.addWidget(self.sync_button)
        top_bar.addWidget(QLabel("필터"))
        top_bar.addWidget(self.favorite_filter)
        top_bar.addWidget(QLabel("검색"))
        top_bar.addWidget(self.search_input, 0)
        top_bar.addSpacing(8)
        top_bar.addWidget(self.today_sales_amount_label)
        top_bar.addStretch(1)

        self.table.setHorizontalHeaderLabels(
            [
                "★",
                "연번",
                "상품이미지",
                "상품명",
                "재고",
                "오늘판매",
                "30일",
                "품절예상",
                "예상월매출",
                "판매가",
            ]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.table.setSortingEnabled(False)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._open_table_context_menu)
        self.table.itemSelectionChanged.connect(self._on_table_selection_changed)
        self.table.itemClicked.connect(self._on_table_item_clicked)

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
        self.table.setColumnWidth(4, 68)
        self.table.setColumnWidth(5, 76)
        self.table.setColumnWidth(6, 66)
        self.table.setColumnWidth(7, 84)
        self.table.setColumnWidth(8, 106)
        self.table.setColumnWidth(9, 86)

        self.detail_title_label.setStyleSheet("color: #0f172a; font-weight: 700;")
        self.detail_meta_label.setStyleSheet("color: #64748b; font-size: 11px;")
        self.detail_meta_label.setWordWrap(True)

        self.detail_table.setHorizontalHeaderLabels(["시간", "수량", "매출"])
        self.detail_table.verticalHeader().setVisible(False)
        self.detail_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.detail_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.detail_table.setSelectionMode(QTableWidget.SingleSelection)
        self.detail_table.setAlternatingRowColors(True)
        self.detail_table.setSortingEnabled(False)
        detail_header = self.detail_table.horizontalHeader()
        detail_header.setStretchLastSection(False)
        detail_header.setSectionResizeMode(0, QHeaderView.Fixed)
        detail_header.setSectionResizeMode(1, QHeaderView.Fixed)
        detail_header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.detail_table.setColumnWidth(0, 80)
        self.detail_table.setColumnWidth(1, 56)

        detail_panel = QWidget()
        detail_panel.setObjectName("channelDetailPanel")
        detail_panel.setFixedWidth(340)
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(10, 10, 10, 10)
        detail_layout.setSpacing(8)
        detail_layout.addWidget(self.detail_title_label)
        detail_layout.addWidget(self.detail_meta_label)
        detail_layout.addWidget(self.detail_table, 1)

        self.status_label.setStyleSheet("color: #475569;")

        root_layout.addLayout(top_bar)
        content_layout = QHBoxLayout()
        content_layout.setSpacing(8)
        content_layout.addWidget(self.table, 1)
        content_layout.addWidget(detail_panel, 0)
        root_layout.addLayout(content_layout, 1)
        root_layout.addWidget(self.status_label)

        self.search_input.clearFocus()
        self.sync_button.setFocus(Qt.OtherFocusReason)

        self._apply_styles()

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            _with_base_widget_font(
                """
            QWidget {
                __BASE_WIDGET_FONT_CSS__
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
                selection-background-color: rgba(59, 130, 246, 0.18);
                selection-color: #111827;
            }
            QTableWidget::item:selected {
                background: rgba(59, 130, 246, 0.18);
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
            #channelDetailPanel {
                background: #ffffff;
                border: 1px solid #d8dee4;
                border-radius: 8px;
            }
            """
            )
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
        self._update_today_sales_amount_label()
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
        self._fetch_today_sales_events(force=True)
        self._update_today_sales_amount_label()
        self._on_table_selection_changed()

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

    def _update_today_sales_amount_label(self) -> None:
        total_amount = 0
        for row in self.rows:
            qty = int(self._effective_today_sales(row) or 0)
            price = int(row.price or 0)
            if qty <= 0 or price <= 0:
                continue
            total_amount += qty * price
        self.today_sales_amount_label.setText(f"오늘 총 판매금액: {total_amount:,}원")

    @staticmethod
    def _monitor_item_key(product_id: str, item_id: str | None) -> str:
        return f"{product_id}|{item_id or ''}"

    def _fetch_today_sales_events(self, force: bool = False) -> None:
        if not self.monitor_url:
            self._today_sales_events_by_exact = {}
            self._today_sales_events_by_product = {}
            self._today_detail_date = None
            return

        today = QDate.currentDate().toString("yyyy-MM-dd")
        if not force and self._today_detail_date == today and self._today_sales_events_by_exact:
            return

        self._today_sales_events_by_exact = {}
        self._today_sales_events_by_product = {}
        self._today_detail_date = today

        try:
            response = httpx.get(
                f"{self.monitor_url.rstrip('/')}/sales",
                params={"date": today},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return

        sales = payload.get("sales", []) if isinstance(payload, dict) else []
        if not isinstance(sales, list):
            return

        for event in sales:
            if not isinstance(event, dict):
                continue
            if str(event.get("channel") or "").strip().lower() != self.channel_code:
                continue
            product_id = str(event.get("product_id") or "").strip()
            if not product_id:
                continue
            item_id = str(event.get("item_id")) if event.get("item_id") else None
            exact_key = self._monitor_item_key(product_id, item_id)
            self._today_sales_events_by_exact.setdefault(exact_key, []).append(event)
            self._today_sales_events_by_product.setdefault(product_id, []).append(event)

        for rows in self._today_sales_events_by_exact.values():
            rows.sort(key=lambda r: str(r.get("recorded_at") or ""), reverse=True)
        for rows in self._today_sales_events_by_product.values():
            rows.sort(key=lambda r: str(r.get("recorded_at") or ""), reverse=True)

    def _set_detail_table_mode(self, mode: str) -> None:
        """상세 테이블 헤더/컬럼 너비를 모드에 맞게 전환.

        mode:
          - 'row_log' : 선택 상품 판매 로그 (시간/수량/매출)
          - 'today_all': 오늘 판매된 상품 집계 (상품명/수량/매출)
        """
        header = self.detail_table.horizontalHeader()
        if mode == "today_all":
            self.detail_table.setHorizontalHeaderLabels(["상품명", "수량", "매출"])
            header.setSectionResizeMode(0, QHeaderView.Stretch)
            header.setSectionResizeMode(1, QHeaderView.Fixed)
            header.setSectionResizeMode(2, QHeaderView.Fixed)
            self.detail_table.setColumnWidth(1, 56)
            self.detail_table.setColumnWidth(2, 100)
        else:  # row_log
            self.detail_table.setHorizontalHeaderLabels(["시간", "수량", "매출"])
            header.setSectionResizeMode(0, QHeaderView.Fixed)
            header.setSectionResizeMode(1, QHeaderView.Fixed)
            header.setSectionResizeMode(2, QHeaderView.Stretch)
            self.detail_table.setColumnWidth(0, 80)
            self.detail_table.setColumnWidth(1, 56)

    def _show_today_all_sales_detail(self) -> None:
        """상단 '오늘 총 판매금액' 라벨 클릭 시 오늘 판매된 상품 목록을 우측 상세 패널에 표시."""
        from collections import defaultdict as _dd

        self._fetch_today_sales_events()
        self._set_detail_table_mode("today_all")
        self.detail_title_label.setText(f"{self.channel_name} 오늘 판매 집계")

        # 모든 events 모으기 (exact 키 기준, item_id 구분 유지)
        all_events: List[dict] = []
        for events_list in self._today_sales_events_by_exact.values():
            all_events.extend(events_list)

        if not all_events:
            self.detail_meta_label.setText("오늘 판매된 상품이 없습니다.")
            self.detail_table.setRowCount(0)
            return

        # 상품명 룩업 (현재 rows 기준)
        name_map: Dict[str, str] = {}
        for row in self.rows:
            pid = str(row.product_id or "").strip()
            iid = row.item_id if row.item_id else None
            name_map[self._monitor_item_key(pid, iid)] = self._display_name(row)

        per_item: Dict[str, Dict[str, Any]] = _dd(
            lambda: {"qty": 0, "amount": 0, "name": "", "count": 0}
        )
        for event in all_events:
            pid = str(event.get("product_id") or "").strip()
            iid = str(event.get("item_id")) if event.get("item_id") else None
            key = self._monitor_item_key(pid, iid)

            qty = int(event.get("qty_sold") or 0)
            try:
                price = int(event.get("price") or 0)
            except (TypeError, ValueError):
                price = 0
            rec = per_item[key]
            rec["qty"] += qty
            rec["amount"] += qty * price
            rec["count"] += 1
            if not rec["name"]:
                rec["name"] = (
                    name_map.get(key)
                    or str(event.get("name") or "").strip()
                    or pid
                )

        sorted_items = sorted(
            per_item.values(),
            key=lambda r: (r["amount"], r["qty"]),
            reverse=True,
        )

        if not sorted_items:
            self.detail_meta_label.setText("오늘 판매된 상품이 없습니다.")
            self.detail_table.setRowCount(0)
            return

        total_qty = sum(r["qty"] for r in sorted_items)
        total_amount = sum(r["amount"] for r in sorted_items)
        self.detail_meta_label.setText(
            f"{len(sorted_items)}개 상품 · 총 {total_qty:,}개 · ₩{total_amount:,}"
        )

        self.detail_table.setRowCount(len(sorted_items))
        for i, r in enumerate(sorted_items):
            name_item = QTableWidgetItem(r["name"])
            name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
            name_item.setToolTip(r["name"])
            self.detail_table.setItem(i, 0, name_item)

            qty_item = QTableWidgetItem(f"{r['qty']:,}")
            qty_item.setFlags(qty_item.flags() & ~Qt.ItemIsEditable)
            qty_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.detail_table.setItem(i, 1, qty_item)

            amt_item = QTableWidgetItem(f"{r['amount']:,}원")
            amt_item.setFlags(amt_item.flags() & ~Qt.ItemIsEditable)
            amt_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.detail_table.setItem(i, 2, amt_item)

    def _update_detail_for_row(self, row: ChannelProduct | None) -> None:
        # 상품 단위 상세 모드로 헤더 복구
        self._set_detail_table_mode("row_log")
        if row is None:
            self.detail_title_label.setText("선택 상품 판매 로그")
            self.detail_meta_label.setText("상품을 클릭하면 오늘 판매 이력이 표시됩니다.")
            self.detail_table.setRowCount(0)
            return

        self._fetch_today_sales_events()

        product_id = str(row.product_id or "").strip()
        item_id = str(row.item_id) if row.item_id else None
        exact_key = self._monitor_item_key(product_id, item_id)

        events = list(self._today_sales_events_by_exact.get(exact_key, []))
        if not events and product_id:
            events = list(self._today_sales_events_by_product.get(product_id, []))

        self.detail_title_label.setText(f"{self._display_name(row)} 판매 로그")
        if not events:
            self.detail_meta_label.setText("오늘 판매 로그가 없습니다.")
            self.detail_table.setRowCount(0)
            return

        total_qty = sum(int(event.get("qty_sold") or 0) for event in events)
        total_amount = 0
        for event in events:
            qty = int(event.get("qty_sold") or 0)
            price = event.get("price")
            if price is not None:
                try:
                    total_amount += qty * int(price)
                except (TypeError, ValueError):
                    continue

        self.detail_meta_label.setText(
            f"오늘 {len(events)}건 · 총 {total_qty:,}개 · ₩{total_amount:,}"
        )

        self.detail_table.setRowCount(len(events))
        for index, event in enumerate(events):
            recorded = str(event.get("recorded_at") or "")
            time_text = recorded[11:16] if len(recorded) >= 16 else "-"
            qty = int(event.get("qty_sold") or 0)
            price_value = event.get("price")
            amount_text = "-"
            if price_value is not None:
                try:
                    amount_text = f"{qty * int(price_value):,}원"
                except (TypeError, ValueError):
                    amount_text = "-"

            self.detail_table.setItem(
                index,
                0,
                self._table_item(time_text, Qt.AlignCenter | Qt.AlignVCenter),
            )
            self.detail_table.setItem(
                index,
                1,
                self._table_item(f"{qty:,}", Qt.AlignRight | Qt.AlignVCenter),
            )
            self.detail_table.setItem(
                index,
                2,
                self._table_item(amount_text, Qt.AlignRight | Qt.AlignVCenter),
            )

    @Slot()
    def _on_table_selection_changed(self) -> None:
        if self._defer_detail_fetch_until_focus and not self.table.hasFocus():
            self._update_detail_for_row(None)
            return
        self._defer_detail_fetch_until_focus = False
        current = self.table.currentRow()
        if 0 <= current < len(self.filtered_rows):
            self._update_detail_for_row(self.filtered_rows[current])
            return
        self._update_detail_for_row(None)

    @Slot(QTableWidgetItem)
    def _on_table_item_clicked(self, _item: QTableWidgetItem) -> None:
        if self._defer_detail_fetch_until_focus:
            self._defer_detail_fetch_until_focus = False
        self._on_table_selection_changed()

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
                    product_url=self._row_product_url(row),
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

    def _row_product_url(self, row: ChannelProduct) -> str | None:
        # services 레이어가 /main/ URL 을 이미 정상 slug 로 교정해서 넘겨줌.
        direct = _normalize_web_url(row.product_url)
        if direct:
            return direct
        return _normalize_web_url(_build_search_url(self.channel_name, self._display_name(row)))

    def _effective_sales(self, row: ChannelProduct) -> int | None:
        if row.sales is None:
            return None
        return max(0, int(row.sales))

    def _effective_today_sales(self, row: ChannelProduct) -> int | None:
        if row.today_sales is None:
            return None
        return max(0, int(row.today_sales))

    def _predicted_monthly_revenue(self, row: ChannelProduct) -> int | None:
        sales = self._effective_sales(row)
        if sales is None or row.price is None:
            return None
        if not self.sales_period_days:
            return None

        period = max(1, int(self.sales_period_days))
        price = max(0, int(row.price))
        estimated_qty = sales * (30.0 / float(period))
        return int(round(estimated_qty * price))

    def _stockout_days(self, row: ChannelProduct) -> int | None:
        sales = self._effective_sales(row)
        if row.stock is None or sales is None or not self.sales_period_days:
            return None
        stock = max(0, int(row.stock))
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
        container = ClickableImageContainer()
        container.setFixedWidth(58)

        layout = QHBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(0)

        label = self._image_label()
        product_url = self._row_product_url(row)
        label.set_product_url(product_url)
        label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        container.set_product_url(product_url)
        container.clicked.connect(self._open_product_page)
        layout.addWidget(label, 0, Qt.AlignCenter)
        return container, label

    def _stock_cover_meta(self, row: ChannelProduct) -> tuple[int, str, str]:
        period_days = self.sales_period_days
        if period_days is None:
            return 0, "#94a3b8", "판매 기준일 정보 없음"

        stock = row.stock
        sales = self._effective_sales(row)
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
        layout.setSpacing(1)

        if not self.is_row_linked(row):
            unlinked_badge_row = QWidget()
            unlinked_layout = QHBoxLayout(unlinked_badge_row)
            unlinked_layout.setContentsMargins(0, 0, 0, 0)
            unlinked_layout.setSpacing(3)
            unlinked_badge = QLabel("● 미연결")
            unlinked_badge.setToolTip(
                "이 상품은 아직 마스터 상품에 연결되지 않았습니다.\n"
                "우클릭 → ‘마스터에 연결…’ 로 연결하세요."
            )
            unlinked_badge.setStyleSheet(
                """
                color: #ffffff;
                background: #dc2626;
                font-size: 8px;
                font-weight: 700;
                padding: 0px 5px;
                border-radius: 5px;
                """
            )
            unlinked_layout.addWidget(unlinked_badge, 0, Qt.AlignLeft)
            unlinked_layout.addStretch(1)
            layout.addWidget(unlinked_badge_row)

        name_label = EditableNameLabel(display_name)
        name_color = "#b91c1c" if is_zero_stock else "#0f172a"
        if is_customized:
            name_label.setToolTip(
                f"원래 상품명: {original_name}\n더블클릭: 표시 상품명 수정"
            )
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

    def _selected_filtered_rows(self) -> list[ChannelProduct]:
        selected: list[ChannelProduct] = []
        indexes = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        seen_rows: set[int] = set()
        for idx in indexes:
            row_index = idx.row()
            if row_index in seen_rows:
                continue
            seen_rows.add(row_index)
            if 0 <= row_index < len(self.filtered_rows):
                selected.append(self.filtered_rows[row_index])
        if selected:
            return selected

        current = self.table.currentRow()
        if 0 <= current < len(self.filtered_rows):
            return [self.filtered_rows[current]]
        return []

    # ------------------------------------------------------------------
    # Master product linkage
    # ------------------------------------------------------------------

    def _reload_master_links(self) -> None:
        try:
            all_links = self.cache.load_all_links()
        except Exception:  # noqa: BLE001
            all_links = {}
        channel_code = self.channel_code
        self.master_link_map = {
            product_key: link.master_id
            for (channel, product_key), link in all_links.items()
            if channel == channel_code
        }

    def is_row_linked(self, row: ChannelProduct) -> bool:
        return product_identity_key(row) in self.master_link_map

    def _linked_master_id(self, row: ChannelProduct) -> int | None:
        return self.master_link_map.get(product_identity_key(row))

    def _open_link_to_master_dialog(self, rows: List[ChannelProduct]) -> None:
        if not rows:
            return

        masters = self.master_service.list_masters()
        CREATE_LABEL = "+ 새 마스터 만들기"
        options: List[str] = [CREATE_LABEL]
        option_to_master: Dict[str, int] = {}
        for master in masters:
            tag = f"#{master.id} · {master.name}"
            if master.unit_cost is not None:
                tag += f" ({master.unit_cost:,}원)"
            options.append(tag)
            option_to_master[tag] = master.id

        pick_title = (
            "연결할 마스터 선택"
            if len(rows) == 1
            else f"{len(rows)}개 상품을 연결할 마스터 선택"
        )
        pick = _pick_master_with_search(
            self,
            pick_title,
            "마스터 상품을 선택하거나 새로 만들어주세요.",
            options,
            create_label=CREATE_LABEL,
        )
        if pick is None:
            return

        if pick == CREATE_LABEL:
            name, ok = QInputDialog.getText(self, "새 마스터", "마스터 이름:")
            if not ok:
                return
            name = str(name or "").strip()
            if not name:
                QMessageBox.warning(self, "이름 필요", "이름을 입력해주세요.")
                return
            try:
                new_master = self.master_service.create_master(name=name)
            except ValueError as exc:
                QMessageBox.warning(self, "실패", str(exc))
                return
            except MasterRemoteError as exc:
                QMessageBox.critical(self, "파이 서버 오류", f"마스터 생성 실패: {exc}")
                return
            master_id = new_master.id
            master_label = f"#{new_master.id} · {new_master.name}"
        else:
            master_id = option_to_master[pick]
            master_label = pick

        # 배수는 기본 1 로 자동 연결. 수정 필요하면 상품등록 탭 상세 팝업에서 변경.
        # 기존 링크가 있으면 해당 multiplier 유지.
        linked_count = 0
        for row in rows:
            existing_link = self.master_service.get_link(
                self.channel_code, product_identity_key(row)
            )
            multiplier = existing_link.multiplier if existing_link else 1
            try:
                self.master_service.link(
                    self.channel_code,
                    product_identity_key(row),
                    master_id,
                    multiplier=int(multiplier),
                )
            except MasterRemoteError as exc:
                QMessageBox.critical(self, "파이 서버 오류", f"연결 실패: {exc}")
                break
            linked_count += 1

        if linked_count == 0:
            return

        self._reload_master_links()
        self._force_full_render_next = True
        self._apply_filters()
        self.status_label.setText(
            f"{self.channel_name} → 마스터 연결: {linked_count}건"
        )
        self.masters_changed.emit(self.channel_name)

    def _unlink_selected_from_master(self, rows: List[ChannelProduct]) -> None:
        if not rows:
            return
        unlinked = 0
        for row in rows:
            if not self.is_row_linked(row):
                continue
            try:
                self.master_service.unlink(
                    self.channel_code, product_identity_key(row)
                )
            except MasterRemoteError as exc:
                QMessageBox.critical(self, "파이 서버 오류", f"연결 해제 실패: {exc}")
                break
            unlinked += 1
        if unlinked == 0:
            return
        self._reload_master_links()
        self._force_full_render_next = True
        self._apply_filters()
        self.status_label.setText(
            f"{self.channel_name} → 마스터 연결 해제: {unlinked}건"
        )
        self.masters_changed.emit(self.channel_name)

    def refresh_master_links_from_external(self) -> None:
        """상품등록 탭 등에서 링크가 바뀐 경우 이 탭의 배지 갱신."""
        self._reload_master_links()
        self._force_full_render_next = True
        self._apply_filters()

    @Slot(object)
    def _open_table_context_menu(self, pos: object) -> None:
        if not hasattr(pos, "x") or not hasattr(pos, "y"):
            return
        index = self.table.indexAt(pos)
        if index.isValid():
            row_index = int(index.row())
            selected_indexes = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
            selected_rows = {idx.row() for idx in selected_indexes}
            if row_index not in selected_rows:
                self.table.clearSelection()
                self.table.selectRow(row_index)
                self.table.setCurrentCell(row_index, 1)

        selected = self._selected_filtered_rows()
        if not selected:
            return

        menu = QMenu(self)
        label = f"선택 {len(selected)}개" if len(selected) > 1 else "선택 상품"

        link_action = menu.addAction(f"{label} 마스터에 연결…")
        unlink_action = None
        if any(self.is_row_linked(row) for row in selected):
            unlink_action = menu.addAction(f"{label} 마스터 연결 해제")

        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen == link_action:
            self._open_link_to_master_dialog(selected)
        elif unlink_action and chosen == unlink_action:
            self._unlink_selected_from_master(selected)

    def _open_product_page(self, url: str) -> None:
        normalized = _normalize_web_url(url)
        if not normalized:
            QMessageBox.information(self, "상품 링크", "유효한 상품 링크가 없습니다.")
            return
        QDesktopServices.openUrl(QUrl(normalized))

    def _schedule_filter_refresh(self, *_args: object) -> None:
        self._filter_timer.start()

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
            return self._sort_nullable_numeric(rows, self._effective_today_sales, descending)
        if col == 6:
            return self._sort_nullable_numeric(rows, self._effective_sales, descending)
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

    def _selected_row_identity_key(self) -> tuple[str, str] | None:
        current = self.table.currentRow()
        if 0 <= current < len(self.filtered_rows):
            return self._row_identity_key(self.filtered_rows[current])
        return None

    def _render_row(self, index: int, row: ChannelProduct, token: int) -> None:
        today_sales = self._effective_today_sales(row)
        period_sales = self._effective_sales(row)

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
                self._fmt_int(today_sales),
                Qt.AlignRight | Qt.AlignVCenter,
                sort_value=self._sortable_none_last(today_sales),
            ),
        )

        self.table.setItem(
            index,
            6,
            self._table_item(
                self._fmt_int(period_sales),
                Qt.AlignRight | Qt.AlignVCenter,
                sort_value=self._sortable_none_last(period_sales),
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

        # 품절 행 전체 빨간 배경
        is_soldout = row.stock is not None and int(row.stock) == 0
        bg_color = QColor("#fee2e2") if is_soldout else QColor(0, 0, 0, 0)
        # setItem 기반 컬럼 (4~9)
        for col in range(4, 10):
            item = self.table.item(index, col)
            if item:
                item.setBackground(bg_color)
        # setCellWidget 기반 컬럼 (0~3): QPalette로 배경색 지정
        for col in range(0, 4):
            widget = self.table.cellWidget(index, col)
            if widget:
                if is_soldout:
                    p = widget.palette()
                    p.setColor(widget.backgroundRole(), QColor("#fee2e2"))
                    widget.setPalette(p)
                    widget.setAutoFillBackground(True)
                else:
                    widget.setAutoFillBackground(False)
                    widget.setPalette(self.table.palette())

    def _render_table(self, rows: List[ChannelProduct]) -> None:
        selected_key = self._selected_row_identity_key()
        self.render_token += 1
        token = self.render_token

        self._image_waiters.clear()
        self.table.setUpdatesEnabled(False)
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(len(rows))
            for index, row in enumerate(rows):
                self._render_row(index, row, token)
        finally:
            self.table.blockSignals(False)
            self.table.setUpdatesEnabled(True)

        target_index = -1
        if selected_key is not None:
            for index, row in enumerate(rows):
                if self._row_identity_key(row) == selected_key:
                    target_index = index
                    break
        if target_index < 0 and rows:
            target_index = 0

        if target_index >= 0:
            self.table.setCurrentCell(target_index, 1)
        else:
            self._update_detail_for_row(None)

    def _patch_table_rows(self, rows: List[ChannelProduct], changed_indexes: list[int]) -> None:
        token = self.render_token
        self.table.setUpdatesEnabled(False)
        self.table.blockSignals(True)
        try:
            for index in changed_indexes:
                if 0 <= index < len(rows):
                    self._render_row(index, rows[index], token)
        finally:
            self.table.blockSignals(False)
            self.table.setUpdatesEnabled(True)

        current = self.table.currentRow()
        if 0 <= current < len(rows):
            self._update_detail_for_row(rows[current])


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
        # 디스크 캐시 우선 → 앱을 재시작해도 재다운로드 하지 않음.
        from inventory_app.services.image_cache import get_image_bytes
        return get_image_bytes(url, timeout=15)

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
        self._filter_timer.stop()
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
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(SEARCH_DEBOUNCE_MS)
        self._filter_timer.timeout.connect(self._apply_filters)

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
        self.search_input.textChanged.connect(self._schedule_filter_refresh)

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
            _with_base_widget_font(
                """
            QWidget {
                __BASE_WIDGET_FONT_CSS__
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
                selection-background-color: rgba(59, 130, 246, 0.18);
                selection-color: #111827;
            }
            QTableWidget::item:selected {
                background: rgba(59, 130, 246, 0.18);
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

    def _schedule_filter_refresh(self, *_args: object) -> None:
        self._filter_timer.start()

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
        container = ClickableImageContainer()
        container.setFixedWidth(62)
        layout = QHBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(0)
        label = ProductImageLabel()
        label.set_product_url(row.product_url)
        label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        container.set_product_url(row.product_url)
        container.clicked.connect(self._open_product_page)
        layout.addWidget(label, 0, Qt.AlignCenter)
        return container, label

    def _render_rows(self, rows: List[FavoriteInventoryRow]) -> None:
        self.render_token += 1
        token = self.render_token
        self._image_waiters.clear()

        self.table.setSortingEnabled(False)
        self.table.setUpdatesEnabled(False)
        self.table.blockSignals(True)
        try:
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
        finally:
            self.table.blockSignals(False)
            self.table.setUpdatesEnabled(True)
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
        normalized = _normalize_web_url(url)
        if not normalized:
            QMessageBox.information(self, "상품 링크", "유효한 상품 링크가 없습니다.")
            return
        QDesktopServices.openUrl(QUrl(normalized))

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
        normalized = _normalize_web_url(url)
        if not normalized:
            QMessageBox.information(self, "상품 링크", "유효한 상품 링크가 없습니다.")
            return
        QDesktopServices.openUrl(QUrl(normalized))

    def shutdown(self) -> None:
        self._filter_timer.stop()
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
            _with_base_widget_font(
                """
            QWidget {
                __BASE_WIDGET_FONT_CSS__
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
                selection-background-color: rgba(59, 130, 246, 0.18);
                selection-color: #111827;
            }
            QTableWidget::item:selected {
                background: rgba(59, 130, 246, 0.18);
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
            _with_base_widget_font(
                """
            QWidget {
                __BASE_WIDGET_FONT_CSS__
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
                selection-background-color: rgba(59, 130, 246, 0.18);
                selection-color: #14532d;
            }
            QTableWidget::item:selected {
                background: rgba(59, 130, 246, 0.18);
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


class SalesDailyTab(QWidget):
    """판매일보 탭 — 캘린더에서 날짜 클릭 시 해당 일자 판매 내역을 마스터 단위로 집계.

    마스터 기준 행:
    - 채널 상품의 qty_sold × link.multiplier 를 합산 (마스터 단위로 환산)
    - 채널별 (네이버/쿠팡) 소계 + 추정매출 표시
    - 총 판매수량 내림차순 정렬 ("재고 빠진 순")
    - 마스터에 연결되지 않은 이벤트는 하단 "미연결" 섹션에 따로
    """

    image_downloaded = Signal(str, object)

    def __init__(
        self,
        monitor_url: str | None = None,
        timeout: int = 30,
        cache: ChannelProductCache | None = None,
    ) -> None:
        super().__init__()
        self.monitor_url = monitor_url
        self.timeout = timeout
        self.cache = cache or ChannelProductCache()
        # 상품등록 탭과 동일한 write-through 서비스 사용 — 매 조회 시 Pi 에서
        # 마스터/링크 최신 스냅샷을 fetch 해 multiplier·새 마스터를 즉시 반영.
        self.master_service = build_master_service(
            cache=self.cache, monitor_url=monitor_url
        )
        self._master_remote_warning: str = ""
        self._loaded_once = False
        self.image_cache: dict[str, QPixmap] = {}
        self._image_pending: set[str] = set()
        self._image_waiters: dict[str, list[tuple[QLabel, int]]] = {}
        self.render_token = 0
        self.image_executor = ThreadPoolExecutor(max_workers=6)
        self.image_downloaded.connect(self._on_image_downloaded)

        self._sales_dates: dict[str, int] = {}
        self._highlighted_dates: set[str] = set()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)

        # 왼쪽: 캘린더 + 요약
        left = QVBoxLayout()
        self.calendar = QCalendarWidget()
        self.calendar.setGridVisible(True)
        self.calendar.setFixedWidth(320)
        self.calendar.setFixedHeight(260)
        self.calendar.clicked.connect(self._on_date_selected)
        left.addWidget(self.calendar)

        self.summary_label = QLabel("날짜를 선택하세요")
        self.summary_label.setWordWrap(True)
        left.addWidget(self.summary_label)
        left.addStretch(1)
        layout.addLayout(left, 0)

        # 오른쪽: 마스터 단위 판매 집계 테이블
        right = QVBoxLayout()
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            [
                "이미지",
                "마스터 상품",
                "총판매",
                "네이버",
                "쿠팡",
                "추정매출",
                "비고",
            ]
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for c, w in [(0, 56), (2, 84), (3, 80), (4, 80), (5, 110), (6, 120)]:
            self.table.setColumnWidth(c, w)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setSortingEnabled(False)  # 이미 정렬해 넣음 (재고 빠진 순)
        right.addWidget(self.table, 1)
        layout.addLayout(right, 1)

        self._apply_styles()
        self.summary_label.setText("판매일보 탭을 열면 데이터를 불러옵니다.")

    def _apply_styles(self) -> None:
        self.summary_label.setStyleSheet(
            _with_base_widget_font(
                """
                QLabel {
                    __BASE_WIDGET_FONT_CSS__
                    padding: 8px;
                    background: #f8fafc;
                    border: 1px solid #e2e8f0;
                    border-radius: 8px;
                }
                """
            )
        )
        self.table.setStyleSheet(
            _with_base_widget_font(
                """
                QTableWidget {
                    __BASE_WIDGET_FONT_CSS__
                    gridline-color: #e8ecf0;
                    selection-background-color: rgba(59, 130, 246, 0.18);
                }
                QHeaderView::section {
                    background: #f1f5f9;
                    border: none;
                    border-bottom: 2px solid #d0d7de;
                    padding: 6px;
                    font-weight: 700;
                }
                """
            )
        )

    @staticmethod
    def _qdate_from_iso(date_str: str) -> QDate | None:
        try:
            y, m, d = [int(x) for x in date_str.split("-")]
            qdate = QDate(y, m, d)
            if qdate.isValid():
                return qdate
        except Exception:
            return None
        return None

    def reload(self, preserve_selection: bool = True) -> None:
        self._loaded_once = True
        self._load_sales_dates(preserve_selection=preserve_selection)

    def _load_sales_dates(self, preserve_selection: bool = True) -> None:
        if not self.monitor_url:
            self.table.setRowCount(0)
            self.summary_label.setText("라즈베리파이 미연결")
            return

        selected = self.calendar.selectedDate() if preserve_selection else None
        try:
            resp = httpx.get(
                f"{self.monitor_url.rstrip('/')}/sales/dates",
                timeout=self.timeout,
            )
            resp.raise_for_status()
            raw_dates = resp.json().get("dates", {})
            if isinstance(raw_dates, dict):
                cleaned: dict[str, int] = {}
                for key, count in raw_dates.items():
                    date_key = str(key)
                    if self._qdate_from_iso(date_key) is None:
                        continue
                    try:
                        cleaned[date_key] = int(count)
                    except Exception:
                        cleaned[date_key] = 0
                self._sales_dates = cleaned
            else:
                self._sales_dates = {}
            self._highlight_calendar()
        except Exception as e:
            self._sales_dates = {}
            self.table.setRowCount(0)
            self.summary_label.setText(f"판매일보 연결 실패: {e}")
            return

        target_date: QDate | None = None
        if (
            selected
            and selected.isValid()
            and selected.toString("yyyy-MM-dd") in self._sales_dates
        ):
            target_date = selected
        elif self._sales_dates:
            latest_date = max(self._sales_dates.keys())
            target_date = self._qdate_from_iso(latest_date)
        elif selected and selected.isValid():
            target_date = selected
        else:
            target_date = QDate.currentDate()

        if target_date and target_date.isValid():
            self.calendar.setSelectedDate(target_date)
            self._on_date_selected(target_date)
        else:
            self.table.setRowCount(0)
            self.summary_label.setText("판매 데이터가 없습니다.")

    def _highlight_calendar(self) -> None:
        """판매 이벤트가 있는 날짜에 마커 표시."""
        empty_fmt = QTextCharFormat()
        for date_str in self._highlighted_dates:
            qd = self._qdate_from_iso(date_str)
            if qd is not None:
                self.calendar.setDateTextFormat(qd, empty_fmt)

        fmt = QTextCharFormat()
        fmt.setBackground(QColor("#dbeafe"))
        fmt.setForeground(QColor("#1e40af"))
        highlighted: set[str] = set()
        for date_str in self._sales_dates:
            qd = self._qdate_from_iso(date_str)
            if qd is None:
                continue
            self.calendar.setDateTextFormat(qd, fmt)
            highlighted.add(date_str)
        self._highlighted_dates = highlighted

    def _sale_product_url(self, sale_row: dict) -> str | None:
        direct = _normalize_web_url(sale_row.get("product_url"))
        # 잘못된 /main/ URL 필터
        if direct and "smartstore.naver.com/main/" not in direct:
            return direct

        channel = str(sale_row.get("channel") or "").strip().lower()
        name = str(sale_row.get("name") or "").strip()
        return _normalize_web_url(_build_search_url(channel, name))

    def _open_sale_product_page(self, url: str) -> None:
        normalized = _normalize_web_url(url)
        if not normalized:
            QMessageBox.information(self, "상품 링크", "유효한 상품 링크가 없습니다.")
            return
        QDesktopServices.openUrl(QUrl(normalized))

    def _on_date_selected(self, qdate: QDate) -> None:
        date_str = qdate.toString("yyyy-MM-dd")
        self.render_token += 1
        self.table.setRowCount(0)
        self.summary_label.setText(f"{date_str} 조회 중...")

        if not self.monitor_url:
            self.summary_label.setText("라즈베리파이 미연결")
            return

        try:
            resp = httpx.get(
                f"{self.monitor_url.rstrip('/')}/sales?date={date_str}",
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            self.summary_label.setText(f"조회 실패: {e}")
            return

        sales = data.get("sales", [])

        # 마스터/링크 로드 — 상품등록 탭과 동일하게 Pi 에서 최신 스냅샷 fetch.
        # 실패 시 로컬 캐시 사용 + 요약 영역에 경고.
        self._master_remote_warning = ""
        if self.master_service.has_remote():
            try:
                self.master_service.refresh_from_remote()
            except MasterRemoteError as exc:
                self._master_remote_warning = (
                    f"Pi 마스터 동기화 실패 (로컬 캐시 사용): {exc}"
                )
        try:
            links = self.cache.load_all_links()
            masters_by_id = {m.id: m for m in self.cache.list_masters()}
        except Exception:  # noqa: BLE001
            links = {}
            masters_by_id = {}

        # 마스터별 집계 — 미연결 상품은 무시 (마스터 단위 판매일보)
        agg: dict[int, dict] = {}
        skipped_unlinked = 0
        for event in sales:
            if not isinstance(event, dict):
                continue
            channel = str(event.get("channel") or "").strip().lower()
            pid = str(event.get("product_id") or "").strip()
            iid_raw = event.get("item_id")
            iid = str(iid_raw) if iid_raw not in (None, "") else ""
            product_key = f"id:{pid}|item:{iid}" if pid else ""
            qty = int(event.get("qty_sold") or 0)
            price = event.get("price")
            try:
                price_val = int(price) if price is not None else 0
            except (TypeError, ValueError):
                price_val = 0
            revenue = qty * price_val

            link = links.get((channel, product_key))
            if link is None or link.master_id not in masters_by_id:
                skipped_unlinked += 1
                continue
            multiplier = max(1, int(link.multiplier))
            master_qty_delta = qty * multiplier
            entry = agg.setdefault(
                link.master_id,
                {
                    "naver_qty": 0,
                    "coupang_qty": 0,
                    "revenue": 0,
                    "sample_image": None,
                    "sample_name": None,
                },
            )
            if channel == "naver":
                entry["naver_qty"] += master_qty_delta
            elif channel == "coupang":
                entry["coupang_qty"] += master_qty_delta
            entry["revenue"] += revenue
            if not entry["sample_image"]:
                entry["sample_image"] = event.get("image_url")
            if not entry["sample_name"]:
                entry["sample_name"] = event.get("name")

        # 정렬: 총 판매수량 내림차순
        rows: list[tuple[int, dict, int]] = []
        for master_id, data_row in agg.items():
            total = data_row["naver_qty"] + data_row["coupang_qty"]
            rows.append((master_id, data_row, total))
        rows.sort(key=lambda t: (-t[2], masters_by_id[t[0]].name))

        total_qty = sum(t[2] for t in rows)
        total_revenue = sum(t[1]["revenue"] for t in rows)

        lines = [f"<b>{date_str} 판매일보 (마스터 단위)</b>"]
        lines.append(
            f"마스터 {len(rows)}개 · 총 <b>{total_qty:,}건</b> / ₩{total_revenue:,.0f}"
        )
        if skipped_unlinked:
            lines.append(
                f"<span style='color:#94a3b8'>미연결 {skipped_unlinked}건 제외</span>"
            )
        if self._master_remote_warning:
            lines.append(
                f"<span style='color:#b45309'>⚠ {self._master_remote_warning}</span>"
            )
        self.summary_label.setText("<br>".join(lines))

        # 테이블 렌더 — 마스터 연결된 행만
        self.table.setRowCount(len(rows))
        token = self.render_token
        row_idx = 0
        for master_id, data_row, total in rows:
            self.table.setRowHeight(row_idx, 50)
            master = masters_by_id[master_id]
            # 이미지
            img_url = data_row["sample_image"]
            img_container = ClickableImageContainer()
            img_label = ProductImageLabel()
            img_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            img_layout = QHBoxLayout(img_container)
            img_layout.setContentsMargins(3, 3, 3, 3)
            img_layout.setSpacing(0)
            img_layout.addWidget(img_label, 0, Qt.AlignCenter)
            if img_url:
                self._request_image(img_label, img_url, token)
            self.table.setCellWidget(row_idx, 0, img_container)

            def _mk_num(text: str, sort_val) -> QTableWidgetItem:
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                item.setData(Qt.UserRole, sort_val)
                return item

            name_item = QTableWidgetItem(master.name)
            name_item.setData(Qt.UserRole, master.id)
            self.table.setItem(row_idx, 1, name_item)
            self.table.setItem(row_idx, 2, _mk_num(f"{total:,}", total))
            self.table.setItem(
                row_idx, 3,
                _mk_num(f"{data_row['naver_qty']:,}" if data_row['naver_qty'] else "-", data_row['naver_qty']),
            )
            self.table.setItem(
                row_idx, 4,
                _mk_num(f"{data_row['coupang_qty']:,}" if data_row['coupang_qty'] else "-", data_row['coupang_qty']),
            )
            self.table.setItem(
                row_idx, 5,
                _mk_num(f"₩{data_row['revenue']:,}" if data_row['revenue'] else "-", data_row['revenue']),
            )
            self.table.setItem(row_idx, 6, QTableWidgetItem(""))
            row_idx += 1

    def _request_image(self, label: QLabel, url: str, token: int) -> None:
        normalized = url.strip()
        if not normalized:
            return
        cached = self.image_cache.get(normalized)
        if cached:
            self._set_label_pixmap(label, cached)
            return
        self._image_waiters.setdefault(normalized, []).append((label, token))
        if normalized in self._image_pending:
            return
        self._image_pending.add(normalized)
        future = self.image_executor.submit(ChannelTab._download_image_bytes, normalized)
        future.add_done_callback(
            lambda f, u=normalized: self._emit_image_downloaded(u, f)
        )

    def _emit_image_downloaded(self, url: str, future: Future[bytes | None]) -> None:
        try:
            data = future.result()
        except Exception:
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

    @staticmethod
    def _set_label_pixmap(label: QLabel, pixmap: QPixmap) -> None:
        scaled = pixmap.scaled(42, 42, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        label.setPixmap(scaled)

    def shutdown(self) -> None:
        self.image_executor.shutdown(wait=False)


class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        self.settings = QSettings(APP_SETTINGS_ORG, APP_SETTINGS_APP)
        app = QApplication.instance()
        self._default_app_font = QFont(app.font()) if app is not None else QFont()
        self._restore_saved_app_font()

        self.naver_service = NaverChannelService(config)
        self.coupang_service = CoupangChannelService(config)
        self.revenue_service = RevenueComparisonService(config)
        self.keyword_service = NaverKeywordRevenueService(config)

        self.setWindowTitle("스마트스토어 / 쿠팡 분리 재고 대시보드")
        self.resize(1780, 900)

        self.sync_all_button = QPushButton("전체 동기화")
        self.sync_all_button.setObjectName("primarySyncButton")
        self.font_button = QPushButton("글꼴")
        self.font_button.setObjectName("fontMenuButton")
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
            monitor_url=config.monitor_url,
            timeout_seconds=config.timeout_seconds,
        )
        self.coupang_tab = ChannelTab(
            channel_name="쿠팡",
            sales_header="판매량(30일)",
            sales_period_days=30,
            fetch_fn=self.coupang_service.fetch,
            initial_fetch_fn=self.coupang_service.fetch_cached,
            monitor_url=config.monitor_url,
            timeout_seconds=config.timeout_seconds,
        )
        self.product_master_tab = ProductMasterTab(monitor_url=config.monitor_url)
        self.inventory_tab = InventoryManagementTab()
        self.sales_daily_tab = SalesDailyTab(
            monitor_url=config.monitor_url,
            timeout=config.timeout_seconds,
            cache=self.product_master_tab.cache,
        )
        self.revenue_tab = RevenueTab(
            fetch_fn=self.revenue_service.fetch,
            default_days=sales_days,
        )
        self.keyword_tab = KeywordRevenueTab(
            fetch_fn=self.keyword_service.fetch,
            default_days=sales_days,
        )
        self.fassto_tab = FasstoTab(config)

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

        self.tab_shortcut_6 = QShortcut(QKeySequence("6"), self)
        self.tab_shortcut_6.setContext(Qt.ApplicationShortcut)
        self.tab_shortcut_6.activated.connect(lambda: self._activate_tab_shortcut(5))

        self.tab_shortcut_7 = QShortcut(QKeySequence("7"), self)
        self.tab_shortcut_7.setContext(Qt.ApplicationShortcut)
        self.tab_shortcut_7.activated.connect(lambda: self._activate_tab_shortcut(6))

        self.tab_shortcut_8 = QShortcut(QKeySequence("8"), self)
        self.tab_shortcut_8.setContext(Qt.ApplicationShortcut)
        self.tab_shortcut_8.activated.connect(lambda: self._activate_tab_shortcut(7))

        self.naver_tab.sync_finished.connect(self._on_sub_sync_finished)
        self.coupang_tab.sync_finished.connect(self._on_sub_sync_finished)
        self.naver_tab.favorites_changed.connect(self._refresh_inventory_tab)
        self.coupang_tab.favorites_changed.connect(self._refresh_inventory_tab)
        self.naver_tab.sync_finished.connect(self._on_channel_sync_finished_for_masters)
        self.coupang_tab.sync_finished.connect(self._on_channel_sync_finished_for_masters)
        self.naver_tab.masters_changed.connect(self._on_masters_changed)
        self.coupang_tab.masters_changed.connect(self._on_masters_changed)
        self.product_master_tab.masters_changed.connect(self._on_masters_changed)
        self.revenue_tab.sync_finished.connect(self._on_sub_sync_finished)
        self.keyword_tab.sync_finished.connect(self._on_sub_sync_finished)
        self._refresh_inventory_tab()
        QTimer.singleShot(0, self._load_initial_visible_channel_tab)
        # 시작 시 자동 동기화: 창이 뜨고 캐시가 화면에 그려진 뒤 백그라운드 동기화 시작.
        # 캐시 렌더링 → (500ms) → 자동 sync_now → 변경된 row만 diff 적용
        QTimer.singleShot(500, self._auto_initial_sync)

    def _auto_initial_sync(self) -> None:
        """시작 시 자동 동기화 트리거. 이미 동기화 중이면 건너뜀."""
        if self._sync_session_active:
            return
        try:
            self.sync_now()
        except Exception:  # noqa: BLE001
            # 자동 트리거는 실패해도 조용히 넘김 (수동 버튼은 살아있음)
            pass

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
        self.tabs.addTab(self.product_master_tab, "상품등록")
        self.tabs.addTab(self.inventory_tab, "재고관리")
        self.tabs.addTab(self.sales_daily_tab, "판매일보")
        self.tabs.addTab(self.revenue_tab, "매출비교")
        self.tabs.addTab(self.keyword_tab, "키워드매출")
        self.tabs.addTab(self.fassto_tab, "파스토")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        corner = QWidget()
        corner_layout = QHBoxLayout(corner)
        corner_layout.setContentsMargins(0, 0, 0, 0)
        corner_layout.setSpacing(0)
        font_menu = QMenu(self.font_button)
        font_menu.addAction("글꼴 선택...", self._choose_app_font)
        font_menu.addAction("기본 글꼴로 복원", self._reset_app_font)
        self.font_button.setMenu(font_menu)
        self._update_font_button_state()
        self.pi_status_button.clicked.connect(self._check_pi_status)
        corner_layout.addWidget(self.font_button)
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
            #fontMenuButton {
                background: #ffffff;
                color: #334155;
                border: 1px solid #cbd5e1;
                border-radius: 9px;
                padding: 6px 12px;
                font-weight: 600;
                margin-right: 6px;
            }
            #fontMenuButton:hover {
                background: #f8fafc;
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

    def _restore_saved_app_font(self) -> None:
        saved_font = self.settings.value(APP_FONT_KEY, "", type=str)
        if not saved_font:
            return
        restored = QFont(self._default_app_font)
        if restored.fromString(saved_font):
            self._apply_app_font(restored, persist=False)

    def _apply_app_font(self, font: QFont, persist: bool = True) -> None:
        app = QApplication.instance()
        if app is None:
            return
        applied = QFont(font)
        if applied.pointSize() <= 0 and self._default_app_font.pointSize() > 0:
            applied.setPointSize(self._default_app_font.pointSize())
        app.setFont(applied)
        self.setFont(applied)
        for widget in self.findChildren(QWidget):
            widget.setFont(applied)
        self._refresh_font_sensitive_ui()
        for table in self.findChildren(QTableWidget):
            table.viewport().update()
            table.update()
            header = table.horizontalHeader()
            if header is not None:
                header.viewport().update()
        if persist:
            self.settings.setValue(APP_FONT_KEY, applied.toString())
        self._update_font_button_state()

    def _update_font_button_state(self) -> None:
        if not hasattr(self, "font_button"):
            return
        app = QApplication.instance()
        if app is None:
            return
        current = app.font()
        size_label = f"{current.pointSize()}pt" if current.pointSize() > 0 else "기본 크기"
        self.font_button.setToolTip(f"현재 글꼴: {current.family()} {size_label}")

    def _choose_app_font(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        selected, ok = QFontDialog.getFont(app.font(), self, "앱 글꼴 선택")
        if not ok:
            return
        self._apply_app_font(selected, persist=True)

    def _reset_app_font(self) -> None:
        self.settings.remove(APP_FONT_KEY)
        self._apply_app_font(self._default_app_font, persist=False)

    def _refresh_font_sensitive_ui(self) -> None:
        self._apply_styles()
        for attr_name in (
            "naver_tab",
            "coupang_tab",
            "inventory_tab",
            "revenue_tab",
            "keyword_tab",
            "sales_daily_tab",
        ):
            widget = getattr(self, attr_name, None)
            apply_styles = getattr(widget, "_apply_styles", None)
            if callable(apply_styles):
                apply_styles()

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

            naver_cnt = data.get("naver_collections", 0)
            coupang_cnt = data.get("coupang_collections", 0)
            msg = (
                f"✅ 정상 작동 중\n\n"
                f"네이버 마지막 수집: {_fmt(naver_ts)}  ({naver_cnt:,}회)\n"
                f"쿠팡 마지막 수집:   {_fmt(coupang_ts)}  ({coupang_cnt:,}회)\n"
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
    def _on_channel_sync_finished_for_masters(self, source: str, succeeded: bool) -> None:
        # 채널 동기화로 cache 에 저장된 raw rows 가 바뀌었을 수 있으니
        # 상품등록 탭의 마스터 집계도 재계산.
        if not succeeded:
            return
        try:
            self.product_master_tab.refresh()
        except Exception:  # noqa: BLE001
            pass

    def _on_masters_changed(self, *_args: object) -> None:
        # 마스터/링크가 어느 탭에서든 바뀌면 관련 탭들 재조정
        try:
            self.product_master_tab.refresh()
        except Exception:  # noqa: BLE001
            pass
        for tab in (self.naver_tab, self.coupang_tab):
            try:
                tab.refresh_master_links_from_external()
            except Exception:  # noqa: BLE001
                pass
        self._refresh_inventory_tab()

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

    def keyPressEvent(self, event: Any) -> None:
        # ₩ 키 (한국 키보드) — text()로 체크해서 어떤 키코드든 잡음
        if event.text() in ("₩", "`", "~"):
            focused = QApplication.focusWidget()
            if not isinstance(focused, QLineEdit):
                self._toggle_favorite_on_current_tab()
                event.accept()
                return
        super().keyPressEvent(event)

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

    @Slot(int)
    def _on_tab_changed(self, index: int) -> None:
        widget = self.tabs.widget(index)
        if isinstance(widget, ChannelTab):
            widget.load_initial_rows_if_needed()
            return
        if widget is self.sales_daily_tab and not self.sales_daily_tab._loaded_once:
            self.sales_daily_tab.reload()

    def _load_initial_visible_channel_tab(self) -> None:
        current = self.tabs.currentWidget()
        if isinstance(current, ChannelTab):
            current.load_initial_rows_if_needed()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.naver_tab.shutdown()
        self.coupang_tab.shutdown()
        self.inventory_tab.shutdown()
        self.sales_daily_tab.shutdown()
        self.revenue_tab.shutdown()
        self.keyword_tab.shutdown()
        super().closeEvent(event)
