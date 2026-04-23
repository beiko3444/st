"""파스토 풀필먼트 운영 콘솔 탭.

기존 main_window.py의 탭 패턴을 따라 단일 QWidget으로 구성합니다.
내부에 5개 서브탭 (개요/상품/재고/입고/출고)을 가집니다.

네트워크 호출은 모두 QThread 워커(FasstoJob)로 위임하여 UI를 블록하지 않습니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable, List, Optional, Sequence

from PySide6.QtCore import QDate, QObject, QThread, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateEdit,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from inventory_app.config import AppConfig
from inventory_app.connectors.fassto import (
    FasstoApiError,
    FasstoConnector,
    extract_fassto_list,
    normalize_fassto_deliveries,
    normalize_fassto_goods,
    normalize_fassto_stocks,
)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


@dataclass
class JobResult:
    ok: bool
    data: Any = None
    error: Optional[str] = None


class FasstoJob(QObject):
    """QThread 안에서 돌릴 단일 호출 워커."""

    finished = Signal(object)  # JobResult

    def __init__(self, func: Callable[[], Any]) -> None:
        super().__init__()
        self._func = func

    def run(self) -> None:
        try:
            data = self._func()
            self.finished.emit(JobResult(ok=True, data=data))
        except FasstoApiError as exc:
            self.finished.emit(
                JobResult(
                    ok=False,
                    error=f"[{exc.error_code or exc.status}] {exc}",
                    data=exc.to_dict(),
                )
            )
        except Exception as exc:  # noqa: BLE001
            self.finished.emit(JobResult(ok=False, error=str(exc)))


def _run_async(
    parent: QObject, func: Callable[[], Any], on_done: Callable[[JobResult], None]
) -> None:
    thread = QThread(parent)
    worker = FasstoJob(func)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)

    def _cleanup(result: JobResult) -> None:
        try:
            on_done(result)
        finally:
            thread.quit()
            thread.wait()
            worker.deleteLater()
            thread.deleteLater()

    worker.finished.connect(_cleanup)
    thread.start()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_api_date(d: date) -> str:
    """Fassto API 는 경로 파라미터에 YYYY-MM-DD 형식을 요구 (Swagger 스펙)."""
    return d.strftime("%Y-%m-%d")


def _default_range() -> tuple[date, date]:
    today = date.today()
    return today - timedelta(days=7), today


def _fill_table(
    table: QTableWidget,
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
) -> None:
    table.clear()
    table.setColumnCount(len(headers))
    table.setHorizontalHeaderLabels(list(headers))
    table.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            item = QTableWidgetItem("" if value is None else str(value))
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            table.setItem(r, c, item)
    table.resizeColumnsToContents()


# ---------------------------------------------------------------------------
# Sub-tabs
# ---------------------------------------------------------------------------


class _OverviewSubTab(QWidget):
    def __init__(self, tab: "FasstoTab") -> None:
        super().__init__()
        self._tab = tab
        layout = QVBoxLayout(self)

        self.status_label = QLabel("—")
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("fasstoStatusLabel")

        self.config_box = QGroupBox("설정 상태")
        config_form = QFormLayout(self.config_box)
        self.lbl_api_url = QLabel("-")
        self.lbl_cst_cd = QLabel("-")
        self.lbl_configured = QLabel("-")
        config_form.addRow("API URL", self.lbl_api_url)
        config_form.addRow("고객사 코드", self.lbl_cst_cd)
        config_form.addRow("설정 완료", self.lbl_configured)

        self.remote_box = QGroupBox("파스토 원격 요약")
        remote_form = QFormLayout(self.remote_box)
        self.lbl_goods_count = QLabel("-")
        self.lbl_stock_rows = QLabel("-")
        remote_form.addRow("상품 수", self.lbl_goods_count)
        remote_form.addRow("재고 레코드 수", self.lbl_stock_rows)

        btn_row = QHBoxLayout()
        self.refresh_btn = QPushButton("연결 테스트 & 요약 조회")
        self.refresh_btn.clicked.connect(self._refresh)
        btn_row.addWidget(self.refresh_btn)
        btn_row.addStretch(1)

        layout.addWidget(self.status_label)
        layout.addLayout(btn_row)
        layout.addWidget(self.config_box)
        layout.addWidget(self.remote_box)
        layout.addStretch(1)

        self._render_config()

    def _render_config(self) -> None:
        summary = self._tab.config_summary()
        self.lbl_api_url.setText(str(summary.get("apiUrl") or "-"))
        self.lbl_cst_cd.setText(str(summary.get("cstCd") or "-"))
        self.lbl_configured.setText("예" if summary.get("configured") else "아니오")

    def _refresh(self) -> None:
        self._render_config()
        if not self._tab.is_configured():
            self.status_label.setText(
                "❌ 설정이 비어 있습니다. credentials.json 의 fassto 섹션을 확인하세요."
            )
            return

        self.status_label.setText("조회 중...")
        self.refresh_btn.setEnabled(False)

        def work() -> dict:
            goods_env = self._tab.connector.get_goods_list()
            stock_env = self._tab.connector.get_stock_list()
            return {
                "goods": len(extract_fassto_list(goods_env)),
                "stocks": len(extract_fassto_list(stock_env)),
            }

        def done(result: JobResult) -> None:
            self.refresh_btn.setEnabled(True)
            if not result.ok:
                self.status_label.setText(f"❌ 실패: {result.error}")
                self.lbl_goods_count.setText("-")
                self.lbl_stock_rows.setText("-")
                return
            self.status_label.setText("✅ 파스토 연결 성공")
            self.lbl_goods_count.setText(str(result.data["goods"]))
            self.lbl_stock_rows.setText(str(result.data["stocks"]))

        _run_async(self, work, done)


class _GoodsSubTab(QWidget):
    COLUMNS = ("cstGodCd", "godNm", "godType", "giftDiv", "barcode", "useYn")

    def __init__(self, tab: "FasstoTab") -> None:
        super().__init__()
        self._tab = tab
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        self.refresh_btn = QPushButton("상품 목록 조회")
        self.refresh_btn.clicked.connect(self._refresh)
        self.status = QLabel("")
        top.addWidget(self.refresh_btn)
        top.addWidget(self.status, 1)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSortingEnabled(True)

        layout.addLayout(top)
        layout.addWidget(self.table, 1)

    def _refresh(self) -> None:
        if not self._tab.require_configured(self):
            return
        self.status.setText("조회 중...")
        self.refresh_btn.setEnabled(False)

        def work() -> list:
            env = self._tab.connector.get_goods_list()
            return normalize_fassto_goods(extract_fassto_list(env))

        def done(result: JobResult) -> None:
            self.refresh_btn.setEnabled(True)
            if not result.ok:
                self.status.setText(f"❌ {result.error}")
                return
            rows = [
                (r.cstGodCd, r.godNm, r.godType, r.giftDiv, r.barcode or "", r.useYn or "")
                for r in result.data
            ]
            _fill_table(self.table, self.COLUMNS, rows)
            self.status.setText(f"총 {len(rows)}건")

        _run_async(self, work, done)


class _StockSubTab(QWidget):
    COLUMNS = ("cstGodCd", "stockQty", "canStockQty", "badStockQty", "goodsSerialNo")

    def __init__(self, tab: "FasstoTab") -> None:
        super().__init__()
        self._tab = tab
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        self.refresh_btn = QPushButton("재고 조회")
        self.refresh_btn.clicked.connect(self._refresh)
        self.status = QLabel("기준: 가용재고 canStockQty")
        top.addWidget(self.refresh_btn)
        top.addWidget(self.status, 1)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSortingEnabled(True)

        layout.addLayout(top)
        layout.addWidget(self.table, 1)

    def _refresh(self) -> None:
        if not self._tab.require_configured(self):
            return
        self.status.setText("조회 중...")
        self.refresh_btn.setEnabled(False)

        def work() -> list:
            env = self._tab.connector.get_stock_list()
            return normalize_fassto_stocks(extract_fassto_list(env))

        def done(result: JobResult) -> None:
            self.refresh_btn.setEnabled(True)
            if not result.ok:
                self.status.setText(f"❌ {result.error}")
                return
            rows = [
                (
                    r.cstGodCd,
                    int(r.stockQty) if r.stockQty == int(r.stockQty) else r.stockQty,
                    int(r.canStockQty) if r.canStockQty == int(r.canStockQty) else r.canStockQty,
                    int(r.badStockQty) if r.badStockQty == int(r.badStockQty) else r.badStockQty,
                    r.goodsSerialNo or "",
                )
                for r in result.data
            ]
            _fill_table(self.table, self.COLUMNS, rows)
            self.status.setText(f"총 {len(rows)}건 (기준: canStockQty)")

        _run_async(self, work, done)


class _WarehousingSubTab(QWidget):
    COLUMNS_LIST = ("slipNo", "ordDt", "status", "statusNm", "custNm", "raw_summary")

    def __init__(self, tab: "FasstoTab") -> None:
        super().__init__()
        self._tab = tab
        layout = QVBoxLayout(self)

        start_d, end_d = _default_range()

        top = QHBoxLayout()
        self.start_edit = QDateEdit(QDate(start_d.year, start_d.month, start_d.day))
        self.start_edit.setCalendarPopup(True)
        self.start_edit.setDisplayFormat("yyyy-MM-dd")
        self.end_edit = QDateEdit(QDate(end_d.year, end_d.month, end_d.day))
        self.end_edit.setCalendarPopup(True)
        self.end_edit.setDisplayFormat("yyyy-MM-dd")
        self.refresh_btn = QPushButton("기간 조회")
        self.refresh_btn.clicked.connect(self._refresh_list)
        self.slip_edit = QLineEdit()
        self.slip_edit.setPlaceholderText("전표번호(slipNo) 입력")
        self.detail_btn = QPushButton("상세 조회")
        self.detail_btn.clicked.connect(self._refresh_detail)
        self.status = QLabel("")

        top.addWidget(QLabel("시작"))
        top.addWidget(self.start_edit)
        top.addWidget(QLabel("종료"))
        top.addWidget(self.end_edit)
        top.addWidget(self.refresh_btn)
        top.addSpacing(16)
        top.addWidget(self.slip_edit, 1)
        top.addWidget(self.detail_btn)
        top.addWidget(self.status)

        self.table = QTableWidget(0, len(self.COLUMNS_LIST))
        self.table.setHorizontalHeaderLabels(self.COLUMNS_LIST)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSortingEnabled(True)
        self.table.cellClicked.connect(self._on_row_clicked)

        self.detail_view = QTextEdit()
        self.detail_view.setReadOnly(True)
        self.detail_view.setPlaceholderText("전표를 선택하거나 번호 입력 후 상세 조회")

        layout.addLayout(top)
        layout.addWidget(self.table, 2)
        layout.addWidget(QLabel("상세"))
        layout.addWidget(self.detail_view, 1)

    def _refresh_list(self) -> None:
        if not self._tab.require_configured(self):
            return
        start = _fmt_api_date(self.start_edit.date().toPython())
        end = _fmt_api_date(self.end_edit.date().toPython())
        self.status.setText("조회 중...")
        self.refresh_btn.setEnabled(False)

        def work() -> list:
            env = self._tab.connector.get_warehousing_list(start, end)
            return extract_fassto_list(env)

        def done(result: JobResult) -> None:
            self.refresh_btn.setEnabled(True)
            if not result.ok:
                self.status.setText(f"❌ {result.error}")
                return
            rows = []
            for row in result.data:
                if not isinstance(row, dict):
                    continue
                slip = row.get("slipNo") or row.get("fmsSlipNo") or ""
                ord_dt = row.get("ordDt") or row.get("inDt") or ""
                status = row.get("status") or row.get("crgSt") or ""
                status_nm = row.get("statusNm") or row.get("crgStNm") or ""
                cust = row.get("custNm") or row.get("supplierNm") or ""
                rows.append((slip, ord_dt, status, status_nm, cust, ", ".join(str(k) for k in row.keys())))
            _fill_table(self.table, self.COLUMNS_LIST, rows)
            self.status.setText(f"총 {len(rows)}건")

        _run_async(self, work, done)

    def _on_row_clicked(self, row: int, _col: int) -> None:
        item = self.table.item(row, 0)
        if item is not None:
            self.slip_edit.setText(item.text())

    def _refresh_detail(self) -> None:
        if not self._tab.require_configured(self):
            return
        slip = self.slip_edit.text().strip()
        if not slip:
            QMessageBox.information(self, "안내", "전표번호를 입력하세요.")
            return
        self.detail_view.setPlainText("조회 중...")
        self.detail_btn.setEnabled(False)

        def work() -> Any:
            return self._tab.connector.get_warehousing_detail(slip)

        def done(result: JobResult) -> None:
            self.detail_btn.setEnabled(True)
            if not result.ok:
                self.detail_view.setPlainText(f"❌ {result.error}")
                return
            import json as _json
            self.detail_view.setPlainText(_json.dumps(result.data, ensure_ascii=False, indent=2))

        _run_async(self, work, done)


class _DeliverySubTab(QWidget):
    COLUMNS = (
        "slipNo",
        "ordNo",
        "ordDt",
        "status",
        "statusNm",
        "outDiv",
        "custNm",
        "invoiceNo",
        "parcelNm",
    )

    def __init__(self, tab: "FasstoTab") -> None:
        super().__init__()
        self._tab = tab
        layout = QVBoxLayout(self)

        start_d, end_d = _default_range()
        top = QHBoxLayout()
        self.start_edit = QDateEdit(QDate(start_d.year, start_d.month, start_d.day))
        self.start_edit.setCalendarPopup(True)
        self.start_edit.setDisplayFormat("yyyy-MM-dd")
        self.end_edit = QDateEdit(QDate(end_d.year, end_d.month, end_d.day))
        self.end_edit.setCalendarPopup(True)
        self.end_edit.setDisplayFormat("yyyy-MM-dd")

        self.status_combo = QComboBox()
        # Swagger 스펙: ALL | ORDER | WORKING | DONE | PARTDONE | CANCEL | SHORTAGE
        self.status_combo.addItems(
            ["ALL", "ORDER", "WORKING", "DONE", "PARTDONE", "CANCEL", "SHORTAGE"]
        )
        self.status_combo.setEditable(True)

        self.out_div_combo = QComboBox()
        # Swagger 스펙: 1(Parcel) | 2(Vehicle) | COUPANG | ONE_DAY
        self.out_div_combo.addItems(["1", "2", "COUPANG", "ONE_DAY"])
        self.out_div_combo.setEditable(True)

        self.refresh_btn = QPushButton("조회")
        self.refresh_btn.clicked.connect(self._refresh)
        self.status = QLabel("")

        top.addWidget(QLabel("시작"))
        top.addWidget(self.start_edit)
        top.addWidget(QLabel("종료"))
        top.addWidget(self.end_edit)
        top.addWidget(QLabel("상태"))
        top.addWidget(self.status_combo)
        top.addWidget(QLabel("출고구분"))
        top.addWidget(self.out_div_combo)
        top.addWidget(self.refresh_btn)
        top.addWidget(self.status, 1)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSortingEnabled(True)

        layout.addLayout(top)
        layout.addWidget(self.table, 1)

    def _refresh(self) -> None:
        if not self._tab.require_configured(self):
            return
        start = _fmt_api_date(self.start_edit.date().toPython())
        end = _fmt_api_date(self.end_edit.date().toPython())
        status = (self.status_combo.currentText() or "ALL").strip() or "ALL"
        out_div = (self.out_div_combo.currentText() or "1").strip() or "1"

        self.status.setText("조회 중...")
        self.refresh_btn.setEnabled(False)

        def work() -> list:
            env = self._tab.connector.get_delivery_list(start, end, status, out_div)
            return normalize_fassto_deliveries(extract_fassto_list(env))

        def done(result: JobResult) -> None:
            self.refresh_btn.setEnabled(True)
            if not result.ok:
                self.status.setText(f"❌ {result.error}")
                return
            rows = [
                (
                    r.slipNo,
                    r.ordNo,
                    r.ordDt,
                    r.status,
                    r.statusNm,
                    r.outDiv,
                    r.custNm,
                    r.invoiceNo or "",
                    r.parcelNm or "",
                )
                for r in result.data
            ]
            _fill_table(self.table, self.COLUMNS, rows)
            self.status.setText(f"총 {len(rows)}건")

        _run_async(self, work, done)


# ---------------------------------------------------------------------------
# Public tab
# ---------------------------------------------------------------------------


class FasstoTab(QWidget):
    """파스토 풀필먼트 운영 콘솔 (상위 탭)."""

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._config = config
        self.connector = FasstoConnector(
            api_cd=config.fassto_api_cd,
            api_key=config.fassto_api_key,
            cst_cd=config.fassto_cst_cd,
            api_url=config.fassto_api_url or "https://fmsapi.fassto.ai",
            timeout_seconds=config.timeout_seconds,
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self._inner = QTabWidget()
        self._overview = _OverviewSubTab(self)
        self._goods = _GoodsSubTab(self)
        self._stock = _StockSubTab(self)
        self._warehousing = _WarehousingSubTab(self)
        self._delivery = _DeliverySubTab(self)

        self._inner.addTab(self._overview, "개요")
        self._inner.addTab(self._goods, "상품")
        self._inner.addTab(self._stock, "재고")
        self._inner.addTab(self._warehousing, "입고")
        self._inner.addTab(self._delivery, "출고")

        layout.addWidget(self._inner, 1)

    # --- helpers exposed to sub-tabs ------------------------------------

    def is_configured(self) -> bool:
        return self.connector.is_configured()

    def config_summary(self) -> dict:
        return self.connector.config_summary()

    def require_configured(self, widget: QWidget) -> bool:
        if self.connector.is_configured():
            return True
        QMessageBox.warning(
            widget,
            "파스토 설정 필요",
            "credentials.json 의 fassto 섹션(api_cd/api_key/cst_cd)을 설정하세요.",
        )
        return False

    def closeEvent(self, event) -> None:  # type: ignore[override]
        try:
            self.connector.close()
        finally:
            super().closeEvent(event)


__all__ = ["FasstoTab"]
