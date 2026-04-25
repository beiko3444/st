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

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
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
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.loader = loader
        self.master_id = master_id  # None = create mode
        self._aggregation = aggregation
        self._dirty = False
        self.loader.signals.loaded.connect(self._on_image_loaded)
        self.setWindowTitle(
            "새 마스터 상품" if master_id is None else "마스터 상품 상세"
        )
        self.resize(820, 640)
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
        layout.addWidget(self.links_table, 1)

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
        self.master_table.verticalHeader().setDefaultSectionSize(48)
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
        self._render_master_table()
        self._render_summary()

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
        warn = self._remote_refresh_warning or ""
        base = (
            f"{revenue_text}  |  {cost_text}  |  "
            f"마스터 {total_masters}개 · 미연결 채널상품 {unlinked_count}개"
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
        self.master_table.setItem(
            row_idx, 5, _number_item(_format_int(master_row.naver_stock), master_row.naver_stock)
        )
        self.master_table.setItem(
            row_idx, 6, _number_item(_format_int(master_row.coupang_stock), master_row.coupang_stock)
        )
        self.master_table.setItem(
            row_idx, 7, _number_item(_format_int(master_row.total_stock), master_row.total_stock)
        )
        if master.unit_cost is not None and master_row.total_stock is not None:
            stock_cost = int(master.unit_cost) * int(master_row.total_stock)
        else:
            stock_cost = None
        self.master_table.setItem(
            row_idx, 8, _number_item(_format_price(stock_cost), stock_cost)
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

    def _on_row_double_clicked(self, _index) -> None:
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
        )
        dlg.changed.connect(self._on_dialog_changed)
        dlg.exec()
        # 다이얼로그 닫힘 — 최신 집계로 refresh
        self.refresh()

    def _on_dialog_changed(self) -> None:
        self.masters_changed.emit()

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
