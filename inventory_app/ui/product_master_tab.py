"""상품등록 탭.

사용자가 만드는 마스터 상품 카탈로그 + 채널 상품과의 연결 상태 관리.

레이아웃:
- 탭 본체: 마스터 상품 테이블 (풀스크린)
- 행 더블클릭 / "상세" 버튼 → `MasterDetailDialog` 팝업 (이름/원가/메모/링크 편집)

이전 버전은 splitter 로 상세 패널을 화면 일부에 박았는데 상품등록 탭 자체가 비좁아져
다이얼로그로 분리함.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

import httpx

from datetime import date, datetime, timedelta

from PySide6.QtCharts import (
    QBarCategoryAxis,
    QBarSet,
    QChart,
    QChartView,
    QLineSeries,
    QStackedBarSeries,
    QValueAxis,
)
from PySide6.QtCore import QDate, QDateTime, QObject, Qt, QTime, Signal, Slot
from PySide6.QtGui import QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QDateEdit,
    QFrame,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QGroupBox,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from inventory_app.models import ChannelProduct, MasterProduct, StockInboundEntry, StockInboundSummary
from inventory_app.services.image_cache import get_image_bytes
from inventory_app.services.local_cache import ChannelProductCache
from inventory_app.services.master_product_service import (
    LinkedChannelView,
    MasterAggregation,
    MasterProductRow,
    MasterProductService,
    build_master_service,
)
from inventory_app.services.master_remote_client import MasterRemoteError
from inventory_app.services.shared_stock_grouping import product_identity_key


_MASTER_COLS = [
    "이미지",
    "이름",
    "원가",
    "네이버가",
    "쿠팡가",
    "네이버재고",
    "쿠팡재고",
    "총재고",
    "재고원가",
    "네이버(오늘)",
    "쿠팡(오늘)",
    "오늘판매",
    "오늘매출",
    "네이버판매(30일)",
    "쿠팡판매(30일)",
    "총판매(30일)",
    "연결",
]
_LINK_COLS = [
    "채널",
    "상품명",
    "배수",
    "재고",
    "판매(30일)",
    "오늘판매",
    "가격",
    "대표",
    "",
]

_COL_NAVER_STOCK = 5
_COL_COUPANG_STOCK = 6
_COL_TOTAL_STOCK = 7
_COL_STOCK_COST = 8


def _format_int(value: Optional[int]) -> str:
    if value is None:
        return "-"
    return f"{int(value):,}"


def _format_sales_int(value: Optional[int]) -> str:
    """판매 수량 포매터 — 0 또는 None 이면 공란 (시각적 노이즈 제거)."""
    if value is None or int(value) == 0:
        return ""
    return f"{int(value):,}"


def _format_price(value: Optional[int]) -> str:
    if value is None:
        return "-"
    return f"{int(value):,}원"


def _number_item(text: str, sort_value) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    if sort_value is not None:
        item.setData(Qt.UserRole, sort_value)
    return item


def _format_consumed_date(value: Optional[date | datetime]) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    return value.isoformat()


def _make_stock_cell_widget(base_value: Optional[int], pending_qty: int) -> QWidget:
    cell = QWidget()
    cell.setAttribute(Qt.WA_TransparentForMouseEvents, True)
    cell.setStyleSheet("background: transparent;")

    layout = QVBoxLayout(cell)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)

    base_label = QLabel(_format_int(base_value))
    base_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    base_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
    base_label.setStyleSheet(
        "padding-right: 6px; border-bottom: 1px solid #e5e7eb; color: #111827;"
    )

    inbound_label = QLabel(f"+{int(pending_qty):,}" if pending_qty > 0 else "")
    inbound_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    inbound_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
    inbound_label.setStyleSheet(
        "padding-right: 6px; color: #16a34a; font-weight: 700;"
    )

    layout.addWidget(base_label, 1)
    layout.addWidget(inbound_label, 1)
    return cell


class InboundManageDialog(QDialog):
    def __init__(
        self,
        product_name: str,
        channel_label: str,
        entries: List[StockInboundEntry],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("입고 관리")
        self._entries: List[StockInboundEntry] = list(entries)
        self.delete_requested_id: Optional[int] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        title = QLabel(f"{product_name}\n{channel_label} 입고 추가 및 삭제")
        title.setWordWrap(True)
        root.addWidget(title)

        content = QHBoxLayout()
        content.setSpacing(12)
        root.addLayout(content, 1)

        input_box = QGroupBox("입고 추가")
        input_layout = QVBoxLayout(input_box)
        input_layout.setSpacing(10)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(8)

        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setDate(QDate.currentDate())

        self.quantity_spin = QSpinBox()
        self.quantity_spin.setRange(1, 1_000_000)
        self.quantity_spin.setValue(1)

        form.addRow("입고 날짜", self.date_edit)
        form.addRow("입고 수량", self.quantity_spin)
        input_layout.addLayout(form)

        add_btn = QPushButton("입고 추가")
        add_btn.clicked.connect(self.accept)
        input_layout.addWidget(add_btn)
        input_layout.addStretch(1)
        content.addWidget(input_box, 0)

        list_box = QGroupBox("입고 리스트")
        list_layout = QVBoxLayout(list_box)
        list_layout.setSpacing(10)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["ID", "입고일", "입고", "잔여", "차감일", "수정일"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        list_layout.addWidget(self.table, 1)
        self._fill_entries()

        delete_btn = QPushButton("선택 입고 삭제")
        delete_btn.clicked.connect(self._request_delete)
        list_layout.addWidget(delete_btn)
        content.addWidget(list_box, 1)

        row = QHBoxLayout()
        close_btn = QPushButton("닫기")
        close_btn.clicked.connect(self.reject)
        row.addStretch(1)
        row.addWidget(close_btn)
        root.addLayout(row)

        self.resize(920, 500)

    def _fill_entries(self) -> None:
        self.table.setRowCount(0)
        for entry in sorted(
            self._entries,
            key=lambda it: (str(it.receipt_date), int(it.id or 0)),
            reverse=True,
        ):
            r = self.table.rowCount()
            self.table.insertRow(r)
            values = [
                str(entry.id or ""),
                str(entry.receipt_date or ""),
                f"{int(entry.input_qty):,}",
                f"{int(entry.remaining_qty):,}",
                _format_consumed_date(entry.last_consumed_at),
                entry.updated_at.strftime("%Y-%m-%d %H:%M") if entry.updated_at else "",
            ]
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                if c == 0:
                    item.setData(Qt.UserRole, int(entry.id or 0))
                self.table.setItem(r, c, item)

    def _request_delete(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "선택 필요", "삭제할 입고 행을 선택하세요.")
            return
        item = self.table.item(rows[0].row(), 0)
        inbound_id = int(item.data(Qt.UserRole) or 0) if item is not None else 0
        if inbound_id <= 0:
            return
        if QMessageBox.question(
            self,
            "입고 삭제",
            f"선택한 입고 #{inbound_id}를 삭제할까요?",
        ) != QMessageBox.Yes:
            return
        self.delete_requested_id = inbound_id
        self.done(2)

    @property
    def receipt_date(self) -> str:
        return self.date_edit.date().toString("yyyy-MM-dd")

    @property
    def quantity(self) -> int:
        return int(self.quantity_spin.value())


class _ImageSignals(QObject):
    loaded = Signal(str, object)  # url, bytes|None


class _ImageLoader:
    """탭 / 다이얼로그가 공유하는 async 이미지 로더 + 픽스맵 캐시."""

    def __init__(self) -> None:
        self._cache: Dict[str, QPixmap] = {}
        self._pending: set[str] = set()
        self._executor = ThreadPoolExecutor(max_workers=4)
        self.signals = _ImageSignals()

    @property
    def cache(self) -> Dict[str, QPixmap]:
        return self._cache

    def request(self, url: str) -> Optional[QPixmap]:
        if not url:
            return None
        pm = self._cache.get(url)
        if pm is not None and not pm.isNull():
            return pm
        if url not in self._pending:
            self._pending.add(url)
            self._executor.submit(self._worker, url)
        return None

    def _worker(self, url: str) -> None:
        data = None
        try:
            data = get_image_bytes(url)
        except Exception:  # noqa: BLE001
            data = None
        self.signals.loaded.emit(url, data)

    def accept_loaded(self, url: str, data) -> Optional[QPixmap]:
        self._pending.discard(url)
        if not data:
            return None
        pm = QPixmap()
        if not pm.loadFromData(data):
            return None
        self._cache[url] = pm
        return pm


def _fetch_rolling_sales_totals(
    monitor_url: str,
    days: int = 30,
    timeout: float = 10.0,
) -> Optional[Dict[Tuple[str, str], int]]:
    """Pi 의 /sales/totals?days=N 호출 → {(channel, product_identity_key): qty} 맵.

    실패 시 None 반환 (호출부는 API 값 그대로 사용).
    """
    if not monitor_url:
        return None
    url = f"{monitor_url.rstrip('/')}/sales/totals?days={int(days)}"
    try:
        resp = httpx.get(url, timeout=timeout)
        if resp.status_code != 200:
            return None
        payload = resp.json()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(payload, dict):
        return None
    totals: Dict[Tuple[str, str], int] = {}
    for row in payload.get("totals") or []:
        if not isinstance(row, dict):
            continue
        channel = str(row.get("channel") or "").strip().lower()
        pid = str(row.get("product_id") or "").strip()
        iid = row.get("item_id")
        iid_str = str(iid) if iid not in (None, "") else ""
        try:
            qty = int(row.get("qty_sold") or 0)
        except (TypeError, ValueError):
            qty = 0
        if not channel or not pid or qty < 0:
            continue
        key = (channel, f"id:{pid}|item:{iid_str}")
        totals[key] = qty
    return totals


def _apply_rolling_sales_override(
    rows_by_channel: Dict[str, List[ChannelProduct]],
    totals: Dict[Tuple[str, str], int],
) -> None:
    """rows 의 sales 필드를 Pi 재고차감 카운팅값으로 교체 (in-place).

    해당 SKU 이벤트가 0이면 sales=0 으로 세팅 (API 30일 값을 쓰지 않고).
    """
    for channel, rows in rows_by_channel.items():
        for row in rows:
            key = (channel, product_identity_key(row))
            row.sales = int(totals.get(key, 0))


def _assign_pixmap(
    label: QLabel,
    url: Optional[str],
    size: int,
    loader: _ImageLoader,
    *,
    text_if_none: str = "-",
) -> None:
    if not url:
        label.setText(text_if_none)
        return
    pm = loader.request(url)
    if pm is not None:
        label.setPixmap(pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    else:
        label.setText(text_if_none)


# ---------------------------------------------------------------------------
# 상세 다이얼로그
# ---------------------------------------------------------------------------


class MasterDetailDialog(QDialog):
    """마스터 상품 상세 편집 팝업.

    - 이름/원가/메모 편집 + 저장
    - 연결된 채널 상품 리스트 + 배수/대표/해제 액션
    - 마스터 삭제
    """

    changed = Signal()  # 저장/삭제/링크 수정 발생 시 방출 → 탭 refresh

    def __init__(
        self,
        service: MasterProductService,
        loader: _ImageLoader,
        master_id: Optional[int],
        aggregation: MasterAggregation,
        parent: Optional[QWidget] = None,
        monitor_url: str = "",
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.loader = loader
        self.master_id = master_id  # None = create mode
        self._aggregation = aggregation
        self._dirty = False
        self._monitor_url = monitor_url
        self.loader.signals.loaded.connect(self._on_image_loaded)
        self.setWindowTitle(
            "새 마스터 상품" if master_id is None else "마스터 상품 상세"
        )
        self.resize(900, 680)
        self._build_ui()
        if master_id is None:
            self._setup_create_mode()
        else:
            self._load_from_aggregation()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(10)

        header_row = QHBoxLayout()
        self.detail_image = QLabel()
        self.detail_image.setFixedSize(120, 120)
        self.detail_image.setStyleSheet(
            "border: 1px solid #cbd5e1; background: #f8fafc; border-radius: 6px;"
        )
        self.detail_image.setAlignment(Qt.AlignCenter)
        self.detail_image.setText("이미지 없음")

        form = QVBoxLayout()
        form.setSpacing(6)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("마스터 이름")
        self.name_edit.returnPressed.connect(self._on_save_master)
        self.cost_edit = QLineEdit()
        self.cost_edit.setPlaceholderText("원가 (숫자)")
        self.cost_edit.returnPressed.connect(self._on_save_master)
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("이름"))
        name_row.addWidget(self.name_edit, 1)
        cost_row = QHBoxLayout()
        cost_row.addWidget(QLabel("원가"))
        cost_row.addWidget(self.cost_edit, 1)
        form.addLayout(name_row)
        form.addLayout(cost_row)
        form.addWidget(QLabel("메모"))
        self.memo_edit = QPlainTextEdit()
        self.memo_edit.setFixedHeight(80)
        form.addWidget(self.memo_edit, 1)

        header_row.addWidget(self.detail_image)
        header_row.addLayout(form, 1)
        layout.addLayout(header_row)

        action_row = QHBoxLayout()
        self.save_master_button = QPushButton("저장")
        self.save_master_button.clicked.connect(self._on_save_master)
        self.delete_master_button = QPushButton("마스터 삭제")
        self.delete_master_button.clicked.connect(self._on_delete_master)
        action_row.addWidget(self.save_master_button)
        action_row.addWidget(self.delete_master_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        separator = QFrame()
        separator.setFrameShape(QFrame.HLine)
        separator.setStyleSheet("color: #e2e8f0;")
        layout.addWidget(separator)

        # 연결된 채널 상품
        layout.addWidget(QLabel("연결된 채널 상품"))
        self.links_table = QTableWidget(0, len(_LINK_COLS))
        self.links_table.setHorizontalHeaderLabels(_LINK_COLS)
        self.links_table.verticalHeader().setVisible(False)
        self.links_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.links_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.links_table.setAlternatingRowColors(True)
        self.links_table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        links_header = self.links_table.horizontalHeader()
        links_header.setStretchLastSection(False)
        links_header.setSectionResizeMode(1, QHeaderView.Stretch)
        for idx in (0, 2, 3, 4, 5, 6, 7, 8):
            links_header.setSectionResizeMode(idx, QHeaderView.ResizeToContents)
        self.links_table.setMaximumHeight(140)
        layout.addWidget(self.links_table, 0)

        # 30일 판매 추이 차트
        layout.addWidget(QLabel("30일 판매 추이"))
        self.chart_summary = QLabel("그래프 로딩 중...")
        self.chart_summary.setStyleSheet(
            "color: #0f172a; padding: 6px 10px; background: #f1f5f9;"
            " border: 1px solid #cbd5e1; border-radius: 6px;"
            " font-size: 12px;"
        )
        self.chart_summary.setFixedHeight(34)
        self.chart_summary.setTextFormat(Qt.RichText)
        layout.addWidget(self.chart_summary)

        # hover 시 일별 상세를 보여주는 라벨 (단일 라인, 자동 사라지지 않음)
        self.chart_hover_label = QLabel(
            "<span style='color:#94a3b8'>막대 hover 시 일별 상세 표시</span>"
        )
        self.chart_hover_label.setStyleSheet(
            "color: #0f172a; padding: 4px 10px; background: #fffbeb;"
            " border: 1px solid #fcd34d; border-radius: 4px; font-size: 11px;"
        )
        self.chart_hover_label.setTextFormat(Qt.RichText)
        self.chart_hover_label.setFixedHeight(26)
        layout.addWidget(self.chart_hover_label)

        self.chart = QChart()
        self.chart.setAnimationOptions(QChart.SeriesAnimations)
        self.chart.legend().setAlignment(Qt.AlignBottom)
        self.chart_view = QChartView(self.chart)
        self.chart_view.setRenderHint(QPainter.Antialiasing)
        self.chart_view.setMinimumHeight(220)
        layout.addWidget(self.chart_view, 1)

        self._chart_loaded = False

        button_box = QDialogButtonBox(QDialogButtonBox.Close)
        button_box.rejected.connect(self.accept)
        layout.addWidget(button_box)

    # ------------------------------------------------------------------
    # Data load
    # ------------------------------------------------------------------

    def _find_master_row(self) -> Optional[MasterProductRow]:
        if self.master_id is None:
            return None
        for mr in self._aggregation.masters:
            if mr.master.id == self.master_id:
                return mr
        return None

    def _setup_create_mode(self) -> None:
        # 이름 입력 후 Enter/저장 시 create_master 실행.
        self.detail_image.clear()
        self.detail_image.setText("이미지 없음")
        self.name_edit.setFocus()
        # 삭제 및 링크 테이블은 의미 없음 (아직 생성 안 됨)
        self.delete_master_button.setEnabled(False)
        self.links_table.setEnabled(False)

    def _reload_aggregation(self) -> None:
        # 다이얼로그 내부에서 링크 변경 후 자체 재집계 (탭 전체 refresh 전에)
        rows_by_channel: Dict[str, List[ChannelProduct]] = {
            "naver": self.service.cache.load_rows("naver"),
            "coupang": self.service.cache.load_rows("coupang"),
        }
        monitor_url = getattr(self.parent(), "monitor_url", "") if self.parent() else ""
        if monitor_url:
            totals = _fetch_rolling_sales_totals(monitor_url, days=30)
            if totals is not None:
                _apply_rolling_sales_override(rows_by_channel, totals)
        self._aggregation = self.service.aggregate(rows_by_channel)

    def _load_from_aggregation(self) -> None:
        mr = self._find_master_row()
        if mr is None:
            # 마스터가 삭제됐거나 aggregation 이 stale — 다이얼로그 강제 종료
            self.close()
            return
        master = mr.master
        self.name_edit.setText(master.name)
        self.cost_edit.setText("" if master.unit_cost is None else str(master.unit_cost))
        self.memo_edit.blockSignals(True)
        self.memo_edit.setPlainText(master.memo or "")
        self.memo_edit.blockSignals(False)

        if mr.image_url:
            _assign_pixmap(
                self.detail_image, mr.image_url, 116, self.loader, text_if_none="이미지 없음"
            )
        else:
            self.detail_image.clear()
            self.detail_image.setText("이미지 없음")

        self._render_links(mr)

    def _render_links(self, master_row: MasterProductRow) -> None:
        self.links_table.setRowCount(len(master_row.linked))
        rep_channel = master_row.master.representative_channel
        rep_key = master_row.master.representative_product_key
        for row_idx, link in enumerate(master_row.linked):
            is_rep = (link.channel == rep_channel and link.product_key == rep_key)
            self._populate_link_row(row_idx, link, is_rep)

    def _populate_link_row(
        self,
        row_idx: int,
        link: LinkedChannelView,
        is_rep: bool,
    ) -> None:
        label = "네이버" if link.channel == "naver" else ("쿠팡" if link.channel == "coupang" else link.channel)
        self.links_table.setItem(row_idx, 0, QTableWidgetItem(label))

        name_item = QTableWidgetItem(link.name)
        name_item.setToolTip(link.name)
        self.links_table.setItem(row_idx, 1, name_item)

        self.links_table.setItem(row_idx, 2, _number_item(f"×{link.multiplier}", link.multiplier))
        self.links_table.setItem(row_idx, 3, _number_item(_format_int(link.stock), link.stock))
        self.links_table.setItem(row_idx, 4, _number_item(_format_sales_int(link.sales), link.sales))
        self.links_table.setItem(row_idx, 5, _number_item(_format_sales_int(link.today_sales), link.today_sales))
        self.links_table.setItem(row_idx, 6, _number_item(_format_price(link.price), link.price))

        rep_item = QTableWidgetItem("★" if is_rep else "")
        rep_item.setTextAlignment(Qt.AlignCenter)
        self.links_table.setItem(row_idx, 7, rep_item)

        action_widget = QWidget()
        action_layout = QHBoxLayout(action_widget)
        action_layout.setContentsMargins(2, 1, 2, 1)
        action_layout.setSpacing(4)
        edit_mult = QPushButton("배수")
        edit_mult.setFixedHeight(24)
        edit_mult.clicked.connect(
            lambda _=None, ch=link.channel, pk=link.product_key, cur=link.multiplier:
            self._on_edit_multiplier(ch, pk, cur)
        )
        rep_btn = QPushButton("대표")
        rep_btn.setFixedHeight(24)
        rep_btn.clicked.connect(
            lambda _=None, ch=link.channel, pk=link.product_key:
            self._on_set_representative(ch, pk)
        )
        unlink_btn = QPushButton("해제")
        unlink_btn.setFixedHeight(24)
        unlink_btn.clicked.connect(
            lambda _=None, ch=link.channel, pk=link.product_key:
            self._on_unlink(ch, pk)
        )
        action_layout.addWidget(edit_mult)
        action_layout.addWidget(rep_btn)
        action_layout.addWidget(unlink_btn)
        self.links_table.setCellWidget(row_idx, 8, action_widget)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _on_save_master(self) -> None:
        new_name = self.name_edit.text().strip()
        if not new_name:
            QMessageBox.warning(self, "이름 필요", "마스터 이름은 비워둘 수 없어요.")
            return
        cost_text = self.cost_edit.text().strip()
        clear_cost = False
        cost_value: int | None
        if not cost_text:
            cost_value = None
            clear_cost = True
        else:
            try:
                cost_value = int(cost_text.replace(",", ""))
            except ValueError:
                QMessageBox.warning(self, "숫자 필요", "원가는 숫자로 입력해주세요.")
                return
        memo_text = self.memo_edit.toPlainText().strip()
        try:
            if self.master_id is None:
                # 생성 모드
                master = self.service.create_master(
                    name=new_name,
                    unit_cost=cost_value,
                    memo=memo_text if memo_text else None,
                )
                self.master_id = master.id
            else:
                self.service.update_master(
                    self.master_id,
                    name=new_name,
                    unit_cost=cost_value,
                    clear_unit_cost=clear_cost,
                    memo=memo_text if memo_text else None,
                    clear_memo=not bool(memo_text),
                )
        except ValueError as exc:
            QMessageBox.warning(self, "실패", str(exc))
            return
        except MasterRemoteError as exc:
            QMessageBox.critical(self, "파이 서버 오류", f"마스터 저장 실패: {exc}")
            return
        self._dirty = True
        self.changed.emit()
        # 저장 성공 시 다이얼로그 닫기 (엔터키 UX)
        self.accept()

    def _on_delete_master(self) -> None:
        mr = self._find_master_row()
        name = mr.master.name if mr else str(self.master_id)
        reply = QMessageBox.question(
            self,
            "마스터 삭제",
            f"‘{name}’ 를 삭제하면 연결된 링크도 함께 제거됩니다. 계속할까요?",
        )
        if reply != QMessageBox.Yes:
            return
        try:
            self.service.delete_master(self.master_id)
        except MasterRemoteError as exc:
            QMessageBox.critical(self, "파이 서버 오류", f"마스터 삭제 실패: {exc}")
            return
        self._dirty = True
        self.changed.emit()
        self.accept()

    def _on_edit_multiplier(self, channel: str, product_key: str, current: int) -> None:
        value, ok = QInputDialog.getInt(
            self,
            "배수 수정",
            "채널 상품 1건이 마스터 몇 단위인지 입력:",
            current,
            1,
            10_000,
            1,
        )
        if not ok:
            return
        try:
            self.service.set_multiplier(channel, product_key, int(value))
        except MasterRemoteError as exc:
            QMessageBox.critical(self, "파이 서버 오류", f"배수 변경 실패: {exc}")
            return
        self._dirty = True
        self.changed.emit()
        self._reload_aggregation()
        self._load_from_aggregation()

    def _on_set_representative(self, channel: str, product_key: str) -> None:
        try:
            self.service.set_representative(self.master_id, channel, product_key)
        except MasterRemoteError as exc:
            QMessageBox.critical(self, "파이 서버 오류", f"대표 지정 실패: {exc}")
            return
        self._dirty = True
        self.changed.emit()
        self._reload_aggregation()
        self._load_from_aggregation()

    def _on_unlink(self, channel: str, product_key: str) -> None:
        reply = QMessageBox.question(
            self, "연결 해제", "이 채널 상품의 마스터 연결을 해제할까요?"
        )
        if reply != QMessageBox.Yes:
            return
        try:
            self.service.unlink(channel, product_key)
        except MasterRemoteError as exc:
            QMessageBox.critical(self, "파이 서버 오류", f"연결 해제 실패: {exc}")
            return
        self._dirty = True
        self.changed.emit()
        self._reload_aggregation()
        self._load_from_aggregation()

    @Slot(str, object)
    def _on_image_loaded(self, url: str, data) -> None:
        pm = self.loader.accept_loaded(url, data)
        if pm is None:
            return
        mr = self._find_master_row()
        if mr and mr.image_url == url:
            self.detail_image.setPixmap(
                pm.scaled(116, 116, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )

    def _maybe_autoload_chart(self) -> None:
        if not self._chart_loaded and self.master_id is not None:
            self._load_sales_chart()

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._maybe_autoload_chart()

    def _load_sales_chart(self) -> None:
        """Pi /sales/series 30일치 응답으로 라인 차트 그리기 (단순 동기, 다이얼로그 그려진 뒤 실행)."""
        self._chart_loaded = True
        if not self._monitor_url:
            self.chart_summary.setText(
                "<span style='color:#dc2626'>monitor.url 미설정 — 그래프를 불러올 수 없습니다.</span>"
            )
            return
        if self._find_master_row() is None:
            self.chart_summary.setText("마스터 정보 없음")
            return

        end_d = date.today()
        start_d = end_d - timedelta(days=29)
        url = f"{self._monitor_url.rstrip('/')}/sales/series"
        self.chart_summary.setText(f"그래프 로딩 중... ({url})")
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, lambda: self._fetch_and_render_chart(url, start_d, end_d))

    def _fetch_and_render_chart(self, url: str, start_d: date, end_d: date) -> None:
        try:
            try:
                resp = httpx.get(
                    url,
                    params={"start": start_d.isoformat(), "end": end_d.isoformat()},
                    timeout=httpx.Timeout(connect=3.0, read=8.0, write=3.0, pool=3.0),
                )
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:  # noqa: BLE001
                self.chart_summary.setText(
                    f"<span style='color:#dc2626'>그래프 조회 실패: {type(exc).__name__}: {exc}</span>"
                )
                return

            if not isinstance(payload, dict):
                self.chart_summary.setText(
                    "<span style='color:#dc2626'>그래프 응답 형식 오류</span>"
                )
                return
            master_row = self._find_master_row()
            if master_row is None:
                self.chart_summary.setText("마스터 정보 없음")
                return
            self._render_chart(payload, master_row, start_d, end_d)
        except Exception as exc:  # noqa: BLE001
            import traceback
            tb = traceback.format_exc()
            self.chart_summary.setText(
                f"<span style='color:#dc2626'>차트 렌더 오류: {type(exc).__name__}: {exc}</span>"
            )
            try:
                from pathlib import Path
                Path.home().joinpath(".smartinventory_chart_error.log").write_text(tb)
            except Exception:
                pass

    def _render_chart(
        self,
        payload: dict,
        master_row: MasterProductRow,
        start_d: date,
        end_d: date,
    ) -> None:
        rows = payload.get("rows") or []
        link_mult: Dict[Tuple[str, str], int] = {
            (lk.channel, lk.product_key): max(1, int(lk.multiplier))
            for lk in master_row.linked
        }

        day_naver: Dict[str, int] = {}
        day_coupang: Dict[str, int] = {}
        day_naver_rev: Dict[str, int] = {}
        day_coupang_rev: Dict[str, int] = {}
        total_qty = 0
        total_revenue = 0
        for r in rows:
            channel = str(r.get("channel") or "").strip().lower()
            pid = str(r.get("product_id") or "").strip()
            iid = r.get("item_id")
            iid_text = str(iid) if iid not in (None, "") else ""
            product_key = f"id:{pid}|item:{iid_text}" if pid else ""
            mult = link_mult.get((channel, product_key))
            if mult is None:
                continue
            day = str(r.get("date") or "")
            qty_master = int(r.get("qty_sold") or 0) * mult
            revenue = int(r.get("revenue") or 0)
            if channel == "naver":
                day_naver[day] = day_naver.get(day, 0) + qty_master
                day_naver_rev[day] = day_naver_rev.get(day, 0) + revenue
            elif channel == "coupang":
                day_coupang[day] = day_coupang.get(day, 0) + qty_master
                day_coupang_rev[day] = day_coupang_rev.get(day, 0) + revenue
            total_qty += qty_master
            total_revenue += revenue

        # 30일 모든 날짜 채우기
        all_days: list[date] = []
        cur = start_d
        while cur <= end_d:
            all_days.append(cur)
            cur += timedelta(days=1)

        from PySide6.QtGui import QColor
        naver_set = QBarSet("네이버")
        naver_set.setColor(QColor("#03C75A"))  # 네이버 브랜드 녹색
        naver_set.setBorderColor(QColor("#03C75A"))
        coupang_set = QBarSet("쿠팡")
        coupang_set.setColor(QColor("#F50028"))  # 쿠팡 브랜드 레드
        coupang_set.setBorderColor(QColor("#F50028"))

        max_qty = 0
        categories: list[str] = []
        n_days = len(all_days)
        # 30개 라벨 모두 고유 + 간략 표시 (M/D). 가독성은 폰트/회전으로 처리.
        for d in all_days:
            key = d.isoformat()
            n = int(day_naver.get(key, 0))
            c = int(day_coupang.get(key, 0))
            t = n + c
            if t > max_qty:
                max_qty = t
            naver_set.append(n)
            coupang_set.append(c)
            categories.append(f"{d.month}/{d.day}")

        bar_series = QStackedBarSeries()
        bar_series.append(naver_set)
        bar_series.append(coupang_set)

        # reset chart
        for s in self.chart.series()[:]:
            self.chart.removeSeries(s)
        for ax in self.chart.axes():
            self.chart.removeAxis(ax)

        self.chart.addSeries(bar_series)

        from PySide6.QtCore import QMargins
        from PySide6.QtGui import QFont

        x_axis = QBarCategoryAxis()
        x_axis.append(categories)
        x_axis.setLabelsAngle(-45)
        small_font = QFont()
        small_font.setPointSize(8)
        x_axis.setLabelsFont(small_font)
        x_axis.setGridLineVisible(False)
        self.chart.addAxis(x_axis, Qt.AlignBottom)
        bar_series.attachAxis(x_axis)

        y_axis = QValueAxis()
        y_axis.setLabelFormat("%d")
        y_axis.setRange(0, max(1, int(max_qty * 1.15)))
        y_axis.setLabelsFont(small_font)
        y_axis.setTickCount(5)
        y_axis.setTitleText("일별 판매수량")
        y_axis.setTitleFont(small_font)
        self.chart.addAxis(y_axis, Qt.AlignLeft)
        bar_series.attachAxis(y_axis)

        # ── 재고 차감 직선 그래프 ──
        # 오늘의 총재고 S 기준으로, 과거 30일을 거꾸로 누적해 일별 "남은 재고" 계산.
        # day_30 (오늘) = S, day_29 = S + sales_30, day_28 = S + sales_30 + sales_29, ...
        current_stock_val = master_row.total_stock
        line_series = QLineSeries()
        line_series.setName("남은 재고")
        from PySide6.QtGui import QColor as _QColor
        line_pen = QPen(_QColor("#1d4ed8"))
        line_pen.setWidth(2)
        line_series.setPen(line_pen)
        line_series.setPointsVisible(True)
        # 오늘부터 거꾸로 채워넣기
        remaining_by_day: list[int] = [0] * len(all_days)
        if current_stock_val is not None:
            remaining = int(current_stock_val)
            # 마지막 날(today) = current stock
            remaining_by_day[-1] = remaining
            for i in range(len(all_days) - 2, -1, -1):
                # day i 의 남은재고 = day i+1 의 남은재고 + day i+1 일자 판매량
                next_key = all_days[i + 1].isoformat()
                sold_next = int(day_naver.get(next_key, 0)) + int(day_coupang.get(next_key, 0))
                remaining = remaining + sold_next
                remaining_by_day[i] = remaining
            for i, val in enumerate(remaining_by_day):
                line_series.append(float(i), float(val))
            self.chart.addSeries(line_series)
            line_series.attachAxis(x_axis)
            stock_y_axis = QValueAxis()
            stock_y_axis.setLabelFormat("%d")
            stock_max = max(remaining_by_day) if remaining_by_day else 1
            stock_y_axis.setRange(0, max(1, int(stock_max * 1.1)))
            stock_y_axis.setLabelsFont(small_font)
            stock_y_axis.setTickCount(5)
            stock_y_axis.setTitleText("남은 재고")
            stock_y_axis.setTitleFont(small_font)
            stock_y_axis.setLabelsColor(_QColor("#1d4ed8"))
            self.chart.addAxis(stock_y_axis, Qt.AlignRight)
            line_series.attachAxis(stock_y_axis)
        else:
            # 재고 정보 없음 — 라인 시리즈 비활성
            pass
        # hover 데이터에 남은재고/요일/차감수량 포함시키기 위해 보관
        self._stock_remaining_by_day = remaining_by_day

        # 차트 디자인 정리
        self.chart.setTitle("")
        self.chart.setMargins(QMargins(4, 4, 4, 28))  # 하단 여유 → 회전된 X축 라벨
        self.chart.setBackgroundRoundness(8)
        self.chart.legend().setAlignment(Qt.AlignTop)
        self.chart.legend().setFont(small_font)
        bar_series.setLabelsVisible(False)
        bar_series.setBarWidth(0.85)

        # 일별 데이터 보관 → hover 툴팁
        self._chart_day_data = []
        for d in all_days:
            key = d.isoformat()
            self._chart_day_data.append({
                "date": d,
                "naver_qty": int(day_naver.get(key, 0)),
                "coupang_qty": int(day_coupang.get(key, 0)),
                "naver_rev": int(day_naver_rev.get(key, 0)),
                "coupang_rev": int(day_coupang_rev.get(key, 0)),
            })

        # 한국식 요일 매핑
        _WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]

        def _on_hover(status: bool, index: int, barset) -> None:
            if not status or index < 0 or index >= len(self._chart_day_data):
                return  # 사라지지 않게 유지 — 다음 막대 hover 시 갱신
            row = self._chart_day_data[index]
            d = row["date"]
            n_qty = row["naver_qty"]
            c_qty = row["coupang_qty"]
            n_rev = row["naver_rev"]
            c_rev = row["coupang_rev"]
            total = n_rev + c_rev
            depleted = n_qty + c_qty
            weekday_ko = _WEEKDAY_KO[d.weekday()]
            remaining_txt = ""
            try:
                rem = self._stock_remaining_by_day[index]
                remaining_txt = (
                    f" &nbsp;·&nbsp; <span style='color:#1d4ed8'>● 남은재고</span> "
                    f"<b>{rem:,}</b>"
                )
            except Exception:  # noqa: BLE001
                pass
            self.chart_hover_label.setText(
                f"<b>{d.strftime('%Y-%m-%d')} ({weekday_ko})</b> &nbsp; "
                f"<span style='color:#03C75A'>● 네이버</span> {n_qty}건 · ₩{n_rev:,} &nbsp; "
                f"<span style='color:#F50028'>● 쿠팡</span> {c_qty}건 · ₩{c_rev:,} &nbsp; "
                f"<b>차감 {depleted}건 · 합계 ₩{total:,}</b>"
                f"{remaining_txt}"
            )

        bar_series.hovered.connect(_on_hover)

        # 라인 시리즈도 hover 대응 — 점 위에 마우스 갈 때 동일한 정보 표시
        try:
            def _on_line_hover(point, state: bool) -> None:
                if not state:
                    return
                idx = int(round(point.x()))
                _on_hover(True, idx, None)
            line_series.hovered.connect(_on_line_hover)
        except Exception:  # noqa: BLE001
            pass

        # 만원 단위 변환
        if total_revenue >= 10000:
            man = total_revenue // 10000
            rest = total_revenue % 10000
            man_str = f"{man:,}만 {rest:,}원" if rest else f"{man:,}만원"
        else:
            man_str = f"{total_revenue:,}원"
        self.chart_summary.setText(
            f"<span style='font-size:14px; color:#0f172a'>총 매출 "
            f"<b style='color:#dc2626'>₩{total_revenue:,}</b> "
            f"<span style='color:#64748b'>({man_str})</span></span>"
            f" &nbsp;·&nbsp; <span style='color:#475569'>총 판매 "
            f"<b style='color:#0f172a'>{total_qty:,}건</b></span>"
            f" &nbsp;·&nbsp; <span style='color:#94a3b8; font-size:11px'>"
            f"{start_d.isoformat()} ~ {end_d.isoformat()}</span>"
        )


# ---------------------------------------------------------------------------
# 상품등록 탭
# ---------------------------------------------------------------------------



class ProductMasterTab(QWidget):
    """상품등록 탭 — 마스터 테이블만 표시. 상세는 더블클릭으로 팝업."""

    masters_changed = Signal()

    def __init__(
        self,
        cache: Optional[ChannelProductCache] = None,
        monitor_url: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.cache = cache or ChannelProductCache()
        self.monitor_url = (str(monitor_url).strip() if monitor_url else "")
        self.service = build_master_service(
            cache=self.cache, monitor_url=self.monitor_url
        )
        self._remote_refresh_warning: str = ""
        self._current_aggregation: MasterAggregation | None = None
        self._inbound_summaries: Dict[tuple[int, str], StockInboundSummary] = {}

        self._loader = _ImageLoader()
        self._loader.signals.loaded.connect(self._on_image_loaded)

        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(8)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.new_master_button = QPushButton("새 마스터")
        self.new_master_button.clicked.connect(self._on_new_master)
        self.refresh_button = QPushButton("새로고침")
        self.refresh_button.clicked.connect(self.refresh)
        self.detail_button = QPushButton("상세")
        self.detail_button.setToolTip("선택된 마스터 상품 상세 편집 (행 더블클릭으로도 가능)")
        self.detail_button.clicked.connect(self._on_detail_button_clicked)
        self.summary_label = QLabel("")
        self.summary_label.setStyleSheet("color: #475569;")
        toolbar.addWidget(self.new_master_button)
        toolbar.addWidget(self.detail_button)
        toolbar.addWidget(self.refresh_button)
        toolbar.addStretch(1)
        toolbar.addWidget(self.summary_label)
        root_layout.addLayout(toolbar)

        self.master_table = QTableWidget(0, len(_MASTER_COLS))
        self.master_table.setHorizontalHeaderLabels(_MASTER_COLS)
        self.master_table.verticalHeader().setVisible(False)
        self.master_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.master_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.master_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.master_table.setAlternatingRowColors(True)
        self.master_table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.master_table.doubleClicked.connect(self._on_row_double_clicked)
        header = self.master_table.horizontalHeader()
        header.setStretchLastSection(False)
        for idx in range(len(_MASTER_COLS)):
            header.setSectionResizeMode(idx, QHeaderView.Interactive)
        _col_widths = {
            0: 56, 1: 260, 2: 80, 3: 92, 4: 88, 5: 86, 6: 80, 7: 80,
            8: 110, 9: 92, 10: 84, 11: 86, 12: 110,
            13: 116, 14: 110, 15: 108, 16: 56,
        }
        for col, w in _col_widths.items():
            self.master_table.setColumnWidth(col, w)
        self.master_table.verticalHeader().setDefaultSectionSize(58)
        for total_col in (7, 11, 12, 15):
            header_item = self.master_table.horizontalHeaderItem(total_col)
            if header_item is not None:
                f = header_item.font()
                f.setBold(True)
                header_item.setFont(f)

        root_layout.addWidget(self.master_table, 1)

    # ------------------------------------------------------------------
    # Refresh pipeline
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        self._remote_refresh_warning = ""
        if self.service.has_remote():
            try:
                self.service.refresh_from_remote()
            except MasterRemoteError as exc:
                if exc.status == 0:
                    self._remote_refresh_warning = "Pi 오프라인 (로컬 캐시 사용)"
                else:
                    self._remote_refresh_warning = f"Pi 동기화 실패 (HTTP {exc.status})"
        rows_by_channel: Dict[str, List[ChannelProduct]] = {
            "naver": self.cache.load_rows("naver"),
            "coupang": self.cache.load_rows("coupang"),
        }
        # 30일 판매량은 API 누적값 대신 Pi 재고차감 이벤트 기반 롤링 합계로 덮어쓴다.
        # Pi 미연결/오류 시 원본 API 값 유지.
        if self.monitor_url:
            totals = _fetch_rolling_sales_totals(self.monitor_url, days=30)
            if totals is not None:
                _apply_rolling_sales_override(rows_by_channel, totals)
            elif not self._remote_refresh_warning:
                self._remote_refresh_warning = "판매 집계 미동기화 (Pi /sales/totals 실패)"
        self._current_aggregation = self.service.aggregate(rows_by_channel)
        self._reconcile_stock_inbounds()
        self._reload_inbound_summaries()
        self._render_master_table()
        self._render_summary()

    def _reconcile_stock_inbounds(self) -> None:
        remote = self.service.remote
        if remote is None:
            self._remote_refresh_warning = "Pi 미연결: 입고 기능 비활성"
            return
        if self._current_aggregation is None:
            return
        items: List[Dict[str, int | str]] = []
        for row in self._current_aggregation.masters:
            if row.naver_stock is not None:
                items.append(
                    {
                        "master_id": int(row.master.id),
                        "channel": "naver",
                        "current_stock": int(row.naver_stock),
                    }
                )
            if row.coupang_stock is not None:
                items.append(
                    {
                        "master_id": int(row.master.id),
                        "channel": "coupang",
                        "current_stock": int(row.coupang_stock),
                    }
                )
        try:
            summaries = remote.reconcile_stock_inbounds(items)
        except MasterRemoteError as exc:
            if exc.status == 0:
                self._remote_refresh_warning = "Pi 오프라인: 입고 자동 차감 실패"
            else:
                self._remote_refresh_warning = f"Pi 입고 차감 실패 (HTTP {exc.status})"
            return
        self._inbound_summaries = {
            (int(row.master_id), str(row.channel).strip().lower()): row
            for row in summaries
        }

    def _reload_inbound_summaries(self) -> None:
        if self._inbound_summaries:
            return
        remote = self.service.remote
        if remote is None:
            return
        try:
            rows = remote.list_stock_inbound_summaries()
        except MasterRemoteError as exc:
            if exc.status == 0:
                self._remote_refresh_warning = "Pi 오프라인: 입고내역을 불러오지 못했습니다."
            else:
                self._remote_refresh_warning = f"Pi 입고 조회 실패 (HTTP {exc.status})"
            return
        self._inbound_summaries = {
            (int(row.master_id), str(row.channel).strip().lower()): row
            for row in rows
        }

    def _inbound_pending_qty(self, master_id: int, channel: str) -> int:
        row = self._inbound_summaries.get((int(master_id), str(channel).strip().lower()))
        return int(row.pending_qty) if row is not None else 0

    def _inbound_last_consumed_at(
        self,
        master_id: int,
        channel: str,
    ) -> Optional[datetime]:
        row = self._inbound_summaries.get((int(master_id), str(channel).strip().lower()))
        return row.last_consumed_at if row is not None else None

    def _render_summary(self) -> None:
        if self._current_aggregation is None:
            self.summary_label.setText("")
            return
        total_masters = len(self._current_aggregation.masters)
        unlinked_count = sum(
            len(rows)
            for rows in self._current_aggregation.unlinked_by_channel.values()
        )
        # 재고원가 합계: 단가 있는 마스터만 (단가 × 총재고) 합산
        total_stock_cost = 0
        for mr in self._current_aggregation.masters:
            unit_cost = mr.master.unit_cost
            stock = mr.total_stock
            if stock is not None:
                stock += self._inbound_pending_qty(mr.master.id, "naver")
                stock += self._inbound_pending_qty(mr.master.id, "coupang")
            if unit_cost is None or stock is None:
                continue
            total_stock_cost += int(unit_cost) * int(stock)
        # 오늘 판매금액: 각 채널 링크의 (오늘판매수량 × 채널 판매가) 채널별 합산
        naver_revenue = 0
        coupang_revenue = 0
        for mr in self._current_aggregation.masters:
            for link in mr.linked:
                if link.today_sales is None or link.price is None:
                    continue
                amount = int(link.today_sales) * int(link.price)
                if link.channel == "naver":
                    naver_revenue += amount
                elif link.channel == "coupang":
                    coupang_revenue += amount
        total_today_revenue = naver_revenue + coupang_revenue
        revenue_text = (
            f"오늘 판매금액 네이버 {naver_revenue:,}원 + 쿠팡 {coupang_revenue:,}원 "
            f"= {total_today_revenue:,}원"
        )
        cost_text = f"재고원가 합계 {total_stock_cost:,}원"
        inbound_total = sum(int(row.pending_qty) for row in self._inbound_summaries.values())
        warn = self._remote_refresh_warning or ""
        base = (
            f"{revenue_text}  |  {cost_text}  |  "
            f"마스터 {total_masters}개 · 미연결 채널상품 {unlinked_count}개"
        )
        if inbound_total > 0:
            base += (
                f"  |  입고대기 합계 {inbound_total:,}개"
            )
        self.summary_label.setText(f"{base}  |  ⚠ {warn}" if warn else base)

    def _render_master_table(self) -> None:
        if self._current_aggregation is None:
            self.master_table.setRowCount(0)
            return
        masters = self._current_aggregation.masters
        self.master_table.setSortingEnabled(False)
        self.master_table.setRowCount(len(masters))
        for row_idx, master_row in enumerate(masters):
            self._populate_master_row(row_idx, master_row)
        self.master_table.setSortingEnabled(True)

    def _populate_master_row(self, row_idx: int, master_row: MasterProductRow) -> None:
        master = master_row.master
        naver_inbound = self._inbound_pending_qty(master.id, "naver")
        coupang_inbound = self._inbound_pending_qty(master.id, "coupang")
        naver_consumed_at = self._inbound_last_consumed_at(master.id, "naver")
        coupang_consumed_at = self._inbound_last_consumed_at(master.id, "coupang")
        total_inbound = naver_inbound + coupang_inbound
        total_consumed_at = self._latest_consumed_at(naver_consumed_at, coupang_consumed_at)

        image_label = QLabel()
        image_label.setAlignment(Qt.AlignCenter)
        image_label.setFixedSize(48, 48)
        _assign_pixmap(image_label, master_row.image_url, 44, self._loader)
        self.master_table.setCellWidget(row_idx, 0, image_label)

        name_item = QTableWidgetItem(master.name)
        name_item.setData(Qt.UserRole, master.id)
        self.master_table.setItem(row_idx, 1, name_item)

        cost_text = _format_price(master.unit_cost) if master.unit_cost is not None else "-"
        self.master_table.setItem(row_idx, 2, _number_item(cost_text, master.unit_cost))
        self.master_table.setItem(
            row_idx, 3, _number_item(_format_price(master_row.naver_price), master_row.naver_price)
        )
        self.master_table.setItem(
            row_idx, 4, _number_item(_format_price(master_row.coupang_price), master_row.coupang_price)
        )
        self._set_stock_display_cell(
            row_idx, _COL_NAVER_STOCK, master_row.naver_stock, naver_inbound, naver_consumed_at
        )
        self._set_stock_display_cell(
            row_idx, _COL_COUPANG_STOCK, master_row.coupang_stock, coupang_inbound, coupang_consumed_at
        )
        self._set_stock_display_cell(
            row_idx, _COL_TOTAL_STOCK, master_row.total_stock, total_inbound, total_consumed_at
        )
        total_stock_for_cost = master_row.total_stock
        if total_stock_for_cost is not None:
            total_stock_for_cost += total_inbound
        elif total_inbound > 0:
            total_stock_for_cost = total_inbound
        if master.unit_cost is not None and total_stock_for_cost is not None:
            stock_cost = int(master.unit_cost) * int(total_stock_for_cost)
        else:
            stock_cost = None
        self.master_table.setItem(
            row_idx, _COL_STOCK_COST, _number_item(_format_price(stock_cost), stock_cost)
        )
        self.master_table.setItem(
            row_idx, 9, _number_item(_format_sales_int(master_row.naver_today_sales), master_row.naver_today_sales)
        )
        self.master_table.setItem(
            row_idx, 10, _number_item(_format_sales_int(master_row.coupang_today_sales), master_row.coupang_today_sales)
        )
        self.master_table.setItem(
            row_idx, 11, _number_item(_format_sales_int(master_row.total_today_sales), master_row.total_today_sales)
        )
        # 오늘 매출: 채널 링크별 (오늘판매수량 × 채널판매가) 합산.
        # 채널마다 가격이 달라 master_row 의 channel_today_sales 에 단일 가격 곱하면 부정확.
        today_revenue = 0
        for link in master_row.linked:
            if link.today_sales is None or link.price is None:
                continue
            today_revenue += int(link.today_sales) * int(link.price)
        self.master_table.setItem(
            row_idx,
            12,
            _number_item(
                _format_price(today_revenue) if today_revenue else "",
                today_revenue,
            ),
        )
        self.master_table.setItem(
            row_idx, 13, _number_item(_format_sales_int(master_row.naver_sales), master_row.naver_sales)
        )
        self.master_table.setItem(
            row_idx, 14, _number_item(_format_sales_int(master_row.coupang_sales), master_row.coupang_sales)
        )
        self.master_table.setItem(
            row_idx, 15, _number_item(_format_sales_int(master_row.total_sales), master_row.total_sales)
        )
        link_count = len(master_row.linked)
        self.master_table.setItem(row_idx, 16, _number_item(str(link_count), link_count))

    def _set_stock_display_cell(
        self,
        row_idx: int,
        col_idx: int,
        base_value: Optional[int],
        inbound_qty: int,
        _consumed_at: Optional[datetime],
    ) -> None:
        sort_value: Optional[int] = None
        if base_value is not None:
            sort_value = int(base_value) + max(0, int(inbound_qty))
        elif inbound_qty > 0:
            sort_value = int(inbound_qty)
        self.master_table.setItem(
            row_idx,
            col_idx,
            _number_item("", sort_value),
        )
        self.master_table.setCellWidget(
            row_idx,
            col_idx,
            _make_stock_cell_widget(base_value, inbound_qty),
        )

    @staticmethod
    def _latest_consumed_at(*values: Optional[datetime]) -> Optional[datetime]:
        present = [value for value in values if value is not None]
        if not present:
            return None
        return max(present)

    # ------------------------------------------------------------------
    # Table interaction
    # ------------------------------------------------------------------

    def _selected_master_id(self) -> Optional[int]:
        rows = self.master_table.selectionModel().selectedRows()
        if not rows:
            return None
        name_item = self.master_table.item(rows[0].row(), 1)
        if name_item is None:
            return None
        data = name_item.data(Qt.UserRole)
        return int(data) if data is not None else None

    def _on_row_double_clicked(self, index) -> None:
        if index is not None and index.column() in (_COL_NAVER_STOCK, _COL_COUPANG_STOCK):
            self._open_inbound_input(index.row(), index.column())
            return
        master_id = self._selected_master_id()
        if master_id is not None:
            self._open_detail_dialog(master_id)

    def _on_detail_button_clicked(self) -> None:
        master_id = self._selected_master_id()
        if master_id is None:
            QMessageBox.information(self, "선택 필요", "먼저 테이블에서 마스터를 선택하세요.")
            return
        self._open_detail_dialog(master_id)

    def _open_detail_dialog(self, master_id: int) -> None:
        if self._current_aggregation is None:
            return
        dlg = MasterDetailDialog(
            service=self.service,
            loader=self._loader,
            master_id=master_id,
            aggregation=self._current_aggregation,
            parent=self,
            monitor_url=self.monitor_url,
        )
        dlg.changed.connect(self._on_dialog_changed)
        dlg.exec()
        # 다이얼로그 닫힘 — 최신 집계로 refresh
        self.refresh()

    def _on_dialog_changed(self) -> None:
        self.masters_changed.emit()

    def _open_inbound_input(self, row_idx: int, col_idx: int) -> None:
        if self._current_aggregation is None or self.service.remote is None:
            QMessageBox.warning(
                self,
                "입고 저장 불가",
                "라즈베리파이 DB 연결이 없어 입고를 저장할 수 없습니다.",
            )
            return
        name_item = self.master_table.item(row_idx, 1)
        if name_item is None:
            return
        master_id = int(name_item.data(Qt.UserRole) or 0)
        master_row = self._find_master_row(master_id)
        if master_row is None:
            return
        channel = "naver" if col_idx == _COL_NAVER_STOCK else "coupang"
        channel_label = "네이버" if channel == "naver" else "쿠팡"
        try:
            entries = self.service.remote.list_stock_inbounds(
                master_id=master_id,
                channel=channel,
            )
        except MasterRemoteError as exc:
            msg = "Pi 통신 실패" if exc.status == 0 else f"Pi 입고 조회 실패 (HTTP {exc.status})"
            QMessageBox.critical(self, "입고 조회 실패", msg)
            return

        dlg = InboundManageDialog(master_row.master.name, channel_label, entries, self)
        result = dlg.exec()
        if result == 2 and dlg.delete_requested_id:
            try:
                self.service.remote.delete_stock_inbound(dlg.delete_requested_id)
            except MasterRemoteError as exc:
                msg = "Pi 통신 실패" if exc.status == 0 else f"Pi 삭제 실패 (HTTP {exc.status})"
                QMessageBox.critical(self, "입고 삭제 실패", msg)
                return
            self._inbound_summaries = {}
            self._reload_inbound_summaries()
            self._render_master_table()
            self._render_summary()
            return
        if result != QDialog.Accepted:
            return
        try:
            self.service.remote.add_stock_inbound(
                master_id=master_id,
                channel=channel,
                quantity=dlg.quantity,
                receipt_date=dlg.receipt_date,
            )
        except MasterRemoteError as exc:
            msg = "Pi 통신 실패" if exc.status == 0 else f"Pi 저장 실패 (HTTP {exc.status})"
            QMessageBox.critical(self, "입고 저장 실패", msg)
            return
        self._inbound_summaries = {}
        self._reload_inbound_summaries()
        self._render_master_table()
        self._render_summary()

    # ------------------------------------------------------------------
    # Create master
    # ------------------------------------------------------------------

    def _on_new_master(self) -> None:
        # 상세 다이얼로그를 create 모드로 바로 연다. 이름 입력 후 Enter/저장.
        if self._current_aggregation is None:
            return
        dlg = MasterDetailDialog(
            service=self.service,
            loader=self._loader,
            master_id=None,
            aggregation=self._current_aggregation,
            parent=self,
            monitor_url=self.monitor_url,
        )
        dlg.changed.connect(self._on_dialog_changed)
        dlg.exec()
        self.refresh()

    # ------------------------------------------------------------------
    # Image async update
    # ------------------------------------------------------------------

    @Slot(str, object)
    def _on_image_loaded(self, url: str, data) -> None:
        pm = self._loader.accept_loaded(url, data)
        if pm is None or self._current_aggregation is None:
            return
        # 테이블 내 해당 URL 참조 행 업데이트
        for row in range(self.master_table.rowCount()):
            widget = self.master_table.cellWidget(row, 0)
            if not isinstance(widget, QLabel):
                continue
            name_item = self.master_table.item(row, 1)
            if name_item is None:
                continue
            master_id = int(name_item.data(Qt.UserRole) or 0)
            mr = self._find_master_row(master_id)
            if mr and mr.image_url == url:
                widget.setPixmap(
                    pm.scaled(44, 44, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )

    def _find_master_row(self, master_id: int) -> Optional[MasterProductRow]:
        if self._current_aggregation is None:
            return None
        for mr in self._current_aggregation.masters:
            if mr.master.id == master_id:
                return mr
        return None

    # ------------------------------------------------------------------
    # External API
    # ------------------------------------------------------------------

    def open_master_for_channel(self, channel: str, product_key: str) -> None:
        """채널 탭에서 링크 직후 호출 — 해당 마스터 상세 팝업 띄움."""
        link = self.service.get_link(channel, product_key)
        if link is None:
            return
        self.refresh()
        self._open_detail_dialog(link.master_id)
