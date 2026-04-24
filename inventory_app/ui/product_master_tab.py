"""상품등록 탭.

사용자가 만드는 마스터 상품 카탈로그 + 채널 상품과의 연결 상태 관리.

책임:
- 마스터 CRUD (이름 / 원가 / 메모)
- 마스터에 연결된 채널 상품 목록 표시 (multiplier, 재고, 판매량)
- 대표 이미지 지정 (연결된 채널 상품 중 하나)
- 연결 해제 / multiplier 수정
- 전체 "미연결 채널 상품" 요약 (연결은 Channel 탭 우클릭 메뉴에서)
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from inventory_app.models import ChannelProduct, MasterProduct
from inventory_app.services.image_cache import get_image_bytes
from inventory_app.services.local_cache import ChannelProductCache
from inventory_app.services.master_product_service import (
    LinkedChannelView,
    MasterAggregation,
    MasterProductRow,
    MasterProductService,
)
from inventory_app.services.shared_stock_grouping import product_identity_key


_MASTER_COLS = [
    "이미지",
    "이름",
    "원가",
    "네이버재고",
    "쿠팡재고",
    "총재고",
    "재고원가",
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


def _format_int(value: Optional[int]) -> str:
    if value is None:
        return "-"
    return f"{int(value):,}"


def _format_price(value: Optional[int]) -> str:
    if value is None:
        return "-"
    return f"{int(value):,}원"


class _ImageSignals(QObject):
    loaded = Signal(str, object)  # url, bytes|None


class ProductMasterTab(QWidget):
    """상품등록 탭 위젯.

    외부 연동:
    - `masters_changed` 시그널: 마스터/링크가 수정되면 방출됨.
      MainWindow 가 재고관리/매출비교 등 다른 탭 새로고침을 걸 때 사용.
    """

    masters_changed = Signal()

    def __init__(self, cache: Optional[ChannelProductCache] = None) -> None:
        super().__init__()
        self.cache = cache or ChannelProductCache()
        self.service = MasterProductService(cache=self.cache)

        self._current_aggregation: MasterAggregation | None = None
        self._current_master_id: int | None = None

        self._image_cache: Dict[str, QPixmap] = {}
        self._image_pending: set[str] = set()
        self._image_executor = ThreadPoolExecutor(max_workers=4)
        self._image_signals = _ImageSignals()
        self._image_signals.loaded.connect(self._on_image_loaded)

        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

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
        self.summary_label = QLabel("")
        self.summary_label.setStyleSheet("color: #475569;")
        toolbar.addWidget(self.new_master_button)
        toolbar.addWidget(self.refresh_button)
        toolbar.addStretch(1)
        toolbar.addWidget(self.summary_label)
        root_layout.addLayout(toolbar)

        splitter = QSplitter(Qt.Horizontal)

        # --- Left: master table -----------------------------------------
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)

        self.master_table = QTableWidget(0, len(_MASTER_COLS))
        self.master_table.setHorizontalHeaderLabels(_MASTER_COLS)
        self.master_table.verticalHeader().setVisible(False)
        self.master_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.master_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.master_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.master_table.setAlternatingRowColors(True)
        self.master_table.itemSelectionChanged.connect(self._on_master_selected)
        header = self.master_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for idx in range(2, len(_MASTER_COLS)):
            header.setSectionResizeMode(idx, QHeaderView.ResizeToContents)
        self.master_table.setColumnWidth(0, 56)
        self.master_table.verticalHeader().setDefaultSectionSize(48)
        # 총합 컬럼은 굵게 강조
        for total_col in (5, 9):
            header_item = self.master_table.horizontalHeaderItem(total_col)
            if header_item is not None:
                f = header_item.font()
                f.setBold(True)
                header_item.setFont(f)

        left_layout.addWidget(self.master_table, 1)

        # --- Right: detail panel ----------------------------------------
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        header_row = QHBoxLayout()
        self.detail_image = QLabel()
        self.detail_image.setFixedSize(96, 96)
        self.detail_image.setStyleSheet(
            "border: 1px solid #cbd5e1; background: #f8fafc; border-radius: 6px;"
        )
        self.detail_image.setAlignment(Qt.AlignCenter)
        self.detail_image.setText("이미지 없음")

        header_form = QVBoxLayout()
        header_form.setSpacing(4)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("마스터 이름")
        self.cost_edit = QLineEdit()
        self.cost_edit.setPlaceholderText("원가 (숫자)")
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("이름"))
        name_row.addWidget(self.name_edit, 1)
        cost_row = QHBoxLayout()
        cost_row.addWidget(QLabel("원가"))
        cost_row.addWidget(self.cost_edit, 1)
        header_form.addLayout(name_row)
        header_form.addLayout(cost_row)

        header_row.addWidget(self.detail_image)
        header_row.addLayout(header_form, 1)
        right_layout.addLayout(header_row)

        right_layout.addWidget(QLabel("메모"))
        self.memo_edit = QPlainTextEdit()
        self.memo_edit.setFixedHeight(64)
        right_layout.addWidget(self.memo_edit)

        action_row = QHBoxLayout()
        action_row.setSpacing(6)
        self.save_master_button = QPushButton("저장")
        self.save_master_button.clicked.connect(self._on_save_master)
        self.delete_master_button = QPushButton("마스터 삭제")
        self.delete_master_button.clicked.connect(self._on_delete_master)
        action_row.addWidget(self.save_master_button)
        action_row.addWidget(self.delete_master_button)
        action_row.addStretch(1)
        right_layout.addLayout(action_row)

        separator = QFrame()
        separator.setFrameShape(QFrame.HLine)
        separator.setStyleSheet("color: #e2e8f0;")
        right_layout.addWidget(separator)

        right_layout.addWidget(QLabel("연결된 채널 상품"))
        self.links_table = QTableWidget(0, len(_LINK_COLS))
        self.links_table.setHorizontalHeaderLabels(_LINK_COLS)
        self.links_table.verticalHeader().setVisible(False)
        self.links_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.links_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.links_table.setAlternatingRowColors(True)
        links_header = self.links_table.horizontalHeader()
        links_header.setSectionResizeMode(1, QHeaderView.Stretch)
        for idx in (0, 2, 3, 4, 5, 6, 7, 8):
            links_header.setSectionResizeMode(idx, QHeaderView.ResizeToContents)
        right_layout.addWidget(self.links_table, 1)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([580, 780])
        root_layout.addWidget(splitter, 1)

        self._set_detail_enabled(False)

    # ------------------------------------------------------------------
    # Refresh pipeline
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """캐시에서 모든 마스터 + 링크 + 채널 raw rows 를 다시 읽어 집계."""
        rows_by_channel: Dict[str, List[ChannelProduct]] = {
            "naver": self.cache.load_rows("naver"),
            "coupang": self.cache.load_rows("coupang"),
        }
        self._current_aggregation = self.service.aggregate(rows_by_channel)
        self._render_master_table()
        self._render_summary()
        # 선택 유지
        if self._current_master_id is not None:
            self._select_master_in_table(self._current_master_id)
        else:
            self._clear_detail()

    def _render_summary(self) -> None:
        if self._current_aggregation is None:
            self.summary_label.setText("")
            return
        total_masters = len(self._current_aggregation.masters)
        unlinked_count = sum(
            len(rows)
            for rows in self._current_aggregation.unlinked_by_channel.values()
        )
        self.summary_label.setText(
            f"마스터 {total_masters}개 · 미연결 채널상품 {unlinked_count}개"
        )

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

        image_label = QLabel()
        image_label.setAlignment(Qt.AlignCenter)
        image_label.setFixedSize(48, 48)
        if master_row.image_url:
            self._assign_pixmap(image_label, master_row.image_url, 44)
        else:
            image_label.setText("-")
        self.master_table.setCellWidget(row_idx, 0, image_label)

        name_item = QTableWidgetItem(master.name)
        name_item.setData(Qt.UserRole, master.id)
        self.master_table.setItem(row_idx, 1, name_item)

        cost_text = _format_price(master.unit_cost) if master.unit_cost is not None else "-"
        self.master_table.setItem(row_idx, 2, self._number_item(cost_text, master.unit_cost))
        self.master_table.setItem(
            row_idx, 3, self._number_item(_format_int(master_row.naver_stock), master_row.naver_stock)
        )
        self.master_table.setItem(
            row_idx, 4, self._number_item(_format_int(master_row.coupang_stock), master_row.coupang_stock)
        )
        self.master_table.setItem(
            row_idx, 5, self._number_item(_format_int(master_row.total_stock), master_row.total_stock)
        )
        if master.unit_cost is not None and master_row.total_stock is not None:
            stock_cost = int(master.unit_cost) * int(master_row.total_stock)
        else:
            stock_cost = None
        self.master_table.setItem(
            row_idx, 6, self._number_item(_format_price(stock_cost), stock_cost)
        )
        self.master_table.setItem(
            row_idx, 7, self._number_item(_format_int(master_row.naver_sales), master_row.naver_sales)
        )
        self.master_table.setItem(
            row_idx, 8, self._number_item(_format_int(master_row.coupang_sales), master_row.coupang_sales)
        )
        self.master_table.setItem(
            row_idx, 9, self._number_item(_format_int(master_row.total_sales), master_row.total_sales)
        )
        link_count = len(master_row.linked)
        self.master_table.setItem(
            row_idx, 10, self._number_item(str(link_count), link_count)
        )

    @staticmethod
    def _number_item(text: str, sort_value) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        if sort_value is not None:
            item.setData(Qt.UserRole, sort_value)
        return item

    # ------------------------------------------------------------------
    # Selection + detail rendering
    # ------------------------------------------------------------------

    def _on_master_selected(self) -> None:
        rows = self.master_table.selectionModel().selectedRows()
        if not rows:
            self._current_master_id = None
            self._clear_detail()
            return
        row_idx = rows[0].row()
        name_item = self.master_table.item(row_idx, 1)
        if name_item is None:
            self._clear_detail()
            return
        master_id = name_item.data(Qt.UserRole)
        if master_id is None:
            self._clear_detail()
            return
        self._current_master_id = int(master_id)
        self._render_detail(self._current_master_id)

    def _find_master_row(self, master_id: int) -> MasterProductRow | None:
        if self._current_aggregation is None:
            return None
        for master_row in self._current_aggregation.masters:
            if master_row.master.id == master_id:
                return master_row
        return None

    def _select_master_in_table(self, master_id: int) -> None:
        for row in range(self.master_table.rowCount()):
            name_item = self.master_table.item(row, 1)
            if name_item is None:
                continue
            if int(name_item.data(Qt.UserRole) or 0) == master_id:
                self.master_table.selectRow(row)
                return
        self._current_master_id = None
        self._clear_detail()

    def _render_detail(self, master_id: int) -> None:
        master_row = self._find_master_row(master_id)
        if master_row is None:
            self._clear_detail()
            return
        master = master_row.master
        self._set_detail_enabled(True)
        self.name_edit.setText(master.name)
        self.cost_edit.setText("" if master.unit_cost is None else str(master.unit_cost))
        self.memo_edit.blockSignals(True)
        self.memo_edit.setPlainText(master.memo or "")
        self.memo_edit.blockSignals(False)

        if master_row.image_url:
            self._assign_pixmap(self.detail_image, master_row.image_url, 92, text_if_none="이미지 없음")
        else:
            self.detail_image.clear()
            self.detail_image.setText("이미지 없음")

        self._render_links(master_row)

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
        name_item.setData(Qt.UserRole, (link.channel, link.product_key))
        self.links_table.setItem(row_idx, 1, name_item)

        self.links_table.setItem(
            row_idx, 2, self._number_item(f"×{link.multiplier}", link.multiplier)
        )
        self.links_table.setItem(
            row_idx, 3, self._number_item(_format_int(link.stock), link.stock)
        )
        self.links_table.setItem(
            row_idx, 4, self._number_item(_format_int(link.sales), link.sales)
        )
        self.links_table.setItem(
            row_idx, 5, self._number_item(_format_int(link.today_sales), link.today_sales)
        )
        self.links_table.setItem(
            row_idx, 6, self._number_item(_format_price(link.price), link.price)
        )

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

    def _clear_detail(self) -> None:
        self._set_detail_enabled(False)
        self.name_edit.clear()
        self.cost_edit.clear()
        self.memo_edit.clear()
        self.detail_image.clear()
        self.detail_image.setText("이미지 없음")
        self.links_table.setRowCount(0)

    def _set_detail_enabled(self, enabled: bool) -> None:
        for widget in (
            self.name_edit,
            self.cost_edit,
            self.memo_edit,
            self.save_master_button,
            self.delete_master_button,
            self.links_table,
        ):
            widget.setEnabled(enabled)

    # ------------------------------------------------------------------
    # Master CRUD handlers
    # ------------------------------------------------------------------

    def _on_new_master(self) -> None:
        name, ok = QInputDialog.getText(self, "새 마스터", "마스터 이름:")
        if not ok:
            return
        name = str(name or "").strip()
        if not name:
            QMessageBox.warning(self, "이름 필요", "이름을 입력해주세요.")
            return
        try:
            master = self.service.create_master(name=name)
        except ValueError as exc:
            QMessageBox.warning(self, "실패", str(exc))
            return
        self._current_master_id = master.id
        self.refresh()
        self.masters_changed.emit()

    def _on_save_master(self) -> None:
        if self._current_master_id is None:
            return
        new_name = self.name_edit.text().strip()
        if not new_name:
            QMessageBox.warning(self, "이름 필요", "마스터 이름은 비워둘 수 없어요.")
            return
        cost_text = self.cost_edit.text().strip()
        cost_value: int | None
        clear_cost = False
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
            self.service.update_master(
                self._current_master_id,
                name=new_name,
                unit_cost=cost_value,
                clear_unit_cost=clear_cost,
                memo=memo_text if memo_text else None,
                clear_memo=not bool(memo_text),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "실패", str(exc))
            return
        self.refresh()
        self.masters_changed.emit()

    def _on_delete_master(self) -> None:
        if self._current_master_id is None:
            return
        master_row = self._find_master_row(self._current_master_id)
        name = master_row.master.name if master_row else str(self._current_master_id)
        reply = QMessageBox.question(
            self,
            "마스터 삭제",
            f"‘{name}’ 를 삭제하면 연결된 링크도 함께 제거됩니다. 계속할까요?",
        )
        if reply != QMessageBox.Yes:
            return
        self.service.delete_master(self._current_master_id)
        self._current_master_id = None
        self.refresh()
        self.masters_changed.emit()

    # ------------------------------------------------------------------
    # Link handlers
    # ------------------------------------------------------------------

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
        self.service.set_multiplier(channel, product_key, int(value))
        self.refresh()
        self.masters_changed.emit()

    def _on_set_representative(self, channel: str, product_key: str) -> None:
        if self._current_master_id is None:
            return
        self.service.set_representative(self._current_master_id, channel, product_key)
        self.refresh()
        self.masters_changed.emit()

    def _on_unlink(self, channel: str, product_key: str) -> None:
        reply = QMessageBox.question(
            self, "연결 해제", "이 채널 상품의 마스터 연결을 해제할까요?"
        )
        if reply != QMessageBox.Yes:
            return
        self.service.unlink(channel, product_key)
        self.refresh()
        self.masters_changed.emit()

    # ------------------------------------------------------------------
    # Image loading
    # ------------------------------------------------------------------

    def _assign_pixmap(
        self,
        label: QLabel,
        url: str,
        size: int,
        text_if_none: str = "-",
    ) -> None:
        pixmap = self._image_cache.get(url)
        if pixmap is not None and not pixmap.isNull():
            label.setPixmap(pixmap.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            return
        label.setText(text_if_none)
        if url in self._image_pending:
            return
        self._image_pending.add(url)
        self._image_executor.submit(self._fetch_image_worker, url)

    def _fetch_image_worker(self, url: str) -> None:
        data = None
        try:
            data = get_image_bytes(url)
        except Exception:  # noqa: BLE001
            data = None
        self._image_signals.loaded.emit(url, data)

    @Slot(str, object)
    def _on_image_loaded(self, url: str, data) -> None:
        self._image_pending.discard(url)
        if not data:
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            return
        self._image_cache[url] = pixmap
        # 현재 열려있는 detail 의 이미지 라벨 업데이트
        if self._current_aggregation is None:
            return
        master_row = (
            self._find_master_row(self._current_master_id)
            if self._current_master_id is not None
            else None
        )
        if master_row and master_row.image_url == url:
            self.detail_image.setPixmap(
                pixmap.scaled(92, 92, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        # 마스터 테이블의 이미지 라벨 찾아 업데이트
        for row in range(self.master_table.rowCount()):
            widget = self.master_table.cellWidget(row, 0)
            if not isinstance(widget, QLabel):
                continue
            name_item = self.master_table.item(row, 1)
            if name_item is None:
                continue
            master_id = int(name_item.data(Qt.UserRole) or 0)
            mrow = self._find_master_row(master_id)
            if mrow and mrow.image_url == url:
                widget.setPixmap(
                    pixmap.scaled(44, 44, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )

    # ------------------------------------------------------------------
    # External API
    # ------------------------------------------------------------------

    def open_master_for_channel(self, channel: str, product_key: str) -> None:
        """채널 탭에서 링크 직후 호출 — 해당 링크의 마스터로 포커스 이동."""
        link = self.service.get_link(channel, product_key)
        if link is None:
            return
        self._current_master_id = link.master_id
        self.refresh()
