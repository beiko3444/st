"""파스토 풀필먼트 운영 콘솔 탭.

서브탭 구성:
  - 개요
  - 상품 (마진/검색/CSV)
  - 세트상품 (부모-자식 2단)
  - 재고 (안전재고 경보, 하이라이트)
  - 입고 (날짜 프리셋, 상세 품목)
  - 출고 (상태 색상, 상세 품목)
  - 택배출고 (지연/배송누락 강조)
  - 매출 상세 (요약 + TOP 상품 + 일별 추이)

모든 네트워크 호출은 QThread 워커(FasstoJob)로 위임합니다.
모든 테이블은 숫자 정렬 지원(SortableItem), CSV 내보내기 제공.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from PySide6.QtCore import QDate, QObject, QSettings, QThread, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
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
    FasstoDeliveryRow,
    FasstoDeliveryGoodDetailRow,
    FasstoGoodsElementRow,
    FasstoGoodsRow,
    FasstoStockRow,
    FasstoWarehousingRow,
    build_warehousing_payload,
    extract_fassto_list,
    normalize_fassto_deliveries,
    normalize_fassto_delivery_good_details,
    normalize_fassto_delivery_parcels,
    normalize_fassto_goods,
    normalize_fassto_goods_elements,
    normalize_fassto_stocks,
    normalize_fassto_warehousings,
    summarize_delivery_good_details,
    warehousing_cancel_check,
    warehousing_status_name,
)


_SETTINGS_ORG = "SmartInventory"
_SETTINGS_APP = "SmartInventory"
_FASSTO_LAST_IN_WAY_KEY = "fassto/last_in_way"
_FASSTO_LAST_WH_CD_KEY = "fassto/last_wh_cd"
_FASSTO_LAST_SUP_CD_KEY = "fassto/last_sup_cd"
_FASSTO_DEFAULT_IN_WAY = ("01", "택배")
_FASSTO_DEFAULT_WH = ("YI21", "용인2센터 1층")
_FASSTO_DEFAULT_SUP = ("99999999", "미지정 공급사")
_FASSTO_WEB_URL = "https://fms.fassto.ai"


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


@dataclass
class JobResult:
    ok: bool
    data: Any = None
    error: Optional[str] = None


class FasstoJob(QObject):
    finished = Signal(object)

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
                    error=_format_fassto_error(str(exc), exc.to_dict()),
                    data=exc.to_dict(),
                )
            )
        except Exception as exc:  # noqa: BLE001
            self.finished.emit(JobResult(ok=False, error=str(exc)))


class _AsyncDispatcher(QObject):
    """parent 스레드(보통 메인 UI 스레드)에서 on_done 을 실행하기 위한 헬퍼.

    QThread 안에서 callable closure 로 직접 connect 하면 DirectConnection
    이 적용돼 on_done 이 워커 스레드에서 실행되고, 그 안에서 thread.wait()
    가 호출되면 자기 자신을 기다리며 데드락/크래시가 발생함. parent 가 가진
    QObject 를 슬롯 홀더로 두면 AutoConnection 이 QueuedConnection 으로
    승격돼 on_done 이 메인 스레드에서 안전하게 실행된다.
    """

    handled = Signal()

    def __init__(
        self,
        parent: QObject,
        on_done: Callable[["JobResult"], None],
    ) -> None:
        super().__init__(parent)
        self._on_done = on_done

    @Slot(object)
    def handle(self, result: "JobResult") -> None:
        try:
            self._on_done(result)
        except Exception as exc:  # noqa: BLE001
            # UI 콜백 자체에서 실수가 나도 앱이 죽지 않게 한다.
            print(f"[fassto async on_done error] {exc}")
        finally:
            self.handled.emit()


def _run_async(
    parent: QObject, func: Callable[[], Any], on_done: Callable[[JobResult], None]
) -> None:
    thread = QThread(parent)
    worker = FasstoJob(func)
    worker.moveToThread(thread)
    dispatcher = _AsyncDispatcher(parent, on_done)
    active_jobs = getattr(parent, "_fassto_async_jobs", None)
    if active_jobs is None:
        active_jobs = []
        setattr(parent, "_fassto_async_jobs", active_jobs)
    job_ref = (thread, worker, dispatcher)
    active_jobs.append(job_ref)

    def cleanup() -> None:
        try:
            active_jobs.remove(job_ref)
        except ValueError:
            pass
        dispatcher.deleteLater()

    thread.started.connect(worker.run)
    # 명시적 QueuedConnection — 어떤 환경(번들 vs 개발)이든 worker(작업 스레드)
    # 에서 emit 된 신호가 메인 스레드 이벤트 루프에 큐잉되도록 강제.
    worker.finished.connect(dispatcher.handle, Qt.QueuedConnection)
    worker.finished.connect(thread.quit, Qt.QueuedConnection)
    worker.finished.connect(worker.deleteLater)
    dispatcher.handled.connect(cleanup)
    thread.finished.connect(thread.deleteLater)
    thread.start()


# ---------------------------------------------------------------------------
# Formatting / color
# ---------------------------------------------------------------------------


def _fmt_api_date(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def _month_range() -> tuple[date, date]:
    today = date.today()
    return today - timedelta(days=30), today


def _fmt_num(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if f == int(f):
        return f"{int(f):,}"
    return f"{f:,.2f}"


def _fmt_money(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{int(round(f)):,}"


def _fmt_yyyymmdd(value: Any) -> str:
    s = str(value or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return s


# Colors
_BG_DANGER = QColor(255, 220, 220)
_BG_WARN = QColor(255, 240, 205)
_BG_OK = QColor(225, 245, 225)
_BG_INFO = QColor(225, 235, 255)
_FG_DANGER = QColor(180, 0, 0)
_FG_WARN = QColor(170, 110, 0)
_FG_OK = QColor(0, 115, 50)
_FG_MUTED = QColor(120, 120, 120)


# ---------------------------------------------------------------------------
# Table helpers: Cell + SortableItem + _fill_table + CSV export
# ---------------------------------------------------------------------------


@dataclass
class Cell:
    """Enhanced cell: text + optional numeric sort key + optional colors."""

    text: str = ""
    sort_key: Any = None
    fg: Optional[QColor] = None
    bg: Optional[QColor] = None
    align: Optional[int] = None


class _SortableItem(QTableWidgetItem):
    """Sort by UserRole numeric value if present, else fallback to string."""

    def __lt__(self, other: QTableWidgetItem) -> bool:
        a = self.data(Qt.UserRole)
        b = other.data(Qt.UserRole)
        if a is not None and b is not None:
            try:
                return float(a) < float(b)
            except (TypeError, ValueError):
                pass
        return self.text() < other.text()


def _make_item(value: Any) -> QTableWidgetItem:
    if isinstance(value, Cell):
        item = _SortableItem(value.text)
        if value.sort_key is not None:
            item.setData(Qt.UserRole, value.sort_key)
        if value.fg is not None:
            item.setForeground(value.fg)
        if value.bg is not None:
            item.setBackground(value.bg)
        if value.align is not None:
            item.setTextAlignment(value.align)
    else:
        item = QTableWidgetItem("" if value is None else str(value))
    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
    return item


def _fill_table(
    table: QTableWidget,
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
) -> None:
    was_sorting = table.isSortingEnabled()
    table.setSortingEnabled(False)
    table.clear()
    table.setColumnCount(len(headers))
    table.setHorizontalHeaderLabels(list(headers))
    table.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            table.setItem(r, c, _make_item(value))
    table.resizeColumnsToContents()
    table.setSortingEnabled(was_sorting)


def _num_cell(value: float, formatter: Callable[[Any], str] = _fmt_num, **kw: Any) -> Cell:
    return Cell(text=formatter(value), sort_key=float(value or 0), **kw)


def _export_table_to_csv(
    table: QTableWidget, parent: QWidget, default_name: str
) -> None:
    if table.rowCount() == 0:
        QMessageBox.information(parent, "안내", "저장할 데이터가 없습니다. 먼저 조회하세요.")
        return
    path, _ = QFileDialog.getSaveFileName(
        parent,
        "CSV 저장",
        f"{default_name}_{date.today().isoformat()}.csv",
        "CSV Files (*.csv)",
    )
    if not path:
        return
    try:
        headers = []
        for c in range(table.columnCount()):
            h = table.horizontalHeaderItem(c)
            headers.append(h.text() if h else "")
        # Excel 한글 호환을 위해 utf-8-sig
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(headers)
            for r in range(table.rowCount()):
                row = []
                for c in range(table.columnCount()):
                    item = table.item(r, c)
                    row.append(item.text() if item else "")
                w.writerow(row)
        QMessageBox.information(
            parent, "저장 완료", f"{path}\n{table.rowCount()}건 저장"
        )
    except Exception as exc:  # noqa: BLE001
        QMessageBox.warning(parent, "저장 실패", str(exc))


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _date_to_api_yyyymmdd(edit: QDateEdit) -> str:
    return edit.date().toString("yyyyMMdd")


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _item_text(table: QTableWidget, row: int, col: int) -> str:
    item = table.item(row, col)
    return item.text().strip() if item is not None else ""


def _set_item(table: QTableWidget, row: int, col: int, text: Any) -> None:
    table.setItem(row, col, QTableWidgetItem("" if text is None else str(text)))


def _serial_list(text: str) -> List[str]:
    return [part.strip() for part in text.replace("\n", ",").split(",") if part.strip()]


def _goods_label(row: FasstoGoodsRow) -> str:
    bits = [row.cstGodCd]
    if row.godNm:
        bits.append(row.godNm)
    if row.barcode:
        bits.append(row.barcode)
    return " · ".join(bits)


def _unique_options(rows: Sequence[Any], code_attr: str, name_attr: str) -> List[tuple[str, str]]:
    seen: set[str] = set()
    options: List[tuple[str, str]] = []
    for row in rows:
        code = _clean_text(getattr(row, code_attr, ""))
        name = _clean_text(getattr(row, name_attr, ""))
        if not code or code in seen:
            continue
        seen.add(code)
        options.append((code, name))
    return options


def _merge_options(*groups: Sequence[tuple[str, str]]) -> List[tuple[str, str]]:
    seen: set[str] = set()
    options: List[tuple[str, str]] = []
    for group in groups:
        for code, name in group:
            code = _clean_text(code)
            if not code or code in seen:
                continue
            seen.add(code)
            options.append((code, _clean_text(name)))
    return options


def _combo_with_options(options: Sequence[tuple[str, str]], current: str = "") -> QComboBox:
    combo = QComboBox()
    combo.setEditable(True)
    combo.addItem("", "")
    for code, name in options:
        label = f"{name} ({code})" if name else code
        combo.addItem(label, code)
    if current:
        index = combo.findData(current)
        if index >= 0:
            combo.setCurrentIndex(index)
        else:
            combo.setEditText(current)
    return combo


def _combo_value(combo: QComboBox) -> str:
    data = combo.currentData()
    if isinstance(data, str) and data:
        return data.strip()
    text = combo.currentText().strip()
    if text.endswith(")") and "(" in text:
        return text.rsplit("(", 1)[1][:-1].strip()
    return text


def _combo_display(combo: QComboBox) -> str:
    value = _combo_value(combo)
    text = combo.currentText().strip()
    if text.endswith(")") and "(" in text:
        name = text.rsplit("(", 1)[0].strip()
        if name:
            return f"{name} ({value})"
    return value or "-"


def _default_request_no(prefix: str) -> str:
    return f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def _select_or_add_combo_value(combo: QComboBox, code: str, name: str = "") -> None:
    code = _clean_text(code)
    if not code:
        return
    index = combo.findData(code)
    if index < 0:
        label = f"{_clean_text(name)} ({code})" if _clean_text(name) else code
        combo.addItem(label, code)
        index = combo.findData(code)
    if index >= 0:
        combo.setCurrentIndex(index)


def _select_first_combo_option(combo: QComboBox) -> None:
    if _combo_value(combo):
        return
    if combo.count() > 1:
        combo.setCurrentIndex(1)


def _settings() -> QSettings:
    return QSettings(_SETTINGS_ORG, _SETTINGS_APP)


def _saved_fassto_in_way() -> str:
    return str(_settings().value(_FASSTO_LAST_IN_WAY_KEY, "", type=str) or "").strip()


def _saved_fassto_wh_cd() -> str:
    return str(_settings().value(_FASSTO_LAST_WH_CD_KEY, "", type=str) or "").strip()


def _saved_fassto_sup_cd() -> str:
    return str(_settings().value(_FASSTO_LAST_SUP_CD_KEY, "", type=str) or "").strip()


def _save_fassto_in_way(value: str) -> None:
    value = value.strip()
    if value:
        _settings().setValue(_FASSTO_LAST_IN_WAY_KEY, value)


def _save_fassto_warehousing_defaults(payload: Mapping[str, Any]) -> None:
    settings = _settings()
    for key, setting_key in (
        ("inWay", _FASSTO_LAST_IN_WAY_KEY),
        ("whCd", _FASSTO_LAST_WH_CD_KEY),
        ("supCd", _FASSTO_LAST_SUP_CD_KEY),
    ):
        value = _clean_text(payload.get(key))
        if value:
            settings.setValue(setting_key, value)


class _FasstoWriteDialog(QDialog):
    def __init__(self, parent: QWidget, title: str, goods: Sequence[FasstoGoodsRow]) -> None:
        super().__init__(parent)
        self._goods = list(goods)
        self.setWindowTitle(title)
        self.resize(980, 720)

    def _make_goods_picker(self) -> tuple[QComboBox, QPushButton]:
        combo = QComboBox()
        combo.setEditable(True)
        combo.setMinimumWidth(420)
        for row in self._goods:
            combo.addItem(_goods_label(row), row)
        add_btn = QPushButton("상품 추가")
        return combo, add_btn

    def _match_goods_text(self, text: str) -> Optional[FasstoGoodsRow]:
        needle = text.strip().lower()
        if not needle:
            return None
        exact_matches: List[FasstoGoodsRow] = []
        contains_matches: List[FasstoGoodsRow] = []
        for row in self._goods:
            values = [
                row.cstGodCd,
                row.godNm or "",
                row.barcode or "",
                _goods_label(row),
            ]
            lowered = [value.lower() for value in values if value]
            if needle in lowered:
                exact_matches.append(row)
            elif any(needle in value for value in lowered):
                contains_matches.append(row)
        if len(exact_matches) == 1:
            return exact_matches[0]
        if len(contains_matches) == 1:
            return contains_matches[0]
        return None

    def _selected_goods(self, combo: QComboBox) -> tuple[str, str]:
        text = combo.currentText().strip()
        matched = self._match_goods_text(text)
        if matched is not None:
            return matched.cstGodCd, matched.godNm or ""
        data = combo.currentData()
        if isinstance(data, FasstoGoodsRow):
            return data.cstGodCd, data.godNm or ""
        if "·" in text:
            code = text.split("·", 1)[0].strip()
        else:
            code = text.split(maxsplit=1)[0].strip() if text else ""
        name = text
        for row in self._goods:
            if row.cstGodCd.upper() == code.upper():
                return row.cstGodCd, row.godNm or ""
        return code, name


class _WarehousingWriteDialog(_FasstoWriteDialog):
    COLS = ("상품코드", "상품명", "요청수량", "시리얼(쉼표 구분)")

    def __init__(
        self,
        parent: QWidget,
        *,
        title: str,
        goods: Sequence[FasstoGoodsRow],
        initial: Optional[Mapping[str, Any]] = None,
        update_mode: bool = False,
        warehouse_options: Sequence[tuple[str, str]] = (),
        supplier_options: Sequence[tuple[str, str]] = (),
        in_way_options: Sequence[tuple[str, str]] = (),
        saved_in_way: str = "",
        saved_wh_cd: str = "",
        saved_sup_cd: str = "",
    ) -> None:
        super().__init__(parent, title, goods)
        self._update_mode = update_mode
        self._goods_by_code = {row.cstGodCd.upper(): row for row in self._goods if row.cstGodCd}
        initial = initial or {}

        layout = QVBoxLayout(self)

        form_box = QGroupBox("입고 기본 정보")
        form = QFormLayout(form_box)
        self.slip_edit = QLineEdit(_clean_text(initial.get("slipNo")))
        self.slip_edit.setReadOnly(not update_mode)
        self.slip_edit.setPlaceholderText("생성 시 파스토에서 발급되면 비워둡니다.")
        self.ord_no_edit = QLineEdit(_clean_text(initial.get("ordNo")) or _default_request_no("IN"))
        self.ord_no_edit.setPlaceholderText("자동 생성됩니다. 필요하면 수정하세요.")
        self.ord_dt_edit = QDateEdit()
        self.ord_dt_edit.setCalendarPopup(True)
        self.ord_dt_edit.setDisplayFormat("yyyy-MM-dd")
        raw_dt = _clean_text(initial.get("ordDt") or initial.get("inDt"))
        if len(raw_dt) == 8 and raw_dt.isdigit():
            self.ord_dt_edit.setDate(QDate(int(raw_dt[:4]), int(raw_dt[4:6]), int(raw_dt[6:])))
        else:
            today = date.today()
            self.ord_dt_edit.setDate(QDate(today.year, today.month, today.day))
        saved_wh_cd = _clean_text(saved_wh_cd)
        saved_sup_cd = _clean_text(saved_sup_cd)
        saved_in_way = _clean_text(saved_in_way)
        initial_wh_cd = _clean_text(initial.get("whCd"))
        initial_sup_cd = _clean_text(initial.get("supCd"))
        initial_in_way = _clean_text(initial.get("inWay"))
        self.wh_cd_combo = _combo_with_options(
            _merge_options([_FASSTO_DEFAULT_WH], warehouse_options),
            initial_wh_cd or saved_wh_cd or _FASSTO_DEFAULT_WH[0],
        )
        self.sup_cd_combo = _combo_with_options(
            _merge_options([_FASSTO_DEFAULT_SUP], supplier_options),
            initial_sup_cd or saved_sup_cd or _FASSTO_DEFAULT_SUP[0],
        )
        self.in_way_combo = _combo_with_options(
            _merge_options([_FASSTO_DEFAULT_IN_WAY], in_way_options),
            initial_in_way or _FASSTO_DEFAULT_IN_WAY[0],
        )
        if saved_wh_cd:
            _select_or_add_combo_value(self.wh_cd_combo, saved_wh_cd, "최근 사용")
        if saved_sup_cd:
            _select_or_add_combo_value(self.sup_cd_combo, saved_sup_cd, "최근 사용")
        if saved_in_way:
            _select_or_add_combo_value(self.in_way_combo, saved_in_way, "최근 사용")
        if initial_wh_cd:
            _select_or_add_combo_value(self.wh_cd_combo, initial_wh_cd, _clean_text(initial.get("whNm")))
        if initial_sup_cd:
            _select_or_add_combo_value(self.sup_cd_combo, initial_sup_cd, _clean_text(initial.get("supNm")))
        if initial_in_way:
            _select_or_add_combo_value(self.in_way_combo, initial_in_way, _clean_text(initial.get("inWayNm")))
        self.in_way_combo.setPlaceholderText("예: 파스토 입고 방식 코드")
        if self.in_way_combo.lineEdit() is not None:
            self.in_way_combo.lineEdit().setPlaceholderText("파스토 입고 방식 코드를 입력하세요")
        if not update_mode:
            _select_first_combo_option(self.in_way_combo)
        self.parcel_comp_edit = QLineEdit(_clean_text(initial.get("parcelComp")))
        self.invoice_edit = QLineEdit(_clean_text(initial.get("parcelInvoiceNo")))
        self.remark_edit = QLineEdit(_clean_text(initial.get("remark")))
        if update_mode:
            form.addRow("전표번호", self.slip_edit)
        form.addRow("발주번호", self.ord_no_edit)
        form.addRow("입고예정일", self.ord_dt_edit)
        form.addRow("입고 방식(필수)", self.in_way_combo)
        layout.addWidget(form_box)

        self.defaults_summary = QLabel("")
        self.defaults_summary.setWordWrap(True)
        self.defaults_summary.setStyleSheet("color:#475569;")
        layout.addWidget(self.defaults_summary)
        for combo in (self.in_way_combo, self.wh_cd_combo, self.sup_cd_combo):
            combo.currentTextChanged.connect(self._update_defaults_summary)
        self._update_defaults_summary()

        self.advanced_toggle = QPushButton("상세 옵션 보이기")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setChecked(update_mode)
        layout.addWidget(self.advanced_toggle)

        self.advanced_box = QGroupBox("입고 상세 옵션")
        advanced_form = QFormLayout(self.advanced_box)
        advanced_form.addRow("입고창고(선택)", self.wh_cd_combo)
        advanced_form.addRow("공급사(선택)", self.sup_cd_combo)
        advanced_form.addRow("택배사(선택)", self.parcel_comp_edit)
        advanced_form.addRow("송장번호(선택)", self.invoice_edit)
        advanced_form.addRow("비고(선택)", self.remark_edit)
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        self._toggle_advanced(update_mode)
        layout.addWidget(self.advanced_box)

        search_box = QGroupBox("상품 검색")
        search_layout = QVBoxLayout(search_box)
        picker_row = QHBoxLayout()
        self.goods_search_edit = QLineEdit()
        self.goods_search_edit.setPlaceholderText("상품명/상품코드/바코드 입력")
        self.goods_search_edit.textChanged.connect(self._render_goods_results)
        self.goods_search_edit.returnPressed.connect(self._add_selected_goods)
        add_btn = QPushButton("선택 상품 입고 품목 추가")
        add_btn.clicked.connect(self._add_selected_goods)
        remove_btn = QPushButton("선택 삭제")
        remove_btn.clicked.connect(self._remove_selected_rows)
        picker_row.addWidget(QLabel("검색어"))
        picker_row.addWidget(self.goods_search_edit, 1)
        picker_row.addWidget(add_btn)
        picker_row.addWidget(remove_btn)
        search_layout.addLayout(picker_row)

        self.goods_result_table = QTableWidget(0, 3)
        self.goods_result_table.setHorizontalHeaderLabels(["상품코드", "상품명", "바코드"])
        self.goods_result_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.goods_result_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.goods_result_table.verticalHeader().setVisible(False)
        self.goods_result_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.goods_result_table.horizontalHeader().setStretchLastSection(True)
        self.goods_result_table.itemDoubleClicked.connect(lambda _item: self._add_selected_goods())
        search_layout.addWidget(self.goods_result_table, 1)
        layout.addWidget(search_box, 1)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        layout.addWidget(self.table, 1)

        for item in initial.get("goods") or []:
            if isinstance(item, Mapping):
                self._append_item(
                    _clean_text(item.get("cstGodCd")),
                    _clean_text(item.get("godNm")),
                    item.get("ordQty") or item.get("inQty") or 1,
                    ", ".join(str(v) for v in item.get("goodsSerialNo") or [])
                    if isinstance(item.get("goodsSerialNo"), list)
                    else _clean_text(item.get("goodsSerialNo")),
                )
        self._render_goods_results()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _toggle_advanced(self, checked: bool) -> None:
        self.advanced_box.setVisible(checked)
        self.advanced_toggle.setText("상세 옵션 숨기기" if checked else "상세 옵션 보이기")

    def _update_defaults_summary(self) -> None:
        extra = ""
        if _combo_value(self.in_way_combo) == "01":
            extra = " / 택배 입고는 택배사와 송장번호가 필요합니다."
        self.defaults_summary.setText(
            "기본값: "
            f"입고방식 {_combo_display(self.in_way_combo)} / "
            f"창고 {_combo_display(self.wh_cd_combo)} / "
            f"공급사 {_combo_display(self.sup_cd_combo)}"
            f"{extra}"
        )

    def _goods_matches_query(self, row: FasstoGoodsRow, query: str) -> bool:
        if not query:
            return True
        q = query.lower()
        return any(
            q in value.lower()
            for value in (row.cstGodCd, row.godNm or "", row.barcode or "")
            if value
        )

    def _render_goods_results(self, _text: str = "") -> None:
        query = self.goods_search_edit.text().strip()
        matched = [row for row in self._goods if self._goods_matches_query(row, query)]
        self.goods_result_table.setRowCount(0)
        for row in matched[:200]:
            r = self.goods_result_table.rowCount()
            self.goods_result_table.insertRow(r)
            values = [row.cstGodCd, row.godNm or "", row.barcode or ""]
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                if c == 0:
                    item.setData(Qt.UserRole, row.cstGodCd)
                self.goods_result_table.setItem(r, c, item)
        self.goods_result_table.clearSelection()
        if len(matched) == 1:
            self.goods_result_table.selectRow(0)

    def _selected_result_goods(self) -> Optional[FasstoGoodsRow]:
        selected = self.goods_result_table.selectionModel().selectedRows()
        if not selected and self.goods_result_table.rowCount() == 1:
            selected = [self.goods_result_table.model().index(0, 0)]
        if not selected:
            return None
        item = self.goods_result_table.item(selected[0].row(), 0)
        code = _clean_text(item.data(Qt.UserRole) if item is not None else "")
        return self._goods_by_code.get(code.upper())

    def _append_item(self, code: str, name: str, qty: Any = 1, serial: str = "") -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        _set_item(self.table, row, 0, code)
        _set_item(self.table, row, 1, name)
        _set_item(self.table, row, 2, qty)
        _set_item(self.table, row, 3, serial)

    def _add_selected_goods(self, silent: bool = False) -> None:
        goods = self._selected_result_goods()
        if goods is None:
            if not silent:
                QMessageBox.information(self, "상품 선택", "검색 결과에서 입고할 상품을 선택하세요.")
            return
        code = goods.cstGodCd
        for row in range(self.table.rowCount()):
            if _item_text(self.table, row, 0).upper() == code.upper():
                try:
                    qty = int(float(_item_text(self.table, row, 2) or "0"))
                except ValueError:
                    qty = 0
                _set_item(self.table, row, 2, qty + 1)
                self.table.selectRow(row)
                return
        self._append_item(code, goods.godNm or "", 1, "")
        _select_or_add_combo_value(self.sup_cd_combo, goods.supCd or "", goods.supNm or "")

    def _remove_selected_rows(self) -> None:
        for idx in sorted(self.table.selectionModel().selectedRows(), key=lambda i: i.row(), reverse=True):
            self.table.removeRow(idx.row())

    def payload(self) -> Optional[Dict[str, Any]]:
        goods: List[Dict[str, Any]] = []
        for row in range(self.table.rowCount()):
            code = _item_text(self.table, row, 0)
            if not code:
                continue
            if code.upper() not in self._goods_by_code:
                QMessageBox.warning(self, "입력 오류", f"{row + 1}행 상품코드가 파스토 상품 목록에 없습니다: {code}")
                return None
            try:
                qty = int(float(_item_text(self.table, row, 2) or "0"))
            except ValueError:
                QMessageBox.warning(self, "입력 오류", f"{row + 1}행 요청수량이 숫자가 아닙니다.")
                return None
            if qty <= 0:
                QMessageBox.warning(self, "입력 오류", f"{row + 1}행 요청수량은 1 이상이어야 합니다.")
                return None
            item: Dict[str, Any] = {"cstGodCd": code, "ordQty": qty}
            serials = _serial_list(_item_text(self.table, row, 3))
            if serials:
                item["goodsSerialNo"] = serials
            goods.append(item)
        if not goods:
            QMessageBox.warning(self, "입력 오류", "입고 품목을 1개 이상 추가하세요.")
            return None
        in_way = _combo_value(self.in_way_combo)
        if not in_way:
            QMessageBox.warning(self, "입력 오류", "입고 방식을 선택하거나 파스토 입고 방식 코드를 직접 입력하세요.")
            return None
        ord_no = self.ord_no_edit.text().strip()
        if not ord_no:
            QMessageBox.warning(self, "입력 오류", "발주번호가 필요합니다.")
            return None
        parcel_comp = self.parcel_comp_edit.text().strip()
        parcel_invoice_no = self.invoice_edit.text().strip()
        if in_way == "01" and (not parcel_comp or not parcel_invoice_no):
            QMessageBox.warning(
                self,
                "입력 오류",
                "택배 입고 방식은 택배사와 송장번호가 필요합니다. 송장이 없으면 입고 방식을 차량(02)으로 선택하세요.",
            )
            return None

        body: Dict[str, Any] = {
            "ordNo": ord_no,
            "ordDt": _date_to_api_yyyymmdd(self.ord_dt_edit),
            "goods": goods,
            "inWay": in_way,
        }
        for key, widget in (
            ("slipNo", self.slip_edit),
            ("remark", self.remark_edit),
        ):
            value = widget.text().strip()
            if value:
                body[key] = value
        if parcel_comp:
            body["parcelComp"] = parcel_comp
        if parcel_invoice_no:
            body["parcelInvoiceNo"] = parcel_invoice_no
        if self._update_mode and not body.get("slipNo"):
            QMessageBox.warning(self, "입력 오류", "입고 수정에는 전표번호가 필요합니다.")
            return None
        return build_warehousing_payload(body)

    def accept(self) -> None:
        if self.payload() is None:
            return
        super().accept()


class _DeliveryWriteDialog(_FasstoWriteDialog):
    COLS = ("상품코드", "상품명", "주문수량")

    def __init__(
        self,
        parent: QWidget,
        *,
        title: str,
        goods: Sequence[FasstoGoodsRow],
        initial: Optional[Mapping[str, Any]] = None,
        update_mode: bool = False,
        warehouse_options: Sequence[tuple[str, str]] = (),
        out_div_options: Sequence[tuple[str, str]] = (),
    ) -> None:
        super().__init__(parent, title, goods)
        self._update_mode = update_mode
        initial = initial or {}

        layout = QVBoxLayout(self)
        form_box = QGroupBox("출고 정보")
        form = QFormLayout(form_box)
        self.slip_edit = QLineEdit(_clean_text(initial.get("slipNo")))
        self.slip_edit.setReadOnly(not update_mode)
        self.slip_edit.setPlaceholderText("생성 시 파스토에서 발급되면 비워둡니다.")
        self.ord_no_edit = QLineEdit(_clean_text(initial.get("ordNo")))
        self.ord_no_edit.setPlaceholderText("쇼핑몰 주문번호 또는 내부 주문번호")
        self.ord_dt_edit = QDateEdit()
        self.ord_dt_edit.setCalendarPopup(True)
        self.ord_dt_edit.setDisplayFormat("yyyy-MM-dd")
        raw_dt = _clean_text(initial.get("ordDt"))
        if len(raw_dt) == 8 and raw_dt.isdigit():
            self.ord_dt_edit.setDate(QDate(int(raw_dt[:4]), int(raw_dt[4:6]), int(raw_dt[6:])))
        else:
            today = date.today()
            self.ord_dt_edit.setDate(QDate(today.year, today.month, today.day))
        self.out_div_combo = _combo_with_options(out_div_options, _clean_text(initial.get("outDiv") or "1"))
        self.wh_cd_combo = _combo_with_options(warehouse_options, _clean_text(initial.get("whCd")))
        self.shop_cd_edit = QLineEdit(_clean_text(initial.get("shopCd")))
        self.cust_nm_edit = QLineEdit(_clean_text(initial.get("custNm")))
        self.cust_tel_edit = QLineEdit(_clean_text(initial.get("custTelNo")))
        self.cust_addr_edit = QLineEdit(_clean_text(initial.get("custAddr")))
        self.parcel_cd_edit = QLineEdit(_clean_text(initial.get("parcelCd")))
        self.invoice_edit = QLineEdit(_clean_text(initial.get("invoiceNo") or initial.get("parcelInvoiceNo")))
        self.remark_edit = QLineEdit(_clean_text(initial.get("remark")))
        if update_mode:
            form.addRow("전표번호", self.slip_edit)
        form.addRow("주문번호", self.ord_no_edit)
        form.addRow("주문일", self.ord_dt_edit)
        form.addRow("출고구분", self.out_div_combo)
        form.addRow("출고창고", self.wh_cd_combo)
        form.addRow("판매처코드", self.shop_cd_edit)
        form.addRow("수취인", self.cust_nm_edit)
        form.addRow("연락처", self.cust_tel_edit)
        form.addRow("주소", self.cust_addr_edit)
        form.addRow("택배사코드", self.parcel_cd_edit)
        form.addRow("송장번호", self.invoice_edit)
        form.addRow("비고", self.remark_edit)
        layout.addWidget(form_box)

        picker_row = QHBoxLayout()
        self.goods_combo, add_btn = self._make_goods_picker()
        self.goods_combo.setPlaceholderText("상품명/상품코드/바코드 검색")
        add_btn.clicked.connect(self._add_selected_goods)
        remove_btn = QPushButton("선택 삭제")
        remove_btn.clicked.connect(self._remove_selected_rows)
        picker_row.addWidget(QLabel("상품 검색"))
        picker_row.addWidget(self.goods_combo, 1)
        add_btn.setText("출고 품목 추가")
        picker_row.addWidget(add_btn)
        picker_row.addWidget(remove_btn)
        layout.addLayout(picker_row)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        layout.addWidget(self.table, 1)

        for item in initial.get("goods") or []:
            if isinstance(item, Mapping):
                self._append_item(
                    _clean_text(item.get("cstGodCd")),
                    _clean_text(item.get("godNm")),
                    item.get("ordQty") or item.get("outQty") or 1,
                )

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _append_item(self, code: str, name: str, qty: Any = 1) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        _set_item(self.table, row, 0, code)
        _set_item(self.table, row, 1, name)
        _set_item(self.table, row, 2, qty)

    def _add_selected_goods(self) -> None:
        code, name = self._selected_goods(self.goods_combo)
        if not code:
            QMessageBox.information(self, "상품 선택", "추가할 상품을 선택하거나 상품코드를 입력하세요.")
            return
        self._append_item(code, name, 1)

    def _remove_selected_rows(self) -> None:
        for idx in sorted(self.table.selectionModel().selectedRows(), key=lambda i: i.row(), reverse=True):
            self.table.removeRow(idx.row())

    def payload(self) -> Optional[Dict[str, Any]]:
        goods: List[Dict[str, Any]] = []
        for row in range(self.table.rowCount()):
            code = _item_text(self.table, row, 0)
            if not code:
                continue
            try:
                qty = int(float(_item_text(self.table, row, 2) or "0"))
            except ValueError:
                QMessageBox.warning(self, "입력 오류", f"{row + 1}행 주문수량이 숫자가 아닙니다.")
                return None
            if qty <= 0:
                QMessageBox.warning(self, "입력 오류", f"{row + 1}행 주문수량은 1 이상이어야 합니다.")
                return None
            goods.append({"cstGodCd": code, "ordQty": qty})
        if not goods and not self._update_mode:
            QMessageBox.warning(self, "입력 오류", "출고 품목을 1개 이상 추가하세요.")
            return None

        body: Dict[str, Any] = {
            "ordDt": _date_to_api_yyyymmdd(self.ord_dt_edit),
        }
        if goods:
            body["goods"] = goods
        for key, widget in (
            ("slipNo", self.slip_edit),
            ("ordNo", self.ord_no_edit),
            ("shopCd", self.shop_cd_edit),
            ("custNm", self.cust_nm_edit),
            ("custTelNo", self.cust_tel_edit),
            ("custAddr", self.cust_addr_edit),
            ("parcelCd", self.parcel_cd_edit),
            ("invoiceNo", self.invoice_edit),
            ("remark", self.remark_edit),
        ):
            value = widget.text().strip()
            if value:
                body[key] = value
        for key, combo in (
            ("outDiv", self.out_div_combo),
            ("whCd", self.wh_cd_combo),
        ):
            value = _combo_value(combo)
            if value:
                body[key] = value
        if self._update_mode and not body.get("slipNo"):
            QMessageBox.warning(self, "입력 오류", "출고 수정에는 전표번호가 필요합니다.")
            return None
        return body

    def accept(self) -> None:
        if self.payload() is None:
            return
        super().accept()


class _DeliveryCancelDialog(QDialog):
    def __init__(self, parent: QWidget, initial: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("출고 취소")
        self.resize(420, 180)
        initial = initial or {}
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.slip_edit = QLineEdit(_clean_text(initial.get("slipNo")))
        self.ord_no_edit = QLineEdit(_clean_text(initial.get("ordNo")))
        self.remark_edit = QLineEdit(_clean_text(initial.get("remark")))
        form.addRow("전표번호", self.slip_edit)
        form.addRow("주문번호", self.ord_no_edit)
        form.addRow("취소사유/비고", self.remark_edit)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def payload(self) -> Optional[Dict[str, Any]]:
        body: Dict[str, Any] = {}
        for key, widget in (
            ("slipNo", self.slip_edit),
            ("ordNo", self.ord_no_edit),
            ("remark", self.remark_edit),
        ):
            value = widget.text().strip()
            if value:
                body[key] = value
        if not body.get("slipNo") and not body.get("ordNo"):
            QMessageBox.warning(self, "입력 오류", "전표번호 또는 주문번호가 필요합니다.")
            return None
        return body

    def accept(self) -> None:
        if self.payload() is None:
            return
        super().accept()


def _show_write_result(parent: QWidget, title: str, data: Any) -> None:
    detail = _json_text(data)
    if len(detail) > 12000:
        detail = detail[:12000] + "\n..."
    message = _fassto_write_error(data)
    if not message and isinstance(data, Mapping):
        header = data.get("header")
        if isinstance(header, Mapping):
            header_msg = _clean_text(header.get("msg")) or "요청 처리 완료"
            count = _clean_text(header.get("dataCount"))
            message = f"{header_msg}" + (f" · dataCount {count}" if count else "")
    if not message:
        message = "요청 처리 완료"
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Warning if "실패" in title or _fassto_write_error(data) else QMessageBox.Information)
    box.setText(message)
    box.setDetailedText(detail)
    box.exec()


def _fassto_write_error(data: Any) -> Optional[str]:
    if not isinstance(data, Mapping):
        return None
    header = data.get("header")
    if isinstance(header, Mapping):
        header_code = _clean_text(header.get("code"))
        header_msg = _clean_text(header.get("msg"))
        if header_code and header_code != "200":
            return f"{header_msg or '요청 실패'} (code {header_code})"
    rows = data.get("data")
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        code = _clean_text(row.get("code"))
        if not code or code == "200":
            continue
        msg = _clean_text(row.get("msg")) or "요청 실패"
        order_no = _clean_text(row.get("orderNo") or row.get("ordNo") or row.get("fmsSlipNo"))
        parts = [msg, f"code {code}"]
        if order_no:
            parts.append(f"orderNo {order_no}")
        return f"{parts[0]} ({', '.join(parts[1:])})"
    return None


# ---------------------------------------------------------------------------
# Date presets + range validation
# ---------------------------------------------------------------------------


class _DatePresetBar(QWidget):
    """오늘/7일/30일/당월/지난달 빠른 선택 버튼."""

    def __init__(self, start_edit: QDateEdit, end_edit: QDateEdit) -> None:
        super().__init__()
        self._start = start_edit
        self._end = end_edit
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        for label, handler in [
            ("오늘", self._today),
            ("7일", self._seven),
            ("30일", self._thirty),
            ("당월", self._current_month),
            ("지난달", self._last_month),
        ]:
            btn = QPushButton(label)
            btn.setFixedWidth(56)
            btn.clicked.connect(handler)
            row.addWidget(btn)

    def _set(self, s: date, e: date) -> None:
        self._start.setDate(QDate(s.year, s.month, s.day))
        self._end.setDate(QDate(e.year, e.month, e.day))

    def _today(self) -> None:
        today = date.today()
        self._set(today, today)

    def _seven(self) -> None:
        today = date.today()
        self._set(today - timedelta(days=6), today)

    def _thirty(self) -> None:
        today = date.today()
        self._set(today - timedelta(days=29), today)

    def _current_month(self) -> None:
        today = date.today()
        self._set(today.replace(day=1), today)

    def _last_month(self) -> None:
        today = date.today()
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        first_prev = last_prev.replace(day=1)
        self._set(first_prev, last_prev)


def _normalize_range(
    start_edit: QDateEdit, end_edit: QDateEdit, parent: QWidget
) -> Optional[Tuple[str, str]]:
    """start>end면 스왑 확인. 반환값 None이면 취소."""
    s = start_edit.date().toPython()
    e = end_edit.date().toPython()
    if s > e:
        btn = QMessageBox.question(
            parent,
            "날짜 범위 확인",
            f"시작일({s})이 종료일({e})보다 뒤입니다. 자동으로 바꿀까요?",
        )
        if btn != QMessageBox.Yes:
            return None
        start_edit.setDate(QDate(e.year, e.month, e.day))
        end_edit.setDate(QDate(s.year, s.month, s.day))
        s, e = e, s
    return _fmt_api_date(s), _fmt_api_date(e)


# ---------------------------------------------------------------------------
# Error formatting
# ---------------------------------------------------------------------------


def _format_fassto_error(error: str | None, details: Any = None) -> str:
    base = (error or "알 수 없는 오류").strip()
    if not isinstance(details, dict):
        return base

    error_code = str(details.get("errorCode") or "").strip()
    path = str(details.get("path") or "").strip()
    status = details.get("status")
    stage = "파스토 API 호출"
    if path.startswith("/api/v1/auth/connect"):
        stage = "파스토 accessToken 발급"

    parts = [f"{stage} 실패"]
    if error_code:
        parts.append(f"코드: {error_code}")
    if status not in (None, ""):
        parts.append(f"HTTP: {status}")

    message = " / ".join(parts)
    if base:
        message = f"{message}\n사유: {base}"

    if error_code == "INVALID_API_KEY_OR_DELETED":
        message += "\n현재 apiKey가 잘못됐거나 삭제된 상태입니다. 파스토에서 apiCd/apiKey 재확인이 필요합니다."
    elif error_code == "INVALID_ACCESS":
        message += "\n토큰이 만료됐거나 accessToken이 유효하지 않습니다. 다시 인증이 필요합니다."

    return message


# ---------------------------------------------------------------------------
# Business color helpers
# ---------------------------------------------------------------------------


def _goods_margin_cell(sal_pr: float, in_pr: float) -> Cell:
    """마진율% 셀: 낮을수록 빨강/주황."""
    if sal_pr <= 0:
        return Cell(text="")
    margin = (sal_pr - in_pr) / sal_pr * 100.0
    text = f"{margin:.1f}%"
    if margin < 0:
        return Cell(text=text, sort_key=margin, fg=_FG_DANGER, bg=_BG_DANGER)
    if margin < 20:
        return Cell(text=text, sort_key=margin, fg=_FG_DANGER)
    if margin < 30:
        return Cell(text=text, sort_key=margin, fg=_FG_WARN)
    return Cell(text=text, sort_key=margin, fg=_FG_OK)


def _stock_alert_cells(
    can_stock: float, safety: float
) -> Tuple[str, Optional[QColor], Optional[QColor]]:
    """(라벨, fg, bg) — 안전재고 대비 경보 레벨."""
    if safety <= 0:
        # safetyStock 설정 없음 — can_stock이 0이면 위험 표시
        if can_stock <= 0:
            return "품절", _FG_DANGER, _BG_DANGER
        return "-", None, None
    if can_stock <= 0:
        return "품절", _FG_DANGER, _BG_DANGER
    if can_stock <= safety:
        return "부족", _FG_DANGER, _BG_DANGER
    if can_stock <= safety * 1.5:
        return "경고", _FG_WARN, _BG_WARN
    return "정상", _FG_OK, None


def _delivery_status_cell(status_code: str, status_name: str) -> Cell:
    name = status_name or status_code or ""
    lower = (status_code or "") + "|" + (status_name or "")
    if "취소" in lower or "CANCEL" in lower.upper():
        return Cell(text=name, fg=_FG_DANGER, bg=_BG_DANGER)
    if "부족" in lower or "SHORTAGE" in lower.upper():
        return Cell(text=name, fg=_FG_WARN, bg=_BG_WARN)
    if "완료" in lower or "DONE" in lower.upper() or status_code == "3":
        return Cell(text=name, fg=_FG_OK, bg=_BG_OK)
    if "작업" in lower or "WORKING" in lower.upper():
        return Cell(text=name, fg=_FG_MUTED, bg=_BG_INFO)
    return Cell(text=name)


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

        self.config_box = QGroupBox("설정 상태")
        cf = QFormLayout(self.config_box)
        self.lbl_api_url = QLabel("-")
        self.lbl_cst_cd = QLabel("-")
        self.lbl_configured = QLabel("-")
        cf.addRow("API URL", self.lbl_api_url)
        cf.addRow("고객사 코드", self.lbl_cst_cd)
        cf.addRow("설정 완료", self.lbl_configured)

        self.remote_box = QGroupBox("파스토 원격 요약")
        rf = QFormLayout(self.remote_box)
        self.lbl_goods_count = QLabel("-")
        self.lbl_element_count = QLabel("-")
        self.lbl_stock_rows = QLabel("-")
        self.lbl_stock_sum = QLabel("-")
        self.lbl_can_sum = QLabel("-")
        self.lbl_bad_sum = QLabel("-")
        self.lbl_alert = QLabel("-")
        rf.addRow("상품(goods) 수", self.lbl_goods_count)
        rf.addRow("세트상품 수", self.lbl_element_count)
        rf.addRow("재고 레코드 수", self.lbl_stock_rows)
        rf.addRow("총재고(stockQty) 합", self.lbl_stock_sum)
        rf.addRow("가용재고(canStockQty) 합", self.lbl_can_sum)
        rf.addRow("불량재고(badStockQty) 합", self.lbl_bad_sum)
        rf.addRow("재고 경보 (안전재고 이하)", self.lbl_alert)

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
            element_env = self._tab.connector.get_goods_elements()
            stock_env = self._tab.connector.get_stock_list()
            goods = normalize_fassto_goods(extract_fassto_list(goods_env))
            stocks = normalize_fassto_stocks(extract_fassto_list(stock_env))
            # safetyStock 매핑
            safety_by_code = {g.cstGodCd.upper(): g.safetyStock for g in goods if g.cstGodCd}
            alert = 0
            for s in stocks:
                safety = safety_by_code.get((s.cstGodCd or "").upper(), 0.0)
                if safety > 0 and s.canStockQty <= safety:
                    alert += 1
                elif safety <= 0 and s.canStockQty <= 0:
                    alert += 1
            return {
                "goods": len(goods),
                "elements": len(extract_fassto_list(element_env)),
                "stock_rows": len(stocks),
                "stock_sum": sum(s.stockQty for s in stocks),
                "can_sum": sum(s.canStockQty for s in stocks),
                "bad_sum": sum(s.badStockQty for s in stocks),
                "alert": alert,
            }

        def done(result: JobResult) -> None:
            self.refresh_btn.setEnabled(True)
            if not result.ok:
                self.status_label.setText(f"❌ 실패: {result.error}")
                for w in (
                    self.lbl_goods_count,
                    self.lbl_element_count,
                    self.lbl_stock_rows,
                    self.lbl_stock_sum,
                    self.lbl_can_sum,
                    self.lbl_bad_sum,
                    self.lbl_alert,
                ):
                    w.setText("-")
                return
            self.status_label.setText("✅ 파스토 연결 성공")
            d = result.data
            self.lbl_goods_count.setText(_fmt_num(d["goods"]))
            self.lbl_element_count.setText(_fmt_num(d["elements"]))
            self.lbl_stock_rows.setText(_fmt_num(d["stock_rows"]))
            self.lbl_stock_sum.setText(_fmt_num(d["stock_sum"]))
            self.lbl_can_sum.setText(_fmt_num(d["can_sum"]))
            self.lbl_bad_sum.setText(_fmt_num(d["bad_sum"]))
            alert = d["alert"]
            self.lbl_alert.setText(f"{alert}건" + (" ⚠" if alert else ""))
            if alert:
                self.lbl_alert.setStyleSheet(
                    "color: #b40000; font-weight: bold;"
                )
            else:
                self.lbl_alert.setStyleSheet("")

        _run_async(self, work, done)


class _GoodsSubTab(QWidget):
    COLUMNS = (
        "상품코드",
        "상품명",
        "바코드",
        "사용",
        "상품구분",
        "카테고리",
        "공급사",
        "매입가",
        "판매가",
        "마진%",
        "중량(g)",
        "박스입수",
        "안전재고",
        "최초입고일",
    )

    def __init__(self, tab: "FasstoTab") -> None:
        super().__init__()
        self._tab = tab
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        self.refresh_btn = QPushButton("상품 목록 조회")
        self.refresh_btn.clicked.connect(self._refresh)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("상품명/코드/바코드 검색")
        self.search_edit.textChanged.connect(self._apply_filter)
        self.export_btn = QPushButton("CSV 저장")
        self.export_btn.clicked.connect(
            lambda: _export_table_to_csv(self.table, self, "fassto_goods")
        )
        self.status = QLabel("")
        top.addWidget(self.refresh_btn)
        top.addWidget(self.search_edit, 1)
        top.addWidget(self.export_btn)
        top.addWidget(self.status)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSortingEnabled(True)

        self._all_rows: List[FasstoGoodsRow] = []

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
            self._all_rows = result.data
            self._apply_filter()

        _run_async(self, work, done)

    def _apply_filter(self) -> None:
        q = self.search_edit.text().strip().lower()
        rows_src = self._all_rows
        if q:
            rows_src = [
                r
                for r in self._all_rows
                if q in (r.cstGodCd or "").lower()
                or q in (r.godNm or "").lower()
                or q in (r.barcode or "").lower()
            ]
        rows: List[List[Any]] = []
        for r in rows_src:
            use_yn_cell = (
                Cell(text="Y", fg=_FG_OK)
                if (r.useYn or "").upper() == "Y"
                else Cell(text=r.useYn or "", fg=_FG_MUTED)
            )
            rows.append(
                [
                    r.cstGodCd,
                    r.godNm,
                    r.barcode or "",
                    use_yn_cell,
                    r.giftDivNm or "",
                    r.cateNm or "",
                    r.supNm or "",
                    _num_cell(r.inPr, _fmt_money),
                    _num_cell(r.salPr, _fmt_money),
                    _goods_margin_cell(r.salPr, r.inPr),
                    _num_cell(r.godWeight),
                    _num_cell(r.boxInCnt),
                    _num_cell(r.safetyStock),
                    _fmt_yyyymmdd(r.firstInDt) if r.firstInDt else "",
                ]
            )
        _fill_table(self.table, self.COLUMNS, rows)
        self.status.setText(f"총 {len(rows)}건 (전체 {len(self._all_rows)}건)")


class _GoodsElementSubTab(QWidget):
    PARENT_COLS = ("상품코드", "상품명", "사용", "구성품 수")
    CHILD_COLS = ("상품코드", "바코드", "상품명", "구성품구분", "수량")

    def __init__(self, tab: "FasstoTab") -> None:
        super().__init__()
        self._tab = tab
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        self.refresh_btn = QPushButton("세트상품 조회")
        self.refresh_btn.clicked.connect(self._refresh)
        self.export_parent_btn = QPushButton("부모 CSV")
        self.export_parent_btn.clicked.connect(
            lambda: _export_table_to_csv(self.parent_table, self, "fassto_set_parents")
        )
        self.export_child_btn = QPushButton("구성품 CSV")
        self.export_child_btn.clicked.connect(
            lambda: _export_table_to_csv(self.child_table, self, "fassto_set_children")
        )
        self.status = QLabel("")
        top.addWidget(self.refresh_btn)
        top.addWidget(self.export_parent_btn)
        top.addWidget(self.export_child_btn)
        top.addWidget(self.status, 1)

        splitter = QSplitter(Qt.Vertical)

        self.parent_table = QTableWidget(0, len(self.PARENT_COLS))
        self.parent_table.setHorizontalHeaderLabels(self.PARENT_COLS)
        self.parent_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.parent_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.parent_table.setSortingEnabled(True)
        self.parent_table.itemSelectionChanged.connect(self._on_parent_selected)

        child_box = QGroupBox("선택한 세트의 구성품")
        child_layout = QVBoxLayout(child_box)
        self.child_table = QTableWidget(0, len(self.CHILD_COLS))
        self.child_table.setHorizontalHeaderLabels(self.CHILD_COLS)
        self.child_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.child_table.setSortingEnabled(True)
        child_layout.addWidget(self.child_table)

        splitter.addWidget(self.parent_table)
        splitter.addWidget(child_box)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)

        layout.addLayout(top)
        layout.addWidget(splitter, 1)

        self._rows: List[FasstoGoodsElementRow] = []

    def _refresh(self) -> None:
        if not self._tab.require_configured(self):
            return
        self.status.setText("조회 중...")
        self.refresh_btn.setEnabled(False)

        def work() -> list:
            env = self._tab.connector.get_goods_elements()
            return normalize_fassto_goods_elements(extract_fassto_list(env))

        def done(result: JobResult) -> None:
            self.refresh_btn.setEnabled(True)
            if not result.ok:
                self.status.setText(f"❌ {result.error}")
                return
            self._rows = result.data
            rows = [
                [
                    r.cstGodCd,
                    r.godNm or "",
                    r.useYn or "",
                    _num_cell(len(r.elements)),
                ]
                for r in self._rows
            ]
            _fill_table(self.parent_table, self.PARENT_COLS, rows)
            self.status.setText(f"총 {len(rows)}건")
            if rows:
                self.parent_table.selectRow(0)
            else:
                _fill_table(self.child_table, self.CHILD_COLS, [])

        _run_async(self, work, done)

    def _on_parent_selected(self) -> None:
        selected = self.parent_table.currentRow()
        if selected < 0:
            _fill_table(self.child_table, self.CHILD_COLS, [])
            return
        code_item = self.parent_table.item(selected, 0)
        if code_item is None:
            return
        code = code_item.text()
        target = next((r for r in self._rows if r.cstGodCd == code), None)
        if target is None:
            _fill_table(self.child_table, self.CHILD_COLS, [])
            return
        rows = [
            [
                e.cstGodCd,
                e.godBarcd or "",
                e.godNm or "",
                e.godTypeNm or "",
                _num_cell(e.qty),
            ]
            for e in target.elements
        ]
        _fill_table(self.child_table, self.CHILD_COLS, rows)


class _StockSubTab(QWidget):
    """재고 탭 — goods·stock 병합 후 safetyStock 경보 색 코딩."""

    COLUMNS = (
        "상태",
        "상품코드",
        "상품명",
        "바코드",
        "창고",
        "총재고",
        "가용재고",
        "불량재고",
        "안전재고",
        "유통기한",
        "공급사",
        "전표번호",
        "시리얼",
    )

    def __init__(self, tab: "FasstoTab") -> None:
        super().__init__()
        self._tab = tab
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        self.refresh_btn = QPushButton("재고 조회")
        self.refresh_btn.clicked.connect(self._refresh)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("상품명/코드/바코드 검색")
        self.search_edit.textChanged.connect(self._apply_filter)
        self.alert_only = QCheckBox("경보만")
        self.alert_only.toggled.connect(self._apply_filter)
        self.export_btn = QPushButton("CSV 저장")
        self.export_btn.clicked.connect(
            lambda: _export_table_to_csv(self.table, self, "fassto_stock")
        )
        self.status = QLabel("기준: 가용재고 canStockQty, 안전재고는 goods.safetyStock")
        top.addWidget(self.refresh_btn)
        top.addWidget(self.search_edit, 1)
        top.addWidget(self.alert_only)
        top.addWidget(self.export_btn)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSortingEnabled(True)

        self._stocks: List[FasstoStockRow] = []
        self._safety_by_code: Dict[str, float] = {}
        self._totals = {"stock": 0.0, "can": 0.0, "bad": 0.0, "alert": 0}

        layout.addLayout(top)
        layout.addWidget(self.status)
        layout.addWidget(self.table, 1)

    def _refresh(self) -> None:
        if not self._tab.require_configured(self):
            return
        self.status.setText("조회 중... (goods + stock)")
        self.refresh_btn.setEnabled(False)

        def work() -> dict:
            goods = normalize_fassto_goods(
                extract_fassto_list(self._tab.connector.get_goods_list())
            )
            stocks = normalize_fassto_stocks(
                extract_fassto_list(self._tab.connector.get_stock_list())
            )
            safety = {
                (g.cstGodCd or "").upper(): g.safetyStock for g in goods if g.cstGodCd
            }
            return {"stocks": stocks, "safety": safety}

        def done(result: JobResult) -> None:
            self.refresh_btn.setEnabled(True)
            if not result.ok:
                self.status.setText(f"❌ {result.error}")
                return
            self._stocks = result.data["stocks"]
            self._safety_by_code = result.data["safety"]
            self._totals = {
                "stock": sum(r.stockQty for r in self._stocks),
                "can": sum(r.canStockQty for r in self._stocks),
                "bad": sum(r.badStockQty for r in self._stocks),
                "alert": 0,
            }
            self._apply_filter()

        _run_async(self, work, done)

    def _apply_filter(self) -> None:
        q = self.search_edit.text().strip().lower()
        alert_only = self.alert_only.isChecked()
        rows: List[List[Any]] = []
        alert_count = 0
        for r in self._stocks:
            safety = self._safety_by_code.get((r.cstGodCd or "").upper(), 0.0)
            label, fg, bg = _stock_alert_cells(r.canStockQty, safety)
            is_alert = label in ("품절", "부족", "경고")
            if is_alert:
                alert_count += 1

            if q and not (
                q in (r.cstGodCd or "").lower()
                or q in (r.godNm or "").lower()
                or q in (r.godBarcd or "").lower()
            ):
                continue
            if alert_only and not is_alert:
                continue

            row_bg = bg
            rows.append(
                [
                    Cell(text=label, fg=fg, bg=bg),
                    Cell(text=r.cstGodCd, bg=row_bg),
                    Cell(text=r.godNm or "", bg=row_bg),
                    Cell(text=r.godBarcd or "", bg=row_bg),
                    Cell(text=r.whCd or "", bg=row_bg),
                    _num_cell(r.stockQty, bg=row_bg),
                    _num_cell(r.canStockQty, fg=fg, bg=row_bg),
                    _num_cell(r.badStockQty, bg=row_bg),
                    _num_cell(safety, bg=row_bg),
                    Cell(text=_fmt_yyyymmdd(r.distTermDt) if r.distTermDt else "", bg=row_bg),
                    Cell(text=r.supNm or "", bg=row_bg),
                    Cell(text=r.slipNo or "", bg=row_bg),
                    Cell(text=r.goodsSerialNo or "", bg=row_bg),
                ]
            )
        self._totals["alert"] = alert_count
        _fill_table(self.table, self.COLUMNS, rows)
        self.status.setText(
            f"표시 {len(rows)}건 / 전체 {len(self._stocks)}건 · "
            f"총재고 {_fmt_num(self._totals['stock'])} · "
            f"가용 {_fmt_num(self._totals['can'])} · "
            f"불량 {_fmt_num(self._totals['bad'])} · "
            f"경보 {self._totals['alert']}건"
        )


class _WarehousingSubTab(QWidget):
    COLUMNS_LIST = (
        "전표번호",
        "입고예정일",
        "창고",
        "작업상태",
        "공급사",
        "SKU",
        "요청수량",
        "입고수량",
        "검수수량",
        "입고경로",
        "택배사",
        "송장번호",
    )
    DETAIL_COLS = ("상품코드", "상품명", "요청수량", "입고수량", "검수수량", "시리얼")

    def __init__(self, tab: "FasstoTab") -> None:
        super().__init__()
        self._tab = tab
        layout = QVBoxLayout(self)

        start_d, end_d = _month_range()

        top = QHBoxLayout()
        self.start_edit = QDateEdit(QDate(start_d.year, start_d.month, start_d.day))
        self.start_edit.setCalendarPopup(True)
        self.start_edit.setDisplayFormat("yyyy-MM-dd")
        self.end_edit = QDateEdit(QDate(end_d.year, end_d.month, end_d.day))
        self.end_edit.setCalendarPopup(True)
        self.end_edit.setDisplayFormat("yyyy-MM-dd")
        self.refresh_btn = QPushButton("기간 조회")
        self.refresh_btn.clicked.connect(self._refresh_list)
        self.create_btn = QPushButton("입고 생성")
        self.create_btn.clicked.connect(self._create_warehousing)
        self.update_btn = QPushButton("입고 수정")
        self.update_btn.clicked.connect(self._update_warehousing)
        self.cancel_btn = QPushButton("입고 취소")
        self.cancel_btn.clicked.connect(self._cancel_warehousing)
        self.export_btn = QPushButton("CSV 저장")
        self.export_btn.clicked.connect(
            lambda: _export_table_to_csv(self.table, self, "fassto_warehousing")
        )

        top.addWidget(QLabel("시작"))
        top.addWidget(self.start_edit)
        top.addWidget(QLabel("종료"))
        top.addWidget(self.end_edit)
        top.addWidget(_DatePresetBar(self.start_edit, self.end_edit))
        top.addWidget(self.refresh_btn)
        top.addWidget(self.create_btn)
        top.addWidget(self.update_btn)
        top.addWidget(self.cancel_btn)
        top.addWidget(self.export_btn)

        detail_row = QHBoxLayout()
        self.slip_edit = QLineEdit()
        self.slip_edit.setPlaceholderText("전표번호(slipNo)")
        self.detail_btn = QPushButton("상세")
        self.detail_btn.clicked.connect(self._refresh_detail)
        self.status = QLabel("")
        detail_row.addWidget(self.slip_edit, 1)
        detail_row.addWidget(self.detail_btn)
        detail_row.addWidget(self.status, 2)

        self.table = QTableWidget(0, len(self.COLUMNS_LIST))
        self.table.setHorizontalHeaderLabels(self.COLUMNS_LIST)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSortingEnabled(True)
        self.table.cellClicked.connect(self._on_row_clicked)
        self._rows: List[FasstoWarehousingRow] = []

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.table)

        detail_box = QGroupBox("상세 — 헤더")
        detail_layout = QVBoxLayout(detail_box)
        self.header_view = QTextEdit()
        self.header_view.setReadOnly(True)
        self.header_view.setPlaceholderText("전표 선택 시 헤더가 표시됩니다.")
        self.header_view.setMaximumHeight(140)
        detail_layout.addWidget(self.header_view)

        detail_goods = QGroupBox("상세 — 품목")
        goods_layout = QVBoxLayout(detail_goods)
        self.detail_table = QTableWidget(0, len(self.DETAIL_COLS))
        self.detail_table.setHorizontalHeaderLabels(self.DETAIL_COLS)
        self.detail_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.detail_table.setSortingEnabled(True)
        goods_layout.addWidget(self.detail_table)

        sub = QSplitter(Qt.Vertical)
        sub.addWidget(detail_box)
        sub.addWidget(detail_goods)
        sub.setStretchFactor(0, 1)
        sub.setStretchFactor(1, 2)
        splitter.addWidget(sub)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        layout.addLayout(top)
        layout.addLayout(detail_row)
        layout.addWidget(splitter, 1)

    def _refresh_list(self) -> None:
        if not self._tab.require_configured(self):
            return
        r = _normalize_range(self.start_edit, self.end_edit, self)
        if not r:
            return
        start, end = r
        self.status.setText("조회 중...")
        self.refresh_btn.setEnabled(False)

        def work() -> list:
            env = self._tab.connector.get_warehousing_list(start, end)
            return normalize_fassto_warehousings(extract_fassto_list(env))

        def done(result: JobResult) -> None:
            self.refresh_btn.setEnabled(True)
            if not result.ok:
                self.status.setText(f"❌ {result.error}")
                return
            self._rows = result.data
            rows: List[List[Any]] = []
            for r_ in self._rows:
                status_cell = _delivery_status_cell(r_.wrkStat or "", r_.wrkStatNm or "")
                # ordQty == inQty ? 완료
                qty_fg = None
                if r_.tarQty and r_.tarQty > 0:
                    qty_fg = _FG_WARN
                rows.append(
                    [
                        r_.slipNo,
                        _fmt_yyyymmdd(r_.ordDt),
                        r_.whNm or "",
                        status_cell,
                        r_.supNm or "",
                        _num_cell(r_.sku),
                        _num_cell(r_.ordQty),
                        _num_cell(r_.inQty),
                        _num_cell(r_.tarQty, fg=qty_fg),
                        r_.inWayNm or "",
                        r_.parcelComp or "",
                        r_.parcelInvoiceNo or "",
                    ]
                )
            _fill_table(self.table, self.COLUMNS_LIST, rows)
            self.status.setText(f"총 {len(rows)}건")

        _run_async(self, work, done)

    def _on_row_clicked(self, row: int, _col: int) -> None:
        item = self.table.item(row, 0)
        if item is not None:
            self.slip_edit.setText(item.text())

    def _selected_payload(self) -> Dict[str, Any]:
        slip = self.slip_edit.text().strip()
        for row in self._rows:
            if row.slipNo == slip and isinstance(row.raw, dict):
                return dict(row.raw)
        payload: Dict[str, Any] = {}
        if slip:
            payload["slipNo"] = slip
        return payload

    def _selected_row(self) -> Optional[FasstoWarehousingRow]:
        slip = self.slip_edit.text().strip()
        if not slip:
            return None
        for row in self._rows:
            if row.slipNo == slip:
                return row
        return None

    def _cancel_warehousing(self) -> None:
        row = self._selected_row()
        if row is None:
            QMessageBox.information(self, "입고 취소", "취소할 입고 전표를 선택하세요.")
            return

        allowed, reason = warehousing_cancel_check(row.wrkStat, row.wrkStatNm)
        status_name = warehousing_status_name(row.wrkStat, row.wrkStatNm) or "-"
        if not allowed:
            QMessageBox.information(
                self,
                "입고 취소 불가",
                "\n".join(
                    [
                        f"전표번호: {row.slipNo or '-'}",
                        f"작업상태: {status_name}",
                        reason,
                    ]
                ),
            )
            return

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("입고 취소")
        box.setText("선택한 입고 전표는 파스토 웹에서 취소해야 합니다.")
        box.setInformativeText(
            "\n".join(
                [
                    "공개 OpenAPI에는 입고취소 endpoint가 없어 앱에서 직접 취소 전송은 하지 않습니다.",
                    "파스토 웹에서 아래 전표를 검색한 뒤 입고취소를 진행하세요.",
                    "",
                    f"전표번호: {row.slipNo or '-'}",
                    f"입고예정일: {_fmt_yyyymmdd(row.ordDt)}",
                    f"작업상태: {status_name}",
                    f"SKU: {_fmt_num(row.sku)}",
                    f"요청수량: {_fmt_num(row.ordQty)}",
                    f"입고수량: {_fmt_num(row.inQty)}",
                    f"검수수량: {_fmt_num(row.tarQty)}",
                ]
            )
        )
        open_btn = box.addButton("파스토 웹 열기", QMessageBox.AcceptRole)
        box.addButton("닫기", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() == open_btn:
            QDesktopServices.openUrl(QUrl(_FASSTO_WEB_URL))
            self.status.setText(f"입고취소: 파스토 웹에서 {row.slipNo or ''} 전표를 취소하세요.")

    def _run_write(
        self,
        *,
        title: str,
        payload: Dict[str, Any],
        call: Callable[[List[Dict[str, Any]]], Any],
    ) -> None:
        self.status.setText(f"{title} 요청 중...")
        self.create_btn.setEnabled(False)
        self.update_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)

        def done(result: JobResult) -> None:
            self.create_btn.setEnabled(True)
            self.update_btn.setEnabled(True)
            self.cancel_btn.setEnabled(True)
            if not result.ok:
                self.status.setText(f"❌ {title} 실패: {result.error}")
                return
            write_error = _fassto_write_error(result.data)
            if write_error:
                self.status.setText(f"❌ {title} 실패: {write_error}")
                _show_write_result(self, f"{title} 실패 응답", result.data)
                return
            _save_fassto_warehousing_defaults(payload)
            self.status.setText(f"✅ {title} 완료")
            _show_write_result(self, f"{title} 응답", result.data)
            self._refresh_list()

        _run_async(self, lambda: call([payload]), done)

    def _create_warehousing(self) -> None:
        if not self._tab.require_configured(self):
            return
        self.status.setText("파스토 상품 불러오는 중...")
        self.create_btn.setEnabled(False)

        def work() -> Tuple[List[FasstoGoodsRow], List[FasstoWarehousingRow]]:
            goods_env = self._tab.connector.get_goods_list()
            goods = normalize_fassto_goods(extract_fassto_list(goods_env))
            recent_rows: List[FasstoWarehousingRow] = []
            today = date.today()
            try:
                recent_env = self._tab.connector.get_warehousing_list(
                    (today - timedelta(days=365)).strftime("%Y%m%d"),
                    today.strftime("%Y%m%d"),
                )
                recent_rows = normalize_fassto_warehousings(extract_fassto_list(recent_env))
            except Exception:
                recent_rows = []
            return goods, recent_rows

        def done(result: JobResult) -> None:
            self.create_btn.setEnabled(True)
            if not result.ok:
                self.status.setText(f"❌ 상품 조회 실패: {result.error}")
                return
            goods, recent_rows = result.data
            option_rows = list(self._rows) + list(recent_rows)
            dialog = _WarehousingWriteDialog(
                self,
                title="입고 생성",
                goods=goods,
                warehouse_options=_unique_options(option_rows, "whCd", "whNm"),
                supplier_options=_unique_options(option_rows, "supCd", "supNm"),
                in_way_options=_unique_options(option_rows, "inWay", "inWayNm"),
                saved_in_way=_saved_fassto_in_way(),
                saved_wh_cd=_saved_fassto_wh_cd(),
                saved_sup_cd=_saved_fassto_sup_cd(),
            )
            if dialog.exec() != QDialog.Accepted:
                return
            payload = dialog.payload()
            if payload is None:
                return
            self._run_write(
                title="입고 생성",
                payload=payload,
                call=self._tab.connector.create_warehousing,
            )

        _run_async(self, work, done)

    def _update_warehousing(self) -> None:
        if not self._tab.require_configured(self):
            return
        selected = self._selected_payload()
        self.status.setText("파스토 상품 불러오는 중...")
        self.update_btn.setEnabled(False)

        def work() -> Tuple[List[FasstoGoodsRow], List[FasstoWarehousingRow]]:
            goods_env = self._tab.connector.get_goods_list()
            goods = normalize_fassto_goods(extract_fassto_list(goods_env))
            recent_rows: List[FasstoWarehousingRow] = []
            today = date.today()
            try:
                recent_env = self._tab.connector.get_warehousing_list(
                    (today - timedelta(days=365)).strftime("%Y%m%d"),
                    today.strftime("%Y%m%d"),
                )
                recent_rows = normalize_fassto_warehousings(extract_fassto_list(recent_env))
            except Exception:
                recent_rows = []
            return goods, recent_rows

        def done(result: JobResult) -> None:
            self.update_btn.setEnabled(True)
            if not result.ok:
                self.status.setText(f"❌ 상품 조회 실패: {result.error}")
                return
            goods, recent_rows = result.data
            option_rows = list(self._rows) + list(recent_rows)
            dialog = _WarehousingWriteDialog(
                self,
                title="입고 수정",
                goods=goods,
                initial=selected or {"slipNo": self.slip_edit.text().strip()},
                update_mode=True,
                warehouse_options=_unique_options(option_rows, "whCd", "whNm"),
                supplier_options=_unique_options(option_rows, "supCd", "supNm"),
                in_way_options=_unique_options(option_rows, "inWay", "inWayNm"),
                saved_in_way=_saved_fassto_in_way(),
                saved_wh_cd=_saved_fassto_wh_cd(),
                saved_sup_cd=_saved_fassto_sup_cd(),
            )
            if dialog.exec() != QDialog.Accepted:
                return
            payload = dialog.payload()
            if payload is None:
                return
            self._run_write(
                title="입고 수정",
                payload=payload,
                call=self._tab.connector.update_warehousing,
            )

        _run_async(self, work, done)

    def _refresh_detail(self) -> None:
        if not self._tab.require_configured(self):
            return
        slip = self.slip_edit.text().strip()
        if not slip:
            QMessageBox.information(self, "안내", "전표번호를 입력하세요.")
            return
        self.header_view.setPlainText("조회 중...")
        self.detail_btn.setEnabled(False)
        _fill_table(self.detail_table, self.DETAIL_COLS, [])

        def work() -> Any:
            return self._tab.connector.get_warehousing_detail(slip)

        def done(result: JobResult) -> None:
            self.detail_btn.setEnabled(True)
            if not result.ok:
                self.header_view.setPlainText(f"❌ {result.error}")
                return
            rows = extract_fassto_list(result.data)
            if not rows:
                self.header_view.setPlainText("데이터가 없습니다.")
                return
            header = rows[0] if isinstance(rows[0], dict) else {}
            status_name = warehousing_status_name(header.get("wrkStat"), header.get("wrkStatNm"))
            header_lines = [
                f"전표: {header.get('slipNo', '')} / 발주번호: {header.get('ordNo') or '-'}",
                f"일자: {_fmt_yyyymmdd(header.get('ordDt'))} / 창고: {header.get('whNm', '')} ({header.get('whCd', '')})",
                f"공급사: {header.get('supNm', '')} / 입고방식: {header.get('inWayNm', '')} / 상태: {status_name or '-'}",
                f"수량(주문/입고/타겟): {_fmt_num(header.get('ordQty'))} / {_fmt_num(header.get('inQty'))} / {_fmt_num(header.get('tarQty'))} · SKU: {_fmt_num(header.get('sku'))}",
                f"택배: {header.get('parcelComp', '')} / 송장: {header.get('parcelInvoiceNo') or '-'}",
            ]
            remark = (header.get("remark") or "").strip()
            if remark:
                header_lines.append(f"비고: {remark}")
            self.header_view.setPlainText("\n".join(header_lines))

            goods = header.get("goods") if isinstance(header.get("goods"), list) else []
            detail_rows: List[List[Any]] = []
            for g in goods:
                if not isinstance(g, dict):
                    continue
                serial = g.get("goodsSerialNo")
                if isinstance(serial, list):
                    serial = ", ".join(str(v) for v in serial if v)
                detail_rows.append(
                    [
                        g.get("cstGodCd", ""),
                        g.get("godNm", ""),
                        _num_cell(g.get("ordQty") or 0),
                        _num_cell(g.get("inQty") or 0),
                        _num_cell(g.get("tarQty") or 0),
                        serial or "",
                    ]
                )
            _fill_table(self.detail_table, self.DETAIL_COLS, detail_rows)

        _run_async(self, work, done)


class _DeliverySubTab(QWidget):
    COLUMNS = (
        "전표번호",
        "출고일",
        "주문일",
        "판매채널",
        "작업상태",
        "출고구분",
        "주문수량",
        "수취인",
        "연락처",
        "송장번호",
        "택배사",
        "창고",
        "수정시각",
    )
    DETAIL_COLS = ("상품코드", "상품명", "주문수량", "상품구분")

    def __init__(self, tab: "FasstoTab") -> None:
        super().__init__()
        self._tab = tab
        layout = QVBoxLayout(self)

        start_d, end_d = _month_range()
        top = QHBoxLayout()
        self.start_edit = QDateEdit(QDate(start_d.year, start_d.month, start_d.day))
        self.start_edit.setCalendarPopup(True)
        self.start_edit.setDisplayFormat("yyyy-MM-dd")
        self.end_edit = QDateEdit(QDate(end_d.year, end_d.month, end_d.day))
        self.end_edit.setCalendarPopup(True)
        self.end_edit.setDisplayFormat("yyyy-MM-dd")

        self.status_combo = QComboBox()
        self.status_combo.addItems(
            ["ALL", "ORDER", "WORKING", "DONE", "PARTDONE", "CANCEL", "SHORTAGE"]
        )
        self.status_combo.setEditable(True)

        self.out_div_combo = QComboBox()
        self.out_div_combo.addItems(["1", "2", "COUPANG", "ONE_DAY"])
        self.out_div_combo.setEditable(True)

        self.refresh_btn = QPushButton("조회")
        self.refresh_btn.clicked.connect(self._refresh)
        self.create_btn = QPushButton("출고 생성")
        self.create_btn.clicked.connect(self._create_delivery)
        self.update_btn = QPushButton("출고 수정")
        self.update_btn.clicked.connect(self._update_delivery)
        self.cancel_btn = QPushButton("출고 취소")
        self.cancel_btn.clicked.connect(self._cancel_delivery)
        self.export_btn = QPushButton("CSV 저장")
        self.export_btn.clicked.connect(
            lambda: _export_table_to_csv(self.table, self, "fassto_delivery")
        )
        self.status = QLabel("")

        top.addWidget(QLabel("시작"))
        top.addWidget(self.start_edit)
        top.addWidget(QLabel("종료"))
        top.addWidget(self.end_edit)
        top.addWidget(_DatePresetBar(self.start_edit, self.end_edit))
        top.addWidget(QLabel("상태"))
        top.addWidget(self.status_combo)
        top.addWidget(QLabel("출고구분"))
        top.addWidget(self.out_div_combo)
        top.addWidget(self.refresh_btn)
        top.addWidget(self.create_btn)
        top.addWidget(self.update_btn)
        top.addWidget(self.cancel_btn)
        top.addWidget(self.export_btn)

        detail_row = QHBoxLayout()
        self.slip_edit = QLineEdit()
        self.slip_edit.setPlaceholderText("전표번호(slipNo)")
        self.detail_btn = QPushButton("상세")
        self.detail_btn.clicked.connect(self._refresh_detail)
        detail_row.addWidget(self.slip_edit, 1)
        detail_row.addWidget(self.detail_btn)
        detail_row.addWidget(self.status, 2)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSortingEnabled(True)
        self.table.cellClicked.connect(self._on_row_clicked)
        self._rows: List[FasstoDeliveryRow] = []

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.table)

        detail_box = QGroupBox("상세 — 헤더")
        detail_layout = QVBoxLayout(detail_box)
        self.header_view = QTextEdit()
        self.header_view.setReadOnly(True)
        self.header_view.setPlaceholderText("전표 선택 시 헤더가 표시됩니다.")
        self.header_view.setMaximumHeight(160)
        detail_layout.addWidget(self.header_view)

        detail_goods = QGroupBox("상세 — 품목")
        goods_layout = QVBoxLayout(detail_goods)
        self.detail_table = QTableWidget(0, len(self.DETAIL_COLS))
        self.detail_table.setHorizontalHeaderLabels(self.DETAIL_COLS)
        self.detail_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.detail_table.setSortingEnabled(True)
        goods_layout.addWidget(self.detail_table)

        sub = QSplitter(Qt.Vertical)
        sub.addWidget(detail_box)
        sub.addWidget(detail_goods)
        sub.setStretchFactor(0, 1)
        sub.setStretchFactor(1, 2)
        splitter.addWidget(sub)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        layout.addLayout(top)
        layout.addLayout(detail_row)
        layout.addWidget(splitter, 1)

    def _refresh(self) -> None:
        if not self._tab.require_configured(self):
            return
        r = _normalize_range(self.start_edit, self.end_edit, self)
        if not r:
            return
        start, end = r
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
            self._rows = result.data
            rows: List[List[Any]] = []
            status_counter: Dict[str, int] = {}
            for r_ in self._rows:
                key = r_.statusNm or r_.status or ""
                status_counter[key] = status_counter.get(key, 0) + 1
                rows.append(
                    [
                        r_.slipNo,
                        _fmt_yyyymmdd(r_.outDt) if r_.outDt else "",
                        _fmt_yyyymmdd(r_.ordDt),
                        r_.salChanel or "",
                        _delivery_status_cell(r_.status or "", r_.statusNm or ""),
                        r_.outDivNm or r_.outDiv or "",
                        _num_cell(r_.ordQty),
                        r_.custNm or "",
                        r_.custTelNo or "",
                        r_.invoiceNo or "",
                        r_.parcelNm or r_.parcelCd or "",
                        r_.whNm or "",
                        r_.updTime or "",
                    ]
                )
            _fill_table(self.table, self.COLUMNS, rows)
            status_line = " · ".join(
                f"{k}: {v}" for k, v in sorted(status_counter.items())
            )
            self.status.setText(f"총 {len(rows)}건" + (f" — {status_line}" if status_line else ""))

        _run_async(self, work, done)

    def _on_row_clicked(self, row: int, _col: int) -> None:
        item = self.table.item(row, 0)
        if item is not None:
            self.slip_edit.setText(item.text())

    def _selected_payload(self) -> Dict[str, Any]:
        slip = self.slip_edit.text().strip()
        for row in self._rows:
            if row.slipNo == slip and isinstance(row.raw, dict):
                return dict(row.raw)
        payload: Dict[str, Any] = {}
        if slip:
            payload["slipNo"] = slip
        return payload

    def _run_write(
        self,
        *,
        title: str,
        payload: Dict[str, Any],
        call: Callable[[List[Dict[str, Any]]], Any],
    ) -> None:
        self.status.setText(f"{title} 요청 중...")
        for button in (self.create_btn, self.update_btn, self.cancel_btn):
            button.setEnabled(False)

        def done(result: JobResult) -> None:
            for button in (self.create_btn, self.update_btn, self.cancel_btn):
                button.setEnabled(True)
            if not result.ok:
                self.status.setText(f"❌ {title} 실패: {result.error}")
                return
            write_error = _fassto_write_error(result.data)
            if write_error:
                self.status.setText(f"❌ {title} 실패: {write_error}")
                _show_write_result(self, f"{title} 실패 응답", result.data)
                return
            self.status.setText(f"✅ {title} 완료")
            _show_write_result(self, f"{title} 응답", result.data)
            self._refresh()

        _run_async(self, lambda: call([payload]), done)

    def _create_delivery(self) -> None:
        if not self._tab.require_configured(self):
            return
        self.status.setText("파스토 상품 불러오는 중...")
        self.create_btn.setEnabled(False)

        def work() -> List[FasstoGoodsRow]:
            env = self._tab.connector.get_goods_list()
            return normalize_fassto_goods(extract_fassto_list(env))

        def done(result: JobResult) -> None:
            self.create_btn.setEnabled(True)
            if not result.ok:
                self.status.setText(f"❌ 상품 조회 실패: {result.error}")
                return
            dialog = _DeliveryWriteDialog(
                self,
                title="출고 생성",
                goods=result.data,
                initial={"outDiv": self.out_div_combo.currentText() or "1"},
                warehouse_options=_unique_options(self._rows, "whCd", "whNm"),
                out_div_options=_unique_options(self._rows, "outDiv", "outDivNm")
                or [("1", "일반"), ("2", "기타")],
            )
            if dialog.exec() != QDialog.Accepted:
                return
            payload = dialog.payload()
            if payload is None:
                return
            self._run_write(
                title="출고 생성",
                payload=payload,
                call=self._tab.connector.create_delivery_parcel,
            )

        _run_async(self, work, done)

    def _update_delivery(self) -> None:
        if not self._tab.require_configured(self):
            return
        selected = self._selected_payload()
        self.status.setText("파스토 상품 불러오는 중...")
        self.update_btn.setEnabled(False)

        def work() -> List[FasstoGoodsRow]:
            env = self._tab.connector.get_goods_list()
            return normalize_fassto_goods(extract_fassto_list(env))

        def done(result: JobResult) -> None:
            self.update_btn.setEnabled(True)
            if not result.ok:
                self.status.setText(f"❌ 상품 조회 실패: {result.error}")
                return
            dialog = _DeliveryWriteDialog(
                self,
                title="출고 수정",
                goods=result.data,
                initial=selected or {"slipNo": self.slip_edit.text().strip()},
                update_mode=True,
                warehouse_options=_unique_options(self._rows, "whCd", "whNm"),
                out_div_options=_unique_options(self._rows, "outDiv", "outDivNm")
                or [("1", "일반"), ("2", "기타")],
            )
            if dialog.exec() != QDialog.Accepted:
                return
            payload = dialog.payload()
            if payload is None:
                return
            self._run_write(
                title="출고 수정",
                payload=payload,
                call=self._tab.connector.update_delivery_parcel,
            )

        _run_async(self, work, done)

    def _cancel_delivery(self) -> None:
        if not self._tab.require_configured(self):
            return
        selected = self._selected_payload()
        dialog = _DeliveryCancelDialog(self, selected or {"slipNo": self.slip_edit.text().strip()})
        if dialog.exec() != QDialog.Accepted:
            return
        payload = dialog.payload()
        if payload is None:
            return
        answer = QMessageBox.question(
            self,
            "출고 취소 확인",
            "이 출고 취소 요청을 파스토로 전송할까요?",
        )
        if answer != QMessageBox.Yes:
            return
        self._run_write(
            title="출고 취소",
            payload=payload,
            call=self._tab.connector.cancel_delivery,
        )

    def _refresh_detail(self) -> None:
        if not self._tab.require_configured(self):
            return
        slip = self.slip_edit.text().strip()
        if not slip:
            QMessageBox.information(self, "안내", "전표번호를 입력하세요.")
            return
        self.header_view.setPlainText("조회 중...")
        self.detail_btn.setEnabled(False)
        _fill_table(self.detail_table, self.DETAIL_COLS, [])

        def work() -> Any:
            return self._tab.connector.get_delivery_detail(slip)

        def done(result: JobResult) -> None:
            self.detail_btn.setEnabled(True)
            if not result.ok:
                self.header_view.setPlainText(f"❌ {result.error}")
                return
            rows = extract_fassto_list(result.data)
            if not rows:
                self.header_view.setPlainText("데이터가 없습니다.")
                return
            header = rows[0] if isinstance(rows[0], dict) else {}
            lines = [
                f"전표: {header.get('slipNo', '')} / 주문번호: {header.get('ordNo') or '-'}",
                f"출고일: {_fmt_yyyymmdd(header.get('outDt'))} / 주문일: {_fmt_yyyymmdd(header.get('ordDt'))}",
                f"채널: {header.get('salChanel', '')} / 상태: {header.get('wrkStatNm', '')} / 출고방식: {header.get('outWayNm', '')}",
                f"수령인: {header.get('custNm', '')} / {header.get('custTelNo') or ''}",
                f"주소: {header.get('custAddr') or ''}",
                f"송장: {header.get('parcelNm') or header.get('parcelCd') or ''} {header.get('invoiceNo') or ''}",
                f"창고: {header.get('whNm', '')} ({header.get('whCd', '')})",
            ]
            remark = (header.get("remark") or "").strip()
            if remark:
                lines.append(f"비고: {remark}")
            self.header_view.setPlainText("\n".join(lines))

            goods = header.get("goods") if isinstance(header.get("goods"), list) else []
            detail_rows: List[List[Any]] = []
            for g in goods:
                if not isinstance(g, dict):
                    continue
                detail_rows.append(
                    [
                        g.get("cstGodCd", ""),
                        g.get("godNm", ""),
                        _num_cell(g.get("ordQty") or g.get("outQty") or 0),
                        g.get("godDiv", ""),
                    ]
                )
            _fill_table(self.detail_table, self.DETAIL_COLS, detail_rows)

        _run_async(self, work, done)


class _DeliveryParcelSubTab(QWidget):
    """택배 출고 상세 — 지연(delayNm) / 배송누락(dlvMisYn=Y) 강조."""

    COLUMNS = (
        "전표번호",
        "포장일",
        "배송상태",
        "박스구분",
        "박스명",
        "송장번호",
        "택배사",
        "상품명",
        "포장수량",
        "SKU",
        "수취인",
        "판매처",
        "판매채널",
        "지연",
        "배송누락",
        "반품예정일",
        "주소",
    )

    def __init__(self, tab: "FasstoTab") -> None:
        super().__init__()
        self._tab = tab
        layout = QVBoxLayout(self)

        start_d, end_d = _month_range()
        top = QHBoxLayout()
        self.start_edit = QDateEdit(QDate(start_d.year, start_d.month, start_d.day))
        self.start_edit.setCalendarPopup(True)
        self.start_edit.setDisplayFormat("yyyy-MM-dd")
        self.end_edit = QDateEdit(QDate(end_d.year, end_d.month, end_d.day))
        self.end_edit.setCalendarPopup(True)
        self.end_edit.setDisplayFormat("yyyy-MM-dd")
        self.out_div_combo = QComboBox()
        self.out_div_combo.addItems(["1", "2", "COUPANG", "ONE_DAY"])
        self.out_div_combo.setEditable(True)
        self.refresh_btn = QPushButton("조회")
        self.refresh_btn.clicked.connect(self._refresh)
        self.only_problems = QCheckBox("지연/누락만")
        self.only_problems.toggled.connect(self._apply_filter)
        self.export_btn = QPushButton("CSV 저장")
        self.export_btn.clicked.connect(
            lambda: _export_table_to_csv(self.table, self, "fassto_parcel")
        )
        self.status = QLabel("")

        top.addWidget(QLabel("시작"))
        top.addWidget(self.start_edit)
        top.addWidget(QLabel("종료"))
        top.addWidget(self.end_edit)
        top.addWidget(_DatePresetBar(self.start_edit, self.end_edit))
        top.addWidget(QLabel("출고구분"))
        top.addWidget(self.out_div_combo)
        top.addWidget(self.refresh_btn)
        top.addWidget(self.only_problems)
        top.addWidget(self.export_btn)
        top.addWidget(self.status, 1)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSortingEnabled(True)

        self._rows_data: List[Any] = []

        layout.addLayout(top)
        layout.addWidget(self.table, 1)

    def _refresh(self) -> None:
        if not self._tab.require_configured(self):
            return
        r = _normalize_range(self.start_edit, self.end_edit, self)
        if not r:
            return
        start, end = r
        out_div = (self.out_div_combo.currentText() or "1").strip() or "1"

        self.status.setText("조회 중...")
        self.refresh_btn.setEnabled(False)

        def work() -> list:
            env = self._tab.connector.get_delivery_parcel_list(start, end, out_div)
            return normalize_fassto_delivery_parcels(extract_fassto_list(env))

        def done(result: JobResult) -> None:
            self.refresh_btn.setEnabled(True)
            if not result.ok:
                self.status.setText(f"❌ {result.error}")
                return
            self._rows_data = result.data
            self._apply_filter()

        _run_async(self, work, done)

    def _apply_filter(self) -> None:
        only_problems = self.only_problems.isChecked()
        rows: List[List[Any]] = []
        problems = 0
        for r in self._rows_data:
            is_miss = (r.dlvMisYn or "").upper() == "Y"
            is_delay = bool((r.delayNm or "").strip())
            is_problem = is_miss or is_delay
            if is_problem:
                problems += 1
            if only_problems and not is_problem:
                continue

            bg = _BG_DANGER if is_miss else (_BG_WARN if is_delay else None)
            fg = _FG_DANGER if is_miss else (_FG_WARN if is_delay else None)
            delay_cell = (
                Cell(text=r.delayNm or "", fg=_FG_WARN, bg=_BG_WARN)
                if is_delay
                else Cell(text=r.delayNm or "")
            )
            miss_cell = (
                Cell(text="Y", fg=_FG_DANGER, bg=_BG_DANGER)
                if is_miss
                else Cell(text=r.dlvMisYn or "")
            )
            rows.append(
                [
                    Cell(text=r.slipNo, bg=bg),
                    Cell(text=_fmt_yyyymmdd(r.packDt) if r.packDt else "", bg=bg),
                    Cell(text=r.crgStNm or r.crgSt or "", bg=bg),
                    Cell(text=r.boxDivNm or "", bg=bg),
                    Cell(text=r.boxNm or "", bg=bg),
                    Cell(text=r.invoiceNo or "", bg=bg),
                    Cell(text=r.parcelNm or r.parcelCd or "", bg=bg),
                    Cell(text=r.godNm or "", bg=bg),
                    _num_cell(r.packQty, bg=bg),
                    _num_cell(r.sku, bg=bg),
                    Cell(text=r.custNm or "", bg=bg),
                    Cell(text=r.shopNm or "", bg=bg),
                    Cell(text=r.salChanel or "", bg=bg),
                    delay_cell,
                    miss_cell,
                    Cell(text=_fmt_yyyymmdd(r.rtnOrdDt) if r.rtnOrdDt else "", bg=bg),
                    Cell(text=(r.custAddr or "")[:40], bg=bg),
                ]
            )
        _fill_table(self.table, self.COLUMNS, rows)
        self.status.setText(
            f"표시 {len(rows)}건 / 전체 {len(self._rows_data)}건 · 지연+누락 {problems}건"
        )


class _RevenueSubTab(QWidget):
    """출고 상품 상세 — 요약 + TOP 상품 + 일별 추이 + 원본 테이블."""

    COLUMNS = (
        "출고일",
        "전표번호",
        "판매채널",
        "주문번호",
        "상품주문번호",
        "수취인",
        "상품코드",
        "상품명",
        "상품구분",
        "출고수량",
        "정상가",
        "판매가",
        "할인액",
        "판매자할인",
        "네이버할인",
        "소계(판매)",
    )
    TOP_COLS = ("순위", "상품코드", "상품명", "수량", "실매출", "비중%")
    DAILY_COLS = ("일자", "건수", "수량", "실매출")
    CHANNEL_COLS = ("판매채널", "건수", "수량합", "실매출", "비중%")

    def __init__(self, tab: "FasstoTab") -> None:
        super().__init__()
        self._tab = tab
        layout = QVBoxLayout(self)

        start_d, end_d = _month_range()
        top = QHBoxLayout()
        self.start_edit = QDateEdit(QDate(start_d.year, start_d.month, start_d.day))
        self.start_edit.setCalendarPopup(True)
        self.start_edit.setDisplayFormat("yyyy-MM-dd")
        self.end_edit = QDateEdit(QDate(end_d.year, end_d.month, end_d.day))
        self.end_edit.setCalendarPopup(True)
        self.end_edit.setDisplayFormat("yyyy-MM-dd")
        self.refresh_btn = QPushButton("조회")
        self.refresh_btn.clicked.connect(self._refresh)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("상품명/코드/채널/주문번호 검색")
        self.search_edit.textChanged.connect(self._apply_filter)
        self.export_btn = QPushButton("CSV 저장")
        self.export_btn.clicked.connect(
            lambda: _export_table_to_csv(self.table, self, "fassto_revenue")
        )
        self.export_top_btn = QPushButton("TOP CSV")
        self.export_top_btn.clicked.connect(
            lambda: _export_table_to_csv(self.top_table, self, "fassto_revenue_top")
        )
        self.export_daily_btn = QPushButton("일별 CSV")
        self.export_daily_btn.clicked.connect(
            lambda: _export_table_to_csv(self.daily_table, self, "fassto_revenue_daily")
        )

        top.addWidget(QLabel("시작"))
        top.addWidget(self.start_edit)
        top.addWidget(QLabel("종료"))
        top.addWidget(self.end_edit)
        top.addWidget(_DatePresetBar(self.start_edit, self.end_edit))
        top.addWidget(self.refresh_btn)
        top.addWidget(self.search_edit, 1)
        top.addWidget(self.export_btn)
        top.addWidget(self.export_top_btn)
        top.addWidget(self.export_daily_btn)

        # 요약 박스 (8지표)
        summary_box = QGroupBox("요약")
        grid = QGridLayout(summary_box)
        self.lbl_rows = QLabel("-")
        self.lbl_qty = QLabel("-")
        self.lbl_gross = QLabel("-")
        self.lbl_selling = QLabel("-")
        self.lbl_dc = QLabel("-")
        self.lbl_seller_dc = QLabel("-")
        self.lbl_naver_dc = QLabel("-")
        self.lbl_avg_price = QLabel("-")
        for col, (label, widget) in enumerate(
            [
                ("건수", self.lbl_rows),
                ("총수량", self.lbl_qty),
                ("정가매출", self.lbl_gross),
                ("실매출(판매가)", self.lbl_selling),
                ("할인합", self.lbl_dc),
                ("판매자부담", self.lbl_seller_dc),
                ("네이버부담", self.lbl_naver_dc),
                ("평균판매가", self.lbl_avg_price),
            ]
        ):
            lbl = QLabel(label)
            lbl.setStyleSheet("color: #666;")
            grid.addWidget(lbl, 0, col)
            widget.setStyleSheet("font-weight: bold;")
            grid.addWidget(widget, 1, col)

        # 3개 요약 테이블을 좌우로
        tri = QSplitter(Qt.Horizontal)

        channel_box = QGroupBox("채널별 매출")
        cb_layout = QVBoxLayout(channel_box)
        self.channel_table = QTableWidget(0, len(self.CHANNEL_COLS))
        self.channel_table.setHorizontalHeaderLabels(self.CHANNEL_COLS)
        self.channel_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.channel_table.setSortingEnabled(True)
        cb_layout.addWidget(self.channel_table)
        tri.addWidget(channel_box)

        top_box = QGroupBox("상품 TOP 15 (실매출 기준)")
        tb_layout = QVBoxLayout(top_box)
        self.top_table = QTableWidget(0, len(self.TOP_COLS))
        self.top_table.setHorizontalHeaderLabels(self.TOP_COLS)
        self.top_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.top_table.setSortingEnabled(True)
        tb_layout.addWidget(self.top_table)
        tri.addWidget(top_box)

        daily_box = QGroupBox("일별 매출 추이")
        db_layout = QVBoxLayout(daily_box)
        self.daily_table = QTableWidget(0, len(self.DAILY_COLS))
        self.daily_table.setHorizontalHeaderLabels(self.DAILY_COLS)
        self.daily_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.daily_table.setSortingEnabled(True)
        db_layout.addWidget(self.daily_table)
        tri.addWidget(daily_box)

        tri.setStretchFactor(0, 2)
        tri.setStretchFactor(1, 3)
        tri.setStretchFactor(2, 2)

        self.status = QLabel("")

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSortingEnabled(True)

        layout.addLayout(top)
        layout.addWidget(summary_box)
        layout.addWidget(tri, 1)
        layout.addWidget(self.status)
        layout.addWidget(self.table, 2)

        self._all_rows: List[FasstoDeliveryGoodDetailRow] = []

    def _refresh(self) -> None:
        if not self._tab.require_configured(self):
            return
        r = _normalize_range(self.start_edit, self.end_edit, self)
        if not r:
            return
        start, end = r
        self.status.setText("조회 중...")
        self.refresh_btn.setEnabled(False)

        def work() -> list:
            env = self._tab.connector.get_delivery_good_detail_list(start, end)
            return normalize_fassto_delivery_good_details(extract_fassto_list(env))

        def done(result: JobResult) -> None:
            self.refresh_btn.setEnabled(True)
            if not result.ok:
                self.status.setText(f"❌ {result.error}")
                return
            self._all_rows = result.data
            self._render_summary()
            self._apply_filter()

        _run_async(self, work, done)

    def _render_summary(self) -> None:
        summary = summarize_delivery_good_details(self._all_rows)
        total_selling = summary["sellingAmount"]
        self.lbl_rows.setText(_fmt_num(summary["rowCount"]))
        self.lbl_qty.setText(_fmt_num(summary["totalQty"]))
        self.lbl_gross.setText(_fmt_money(summary["grossAmount"]))
        self.lbl_selling.setText(_fmt_money(total_selling))
        self.lbl_dc.setText(_fmt_money(summary["discountAmount"]))
        self.lbl_seller_dc.setText(_fmt_money(summary["sellerDiscountAmount"]))
        self.lbl_naver_dc.setText(_fmt_money(summary["naverDiscountAmount"]))
        avg = (total_selling / summary["totalQty"]) if summary["totalQty"] else 0
        self.lbl_avg_price.setText(_fmt_money(avg))

        # --- 채널별
        by_channel: Dict[str, Dict[str, float]] = {}
        for r in self._all_rows:
            key = r.sellerChannel or "(미지정)"
            b = by_channel.setdefault(key, {"rows": 0.0, "qty": 0.0, "rev": 0.0})
            b["rows"] += 1
            b["qty"] += r.outQty
            b["rev"] += r.sellingPrAmount * r.outQty
        ch_rows: List[List[Any]] = []
        for ch, v in sorted(by_channel.items(), key=lambda kv: -kv[1]["rev"]):
            pct = (v["rev"] / total_selling * 100) if total_selling else 0
            ch_rows.append(
                [
                    ch,
                    _num_cell(v["rows"]),
                    _num_cell(v["qty"]),
                    _num_cell(v["rev"], _fmt_money),
                    _num_cell(pct, lambda x: f"{float(x):.1f}%"),
                ]
            )
        _fill_table(self.channel_table, self.CHANNEL_COLS, ch_rows)

        # --- 상품 TOP 15
        by_prod: Dict[Tuple[str, str], Dict[str, float]] = {}
        for r in self._all_rows:
            key = (r.cstGodCd or "", r.godNm or "")
            b = by_prod.setdefault(key, {"qty": 0.0, "rev": 0.0})
            b["qty"] += r.outQty
            b["rev"] += r.sellingPrAmount * r.outQty
        prod_sorted = sorted(by_prod.items(), key=lambda kv: -kv[1]["rev"])
        top_rows: List[List[Any]] = []
        for rank, ((code, nm), v) in enumerate(prod_sorted[:15], start=1):
            pct = (v["rev"] / total_selling * 100) if total_selling else 0
            top_rows.append(
                [
                    _num_cell(rank),
                    code,
                    nm,
                    _num_cell(v["qty"]),
                    _num_cell(v["rev"], _fmt_money),
                    _num_cell(pct, lambda x: f"{float(x):.1f}%"),
                ]
            )
        _fill_table(self.top_table, self.TOP_COLS, top_rows)

        # --- 일별 추이
        by_day: Dict[str, Dict[str, float]] = {}
        for r in self._all_rows:
            d = _fmt_yyyymmdd(r.outDt) if r.outDt else ""
            b = by_day.setdefault(d, {"rows": 0.0, "qty": 0.0, "rev": 0.0})
            b["rows"] += 1
            b["qty"] += r.outQty
            b["rev"] += r.sellingPrAmount * r.outQty
        max_rev = max((v["rev"] for v in by_day.values()), default=0)
        daily_rows: List[List[Any]] = []
        for d, v in sorted(by_day.items()):
            bg = None
            if max_rev > 0 and v["rev"] >= max_rev * 0.8:
                bg = _BG_OK
            daily_rows.append(
                [
                    Cell(text=d, bg=bg),
                    _num_cell(v["rows"], bg=bg),
                    _num_cell(v["qty"], bg=bg),
                    _num_cell(v["rev"], _fmt_money, bg=bg),
                ]
            )
        _fill_table(self.daily_table, self.DAILY_COLS, daily_rows)

    def _apply_filter(self) -> None:
        q = self.search_edit.text().strip().lower()
        rows_src = self._all_rows
        if q:
            rows_src = [
                r
                for r in self._all_rows
                if any(
                    q in str(v).lower()
                    for v in (
                        r.godNm,
                        r.cstGodCd,
                        r.sellerChannel,
                        r.orderNo,
                        r.productOrderNo,
                        r.custNm,
                    )
                    if v
                )
            ]
        table_rows: List[List[Any]] = []
        for r in rows_src:
            subtotal = r.sellingPrAmount * r.outQty
            table_rows.append(
                [
                    _fmt_yyyymmdd(r.outDt),
                    r.slipNo,
                    r.sellerChannel or "",
                    r.orderNo or "",
                    r.productOrderNo or "",
                    r.custNm or "",
                    r.cstGodCd or "",
                    r.godNm or "",
                    r.godDiv or "",
                    _num_cell(r.outQty),
                    _num_cell(r.markedPrAmount, _fmt_money),
                    _num_cell(r.sellingPrAmount, _fmt_money),
                    _num_cell(r.dcAmount, _fmt_money),
                    _num_cell(r.sellerDcAmount, _fmt_money),
                    _num_cell(r.naverDcAmount, _fmt_money),
                    _num_cell(subtotal, _fmt_money),
                ]
            )
        _fill_table(self.table, self.COLUMNS, table_rows)
        self.status.setText(
            f"표시 {len(table_rows)}건 (전체 {len(self._all_rows)}건)"
        )


# ---------------------------------------------------------------------------
# Public tab
# ---------------------------------------------------------------------------


class FasstoTab(QWidget):
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
        self._elements = _GoodsElementSubTab(self)
        self._stock = _StockSubTab(self)
        self._warehousing = _WarehousingSubTab(self)
        self._delivery = _DeliverySubTab(self)
        self._parcel = _DeliveryParcelSubTab(self)
        self._revenue = _RevenueSubTab(self)

        self._inner.addTab(self._overview, "개요")
        self._inner.addTab(self._goods, "상품")
        self._inner.addTab(self._elements, "세트상품")
        self._inner.addTab(self._stock, "재고")
        self._inner.addTab(self._warehousing, "입고")
        self._inner.addTab(self._delivery, "출고")
        self._inner.addTab(self._parcel, "택배출고")
        self._inner.addTab(self._revenue, "매출 상세")

        layout.addWidget(self._inner, 1)

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
