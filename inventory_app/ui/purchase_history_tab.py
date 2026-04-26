from __future__ import annotations

from pathlib import Path
from typing import Any, List

from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
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
from inventory_app.services.purchase_history_service import PurchaseHistoryParser, PurchaseHistoryStore


class _NumberItem(QTableWidgetItem):
    def __lt__(self, other: QTableWidgetItem) -> bool:
        left = self.data(Qt.UserRole)
        right = other.data(Qt.UserRole)
        if left is not None and right is not None:
            return left < right
        return super().__lt__(other)


class _CrawlerWorker(QObject):
    log = Signal(str)
    login_required = Signal(str)
    finished = Signal(object)

    def __init__(self, channel: str, *, headless: bool, max_pages: int, reset_session: bool) -> None:
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
            result = crawl_channel(
                self.channel,
                headless=self.headless,
                max_pages=self.max_pages,
                reset_session=self.reset_session,
                progress=progress,
            )
        except PlaywrightUnavailable as exc:
            result = CrawlResult(channel=self.channel, records=[], error=str(exc))
        except Exception as exc:  # noqa: BLE001
            result = CrawlResult(channel=self.channel, records=[], error=str(exc))
        self.finished.emit(result)


class PurchaseHistoryTab(QWidget):
    CHANNEL_LABELS = {
        "all": "\uc804\uccb4",
        "naver": "\ub124\uc774\ubc84",
        "coupang": "\ucfe0\ud321",
    }
    ORDER_URLS = {
        "naver": "https://order.pay.naver.com/home",
        "coupang": "https://mc.coupang.com/ssr/desktop/order/list",
    }
    HEADERS = (
        "\uc77c\uc790",
        "\ucc44\ub110",
        "\uc8fc\ubb38\ubc88\ud638",
        "\uc0c1\ud488/\ub0b4\uc5ed",
        "\uacb0\uc81c\uae08\uc561",
        "\uacb0\uc81c\uc218\ub2e8",
        "\uac00\uc838\uc628 \uc2dc\uac01",
    )
    CANCEL_KEYWORDS = (
        "\uacb0\uc81c\ucde8\uc18c",
        "\ucde8\uc18c\uc644\ub8cc",
        "\uc8fc\ubb38\ucde8\uc18c",
        "\uad6c\ub9e4\ucde8\uc18c",
        "\ubc18\ud488\uc644\ub8cc",
        "\ubc18\ud488",
    )

    def __init__(self) -> None:
        super().__init__()
        self.store = PurchaseHistoryStore()
        self.parser = PurchaseHistoryParser()
        self._worker_thread: QThread | None = None
        self._worker: _CrawlerWorker | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        top = QHBoxLayout()
        self.channel_combo = QComboBox()
        for code, label in self.CHANNEL_LABELS.items():
            self.channel_combo.addItem(label, code)
        self.channel_combo.currentIndexChanged.connect(self.reload)

        self.open_naver_btn = QPushButton("\ub124\uc774\ubc84 \uc8fc\ubb38\ub0b4\uc5ed \uc5f4\uae30")
        self.open_naver_btn.clicked.connect(lambda: self._open_order_page("naver"))
        self.open_coupang_btn = QPushButton("\ucfe0\ud321 \uc8fc\ubb38\ub0b4\uc5ed \uc5f4\uae30")
        self.open_coupang_btn.clicked.connect(lambda: self._open_order_page("coupang"))
        self.import_btn = QPushButton("HTML \uac00\uc838\uc624\uae30")
        self.import_btn.clicked.connect(self._import_html)
        self.clipboard_btn = QPushButton("\ud074\ub9bd\ubcf4\ub4dc \uac00\uc838\uc624\uae30")
        self.clipboard_btn.setToolTip(
            "\uc77c\ubc18 \ube0c\ub77c\uc6b0\uc800\uc5d0\uc11c \uc8fc\ubb38\ub0b4\uc5ed \ud398\uc774\uc9c0\ub97c \uc5f4\uace0 "
            "Ctrl+A, Ctrl+C \ud55c \ub2e4\uc74c \ub204\ub974\uc138\uc694."
        )
        self.clipboard_btn.clicked.connect(self._import_clipboard)
        self.reload_btn = QPushButton("\uc0c8\ub85c\uace0\uce68")
        self.reload_btn.clicked.connect(self.reload)

        top.addWidget(QLabel("\ucc44\ub110"))
        top.addWidget(self.channel_combo)
        top.addWidget(self.open_naver_btn)
        top.addWidget(self.open_coupang_btn)
        top.addWidget(self.import_btn)
        top.addWidget(self.clipboard_btn)
        top.addWidget(self.reload_btn)
        top.addStretch(1)

        auto_row = QHBoxLayout()
        self.auto_naver_btn = QPushButton("\ub124\uc774\ubc84 \uc790\ub3d9 \uc218\uc9d1")
        self.auto_naver_btn.setToolTip(
            "\ube0c\ub77c\uc6b0\uc800 \ucc3d\uc774 \uc5f4\ub9ac\uba74 \uccab 1\ud68c\ub9cc \uc9c1\uc811 \ub85c\uadf8\uc778\ud558\uc138\uc694.\n"
            "\ub2e4\uc74c\ubd80\ud130\ub294 \uc800\uc7a5\ub41c \uc815\uc0c1 \uc138\uc158\uc744 \uc7ac\uc0ac\uc6a9\ud569\ub2c8\ub2e4."
        )
        self.auto_naver_btn.clicked.connect(lambda: self._start_auto_crawl("naver"))

        self.auto_coupang_btn = QPushButton("\ucfe0\ud321 \uc790\ub3d9 \uc218\uc9d1")
        self.auto_coupang_btn.setToolTip(
            "\ube0c\ub77c\uc6b0\uc800 \ucc3d\uc774 \uc5f4\ub9ac\uba74 \uccab 1\ud68c\ub9cc \uc9c1\uc811 \ub85c\uadf8\uc778\ud558\uc138\uc694.\n"
            "\ucea1\ucc28, 2\ub2e8\uacc4 \uc778\uc99d\uc774 \ub098\uc624\uba74 \uc0ac\uc6a9\uc790\uac00 \uc9c1\uc811 \uc644\ub8cc\ud574\uc57c \ud569\ub2c8\ub2e4."
        )
        self.auto_coupang_btn.clicked.connect(lambda: self._start_auto_crawl("coupang"))

        self.reset_session_chk = QCheckBox("\uc138\uc158 \ucd08\uae30\ud654(\uc0c8\ub85c \ub85c\uadf8\uc778)")
        self.reset_session_chk.setToolTip("\uc800\uc7a5\ub41c \ub85c\uadf8\uc778 \uc138\uc158\uc744 \uc9c0\uc6b0\uace0 \ucc98\uc74c\ubd80\ud130 \ub85c\uadf8\uc778\ud569\ub2c8\ub2e4.")

        self.cancel_auto_btn = QPushButton("\ucde8\uc18c")
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
        return channel if channel in {"naver", "coupang"} else "naver"

    def _open_order_page(self, channel: str) -> None:
        url = self.ORDER_URLS.get(channel)
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _import_html(self) -> None:
        channel = self._selected_import_channel()
        path, _ = QFileDialog.getOpenFileName(
            self,
            "\uad6c\ub9e4\ub0b4\uc5ed HTML \uc120\ud0dd",
            "",
            "HTML files (*.html *.htm);;All files (*.*)",
        )
        if not path:
            return
        try:
            records = self.parser.parse_html_file(channel, Path(path))
            added = self.store.save_records(records)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "\uac00\uc838\uc624\uae30 \uc2e4\ud328", str(exc))
            return
        self.status.setText(f"{self.CHANNEL_LABELS[channel]} {len(records)}\uac74 \ubd84\uc11d, {added}\uac74 \ucd94\uac00")
        self.reload()

    def _import_clipboard(self) -> None:
        channel = self._selected_import_channel()
        text = QApplication.clipboard().text().strip()
        if not text:
            QMessageBox.information(
                self,
                "\ud074\ub9bd\ubcf4\ub4dc\uac00 \ube44\uc5b4\uc788\uc2b5\ub2c8\ub2e4",
                "\uc77c\ubc18 \ube0c\ub77c\uc6b0\uc800\uc5d0\uc11c \uc8fc\ubb38\ub0b4\uc5ed \ud398\uc774\uc9c0\ub97c \uc5f4\uace0 Ctrl+A, Ctrl+C \ud6c4 \ub2e4\uc2dc \ub204\ub974\uc138\uc694.",
            )
            return
        try:
            records = self.parser.parse_text(channel, text, source_url="clipboard")
            added = self.store.save_records(records)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "\ud074\ub9bd\ubcf4\ub4dc \uac00\uc838\uc624\uae30 \uc2e4\ud328", str(exc))
            return
        self.status.setText(
            f"{self.CHANNEL_LABELS[channel]} \ud074\ub9bd\ubcf4\ub4dc {len(records)}\uac74 \ubd84\uc11d, {added}\uac74 \ucd94\uac00"
        )
        self.reload()

    _STATUS_PREFIX_RE = __import__("re").compile(r"^\s*\[([^\]]+)\]")

    @classmethod
    def _is_cancelled(cls, record: PurchaseRecord) -> bool:
        """취소/반품 거래 판별.

        쿠팡 raw_text 에는 "반품, 교환 신청" 같은 버튼 텍스트가 항상 들어가서,
        raw_text 검사하면 정상 [배송완료] 거래도 cancelled 로 잘못 잡힌다.
        title 의 status prefix '[…]' 부분만 검사한다.
        """
        title = str(record.title or "")
        m = cls._STATUS_PREFIX_RE.match(title)
        if not m:
            return False
        status = m.group(1)
        return any(keyword in status for keyword in cls.CANCEL_KEYWORDS)

    @classmethod
    def _signed_amount(cls, record: PurchaseRecord) -> int:
        amount = int(record.amount or 0)
        return -amount if cls._is_cancelled(record) else amount

    def reload(self) -> None:
        channel = self._selected_channel()
        rows = self.store.load_records(channel=channel)
        self._render(rows)
        gross = sum(int(row.amount or 0) for row in rows if not self._is_cancelled(row))
        cancelled = sum(int(row.amount or 0) for row in rows if self._is_cancelled(row))
        net = gross - cancelled
        if cancelled > 0:
            self.status.setText(f"{len(rows):,}\uac74 | \uacb0\uc81c {gross:,}\uc6d0 - \ucde8\uc18c {cancelled:,}\uc6d0 = {net:,}\uc6d0")
        else:
            self.status.setText(f"{len(rows):,}\uac74 | {net:,}\uc6d0")

    def _render(self, rows: List[PurchaseRecord]) -> None:
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        red_brush = QBrush(QColor("#dc2626"))
        gray_brush = QBrush(QColor("#9ca3af"))
        for row_idx, record in enumerate(rows):
            cancelled = self._is_cancelled(record)
            signed_amount = self._signed_amount(record)
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
                    text = f"{signed_amount:,}\uc6d0" if value else "-"
                    item = _NumberItem(text)
                    item.setData(Qt.UserRole, signed_amount)
                    if cancelled:
                        item.setForeground(red_brush)
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)
                else:
                    item = QTableWidgetItem(str(value))
                    item.setData(Qt.UserRole, str(value))
                    if cancelled:
                        item.setForeground(red_brush if col_idx == 3 else gray_brush)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if col_idx == 3:
                    item.setToolTip(record.raw_text[:1000])
                self.table.setItem(row_idx, col_idx, item)
        self.table.resizeColumnsToContents()
        self.table.setSortingEnabled(True)

    def _set_auto_busy(self, busy: bool) -> None:
        for btn in (
            self.auto_naver_btn,
            self.auto_coupang_btn,
            self.import_btn,
            self.clipboard_btn,
            self.open_naver_btn,
            self.open_coupang_btn,
            self.reload_btn,
            self.reset_session_chk,
        ):
            btn.setEnabled(not busy)
        self.cancel_auto_btn.setEnabled(busy)

    def _start_auto_crawl(self, channel: str) -> None:
        if self._worker_thread is not None and self._worker_thread.isRunning():
            QMessageBox.information(self, "\uc548\ub0b4", "\uc774\ubbf8 \uc218\uc9d1 \uc791\uc5c5\uc774 \uc9c4\ud589 \uc911\uc785\ub2c8\ub2e4.")
            return
        thread = QThread(self)
        worker = _CrawlerWorker(
            channel,
            headless=False,
            max_pages=10,
            reset_session=self.reset_session_chk.isChecked(),
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.log.connect(self._on_crawler_log)
        worker.login_required.connect(self._on_login_required)
        worker.finished.connect(self._on_crawler_finished)

        def _cleanup(_result: object = None) -> None:
            thread.quit()
            thread.wait(1000)
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
        self.status.setText(f"{self.CHANNEL_LABELS.get(channel, channel)} \uc790\ub3d9 \uc218\uc9d1 \uc2dc\uc791... \ube0c\ub77c\uc6b0\uc800 \ucc3d\uc5d0\uc11c \ub85c\uadf8\uc778\ud558\uc138\uc694.")
        thread.start()

    def _cancel_auto(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self.status.setText("\ucde8\uc18c \uc694\uccad\ud588\uc2b5\ub2c8\ub2e4. \ud604\uc7ac \ub2e8\uacc4\uac00 \ub05d\ub098\uba74 \uc911\ub2e8\ub429\ub2c8\ub2e4.")

    def _on_crawler_log(self, msg: str) -> None:
        self.status.setText(msg)

    def _on_login_required(self, msg: str) -> None:
        self.status.setText(msg)

    def _on_crawler_finished(self, result: object) -> None:
        if not isinstance(result, CrawlResult):
            self.status.setText("\uc218\uc9d1\uc774 \uc885\ub8cc\ub418\uc5c8\uc9c0\ub9cc \uacb0\uacfc\ub97c \uc77d\uc9c0 \ubabb\ud588\uc2b5\ub2c8\ub2e4.")
            return
        channel_label = self.CHANNEL_LABELS.get(result.channel, result.channel)
        if result.error:
            QMessageBox.warning(self, f"{channel_label} \uc790\ub3d9 \uc218\uc9d1 \uc2e4\ud328", str(result.error)[:1500])
            self.status.setText(f"{channel_label} \uc2e4\ud328: {result.error[:120]}")
            return
        try:
            added = self.store.save_records(result.records)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "\uc800\uc7a5 \uc2e4\ud328", str(exc))
            added = 0
        self.status.setText(f"{channel_label} \uc790\ub3d9 \uc218\uc9d1 \uc644\ub8cc: {len(result.records)}\uac74 \ucd94\ucd9c, {added}\uac74 \uc2e0\uaddc \uc800\uc7a5")
        self.reload()

    def shutdown(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
        if self._worker_thread is not None and self._worker_thread.isRunning():
            self._worker_thread.quit()
            self._worker_thread.wait(2000)
