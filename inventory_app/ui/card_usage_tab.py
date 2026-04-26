"""카드 사용내역 탭.

docs/card-api-relocation-design.md 의 외부 card-api-service 를 호출해
조회/수정/동기화/매칭을 수행한다.

탭 구조:
- 상단 필터: 시작일, 종료일, 카드번호, 가맹점, 페이지 크기, 페이지 이동
- 동기화/매칭 버튼: 외부 API 의 sync/match 호출 (백그라운드)
- 본문 테이블: 사용일자/카드/가맹점/금액/카테고리/메모/검토/매칭ID
- 하단 상태바: 합계/요약 + 진행 메시지
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QDate, QObject, Qt, QThread, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from inventory_app.config import AppConfig
from inventory_app.models import CardUsage
from inventory_app.services.barobill_card_client import (
    BarobillCardClient,
    BarobillError,
)


HEADERS = (
    "사용일시", "카드", "가맹점", "금액", "카테고리", "메모", "검토", "매칭",
)


def _default_range() -> tuple[date, date]:
    today = date.today()
    return today - timedelta(days=90), today


class _NumberItem(QTableWidgetItem):
    def __lt__(self, other: QTableWidgetItem) -> bool:
        l = self.data(Qt.UserRole)
        r = other.data(Qt.UserRole)
        if l is not None and r is not None:
            return l < r
        return super().__lt__(other)


@dataclass
class _JobResult:
    ok: bool
    data: Any = None
    error: Optional[str] = None


class _ApiJob(QObject):
    """단일 카드 API 호출 워커."""

    finished = Signal(object)

    def __init__(self, func: Callable[[], Any]) -> None:
        super().__init__()
        self._func = func

    def run(self) -> None:
        try:
            data = self._func()
            self.finished.emit(_JobResult(ok=True, data=data))
        except BarobillError as exc:
            self.finished.emit(_JobResult(ok=False, error=f"[{exc.code or ''}] {exc}"))
        except Exception as exc:  # noqa: BLE001
            self.finished.emit(_JobResult(ok=False, error=str(exc)))


def _run_async(parent: QObject, func: Callable[[], Any], on_done: Callable[[_JobResult], None]) -> None:
    thread = QThread(parent)
    worker = _ApiJob(func)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)

    def _cleanup(result: _JobResult) -> None:
        try:
            on_done(result)
        finally:
            thread.quit()
            thread.wait()
            worker.deleteLater()
            thread.deleteLater()

    worker.finished.connect(_cleanup)
    thread.start()


class CardUsageTab(QWidget):
    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._config = config
        self.client = BarobillCardClient.from_app_config(config)

        self._page = 1
        self._total_pages = 1
        self._page_size = 500          # 바로빌은 한 호출당 다 가져옴 (페이지네이션 내부 처리)
        self._items: List[CardUsage] = []
        self._all_items: List[CardUsage] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        # ===== 1행: 기간/필터 =====
        row1 = QHBoxLayout()
        start_d, end_d = _default_range()
        self.start_edit = QDateEdit(QDate(start_d.year, start_d.month, start_d.day))
        self.start_edit.setCalendarPopup(True)
        self.start_edit.setDisplayFormat("yyyy-MM-dd")
        self.end_edit = QDateEdit(QDate(end_d.year, end_d.month, end_d.day))
        self.end_edit.setCalendarPopup(True)
        self.end_edit.setDisplayFormat("yyyy-MM-dd")

        self.card_num_edit = QLineEdit()
        self.card_num_edit.setPlaceholderText("카드번호 일부")
        self.card_num_edit.setFixedWidth(140)

        self.store_edit = QLineEdit()
        self.store_edit.setPlaceholderText("가맹점명")
        self.store_edit.setFixedWidth(160)

        self.page_size_spin = QSpinBox()
        self.page_size_spin.setRange(10, 1000)
        self.page_size_spin.setValue(100)
        self.page_size_spin.setFixedWidth(80)

        self.refresh_btn = QPushButton("조회")
        self.refresh_btn.clicked.connect(self._on_refresh)

        row1.addWidget(QLabel("기간"))
        row1.addWidget(self.start_edit)
        row1.addWidget(QLabel("~"))
        row1.addWidget(self.end_edit)
        row1.addWidget(QLabel("카드"))
        row1.addWidget(self.card_num_edit)
        row1.addWidget(QLabel("가맹점"))
        row1.addWidget(self.store_edit)
        row1.addWidget(QLabel("페이지크기"))
        row1.addWidget(self.page_size_spin)
        row1.addWidget(self.refresh_btn)
        row1.addStretch(1)

        # ===== 2행: 동기화/매칭 + 페이지 이동 =====
        row2 = QHBoxLayout()
        self.refresh_before_chk = QCheckBox("동기화 시 갱신(refreshBeforeFetch)")
        self.refresh_before_chk.setToolTip("외부 API 가 캐시를 무시하고 카드사로부터 새로 받아오도록 강제")

        self.sync_btn = QPushButton("🔄 카드 동기화")
        self.sync_btn.clicked.connect(self._on_sync_cards)
        self.match_btn = QPushButton("🔗 쿠팡 매칭")
        self.match_btn.clicked.connect(self._on_match_coupang)

        self.prev_btn = QPushButton("◀ 이전")
        self.prev_btn.clicked.connect(self._on_prev)
        self.next_btn = QPushButton("다음 ▶")
        self.next_btn.clicked.connect(self._on_next)
        self.page_label = QLabel("페이지 - / -")

        row2.addWidget(self.refresh_before_chk)
        row2.addWidget(self.sync_btn)
        row2.addWidget(self.match_btn)
        row2.addStretch(1)
        row2.addWidget(self.prev_btn)
        row2.addWidget(self.page_label)
        row2.addWidget(self.next_btn)

        # ===== 본문: 테이블 =====
        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setStretchLastSection(True)

        # ===== 상태바 =====
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.RichText)

        # 설정 미비 안내
        if not self.client.is_configured():
            miss = ", ".join(self.client.config.missing_fields())
            self.status.setText(
                f"❌ 바로빌 설정 누락 ({miss}) — credentials.json 의 "
                f"<code>barobill.certkey / corp_num / id</code> 를 채우세요."
            )

        layout.addLayout(row1)
        layout.addLayout(row2)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.status)

    # ----- helpers -----

    def _set_busy(self, busy: bool) -> None:
        for w in (
            self.refresh_btn, self.sync_btn, self.match_btn,
            self.prev_btn, self.next_btn,
        ):
            w.setEnabled(not busy)

    def _q_date_to_iso(self, qd: QDate) -> str:
        return qd.toString("yyyy-MM-dd")

    def _require_configured(self) -> bool:
        if self.client.is_configured():
            return True
        miss = ", ".join(self.client.config.missing_fields())
        QMessageBox.warning(
            self, "바로빌 설정 누락",
            f"누락된 항목: {miss}\n\ncredentials.json 의 barobill 섹션에:\n"
            "  certkey, corp_num (사업자번호), id (바로빌 계정 ID)\n"
            "을 모두 채워주세요.",
        )
        return False

    # ----- 조회 -----

    def _on_refresh(self) -> None:
        if not self._require_configured():
            return
        self._page = 1
        self._fetch_page()

    def _on_prev(self) -> None:
        if self._page > 1:
            self._page -= 1
            self._fetch_page()

    def _on_next(self) -> None:
        if self._page < self._total_pages:
            self._page += 1
            self._fetch_page()

    def _fetch_page(self) -> None:
        """바로빌 SOAP 직접 호출 → 모든 데이터 받아서 클라이언트측 페이지네이션."""
        start = self._q_date_to_iso(self.start_edit.date())
        end = self._q_date_to_iso(self.end_edit.date())
        page = self._page
        page_size = int(self.page_size_spin.value())
        card = self.card_num_edit.text().strip() or None
        store = self.store_edit.text().strip() or None
        refresh = self.refresh_before_chk.isChecked()

        self.status.setText(f"바로빌 조회 중... ({start} ~ {end}, 카드 {card or '전체'})")
        self._set_busy(True)

        def work() -> Dict[str, Any]:
            return self.client.fetch_card_usages(
                start_date=start, end_date=end,
                card_num=card, refresh_before_fetch=refresh,
            )

        def done(result: _JobResult) -> None:
            self._set_busy(False)
            if not result.ok:
                self.status.setText(f"❌ {result.error}")
                return
            data = result.data or {}
            logs: List[CardUsage] = data.get("logs") or []
            target_cards = data.get("targetCards") or []

            # 가맹점 필터 (클라이언트측)
            if store:
                logs = [it for it in logs if store.lower() in (it.store_name or "").lower()]

            # 사용일자 내림차순
            logs.sort(key=lambda it: (it.used_at or ""), reverse=True)
            self._all_items = logs

            total = len(logs)
            self._page_size = page_size
            self._total_pages = max(1, (total + page_size - 1) // page_size)
            if page > self._total_pages:
                self._page = self._total_pages
            page = max(1, self._page)
            offset = (page - 1) * page_size
            self._items = logs[offset:offset + page_size]

            self.page_label.setText(f"페이지 {page} / {self._total_pages}")
            self._render(self._items)

            page_amount = sum(int(it.amount or 0) for it in self._items)
            total_amount = sum(int(it.amount or 0) for it in self._all_items)
            cancel_amount = sum(
                abs(int(it.amount or 0))
                for it in self._all_items
                if (it.amount or 0) < 0
            )
            net = total_amount  # 음수가 이미 차감된 합계
            self.status.setText(
                f"카드 {len(target_cards)}장 · 전체 {total:,}건 · "
                f"순합계 ₩{net:,} (취소차감 ₩{cancel_amount:,}) · "
                f"이 페이지 ₩{page_amount:,}"
            )

        _run_async(self, work, done)

    # ----- 동기화 -----

    def _on_sync_cards(self) -> None:
        """동기화 = refreshBeforeFetch 강제 ON 으로 재조회."""
        if not self._require_configured():
            return
        self.refresh_before_chk.setChecked(True)
        self._page = 1
        self._fetch_page()

    def _on_match_coupang(self) -> None:
        """쿠팡 매칭은 외부 card-api-service 가 있어야 가능. 안내만."""
        QMessageBox.information(
            self, "쿠팡 매칭",
            "쿠팡 매칭 기능은 외부 card-api-service 연동이 필요합니다.\n"
            "현재는 바로빌 직접 호출만 지원하므로, 이 기능은 비활성화 상태입니다.\n\n"
            "(매칭이 필요하면 docs/card-api-relocation-design.md 의 \n"
            "card-api-service 를 별도 배포 후 연결하세요.)",
        )

    # ----- 렌더 -----

    def _render(self, items: List[CardUsage]) -> None:
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(items))
        red = QBrush(QColor("#dc2626"))
        gray = QBrush(QColor("#9ca3af"))

        for r, it in enumerate(items):
            amount = int(it.amount or 0)
            cancelled = amount < 0
            values = [
                it.used_at or "-",
                it.card_num or "-",
                it.store_name or "-",
                amount,
                it.category or "-",
                it.memo or "",
                "✓" if it.reviewed else "",
                it.coupang_purchase_id or "",
            ]
            for c, value in enumerate(values):
                if c == 3:  # 금액
                    if cancelled:
                        text = f"{amount:,}원"
                    else:
                        text = f"{amount:,}원" if value else "-"
                    item = _NumberItem(text)
                    item.setData(Qt.UserRole, amount)
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    if cancelled:
                        item.setForeground(red)
                        f = item.font(); f.setBold(True); item.setFont(f)
                else:
                    item = QTableWidgetItem(str(value))
                    item.setData(Qt.UserRole, str(value))
                    if cancelled:
                        item.setForeground(gray if c != 2 else red)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if c == 5 and it.memo:
                    item.setToolTip(it.memo)
                self.table.setItem(r, c, item)
        self.table.resizeColumnsToContents()
        self.table.setSortingEnabled(True)

    def shutdown(self) -> None:
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass


__all__ = ["CardUsageTab"]
