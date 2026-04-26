from __future__ import annotations

from pathlib import Path
from typing import Any, List

from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from inventory_app.models import PurchaseRecord
from inventory_app.services.purchase_crawler import (
    CrawlerProgress,
    CrawlResult,
    PlaywrightUnavailable,
    crawl_channel,
    ensure_browser_installed,
)
from inventory_app.services.purchase_history_service import (
    PurchaseHistoryParser,
    PurchaseHistoryStore,
)


class _NumberItem(QTableWidgetItem):
    def __lt__(self, other: QTableWidgetItem) -> bool:
        left = self.data(Qt.UserRole)
        right = other.data(Qt.UserRole)
        if left is not None and right is not None:
            return left < right
        return super().__lt__(other)


class _CrawlerWorker(QObject):
    """QThread 안에서 실행되는 자동 크롤링 워커.

    UI 스레드를 막지 않으면서 Playwright 브라우저를 띄움.
    """

    log = Signal(str)
    login_required = Signal(str)
    finished = Signal(object)  # CrawlResult

    def __init__(
        self,
        channel: str,
        *,
        headless: bool,
        max_pages: int,
        reset_session: bool,
    ) -> None:
        super().__init__()
        self.channel = channel
        self.headless = headless
        self.max_pages = max_pages
        self.reset_session = reset_session
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        progress = CrawlerProgress(
            on_log=lambda msg: self.log.emit(str(msg)),
            on_login_required=lambda msg: self.login_required.emit(str(msg)),
            cancelled=lambda: self._cancelled,
        )
        try:
            ensure_browser_installed(progress)
        except PlaywrightUnavailable as exc:
            self.finished.emit(
                CrawlResult(channel=self.channel, records=[], error=str(exc))
            )
            return
        except Exception as exc:  # noqa: BLE001
            self.finished.emit(
                CrawlResult(channel=self.channel, records=[], error=f"브라우저 준비 실패: {exc}")
            )
            return

        result = crawl_channel(
            self.channel,
            headless=self.headless,
            max_pages=self.max_pages,
            reset_session=self.reset_session,
            progress=progress,
        )
        self.finished.emit(result)


class PurchaseHistoryTab(QWidget):
    CHANNEL_LABELS = {
        "all": "전체",
        "naver": "네이버",
        "coupang": "쿠팡",
    }
    ORDER_URLS = {
        "naver": "https://order.pay.naver.com/home",
        "coupang": "https://mc.coupang.com/ssr/desktop/order/list",
    }
    HEADERS = ("일자", "채널", "주문번호", "상품/내역", "결제금액", "결제수단", "가져온 시각")

    def __init__(self) -> None:
        super().__init__()
        self.store = PurchaseHistoryStore()
        self.parser = PurchaseHistoryParser()
        self._worker_thread: QThread | None = None
        self._worker: _CrawlerWorker | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        # --- 1행: 필터/수동 가져오기 ---
        top = QHBoxLayout()
        self.channel_combo = QComboBox()
        for code, label in self.CHANNEL_LABELS.items():
            self.channel_combo.addItem(label, code)
        self.channel_combo.currentIndexChanged.connect(self.reload)

        self.open_naver_btn = QPushButton("네이버 주문내역 열기")
        self.open_naver_btn.clicked.connect(lambda: self._open_order_page("naver"))
        self.open_coupang_btn = QPushButton("쿠팡 주문내역 열기")
        self.open_coupang_btn.clicked.connect(lambda: self._open_order_page("coupang"))
        self.import_btn = QPushButton("HTML 가져오기")
        self.import_btn.clicked.connect(self._import_html)
        self.reload_btn = QPushButton("새로고침")
        self.reload_btn.clicked.connect(self.reload)

        top.addWidget(QLabel("채널"))
        top.addWidget(self.channel_combo)
        top.addWidget(self.open_naver_btn)
        top.addWidget(self.open_coupang_btn)
        top.addWidget(self.import_btn)
        top.addWidget(self.reload_btn)
        top.addStretch(1)

        # --- 2행: 자동 수집 ---
        auto_row = QHBoxLayout()
        self.auto_naver_btn = QPushButton("🤖 네이버 자동 수집")
        self.auto_naver_btn.setToolTip(
            "Chromium 창이 뜨면 첫 1회만 직접 로그인하세요.\n"
            "이후엔 저장된 세션으로 자동 로그인되어 주문내역을 긁어옵니다."
        )
        self.auto_naver_btn.clicked.connect(lambda: self._start_auto_crawl("naver"))

        self.auto_coupang_btn = QPushButton("🤖 쿠팡 자동 수집")
        self.auto_coupang_btn.setToolTip(
            "Chromium 창이 뜨면 첫 1회만 직접 로그인하세요.\n"
            "이후엔 저장된 세션으로 자동 로그인되어 주문내역을 긁어옵니다."
        )
        self.auto_coupang_btn.clicked.connect(lambda: self._start_auto_crawl("coupang"))

        self.reset_session_chk = QCheckBox("세션 초기화(재로그인)")
        self.reset_session_chk.setToolTip(
            "체크하면 저장된 로그인 세션을 폐기하고 처음부터 로그인합니다."
        )

        self.cancel_auto_btn = QPushButton("취소")
        self.cancel_auto_btn.clicked.connect(self._cancel_auto)
        self.cancel_auto_btn.setEnabled(False)

        self.status = QLabel("")
        self.status.setWordWrap(True)

        auto_row.addWidget(self.auto_naver_btn)
        auto_row.addWidget(self.auto_coupang_btn)
        auto_row.addWidget(self.reset_session_chk)
        auto_row.addWidget(self.cancel_auto_btn)
        auto_row.addWidget(self.status, 1)

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setStretchLastSection(True)

        layout.addLayout(top)
        layout.addLayout(auto_row)
        layout.addWidget(self.table, 1)
        self.reload()

    def _selected_channel(self) -> str:
        return str(self.channel_combo.currentData() or "all")

    def _selected_import_channel(self) -> str:
        channel = self._selected_channel()
        if channel in {"naver", "coupang"}:
            return channel
        return "naver"

    def _open_order_page(self, channel: str) -> None:
        url = self.ORDER_URLS.get(channel)
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _import_html(self) -> None:
        channel = self._selected_import_channel()
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "구매내역 HTML 선택",
            "",
            "HTML files (*.html *.htm);;All files (*.*)",
        )
        if not path:
            return
        try:
            records = self.parser.parse_html_file(channel, Path(path))
            added = self.store.save_records(records)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "가져오기 실패", str(exc))
            return
        self.status.setText(f"{self.CHANNEL_LABELS[channel]} {len(records)}건 분석, {added}건 추가")
        self.reload()

    # 취소/환불 키워드 (제목 안에서 탐지)
    _CANCEL_KEYWORDS = ("결제취소", "취소완료", "주문취소", "구매취소", "환불완료", "환불")

    @classmethod
    def _is_cancelled(cls, record: PurchaseRecord) -> bool:
        title = str(record.title or "")
        for kw in cls._CANCEL_KEYWORDS:
            if kw in title:
                return True
        return False

    @classmethod
    def _signed_amount(cls, record: PurchaseRecord) -> int:
        """취소 거래는 음수, 정상 거래는 양수."""
        amt = int(record.amount or 0)
        return -amt if cls._is_cancelled(record) else amt

    def reload(self) -> None:
        channel = self._selected_channel()
        rows = self.store.load_records(channel=channel)
        self._render(rows)
        # 정상 합계와 취소 차감을 분리해서 표시 (실수령/실지출 직관적으로)
        gross = sum(int(row.amount or 0) for row in rows if not self._is_cancelled(row))
        cancelled = sum(int(row.amount or 0) for row in rows if self._is_cancelled(row))
        net = gross - cancelled
        if cancelled > 0:
            self.status.setText(
                f"{len(rows):,}건 · 결제 {gross:,}원 - 취소 {cancelled:,}원 = "
                f"<b>순 {net:,}원</b>"
            )
        else:
            self.status.setText(f"{len(rows):,}건 / {net:,}원")

    def _render(self, rows: List[PurchaseRecord]) -> None:
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        red_brush = QBrush(QColor("#dc2626"))  # 빨강
        gray_brush = QBrush(QColor("#9ca3af"))  # 취소된 행의 다른 컬럼 (옅은 회색)
        for row_idx, record in enumerate(rows):
            cancelled = self._is_cancelled(record)
            signed_amt = self._signed_amount(record)

            values: list[Any] = [
                record.order_date or "-",
                self.CHANNEL_LABELS.get(record.channel, record.channel),
                record.order_no or "-",
                record.title,
                record.amount,
                record.payment_method or "-",
                record.imported_at.strftime("%Y-%m-%d %H:%M"),
            ]
            for col_idx, value in enumerate(values):
                if col_idx == 4:
                    # 금액: 취소면 음수 + 빨강, 정상이면 그대로
                    if cancelled:
                        text = f"-{int(value or 0):,}원" if value else "-"
                    else:
                        text = f"{int(value or 0):,}원" if value else "-"
                    item = _NumberItem(text)
                    item.setData(Qt.UserRole, signed_amt)  # 정렬용 signed 값
                    if cancelled:
                        item.setForeground(red_brush)
                        # 굵게 강조
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)
                else:
                    item = QTableWidgetItem(str(value))
                    item.setData(Qt.UserRole, str(value))
                    if cancelled and col_idx != 3:
                        # 상품명 외 다른 셀은 옅은 회색으로 (시각적 약화)
                        item.setForeground(gray_brush)
                    elif cancelled and col_idx == 3:
                        # 상품명도 빨강으로
                        item.setForeground(red_brush)

                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if col_idx == 3:
                    item.setToolTip(record.raw_text[:1000])
                self.table.setItem(row_idx, col_idx, item)
        self.table.resizeColumnsToContents()
        self.table.setSortingEnabled(True)

    # ------------------------------------------------------------------
    # 자동 수집 (Playwright)
    # ------------------------------------------------------------------

    def _set_auto_busy(self, busy: bool) -> None:
        for btn in (
            self.auto_naver_btn,
            self.auto_coupang_btn,
            self.import_btn,
            self.open_naver_btn,
            self.open_coupang_btn,
            self.reload_btn,
            self.reset_session_chk,
        ):
            btn.setEnabled(not busy)
        self.cancel_auto_btn.setEnabled(busy)

    def _start_auto_crawl(self, channel: str) -> None:
        if self._worker_thread is not None and self._worker_thread.isRunning():
            QMessageBox.information(self, "안내", "이미 수집 작업이 진행 중입니다.")
            return

        thread = QThread(self)
        worker = _CrawlerWorker(
            channel,
            headless=False,
            max_pages=5,
            reset_session=self.reset_session_chk.isChecked(),
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.log.connect(self._on_crawler_log)
        worker.login_required.connect(self._on_login_required)
        worker.finished.connect(self._on_crawler_finished)

        def _cleanup(_result: object = None) -> None:
            try:
                thread.quit()
                thread.wait(1000)
            finally:
                worker.deleteLater()
                thread.deleteLater()
                if self._worker_thread is thread:
                    self._worker_thread = None
                    self._worker = None
                self._set_auto_busy(False)

        worker.finished.connect(_cleanup)

        self._worker_thread = thread
        self._worker = worker
        self.reset_session_chk.setChecked(False)
        self._set_auto_busy(True)
        self.status.setText(
            f"{self.CHANNEL_LABELS.get(channel, channel)} 자동 수집 시작... 브라우저 창에서 로그인하세요."
        )
        thread.start()

    def _cancel_auto(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self.status.setText("취소 요청됨 — 진행 중 작업이 끝나는 즉시 중단됩니다.")

    def _on_crawler_log(self, msg: str) -> None:
        self.status.setText(msg)

    def _on_login_required(self, msg: str) -> None:
        # 로그 라벨에 표시. 사용자가 브라우저 창에서 직접 로그인.
        self.status.setText(f"⚠ {msg}")

    def _on_crawler_finished(self, result: object) -> None:
        if not isinstance(result, CrawlResult):
            self.status.setText("수집 종료(알 수 없는 응답)")
            return
        channel_label = self.CHANNEL_LABELS.get(result.channel, result.channel)
        if result.error:
            QMessageBox.warning(
                self,
                f"{channel_label} 자동 수집 실패",
                str(result.error)[:1500],
            )
            self.status.setText(f"❌ {channel_label} 실패: {result.error[:120]}")
            return

        added = 0
        try:
            added = self.store.save_records(result.records)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "저장 실패", str(exc))
        self.status.setText(
            f"✅ {channel_label} 자동 수집 완료: {len(result.records)}건 추출, {added}건 신규 저장"
        )
        self.reload()

    def shutdown(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
        if self._worker_thread is not None and self._worker_thread.isRunning():
            self._worker_thread.quit()
            self._worker_thread.wait(2000)
        return

