"""카드 사용내역 탭 — 통계 카드 + 카테고리 칩 + 거래 카드 리스트.

beico-app 의 카드사용내역 화면 디자인을 참고 (사용자 요청 스크린샷).
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QDate, QObject, Qt, QThread, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from inventory_app.config import AppConfig
from inventory_app.models import CardUsage
from inventory_app.services.barobill_card_client import (
    BarobillCardClient,
    BarobillError,
)
from inventory_app.services.card_category import (
    DEFAULT_CATEGORIES,
    category_meta,
    classify_category,
)


# ---------------------------------------------------------------------------
# 워커
# ---------------------------------------------------------------------------


@dataclass
class _JobResult:
    ok: bool
    data: Any = None
    error: Optional[str] = None


class _ApiJob(QObject):
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


# ---------------------------------------------------------------------------
# 헬퍼 위젯
# ---------------------------------------------------------------------------


def _mask_card_number(card_num: Optional[str]) -> str:
    """5137920086923228 → 5137-****-****-3228"""
    if not card_num:
        return "-"
    digits = re.sub(r"[^0-9]", "", card_num)
    if len(digits) >= 16:
        return f"{digits[:4]}-****-****-{digits[-4:]}"
    if len(digits) >= 10:
        return f"{digits[:4]}-****-{digits[-4:]}"
    return digits


def _fmt_money(amount: int) -> str:
    sign = "-" if amount < 0 else ""
    return f"{sign}{abs(amount):,}원"


def _parse_used_at(used_at: Optional[str]) -> Optional[datetime]:
    if not used_at:
        return None
    try:
        # ISO 형식 파싱
        if "T" in used_at:
            return datetime.fromisoformat(used_at.split("+", 1)[0].split("Z", 1)[0])
        return datetime.fromisoformat(used_at)
    except Exception:  # noqa: BLE001
        return None


def _format_used_at_short(used_at: Optional[str]) -> str:
    """"4월 13일 · 오전 2:43" 같은 사람친화 표기."""
    dt = _parse_used_at(used_at)
    if dt is None:
        return used_at or "-"
    ampm = "오전" if dt.hour < 12 else "오후"
    h12 = dt.hour % 12 or 12
    return f"{dt.month}월 {dt.day}일 · {ampm} {h12}:{dt.minute:02d}"


class _SummaryCard(QFrame):
    """상단 통계 카드 (이번 달 지출 / 거래 건수 / 일평균 지출)."""

    def __init__(self, title: str) -> None:
        super().__init__()
        self.setObjectName("summaryCard")
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(
            "#summaryCard { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(6)

        self.title_label = QLabel(title)
        self.title_label.setStyleSheet("color: #64748b; font-size: 12px; font-weight: 500;")

        self.value_label = QLabel("-")
        f = QFont()
        f.setPointSize(18)
        f.setBold(True)
        self.value_label.setFont(f)
        self.value_label.setStyleSheet("color: #0f172a;")

        self.sub_label = QLabel("")
        self.sub_label.setStyleSheet("color: #94a3b8; font-size: 11px;")
        self.sub_label.setWordWrap(True)

        layout.addWidget(self.title_label)
        layout.addWidget(self.value_label)
        layout.addWidget(self.sub_label)

    def set_value(self, text: str, sub: str = "") -> None:
        self.value_label.setText(text)
        self.sub_label.setText(sub)


class _CategoryChip(QPushButton):
    """카테고리 칩 (선택 시 체크 상태)."""

    def __init__(self, code: str, label_text: str, emoji: str, bg_color: str = "#f1f5f9") -> None:
        super().__init__(f"{emoji} {label_text}")
        self.code = code
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self._bg = bg_color
        self._refresh_style()

    def set_amount(self, amount: int) -> None:
        meta = self.text().split(" ", 1)[0]
        # 보존된 emoji + " " + label + " amount"
        # 실제 라벨은 self.code 로 다시 만들어야 — 하지만 init 에서 emoji+label 만 있고 amount 안 넣었음
        # 여기서 amount 만 추가 표시
        # 단순화: 텍스트 마지막에 amount 추가
        # 매번 재설정
        # NOTE: text 형식: "🛒 쇼핑 1,727,043원"
        from inventory_app.services.card_category import category_meta as _cm
        c = _cm(self.code)
        if amount > 0:
            self.setText(f"{c.emoji} {c.label} {amount:,}원")
        else:
            self.setText(f"{c.emoji} {c.label}")
        self._refresh_style()

    def _refresh_style(self) -> None:
        if self.isChecked():
            self.setStyleSheet(
                "QPushButton { background: #0f172a; color: #ffffff; border: 1px solid #0f172a; "
                "border-radius: 16px; padding: 6px 14px; font-weight: 600; }"
            )
        else:
            self.setStyleSheet(
                f"QPushButton {{ background: {self._bg}; color: #334155; border: 1px solid #e2e8f0; "
                f"border-radius: 16px; padding: 6px 14px; font-weight: 500; }}"
                "QPushButton:hover { border-color: #94a3b8; }"
            )

    def nextCheckState(self) -> None:  # type: ignore[override]
        super().nextCheckState()
        self._refresh_style()


class _UsageRow(QFrame):
    """거래 1건 카드 (가로형)."""

    def __init__(self, usage: CardUsage, category_code: str) -> None:
        super().__init__()
        self.usage = usage
        self.category_code = category_code
        cm = category_meta(category_code)
        self.setObjectName("usageRow")
        self.setStyleSheet(
            "#usageRow { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; }"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 12, 18, 12)
        layout.setSpacing(14)

        # 좌측: 카테고리 아이콘 박스
        cat_box = QFrame()
        cat_box.setFixedWidth(70)
        cat_box.setStyleSheet(
            f"QFrame {{ background: {cm.bg_color}; border-radius: 10px; }}"
        )
        cat_layout = QVBoxLayout(cat_box)
        cat_layout.setContentsMargins(6, 8, 6, 8)
        cat_layout.setSpacing(2)
        emoji = QLabel(cm.emoji)
        emoji.setAlignment(Qt.AlignCenter)
        emoji.setStyleSheet("font-size: 22px;")
        cat_label = QLabel(cm.label)
        cat_label.setAlignment(Qt.AlignCenter)
        cat_label.setStyleSheet("color: #334155; font-size: 11px; font-weight: 600;")
        cat_layout.addWidget(emoji)
        cat_layout.addWidget(cat_label)
        layout.addWidget(cat_box, 0)

        # 가운데: 가맹점명 + 메타정보 + 메모
        center = QVBoxLayout()
        center.setSpacing(4)

        store_name = (usage.store_name or "(가맹점명 미상)").strip()
        amount_int = int(usage.amount or 0)
        cancelled = amount_int < 0

        store_label = QLabel(store_name)
        f = QFont(); f.setBold(True); f.setPointSize(11)
        store_label.setFont(f)
        store_label.setStyleSheet(("color: #ef4444;" if cancelled else "color: #0f172a;"))
        center.addWidget(store_label)

        used_at_short = _format_used_at_short(usage.used_at)
        card_short = _mask_card_number(usage.card_num)
        approval = ""
        if usage.raw and usage.raw.get("ApprovalNum"):
            approval = f" · {usage.raw.get('ApprovalNum')}"
        type_text = ""
        if usage.raw and usage.raw.get("ApprovalType"):
            t = str(usage.raw.get("ApprovalType")).upper()
            if "CANCEL" in t:
                type_text = " · 구분 취소"
            else:
                type_text = " · 구분 승인"
        meta_label = QLabel(f"{used_at_short} · {card_short}{approval}{type_text}")
        meta_label.setStyleSheet("color: #64748b; font-size: 11px;")
        center.addWidget(meta_label)

        # 메모 입력
        self.memo_edit = QLineEdit()
        self.memo_edit.setPlaceholderText("메모 입력")
        self.memo_edit.setText(usage.memo or "")
        self.memo_edit.setStyleSheet(
            "QLineEdit { border: 1px solid #e2e8f0; border-radius: 6px; padding: 4px 8px; "
            "background: #f8fafc; color: #334155; font-size: 11px; }"
            "QLineEdit:focus { background: #ffffff; border-color: #0f172a; }"
        )
        center.addWidget(self.memo_edit)

        layout.addLayout(center, 1)

        # 우측: 금액
        right = QVBoxLayout()
        right.setSpacing(2)
        right.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        amount_label = QLabel(_fmt_money(amount_int))
        af = QFont(); af.setBold(True); af.setPointSize(13)
        amount_label.setFont(af)
        amount_label.setAlignment(Qt.AlignRight)
        amount_label.setStyleSheet("color: #ef4444;" if cancelled else "color: #0f172a;")
        right.addWidget(amount_label)

        plan = ""
        if usage.raw and usage.raw.get("PaymentPlan"):
            plan = str(usage.raw.get("PaymentPlan"))
        elif usage.raw and usage.raw.get("InstallmentMonths"):
            ins = usage.raw.get("InstallmentMonths")
            plan = f"{ins}개월" if ins else "일시불"
        else:
            plan = "일시불"
        cur = (usage.raw or {}).get("CurrencyCode") or "KRW"
        sub = QLabel(f"{plan} · {cur}")
        sub.setAlignment(Qt.AlignRight)
        sub.setStyleSheet("color: #94a3b8; font-size: 10px;")
        right.addWidget(sub)

        layout.addLayout(right, 0)


# ---------------------------------------------------------------------------
# 메인 탭
# ---------------------------------------------------------------------------


class CardUsageTab(QWidget):
    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._config = config
        self.client = BarobillCardClient.from_app_config(config)
        self._all_items: List[CardUsage] = []
        self._categories_index: Dict[str, str] = {}  # use_key → category code
        self._selected_category: Optional[str] = None
        self._last_synced_at: Optional[datetime] = None

        # ===== 헤더 =====
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("카드사용내역")
        tf = QFont(); tf.setBold(True); tf.setPointSize(20)
        title.setFont(tf)
        self.last_sync_label = QLabel("최근 동기화: -")
        self.last_sync_label.setStyleSheet("color: #94a3b8; font-size: 11px; padding-top: 6px;")

        title_box = QVBoxLayout()
        title_box.setSpacing(0)
        title_box.addWidget(title)
        title_box.addWidget(self.last_sync_label)
        header.addLayout(title_box)
        header.addStretch(1)

        self.refresh_chk = QCheckBox("즉시 갱신")
        self.refresh_chk.setToolTip("바로빌이 카드사로부터 새로 받아오도록 강제")

        self.sync_btn = QPushButton("🔄 바로빌 동기화")
        self.sync_btn.setStyleSheet(
            "QPushButton { background: #0f172a; color: #ffffff; border: none; "
            "border-radius: 8px; padding: 8px 16px; font-weight: 600; }"
            "QPushButton:hover { background: #1e293b; }"
            "QPushButton:disabled { background: #cbd5e1; }"
        )
        self.sync_btn.clicked.connect(self._on_sync)

        self.match_btn = QPushButton("🔗 쿠팡 매칭")
        self.match_btn.setStyleSheet(
            "QPushButton { background: #ef4444; color: #ffffff; border: none; "
            "border-radius: 8px; padding: 8px 16px; font-weight: 600; }"
            "QPushButton:hover { background: #dc2626; }"
        )
        self.match_btn.clicked.connect(self._on_match_coupang)

        header.addWidget(self.refresh_chk)
        header.addWidget(self.sync_btn)
        header.addWidget(self.match_btn)

        layout.addLayout(header)

        # ===== 통계 카드 =====
        cards_row = QHBoxLayout()
        cards_row.setSpacing(12)
        self.card_total = _SummaryCard("이번 달 총 지출")
        self.card_count = _SummaryCard("거래 건수")
        self.card_avg = _SummaryCard("일평균 지출")
        for c in (self.card_total, self.card_count, self.card_avg):
            c.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            cards_row.addWidget(c, 1)
        layout.addLayout(cards_row)

        # ===== 상태 배너 =====
        self.status_banner = QLabel("바로빌 동기화를 눌러 데이터를 가져오세요.")
        self.status_banner.setStyleSheet(
            "QLabel { background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 8px; "
            "padding: 10px 14px; color: #166534; font-size: 12px; }"
        )
        layout.addWidget(self.status_banner)

        # ===== 카테고리 칩 =====
        chip_row = QHBoxLayout()
        chip_row.setSpacing(8)
        self.category_chips: Dict[str, _CategoryChip] = {}
        for meta in DEFAULT_CATEGORIES:
            chip = _CategoryChip(meta.code, meta.label, meta.emoji, meta.bg_color)
            chip.clicked.connect(lambda _checked, c=meta.code: self._on_chip_clicked(c))
            self.category_chips[meta.code] = chip
            chip_row.addWidget(chip)
        self.clear_filter_btn = QPushButton("✕ 필터 해제")
        self.clear_filter_btn.setCursor(Qt.PointingHandCursor)
        self.clear_filter_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; color: #64748b; "
            "padding: 6px 8px; font-size: 12px; }"
            "QPushButton:hover { color: #0f172a; }"
        )
        self.clear_filter_btn.clicked.connect(self._on_clear_filter)
        chip_row.addWidget(self.clear_filter_btn)
        chip_row.addStretch(1)

        chip_scroll = QScrollArea()
        chip_scroll.setWidgetResizable(True)
        chip_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        chip_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        chip_scroll.setFixedHeight(48)
        chip_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        chip_inner = QWidget()
        chip_inner.setLayout(chip_row)
        chip_scroll.setWidget(chip_inner)
        layout.addWidget(chip_scroll)

        # ===== 필터 영역 =====
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        start_d, end_d = self._this_month_range()
        self.start_edit = QDateEdit(QDate(start_d.year, start_d.month, start_d.day))
        self.start_edit.setCalendarPopup(True)
        self.start_edit.setDisplayFormat("yyyy-MM-dd")
        self.end_edit = QDateEdit(QDate(end_d.year, end_d.month, end_d.day))
        self.end_edit.setCalendarPopup(True)
        self.end_edit.setDisplayFormat("yyyy-MM-dd")

        self.card_combo = QComboBox()
        self.card_combo.addItem("카드번호 전체", "")

        self.store_filter = QLineEdit()
        self.store_filter.setPlaceholderText("가맹점, 메모, 금액범위(>50000 / <10000 / 10000~50000)")

        self.refresh_btn = QPushButton("🔍 조회")
        self.refresh_btn.setStyleSheet(
            "QPushButton { background: #0f172a; color: #ffffff; border: none; "
            "border-radius: 6px; padding: 6px 14px; font-weight: 600; }"
            "QPushButton:hover { background: #1e293b; }"
        )
        self.refresh_btn.clicked.connect(self._on_refresh)
        self.store_filter.returnPressed.connect(self._on_search_changed)

        filter_row.addWidget(QLabel("기간"))
        filter_row.addWidget(self.start_edit)
        filter_row.addWidget(QLabel("~"))
        filter_row.addWidget(self.end_edit)
        filter_row.addWidget(self.card_combo)
        filter_row.addWidget(self.store_filter, 1)
        filter_row.addWidget(self.refresh_btn)
        layout.addLayout(filter_row)

        # ===== 거래내역 헤더 =====
        list_header = QHBoxLayout()
        self.list_count_label = QLabel("거래내역 0건")
        cf = QFont(); cf.setBold(True); cf.setPointSize(12)
        self.list_count_label.setFont(cf)

        self.sort_btn = QPushButton("날짜순 ↓")
        self.sort_btn.setCheckable(True)
        self.sort_btn.setStyleSheet(
            "QPushButton { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px; "
            "padding: 4px 10px; font-size: 11px; color: #475569; }"
            "QPushButton:checked { background: #0f172a; color: #ffffff; border-color: #0f172a; }"
        )
        self.sort_btn.clicked.connect(self._toggle_sort)
        self._sort_mode = "date"  # "date" or "amount"

        list_header.addWidget(self.list_count_label)
        list_header.addStretch(1)
        list_header.addWidget(self.sort_btn)
        layout.addLayout(list_header)

        # ===== 거래내역 리스트 =====
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setSpacing(6)
        self.list.setStyleSheet(
            "QListWidget { background: #f8fafc; border: none; }"
            "QListWidget::item { background: transparent; padding: 0px; border: none; }"
            "QListWidget::item:selected { background: rgba(15, 23, 42, 0.04); }"
        )
        layout.addWidget(self.list, 1)

        if not self.client.is_configured():
            miss = ", ".join(self.client.config.missing_fields())
            self.status_banner.setText(
                f"⚠ 바로빌 설정 누락: {miss}. credentials.json 의 barobill 섹션을 확인하세요."
            )
            self.status_banner.setStyleSheet(
                "QLabel { background: #fef3c7; border: 1px solid #fde68a; border-radius: 8px; "
                "padding: 10px 14px; color: #92400e; font-size: 12px; }"
            )

    # ----- helpers -----

    @staticmethod
    def _this_month_range() -> tuple[date, date]:
        today = date.today()
        return today.replace(day=1), today

    def _set_busy(self, busy: bool) -> None:
        for w in (self.sync_btn, self.match_btn, self.refresh_btn):
            w.setEnabled(not busy)

    def _q_to_iso(self, qd: QDate) -> str:
        return qd.toString("yyyy-MM-dd")

    def _require_configured(self) -> bool:
        if self.client.is_configured():
            return True
        miss = ", ".join(self.client.config.missing_fields())
        QMessageBox.warning(
            self, "바로빌 설정 누락",
            f"누락: {miss}\n\ncredentials.json 의 barobill 섹션에서 채우세요.",
        )
        return False

    # ----- actions -----

    def _on_sync(self) -> None:
        if not self._require_configured():
            return
        self.refresh_chk.setChecked(True)
        self._fetch()

    def _on_refresh(self) -> None:
        if not self._require_configured():
            return
        self._fetch()

    def _on_match_coupang(self) -> None:
        QMessageBox.information(
            self, "쿠팡 매칭",
            "쿠팡 매칭은 외부 card-api-service 가 필요합니다.\n"
            "기본 모드(바로빌 직접)에서는 칩 표시만 제공됩니다.",
        )

    def _on_chip_clicked(self, code: str) -> None:
        # 토글
        if self._selected_category == code:
            self._selected_category = None
            self.category_chips[code].setChecked(False)
        else:
            self._selected_category = code
            for c, chip in self.category_chips.items():
                chip.setChecked(c == code)
                chip._refresh_style()
        self._render_list()

    def _on_clear_filter(self) -> None:
        self._selected_category = None
        for chip in self.category_chips.values():
            chip.setChecked(False)
            chip._refresh_style()
        self.store_filter.clear()
        self._render_list()

    def _on_search_changed(self) -> None:
        self._render_list()

    def _toggle_sort(self) -> None:
        if self._sort_mode == "date":
            self._sort_mode = "amount"
            self.sort_btn.setText("금액순 ↓")
        else:
            self._sort_mode = "date"
            self.sort_btn.setText("날짜순 ↓")
        self._render_list()

    # ----- 데이터 가져오기 -----

    def _fetch(self) -> None:
        start = self._q_to_iso(self.start_edit.date())
        end = self._q_to_iso(self.end_edit.date())
        refresh = self.refresh_chk.isChecked()
        card = self.card_combo.currentData() or None

        self.status_banner.setText(f"🔄 조회 중... ({start} ~ {end})")
        self.status_banner.setStyleSheet(
            "QLabel { background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 8px; "
            "padding: 10px 14px; color: #1e40af; font-size: 12px; }"
        )
        self._set_busy(True)

        def work() -> Dict[str, Any]:
            return self.client.fetch_card_usages(
                start_date=start, end_date=end,
                card_num=card, refresh_before_fetch=refresh,
            )

        def done(result: _JobResult) -> None:
            self._set_busy(False)
            self.refresh_chk.setChecked(False)
            if not result.ok:
                self.status_banner.setText(f"❌ {result.error}")
                self.status_banner.setStyleSheet(
                    "QLabel { background: #fef2f2; border: 1px solid #fecaca; border-radius: 8px; "
                    "padding: 10px 14px; color: #991b1b; font-size: 12px; }"
                )
                return
            data = result.data or {}
            logs: List[CardUsage] = data.get("logs") or []
            target_cards: List[str] = data.get("targetCards") or []

            # 카드번호 콤보 갱신
            self._refresh_card_combo(target_cards)

            # 카테고리 자동 분류
            self._categories_index = {
                (it.use_key or it.id or ""): classify_category(
                    it.store_name, (it.raw or {}).get("UseStoreBizType")
                )
                for it in logs
            }

            self._all_items = logs
            self._last_synced_at = datetime.now()
            self.last_sync_label.setText(
                f"최근 동기화: {self._last_synced_at.strftime('%Y. %m. %d. %p %I:%M')}"
            )

            confirmed = sum(1 for it in logs if (it.amount or 0) != 0)
            empty = len(logs) - confirmed
            self.status_banner.setText(
                f"● 동기화 완료 · 조회 {len(logs)}건 / 저장 {len(logs)}건 · "
                f"금액확인 {confirmed}건 / 금액없음 {empty}건"
            )
            self.status_banner.setStyleSheet(
                "QLabel { background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 8px; "
                "padding: 10px 14px; color: #166534; font-size: 12px; }"
            )

            self._refresh_summary_cards()
            self._refresh_chip_amounts()
            self._render_list()

        _run_async(self, work, done)

    def _refresh_card_combo(self, cards: List[str]) -> None:
        cur = self.card_combo.currentData()
        self.card_combo.blockSignals(True)
        self.card_combo.clear()
        self.card_combo.addItem("카드번호 전체", "")
        for c in cards:
            self.card_combo.addItem(_mask_card_number(c), c)
        # 이전 선택 복원
        if cur:
            idx = self.card_combo.findData(cur)
            if idx >= 0:
                self.card_combo.setCurrentIndex(idx)
        self.card_combo.blockSignals(False)

    def _refresh_summary_cards(self) -> None:
        items = self._all_items
        # 이번 달
        today = date.today()
        first = today.replace(day=1)
        month_items = []
        for it in items:
            d = _parse_used_at(it.used_at)
            if d and d.date() >= first and d.date() <= today:
                month_items.append(it)
        total = sum(int(it.amount or 0) for it in month_items)
        # 음수(취소) 차감 이미 들어있음 (final amount 가 음수)
        positive_total = sum(int(it.amount or 0) for it in month_items if (it.amount or 0) > 0)
        cancel_total = abs(sum(int(it.amount or 0) for it in month_items if (it.amount or 0) < 0))
        net = positive_total - cancel_total

        self.card_total.set_value(
            f"{net:,}원",
            f"{first.strftime('%Y. %m. %d')} ─ {today.strftime('%m. %d')}",
        )

        confirmed = sum(1 for it in month_items if (it.amount or 0) != 0)
        empty = len(month_items) - confirmed
        self.card_count.set_value(
            f"{len(month_items)}건",
            f"금액확인 {confirmed}건 · 미확인 {empty}건",
        )

        days_passed = max(1, (today - first).days + 1)
        avg = net // days_passed
        self.card_avg.set_value(f"{avg:,}원", f"{days_passed}일 기준")

    def _refresh_chip_amounts(self) -> None:
        sums: Dict[str, int] = {}
        for it in self._all_items:
            cat = self._categories_index.get(it.use_key or it.id or "", "OTHER")
            sums[cat] = sums.get(cat, 0) + max(0, int(it.amount or 0))
        for code, chip in self.category_chips.items():
            chip.set_amount(sums.get(code, 0))

    # ----- 리스트 렌더 -----

    def _render_list(self) -> None:
        self.list.clear()
        items = list(self._all_items)

        # 카테고리 필터
        if self._selected_category:
            items = [
                it for it in items
                if self._categories_index.get(it.use_key or it.id or "", "OTHER") == self._selected_category
            ]

        # 검색 (가맹점/메모/금액범위)
        q = self.store_filter.text().strip()
        if q:
            items = self._apply_search(items, q)

        # 정렬
        if self._sort_mode == "amount":
            items.sort(key=lambda it: abs(int(it.amount or 0)), reverse=True)
        else:
            items.sort(key=lambda it: (it.used_at or ""), reverse=True)

        self.list_count_label.setText(f"거래내역 {len(items)}건")

        for usage in items:
            cat = self._categories_index.get(usage.use_key or usage.id or "", "OTHER")
            row = _UsageRow(usage, cat)
            li = QListWidgetItem()
            li.setSizeHint(row.sizeHint())
            self.list.addItem(li)
            self.list.setItemWidget(li, row)

    def _apply_search(self, items: List[CardUsage], query: str) -> List[CardUsage]:
        # 금액 범위 표현 우선
        m = re.match(r"^>\s*(\d[\d,]*)\s*$", query)
        if m:
            n = int(m.group(1).replace(",", ""))
            return [it for it in items if abs(int(it.amount or 0)) > n]
        m = re.match(r"^<\s*(\d[\d,]*)\s*$", query)
        if m:
            n = int(m.group(1).replace(",", ""))
            return [it for it in items if abs(int(it.amount or 0)) < n]
        m = re.match(r"^(\d[\d,]*)\s*~\s*(\d[\d,]*)\s*$", query)
        if m:
            a = int(m.group(1).replace(",", ""))
            b = int(m.group(2).replace(",", ""))
            lo, hi = min(a, b), max(a, b)
            return [it for it in items if lo <= abs(int(it.amount or 0)) <= hi]
        # 텍스트 검색 (가맹점명 + 메모)
        ql = query.lower()
        return [
            it for it in items
            if ql in (it.store_name or "").lower()
            or ql in (it.memo or "").lower()
        ]

    def shutdown(self) -> None:
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass


__all__ = ["CardUsageTab"]
