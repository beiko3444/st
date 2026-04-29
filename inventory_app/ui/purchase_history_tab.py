from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
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
from PySide6.QtGui import QFont

from inventory_app.models import PurchaseOrder, PurchaseRecord
from inventory_app.services.coupang_credentials import (
    CoupangAccount,
    delete_account as delete_coupang_account,
    list_accounts as list_coupang_accounts,
    save_account as save_coupang_account,
)
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
    dedupe_order_items as _dedupe_order_items,
    normalize_record_title as _normalize_title,
)


class _OrderDetailDialog(QDialog):
    """주문번호 상세 팝업: 결제총액, 캐시 차감, 실 카드결제, 품목 목록."""

    def __init__(
        self,
        order_no: str,
        order: Optional[PurchaseOrder],
        items: List[PurchaseRecord],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"주문 {order_no} 상세")
        self.resize(1000, 760)
        self.setMinimumSize(720, 500)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        head_font = QFont(); head_font.setBold(True); head_font.setPointSize(11)

        # 1) 주문 요약 박스
        summary = QLabel()
        summary.setTextFormat(Qt.RichText)
        summary.setWordWrap(True)
        summary.setStyleSheet(
            "QLabel { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; "
            "padding: 12px 14px; }"
        )

        items_total = sum(int(r.amount or 0) for r in items if (r.amount or 0) > 0)
        payment_total: Optional[int] = None
        cash_used: Optional[int] = None
        card_amount: Optional[int] = None
        status = ""
        order_date = ""
        payment_method = ""

        if order is not None:
            payment_total = order.payment_total
            cash_used = order.cash_used
            card_amount = order.card_amount
            status = order.status or ""
            order_date = order.order_date or ""
            payment_method = order.payment_method or ""

        # card_amount 가 None 이면 payment_total - cash_used 로 추정
        if card_amount is None and payment_total is not None:
            card_amount = int(payment_total) - int(cash_used or 0)

        rows_html: List[str] = []
        rows_html.append(
            f"<div style='font-size:13px; font-weight:700; color:#0f172a; margin-bottom:6px;'>"
            f"주문 {order_no}{(' · ' + status) if status else ''}"
            f"</div>"
        )
        sub_parts: List[str] = []
        if order_date:
            sub_parts.append(f"주문일 {order_date}")
        if items:
            sub_parts.append(f"품목 {len(items)}건")
        if payment_method:
            sub_parts.append(f"결제수단 {payment_method}")
        account_label_str = ""
        if order is not None and getattr(order, "account_label", None):
            account_label_str = order.account_label or ""
        if not account_label_str:
            for r in items:
                lbl = getattr(r, "account_label", None)
                if lbl:
                    account_label_str = lbl
                    break
        if account_label_str:
            sub_parts.append(f"계정 {account_label_str}")
        if sub_parts:
            rows_html.append(
                f"<div style='color:#64748b; font-size:11px; margin-bottom:10px;'>"
                f"{' · '.join(sub_parts)}</div>"
            )

        # 결제 breakdown 표
        rows_html.append("<table style='border-collapse:collapse; width:100%; font-size:12px;'>")

        def _row(label: str, value: str, *, color: str = "#334155", bold: bool = False, hr: bool = False) -> str:
            border = "border-top: 1px solid #e2e8f0;" if hr else ""
            wt = "700" if bold else "500"
            return (
                f"<tr><td style='padding:6px 4px; color:#64748b; {border}'>{label}</td>"
                f"<td style='padding:6px 4px; text-align:right; color:{color}; "
                f"font-weight:{wt}; {border}'>{value}</td></tr>"
            )

        rows_html.append(_row("품목 합계 (양수)", f"{items_total:,}원"))
        if payment_total is not None:
            rows_html.append(_row("결제 총액", f"{int(payment_total):,}원", color="#0f172a", bold=True))
        else:
            rows_html.append(_row("결제 총액", "(데이터 없음)", color="#94a3b8"))
        rows_html.append(
            _row(
                "쿠팡캐시/포인트 차감",
                f"− {int(cash_used or 0):,}원" if cash_used else "0원",
                color="#dc2626" if cash_used else "#94a3b8",
            )
        )
        rows_html.append(
            _row(
                "실 카드 결제금액",
                f"{int(card_amount):,}원" if card_amount is not None else "(데이터 없음)",
                color="#15803d" if card_amount is not None else "#94a3b8",
                bold=True,
                hr=True,
            )
        )
        rows_html.append("</table>")

        # 일치/차이 배지
        if payment_total is not None and items_total > 0:
            diff = items_total - int(payment_total)
            if diff == 0:
                badge = (
                    "<div style='margin-top:10px;'>"
                    "<span style='background:#dcfce7; color:#166534; padding:3px 10px; "
                    "border-radius:10px; font-size:11px; font-weight:600;'>"
                    "✓ 품목합계 = 결제총액</span></div>"
                )
            else:
                sign = "+" if diff > 0 else ""
                badge = (
                    "<div style='margin-top:10px;'>"
                    f"<span style='background:#fef3c7; color:#92400e; padding:3px 10px; "
                    f"border-radius:10px; font-size:11px; font-weight:600;'>"
                    f"⚠ 품목합계 − 결제총액 = {sign}{diff:,}원 (배송비/할인/쿠폰 차이)</span></div>"
                )
            rows_html.append(badge)

        summary.setText("".join(rows_html))
        layout.addWidget(summary)

        # 2) 품목 목록
        items_label = QLabel(f"📦 품목 ({len(items)}건)")
        items_label.setFont(head_font)
        items_label.setStyleSheet("color: #0f172a;")
        layout.addWidget(items_label)

        items_table = QTableWidget(0, 4)
        items_table.setHorizontalHeaderLabels(["일자", "상품/내역", "금액", "결제수단"])
        items_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        items_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        items_table.setAlternatingRowColors(True)
        items_table.verticalHeader().setVisible(False)
        items_table.setShowGrid(False)
        items_table.setStyleSheet(
            "QTableWidget { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px;"
            " gridline-color: #e2e8f0; }"
            "QHeaderView::section { background: #f8fafc; border: none;"
            " border-bottom: 1px solid #e2e8f0; padding: 6px; font-weight: 600; color: #475569; }"
            "QTableWidget::item { padding: 6px 8px; border: none; }"
        )
        h = items_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        from PySide6.QtGui import QBrush as _QB, QColor as _QC
        # rows + 합계 row
        rowcount_with_sum = len(items) + 1 if items else 1
        items_table.setRowCount(rowcount_with_sum)
        sum_amount = 0
        sum_positive = 0
        sum_negative = 0
        any_payment_method = ""
        for r, rec in enumerate(items):
            ramt = int(rec.amount or 0)
            sum_amount += ramt
            if ramt >= 0:
                sum_positive += ramt
            else:
                sum_negative += ramt
            if not any_payment_method and rec.payment_method:
                any_payment_method = rec.payment_method
            cancelled = ramt < 0
            d_item = QTableWidgetItem(rec.order_date or "")
            t_item = QTableWidgetItem((rec.title or "").strip())
            t_item.setToolTip(rec.title or "")
            a_item = QTableWidgetItem(f"{ramt:,}원")
            a_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            af = QFont(); af.setBold(True); a_item.setFont(af)
            pm_item = QTableWidgetItem(rec.payment_method or "-")
            if cancelled:
                a_item.setForeground(_QB(_QC("#dc2626")))
                t_item.setForeground(_QB(_QC("#dc2626")))
            items_table.setItem(r, 0, d_item)
            items_table.setItem(r, 1, t_item)
            items_table.setItem(r, 2, a_item)
            items_table.setItem(r, 3, pm_item)
        if items:
            sum_row = len(items)
            sum_bg = _QB(_QC("#f1f5f9"))
            sum_label_text = f"합계 ({len(items)}건"
            if sum_negative != 0:
                sum_label_text += f", 취소 {sum_negative:,}원 포함"
            sum_label_text += ")"
            sl_item = QTableWidgetItem(sum_label_text)
            sl_item.setBackground(sum_bg)
            slf = QFont(); slf.setBold(True); sl_item.setFont(slf)
            sl_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            empty_item = QTableWidgetItem("")
            empty_item.setBackground(sum_bg)
            sa_item = QTableWidgetItem(f"{sum_amount:,}원")
            sa_item.setBackground(sum_bg)
            sa_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            sf = QFont(); sf.setBold(True); sf.setPointSize(12); sa_item.setFont(sf)
            sa_item.setForeground(_QB(_QC("#0f172a")))
            spm_item = QTableWidgetItem(any_payment_method or "")
            spm_item.setBackground(sum_bg)
            items_table.setItem(sum_row, 0, empty_item)
            items_table.setItem(sum_row, 1, sl_item)
            items_table.setItem(sum_row, 2, sa_item)
            items_table.setItem(sum_row, 3, spm_item)
        else:
            ph = QTableWidgetItem("(품목 데이터 없음 — 자동수집 필요)")
            ph.setForeground(_QB(_QC("#94a3b8")))
            items_table.setSpan(0, 0, 1, 4)
            items_table.setItem(0, 0, ph)
        layout.addWidget(items_table, 1)

        btn_row = QDialogButtonBox(QDialogButtonBox.Close)
        btn_row.rejected.connect(self.reject)
        btn_row.accepted.connect(self.accept)
        layout.addWidget(btn_row)


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

    def __init__(
        self,
        channel: str,
        *,
        headless: bool,
        max_pages: int,
        reset_session: bool,
        coupang_email: str = "",
        coupang_password: str = "",
        login_only: bool = False,
        account_label: str = "",
        crawl_days: int = 0,
    ) -> None:
        super().__init__()
        self.channel = channel
        self.headless = headless
        self.max_pages = max_pages
        self.reset_session = reset_session
        self.coupang_email = coupang_email
        self.coupang_password = coupang_password
        self.login_only = login_only
        self.account_label = account_label
        self.crawl_days = crawl_days
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        import sys as _sys
        def _log(msg):
            try:
                print(f"[crawler] {msg}", file=_sys.stderr, flush=True)
            except Exception:
                pass
            self.log.emit(str(msg))
        progress = CrawlerProgress(
            on_log=_log,
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
                coupang_email=self.coupang_email,
                coupang_password=self.coupang_password,
                login_only=self.login_only,
                account_label=self.account_label,
                crawl_days=self.crawl_days,
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
        "\uacc4\uc815",
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

    def __init__(self, monitor_url: str | None = None) -> None:
        super().__init__()
        # Pi write-through: monitor_url 이 있으면 라즈베리에 동기화
        pi_client = None
        if monitor_url:
            try:
                from inventory_app.services.pi_data_client import PiDataClient
                pi_client = PiDataClient(monitor_url)
            except Exception:  # noqa: BLE001
                pi_client = None
        self._pi_client = pi_client
        self.store = PurchaseHistoryStore(pi_client=pi_client)
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

        # \ucfe0\ud321 \uce90\uc2dc \ucd08\uae30\ud654 (\uad6c\ubc84\uc804 \ud06c\ub864\ub7ec\ub85c \uc800\uc7a5\ub41c \ub354\ub7ec\uc6b4 row \uc81c\uac70 \ud6c4 \uc7ac\uc218\uc9d1)
        self.cleanup_coupang_btn = QPushButton("\ud83e\uddf9 \ucfe0\ud321 \uce90\uc2dc \ucd08\uae30\ud654")
        self.cleanup_coupang_btn.setToolTip(
            "\ucfe0\ud321 \uad6c\ub9e4\ub0b4\uc5ed/\uc8fc\ubb38\uc744 \ubaa8\ub450 \uc0ad\uc81c. \ub2e4\uc74c \uc790\ub3d9\uc218\uc9d1\uc5d0\uc11c \uae68\ub057\ud558\uac8c \uc7ac\uad6c\ucd95.\n"
            "\uc8fc\ubb38\ubc88\ud638\uac00 NULL \uc778 \uc61b\ub0a0 \ub370\uc774\ud130/\uc911\ubcf5 row \uc815\ub9ac\uc6a9."
        )
        self.cleanup_coupang_btn.clicked.connect(self._cleanup_coupang)

        # \ud30c\uc774 \uc77c\uad04 \uc5c5\ub85c\ub4dc (\uae30\uc874 \ub85c\uceec \ub370\uc774\ud130\ub97c Pi \uc5d0 \ud478\uc2dc)
        self.pi_sync_btn = QPushButton("\u2601 \ud30c\uc774 \uc77c\uad04 \uc5c5\ub85c\ub4dc")
        self.pi_sync_btn.setToolTip(
            "\ub85c\uceec DB \uc758 \ubaa8\ub4e0 \uad6c\ub9e4\ub0b4\uc5ed\uacfc \uc8fc\ubb38\uc744 \ub77c\uc988\ubca0\ub9ac\ud30c\uc774 DB \ub85c \uc5c5\ub85c\ub4dc.\n"
            "(\uc774\ubbf8 \uc788\ub294 \uac74 \ub77c\uc988\ubca0\ub9ac \uce21\uc5d0\uc11c fingerprint/order_no \ub85c \uc911\ubcf5 \ubb34\uc2dc.)"
        )
        self.pi_sync_btn.clicked.connect(self._pi_sync_all)
        if self.store._pi is None or not getattr(self.store._pi, "is_configured", False):
            self.pi_sync_btn.setEnabled(False)
            self.pi_sync_btn.setToolTip("monitor_url \ubbf8\uc124\uc815 \u2014 credentials.json \uc758 monitor \uc139\uc158 \ud655\uc778")

        top.addWidget(QLabel("\ucc44\ub110"))
        top.addWidget(self.channel_combo)
        top.addWidget(self.open_naver_btn)
        top.addWidget(self.open_coupang_btn)
        top.addWidget(self.import_btn)
        top.addWidget(self.clipboard_btn)
        top.addWidget(self.reload_btn)
        top.addWidget(self.cleanup_coupang_btn)
        top.addWidget(self.pi_sync_btn)
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

        self.crawl_days_spin = QSpinBox()
        self.crawl_days_spin.setRange(1, 730)
        self.crawl_days_spin.setValue(7)
        self.crawl_days_spin.setSuffix("\uc77c")
        self.crawl_days_spin.setToolTip("\ucd5c\uadfc N\uc77c \uc774\ub0b4\uc758 \uc8fc\ubb38\ub9cc \uc218\uc9d1. \ucef7\uc624\ud504 \ub3c4\ub2ec \uc2dc \uc790\ub3d9 \uc885\ub8cc.")

        self.cancel_auto_btn = QPushButton("\ucde8\uc18c")
        self.cancel_auto_btn.clicked.connect(self._cancel_auto)
        self.cancel_auto_btn.setEnabled(False)

        self.status = QLabel("")
        self.status.setWordWrap(True)

        auto_row.addWidget(self.auto_naver_btn)
        auto_row.addWidget(self.auto_coupang_btn)
        auto_row.addWidget(QLabel("최근"))
        auto_row.addWidget(self.crawl_days_spin)
        auto_row.addWidget(self.reset_session_chk)
        auto_row.addWidget(self.cancel_auto_btn)
        auto_row.addWidget(self.status, 1)

        # 쿠팡 자동로그인용 다중 계정 row
        cred_row = QHBoxLayout()
        cred_row.addWidget(QLabel("저장 계정"))
        self.account_combo = QComboBox()
        self.account_combo.setMinimumWidth(180)
        self.account_combo.setToolTip("계정 선택 후 「선택 계정으로 수집」을 누르면 자동 로그인 + 크롤링.")
        self.account_combo.currentIndexChanged.connect(self._on_account_selected)
        cred_row.addWidget(self.account_combo)

        self.start_with_account_btn = QPushButton("▶ 선택 계정으로 수집")
        self.start_with_account_btn.setToolTip(
            "선택된 쿠팡 계정으로 자동 로그인 후 즉시 주문내역을 크롤링합니다."
        )
        self.start_with_account_btn.clicked.connect(self._start_with_selected_account)
        cred_row.addWidget(self.start_with_account_btn)

        self.delete_account_btn = QPushButton("선택 삭제")
        self.delete_account_btn.clicked.connect(self._delete_selected_account)
        cred_row.addWidget(self.delete_account_btn)

        cred_row.addSpacing(12)
        cred_row.addWidget(QLabel("계정명"))
        self.account_label_edit = QLineEdit()
        self.account_label_edit.setPlaceholderText("예: 메인계정")
        self.account_label_edit.setMinimumWidth(120)
        cred_row.addWidget(self.account_label_edit)

        cred_row.addWidget(QLabel("쿠팡 ID"))
        self.coupang_email_edit = QLineEdit()
        self.coupang_email_edit.setPlaceholderText("이메일 주소")
        self.coupang_email_edit.setMinimumWidth(180)
        cred_row.addWidget(self.coupang_email_edit)

        cred_row.addWidget(QLabel("비밀번호"))
        self.coupang_password_edit = QLineEdit()
        self.coupang_password_edit.setEchoMode(QLineEdit.Password)
        self.coupang_password_edit.setPlaceholderText("비밀번호")
        self.coupang_password_edit.setMinimumWidth(140)
        cred_row.addWidget(self.coupang_password_edit)

        self.show_pw_chk = QCheckBox("표시")
        self.show_pw_chk.toggled.connect(
            lambda checked: self.coupang_password_edit.setEchoMode(
                QLineEdit.Normal if checked else QLineEdit.Password
            )
        )
        cred_row.addWidget(self.show_pw_chk)

        self.save_cred_btn = QPushButton("저장")
        self.save_cred_btn.setToolTip("계정명/ID/비번을 입력하고 저장 → 라즈베리DB 에 영구 저장.")
        self.save_cred_btn.clicked.connect(self._save_coupang_account)
        cred_row.addWidget(self.save_cred_btn)
        cred_row.addStretch(1)

        # 저장된 계정 목록 로드
        self._refresh_account_combo()

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        # 상품명/내역 셀 더블클릭 → 상품 페이지로 이동
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)

        layout.addLayout(top)
        layout.addLayout(auto_row)
        layout.addLayout(cred_row)
        layout.addWidget(self.table, 1)
        self.reload()

    def _selected_channel(self) -> str:
        return str(self.channel_combo.currentData() or "all")

    def _selected_import_channel(self) -> str:
        channel = self._selected_channel()
        return channel if channel in {"naver", "coupang"} else "naver"

    def _open_order_page(self, channel: str) -> None:
        # 쿠팡: 선택된 계정이 있으면 자동로그인 후 주문 페이지 진입까지 자동화
        if channel == "coupang":
            account = self._selected_account()
            if account is not None:
                if self._worker_thread is not None and self._worker_thread.isRunning():
                    QMessageBox.information(
                        self, "안내", "이미 작업이 진행 중입니다.",
                    )
                    return
                self._start_auto_crawl("coupang", login_only=True)
                return
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

    def _cleanup_coupang(self) -> None:
        """쿠팡 구매내역 + 주문 전체 삭제 (재수집용)."""
        ans = QMessageBox.question(
            self, "쿠팡 캐시 초기화",
            "쿠팡 구매내역과 주문 데이터를 모두 삭제할까요?\n"
            "(다음 자동수집에서 깨끗하게 재구축됩니다.\n"
            "라즈베리파이 DB 는 그대로 유지되며, 재수집 후 다시 동기화됩니다.)",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        try:
            n_recs = self.store.delete_records("coupang")
            n_orders = self.store.delete_orders("coupang")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "쿠팡 캐시 초기화 실패", str(exc))
            return
        self.status.setText(f"✓ 쿠팡 캐시 삭제: 구매내역 {n_recs}건 / 주문 {n_orders}개")
        self.reload()
        QMessageBox.information(
            self, "쿠팡 캐시 초기화 완료",
            f"구매내역 {n_recs}건, 주문 {n_orders}개 삭제됨.\n"
            "이제 '쿠팡 자동 수집' 을 다시 실행하세요.",
        )

    def _pi_sync_all(self) -> None:
        """로컬 DB 의 모든 구매내역 + 주문을 Pi 로 일괄 업로드."""
        pi = self.store._pi
        if pi is None or not getattr(pi, "is_configured", False):
            QMessageBox.warning(
                self, "파이 동기화 불가",
                "monitor_url 이 설정돼 있지 않습니다. credentials.json 의 monitor 섹션을 확인하세요.",
            )
            return

        self.pi_sync_btn.setEnabled(False)
        self.status.setText("파이 업로드 중... (잠시 대기)")
        QApplication.processEvents()

        result_lines: list[str] = []
        try:
            # 1) 구매내역 (records)
            recs = self.store.load_records(channel="all", limit=20000)
            if recs:
                inserted = pi.upload_purchase_records(recs, self.store._fingerprint)
                result_lines.append(f"구매내역 {len(recs)}건 전송 → 신규 {inserted}건")
            else:
                result_lines.append("구매내역: 로컬에 데이터 없음")

            # 2) 주문 단위 (orders)
            orders = self.store.load_orders(channel="all", limit=20000)
            if orders:
                changed = pi.upload_purchase_orders(orders)
                result_lines.append(f"주문 {len(orders)}개 전송 → 변경 {changed}건")
            else:
                result_lines.append("주문: 로컬에 데이터 없음 (쿠팡 자동수집 시 자동 채워짐)")

        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "파이 업로드 실패", str(exc))
            self.status.setText(f"파이 업로드 실패: {exc}")
            self.pi_sync_btn.setEnabled(True)
            return

        self.pi_sync_btn.setEnabled(True)
        msg = "✓ 파이 업로드 완료\n" + "\n".join(result_lines)
        self.status.setText(msg.replace("\n", " · "))
        QMessageBox.information(self, "파이 업로드 결과", msg)

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

    def _on_cell_double_clicked(self, row: int, col: int) -> None:
        """주문번호(2) → 결제 상세 / 상품(3) → 상품페이지."""
        if col == 2:
            order_no_item = self.table.item(row, col)
            if order_no_item is None:
                return
            order_no = (order_no_item.text() or "").strip()
            if not order_no or order_no == "-":
                return
            channel_item = self.table.item(row, 1)
            channel_label = (channel_item.text() if channel_item else "") or ""
            channel = "coupang" if "쿠팡" in channel_label else (
                "naver" if "네이버" in channel_label else "coupang"
            )
            self._open_order_detail(channel, order_no)
            return
        if col == 3:  # 상품/내역 컬럼
            item = self.table.item(row, col)
            if item is None:
                return
            url = item.data(Qt.UserRole + 1)
            if not url:
                return
            QDesktopServices.openUrl(QUrl(str(url)))

    def _open_order_detail(self, channel: str, order_no: str) -> None:
        order: Optional[PurchaseOrder] = None
        items: List[PurchaseRecord] = []
        # Pi 우선
        if self._pi_client is not None and getattr(self._pi_client, "is_configured", False):
            try:
                for o in self._pi_client.list_purchase_orders(channel=channel, limit=5000):
                    if (o.order_no or "") == order_no:
                        order = o
                        break
            except Exception:  # noqa: BLE001
                pass
            try:
                items = [
                    r for r in self._pi_client.list_purchase_records(channel=channel, limit=10000)
                    if (r.order_no or "") == order_no
                ]
            except Exception:  # noqa: BLE001
                items = []
        # 로컬 fallback
        if order is None or not items:
            try:
                if order is None:
                    for o in self.store.load_orders(channel=channel, limit=5000):
                        if (o.order_no or "") == order_no:
                            order = o
                            break
                if not items:
                    items = [
                        r for r in self.store.load_records(channel=channel, limit=20000)
                        if (r.order_no or "") == order_no
                    ]
            except Exception:  # noqa: BLE001
                pass

        # 같은 주문 내에서 (order_date, 정규화된 title, amount, payment_method) 가
        # 동일한 품목이 여러 번 잡히는 경우 표시 단계에서 dedupe.
        # 원인: Pi 의 fingerprint 변경 이력 등으로 같은 품목이 다중 레코드로 저장되어 있음.
        # 결제총액과 품목합계의 배수 관계가 이 현상의 신호.
        items = _dedupe_order_items(items)

        dlg = _OrderDetailDialog(order_no, order, items, parent=self)
        dlg.exec()

    def _render(self, rows: List[PurchaseRecord]) -> None:
        self.table.setSortingEnabled(False)
        self.table.setAlternatingRowColors(False)
        red_brush = QBrush(QColor("#dc2626"))
        gray_brush = QBrush(QColor("#9ca3af"))
        zebra_brushes = [QBrush(QColor("#ffffff")), QBrush(QColor("#eef2f7"))]
        summary_bg = QBrush(QColor("#fef9c3"))
        summary_fg = QBrush(QColor("#854d0e"))

        # 합계 행을 같은 주문번호 그룹 끝마다 1개씩 삽입.
        # 결제금액 = 실 카드결제(캐시 차감 후), 상품/내역 = "총 N건 · X원 차감".
        order_meta: dict[str, "PurchaseOrder"] = {}
        try:
            for o in self.store.load_orders(channel="all", limit=20000):
                if o.order_no:
                    order_meta[o.order_no] = o
        except Exception:  # noqa: BLE001
            pass

        from collections import OrderedDict
        groups: "OrderedDict[str, list[PurchaseRecord]]" = OrderedDict()
        for r in rows:
            key = (r.order_no or "").strip() or f"__none_{id(r)}"
            groups.setdefault(key, []).append(r)

        # display sequence: ("item", record) 또는 ("summary", (items, ord_obj))
        display: list[tuple[str, Any]] = []
        for key, items in groups.items():
            for r in items:
                display.append(("item", r))
            order_no_g = (items[0].order_no or "").strip() if items else ""
            if not order_no_g:
                continue
            ord_obj = order_meta.get(order_no_g)
            display.append(("summary", (items, ord_obj)))

        self.table.setRowCount(len(display))

        zebra_idx = 0
        prev_order_no: Optional[str] = None
        bold_f = QFont(); bold_f.setBold(True)
        for row_idx, entry in enumerate(display):
            row_type, payload = entry
            if row_type == "summary":
                items_list, ord_obj = payload  # type: ignore[misc]
                cnt = len(items_list)
                items_total = sum(self._signed_amount(r) for r in items_list)
                cash = 0
                if ord_obj is not None:
                    cash = int(getattr(ord_obj, "cash_used", None) or 0)
                final_amount = items_total - cash if cash > 0 else items_total
                if cash > 0:
                    detail_text = f"총 {cnt}건 · 쿠팡캐시 −{cash:,}원 차감"
                else:
                    detail_text = f"총 {cnt}건"
                amt_item = _NumberItem(f"{final_amount:,}원")
                amt_item.setData(Qt.UserRole, final_amount)
                detail_item = QTableWidgetItem(detail_text)
                cells = [
                    QTableWidgetItem(""),  # 일자
                    QTableWidgetItem(""),  # 채널
                    QTableWidgetItem(""),  # 주문번호
                    detail_item,           # 상품/내역 ← "총 N건 · X원 차감"
                    amt_item,              # 결제금액 ← 실 카드결제 (캐시 차감 후)
                    QTableWidgetItem(""),  # 결제수단
                    QTableWidgetItem(""),  # 계정
                    QTableWidgetItem(""),  # 가져온 시각
                ]
                for c, it in enumerate(cells):
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                    it.setBackground(summary_bg)
                    it.setForeground(summary_fg)
                    it.setFont(bold_f)
                    self.table.setItem(row_idx, c, it)
                self.table.setRowHeight(row_idx, 24)
                continue

            record: PurchaseRecord = payload  # type: ignore[assignment]
            current_order_no = (record.order_no or "").strip() or f"__row_{row_idx}"
            if prev_order_no is None:
                zebra_idx = 0
            elif current_order_no != prev_order_no:
                zebra_idx = 1 - zebra_idx
            prev_order_no = current_order_no
            row_bg = zebra_brushes[zebra_idx]

            cancelled = self._is_cancelled(record)
            signed_amount = self._signed_amount(record)
            values: list[Any] = [
                record.order_date or "-",
                self.CHANNEL_LABELS.get(record.channel, record.channel),
                record.order_no or "-",
                record.title,
                record.amount,
                record.payment_method or "-",
                getattr(record, "account_label", None) or "-",
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
                item.setBackground(row_bg)
                if col_idx == 3:
                    # 상품 페이지 URL 을 cell 에 임베드 → 더블클릭 시 사용 (색상은 기본 유지)
                    if record.source_url and record.source_url != self.ORDER_URLS.get(record.channel, ""):
                        item.setData(Qt.UserRole + 1, record.source_url)
                        item.setToolTip(
                            (record.raw_text[:800] if record.raw_text else "")
                            + f"\n\n📎 더블클릭 → {record.source_url}"
                        )
                    else:
                        item.setToolTip(record.raw_text[:1000] if record.raw_text else "")
                self.table.setItem(row_idx, col_idx, item)
        self.table.resizeColumnsToContents()
        # 합계 행이 정렬에 섞이지 않도록 사용자 정렬 비활성화
        self.table.setSortingEnabled(False)

    # ---------- 쿠팡 다중 계정 ----------

    def _refresh_account_combo(self) -> None:
        try:
            accounts = list_coupang_accounts(pi_client=self._pi_client)
        except Exception:  # noqa: BLE001
            accounts = []
        self.account_combo.blockSignals(True)
        self.account_combo.clear()
        self.account_combo.addItem("(계정 없음)", None)
        for a in accounts:
            self.account_combo.addItem(f"{a.label}  ·  {a.email}", a.label)
        self.account_combo.blockSignals(False)

    def _selected_account(self) -> Optional["CoupangAccount"]:
        label = self.account_combo.currentData()
        if not label:
            return None
        try:
            for a in list_coupang_accounts(pi_client=self._pi_client):
                if a.label == label:
                    return a
        except Exception:  # noqa: BLE001
            return None
        return None

    def _on_account_selected(self, _idx: int) -> None:
        a = self._selected_account()
        if a is None:
            return
        # 입력 필드를 선택된 계정 값으로 채움 (편집/덮어쓰기 편의)
        self.account_label_edit.setText(a.label)
        self.coupang_email_edit.setText(a.email)
        self.coupang_password_edit.setText(a.password)

    def _save_coupang_account(self) -> None:
        label = self.account_label_edit.text().strip()
        email = self.coupang_email_edit.text().strip()
        pw = self.coupang_password_edit.text()
        if not label:
            QMessageBox.warning(self, "저장 실패", "계정명(별칭)을 입력하세요.")
            return
        if not email or not pw:
            QMessageBox.warning(self, "저장 실패", "이메일과 비밀번호를 모두 입력하세요.")
            return
        partial_only = False
        try:
            save_coupang_account(label, email, pw, pi_client=self._pi_client)
        except RuntimeError as exc:
            # 로컬은 저장됨, Pi 만 실패
            partial_only = True
            self.status.setText(f"⚠ {exc}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "저장 실패", str(exc))
            return
        self._refresh_account_combo()
        idx = self.account_combo.findData(label)
        if idx >= 0:
            self.account_combo.setCurrentIndex(idx)
        if not partial_only:
            self.status.setText(f"✓ 계정 '{label}' 저장됨 (라즈베리DB)")

    def _delete_selected_account(self) -> None:
        a = self._selected_account()
        if a is None:
            QMessageBox.information(self, "삭제", "삭제할 계정을 먼저 선택하세요.")
            return
        ans = QMessageBox.question(
            self, "계정 삭제", f"'{a.label}' 계정을 삭제할까요?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        try:
            delete_coupang_account(a.label, pi_client=self._pi_client)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "삭제 실패", str(exc))
            return
        self._refresh_account_combo()
        self.account_label_edit.clear()
        self.coupang_email_edit.clear()
        self.coupang_password_edit.clear()
        self.status.setText(f"✓ 계정 '{a.label}' 삭제됨")

    def _start_with_selected_account(self) -> None:
        a = self._selected_account()
        if a is None:
            QMessageBox.information(
                self, "계정 선택 필요",
                "저장된 계정이 없습니다. 계정명/ID/비번을 입력하고 저장 후 다시 시도하세요.",
            )
            return
        # 입력 필드도 동기화 후 즉시 쿠팡 크롤링 시작
        self.account_label_edit.setText(a.label)
        self.coupang_email_edit.setText(a.email)
        self.coupang_password_edit.setText(a.password)
        self._start_auto_crawl("coupang")

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
            self.account_combo,
            self.account_label_edit,
            self.coupang_email_edit,
            self.coupang_password_edit,
            self.save_cred_btn,
            self.delete_account_btn,
            self.start_with_account_btn,
        ):
            btn.setEnabled(not busy)
        self.cancel_auto_btn.setEnabled(busy)

    def _start_auto_crawl(self, channel: str) -> None:
        if self._worker_thread is not None and self._worker_thread.isRunning():
            QMessageBox.information(self, "\uc548\ub0b4", "\uc774\ubbf8 \uc218\uc9d1 \uc791\uc5c5\uc774 \uc9c4\ud589 \uc911\uc785\ub2c8\ub2e4.")
            return
        thread = QThread(self)
        days = int(self.crawl_days_spin.value())
        # 쿠팡은 페이지당 ~5건. 컷오프(날짜) 도달 시 조기 종료되므로 상한은 넉넉히.
        # 30일에 하루 3주문 = ~90개 → 18페이지 + 버퍼.
        max_pages = max(30, days + 10)
        worker = _CrawlerWorker(
            channel,
            headless=False,
            max_pages=max_pages,
            reset_session=self.reset_session_chk.isChecked(),
            coupang_email=self.coupang_email_edit.text().strip(),
            coupang_password=self.coupang_password_edit.text(),
            account_label=self.account_label_edit.text().strip(),
            crawl_days=days,
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
        # login_only \ubaa8\ub4dc \ub4f1 records \uc5c6\uc774 \ub05d\ub09c \uacbd\uc6b0
        if not result.records and not getattr(result, "orders", None):
            self.status.setText(f"{channel_label} \uc791\uc5c5 \uc644\ub8cc (\uc218\uc9d1\ub41c \ub370\uc774\ud130 \uc5c6\uc74c)")
            self.reload()
            return
        try:
            added = self.store.save_records(result.records)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "\uc800\uc7a5 \uc2e4\ud328", str(exc))
            added = 0
        order_msg = ""
        orders_attr = getattr(result, "orders", None) or []
        if orders_attr:
            try:
                self.store.save_orders(orders_attr)
                paid = sum(1 for o in orders_attr if o.payment_total is not None)
                order_msg = f" \u00b7 \uc8fc\ubb38 {len(orders_attr)}\uac1c (\uacb0\uc81c\uae08 {paid}\uac1c)"
            except Exception as exc:  # noqa: BLE001
                order_msg = f" \u00b7 \uc8fc\ubb38 \uc800\uc7a5 \uc2e4\ud328: {exc}"
        # Pi \uc5c5\ub85c\ub4dc \uc0c1\ud0dc (save_records/save_orders \uac00 write-through \ub85c Pi \uc5d0 \ub3d9\uc2dc \uc5c5\ub85c\ub4dc\ud568)
        pi_msg = ""
        if self._pi_client is not None and getattr(self._pi_client, "is_configured", False):
            pi_msg = " \u00b7 \u2601 \ub77c\uc988\ubca0\ub9ac\ud30c\uc774 \uc790\ub3d9 \uc5c5\ub85c\ub4dc \uc644\ub8cc"
        else:
            pi_msg = " \u00b7 (Pi \ubbf8\uc124\uc815 \u2014 \ub85c\uceec\uc5d0\ub9cc \uc800\uc7a5)"
        self.status.setText(
            f"\u2713 {channel_label} \uc790\ub3d9 \uc218\uc9d1 \uc644\ub8cc: {len(result.records)}\uac74 \ucd94\ucd9c, "
            f"{added}\uac74 \uc2e0\uaddc \uc800\uc7a5{order_msg}{pi_msg}"
        )
        self.reload()

    def shutdown(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
        if self._worker_thread is not None and self._worker_thread.isRunning():
            self._worker_thread.quit()
            self._worker_thread.wait(2000)
