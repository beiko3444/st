"""카드 사용내역 탭 — 통계 카드 + 카테고리 칩 + 거래 카드 리스트.

beico-app 의 카드사용내역 화면 디자인을 참고 (사용자 요청 스크린샷).
"""

from __future__ import annotations

import calendar
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QDate, QEvent, QMimeData, QObject, QPointF, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QDrag, QFont, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
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
from inventory_app.services.card_category import (
    CategoryMeta,
    DEFAULT_CATEGORIES,
    category_meta,
    classify_category,
)
from inventory_app.services.purchase_history_service import (
    PurchaseGroup,
    PurchaseHistoryStore,
    dedupe_order_items,
    group_records_by_order,
)
from inventory_app.services.pi_data_client import PiDataClient, PiDataError
from inventory_app.models import PurchaseOrder


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


class _ReviewCheckButton(QPushButton):
    """검토 토글 — 정사각형 체크박스. ✓ 마크는 QPainter 로 직접 스트로크."""

    _SIZE = 22

    def __init__(self, reviewed: bool, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._reviewed = bool(reviewed)
        self.setFixedSize(self._SIZE, self._SIZE)
        self.setCursor(Qt.PointingHandCursor)
        self.setFlat(True)
        self.setStyleSheet("QPushButton { background: transparent; border: none; padding: 0; }")

    def set_reviewed(self, reviewed: bool) -> None:
        if bool(reviewed) != self._reviewed:
            self._reviewed = bool(reviewed)
            self.update()

    def paintEvent(self, event):  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect().adjusted(1, 1, -1, -1)
        radius = 4.0

        hovered = self.underMouse()
        bg = QColor("#f1f5f9") if hovered else QColor("#ffffff")
        border = QColor("#64748b") if hovered else QColor("#94a3b8")
        p.setBrush(QBrush(bg))
        pen = QPen(border)
        pen.setWidthF(1.2)
        p.setPen(pen)
        p.drawRoundedRect(rect, radius, radius)

        if self._reviewed:
            pen = QPen(QColor("#0f172a"))
            pen.setWidthF(2.2)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            w = rect.width()
            h = rect.height()
            x0 = rect.left()
            y0 = rect.top()
            p1 = QPointF(x0 + w * 0.24, y0 + h * 0.52)
            p2 = QPointF(x0 + w * 0.44, y0 + h * 0.70)
            p3 = QPointF(x0 + w * 0.76, y0 + h * 0.34)
            p.drawPolyline([p1, p2, p3])
        p.end()

    def enterEvent(self, event):  # type: ignore[override]
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):  # type: ignore[override]
        self.update()
        super().leaveEvent(event)


class _SummaryCard(QFrame):
    """상단 통계 카드 (이번 달 지출 / 거래 건수 / 일평균 지출)."""

    def __init__(self, title: str) -> None:
        super().__init__()
        self.setObjectName("summaryCard")
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(
            "#summaryCard { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; }"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        self.title_label = QLabel(title)
        self.title_label.setStyleSheet("color: #64748b; font-size: 11px; font-weight: 500;")

        self.value_label = QLabel("-")
        f = QFont()
        f.setPointSize(13)
        f.setBold(True)
        self.value_label.setFont(f)
        self.value_label.setStyleSheet("color: #0f172a;")

        self.sub_label = QLabel("")
        self.sub_label.setStyleSheet("color: #94a3b8; font-size: 10px;")

        layout.addWidget(self.title_label)
        layout.addStretch(1)
        layout.addWidget(self.value_label)
        layout.addWidget(self.sub_label)

    def set_value(self, text: str, sub: str = "") -> None:
        self.value_label.setText(text)
        self.sub_label.setText(f"· {sub}" if sub else "")


class _FlowLayout(QHBoxLayout):
    """간단 flow 레이아웃 — 가로 공간 부족하면 줄바꿈.

    Qt 표준이 없어 직접 구현. PySide6.QtWidgets.QLayout 을 상속해 heightForWidth 지원.
    """

    def __init__(self, parent=None, spacing_h: int = 8, spacing_v: int = 8) -> None:
        super().__init__()
        # NOTE: QHBoxLayout 을 상속받지만 실제로는 별도 wrap 동작이 필요 — Qt 의
        # QLayout 직접 상속이 더 정확하지만 PySide6 에서 까다로워 wrapper 위젯으로 구현.
        # → 아래 _FlowWrap 위젯 사용을 권장.
        raise NotImplementedError("_FlowLayout placeholder — use _FlowWrap widget")


class _FlowWrap(QWidget):
    """가로 공간이 부족하면 자동으로 줄바꿈하는 컨테이너.

    QLayout 직접 상속 대신 resizeEvent 마다 자식 위젯들의 위치를 재계산.
    """

    def __init__(self, h_spacing: int = 8, v_spacing: int = 6, parent=None) -> None:
        super().__init__(parent)
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing
        self._items: list[QWidget] = []
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.MinimumExpanding)

    def add_widget(self, w: QWidget) -> None:
        w.setParent(self)
        self._items.append(w)
        w.show()
        self._relayout()

    def remove_widget(self, w: QWidget) -> None:
        if w in self._items:
            self._items.remove(w)
        try:
            w.setParent(None)
            w.deleteLater()
        except Exception:  # noqa: BLE001
            pass
        self._relayout()

    def set_order(self, widgets: list[QWidget]) -> None:
        """이미 추가된 위젯들의 표시 순서를 바꾼다."""
        known = [w for w in widgets if w in self._items]
        rest = [w for w in self._items if w not in known]
        self._items = known + rest
        for w in self._items:
            w.raise_()
        self._relayout()

    def _relayout(self) -> None:
        x = 0
        y = 0
        line_h = 0
        max_w = max(0, self.width() - 1)
        for w in self._items:
            sh = w.sizeHint()
            ww = sh.width()
            wh = sh.height()
            if x + ww > max_w and x > 0:
                # 다음 줄로
                x = 0
                y += line_h + self._v_spacing
                line_h = 0
            w.setGeometry(x, y, ww, wh)
            x += ww + self._h_spacing
            if wh > line_h:
                line_h = wh
        total_h = y + line_h
        # 부모에게 적정 높이 전달
        self.setMinimumHeight(total_h)

    def resizeEvent(self, event):  # type: ignore[override]
        super().resizeEvent(event)
        self._relayout()


class _CategoryChip(QPushButton):
    """카테고리 칩 (선택 시 체크 상태)."""

    moved = Signal(str, str)
    edit_requested = Signal(str)

    def __init__(self, code: str, label_text: str, emoji: str, bg_color: str = "#f1f5f9") -> None:
        super().__init__(f"{emoji} {label_text}")
        self.code = code
        self.label_text = label_text
        self.emoji = emoji
        self.shortcut_label = ""
        self._press_pos = None
        self.setCheckable(True)
        self.setAcceptDrops(True)
        self.setContextMenuPolicy(Qt.DefaultContextMenu)
        self.setCursor(Qt.PointingHandCursor)
        self._bg = bg_color
        self._refresh_style()

    def set_display(self, label_text: str, emoji: str, bg_color: Optional[str] = None) -> None:
        self.label_text = label_text
        self.emoji = emoji
        if bg_color:
            self._bg = bg_color
        self.set_amount(0)

    def set_shortcut_label(self, shortcut_label: str) -> None:
        self.shortcut_label = shortcut_label
        self.set_amount(0)

    def set_amount(self, amount: int) -> None:
        prefix = f"{self.shortcut_label} " if self.shortcut_label else ""
        if amount > 0:
            self.setText(f"{prefix}{self.emoji} {self.label_text} {amount:,}원")
        else:
            self.setText(f"{prefix}{self.emoji} {self.label_text}")
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

    def mousePressEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            self._press_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def contextMenuEvent(self, event):  # type: ignore[override]
        self.edit_requested.emit(self.code)
        event.accept()

    def mouseMoveEvent(self, event):  # type: ignore[override]
        if not (event.buttons() & Qt.LeftButton) or self._press_pos is None:
            super().mouseMoveEvent(event)
            return
        if (event.position().toPoint() - self._press_pos).manhattanLength() < QApplication.startDragDistance():
            super().mouseMoveEvent(event)
            return
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData("application/x-smartinventory-card-category", self.code.encode("utf-8"))
        drag.setMimeData(mime)
        # 드래그 중 칩 모양 그대로 보이도록 pixmap 부착 + 그림자 효과
        try:
            from PySide6.QtGui import QPixmap as _QPx, QPainter as _QPa
            from PySide6.QtCore import QSize as _QSize
            src_pix = self.grab()
            # 그림자/투명도 효과 — 시각적 드래그 확인용
            pix = _QPx(src_pix.size())
            pix.fill(Qt.transparent)
            painter = _QPa(pix)
            painter.setOpacity(0.85)
            painter.drawPixmap(0, 0, src_pix)
            painter.end()
            drag.setPixmap(pix)
            drag.setHotSpot(self._press_pos)
        except Exception:  # noqa: BLE001
            drag.setPixmap(self.grab())
            drag.setHotSpot(self._press_pos or self.rect().center())
        # 드래그 시작 시 원본 칩을 살짝 흐리게 — 사용자가 "잡혔다" 는 걸 인식
        try:
            self.setStyleSheet(self.styleSheet() + "\nQPushButton { color: #94a3b8; }")
        except Exception:  # noqa: BLE001
            pass
        try:
            drag.exec(Qt.MoveAction)
        finally:
            self._refresh_style()  # 원래 스타일로 복귀

    def dragEnterEvent(self, event):  # type: ignore[override]
        if event.mimeData().hasFormat("application/x-smartinventory-card-category"):
            source = bytes(
                event.mimeData().data("application/x-smartinventory-card-category")
            ).decode("utf-8")
            if source and source != self.code:
                # 드롭 타겟 강조 — 파란 점선 보더
                self.setStyleSheet(
                    f"QPushButton {{ background: #dbeafe; color: #1e40af; "
                    f"border: 2px dashed #2563eb; "
                    f"border-radius: 16px; padding: 5px 13px; font-weight: 600; }}"
                )
                event.acceptProposedAction()
                return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):  # type: ignore[override]
        if event.mimeData().hasFormat("application/x-smartinventory-card-category"):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dragLeaveEvent(self, event):  # type: ignore[override]
        # 강조 해제
        self._refresh_style()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):  # type: ignore[override]
        # 강조 해제 (성공/실패 둘 다)
        self._refresh_style()
        if not event.mimeData().hasFormat("application/x-smartinventory-card-category"):
            super().dropEvent(event)
            return
        source = bytes(event.mimeData().data("application/x-smartinventory-card-category")).decode("utf-8")
        if source and source != self.code:
            self.moved.emit(source, self.code)
            event.acceptProposedAction()


_CATEGORY_ICON_CHOICES: list[tuple[str, str]] = [
    ("☕", "카페"), ("🍽️", "음식"), ("🥖", "베이커리"), ("🚆", "교통"),
    ("🛒", "쇼핑"), ("🛍️", "편의점"), ("⛽", "주유"), ("💳", "금융"),
    ("📱", "통신"), ("🖨️", "사무"), ("🏥", "의료"), ("📚", "교육"),
    ("🎬", "문화"), ("📦", "기타"), ("🧾", "영수증"), ("💰", "지출"),
    ("🏪", "매장"), ("🍔", "패스트푸드"), ("🍕", "피자"), ("🍜", "면/분식"),
    ("🥗", "건강식"), ("🛻", "배송"), ("🚕", "택시"), ("🚗", "차량"),
    ("🏠", "생활"), ("🧴", "생활용품"), ("💊", "약"), ("🦷", "치과"),
    ("🎮", "게임"), ("🏋️", "운동"), ("✈️", "여행"), ("🎁", "선물"),
]


class _CategoryEditDialog(QDialog):
    """카테고리 표시명/아이콘 편집 + 삭제."""

    def __init__(self, meta: CategoryMeta, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("카테고리 수정")
        self.setMinimumWidth(420)
        self._selected_emoji = meta.emoji
        self._delete_requested = False
        self._meta_code = meta.code

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        title = QLabel(f"{meta.emoji} {meta.label}")
        f = QFont(); f.setBold(True); f.setPointSize(13)
        title.setFont(f)
        title.setStyleSheet("color: #0f172a;")
        layout.addWidget(title)

        name_label = QLabel("카테고리명")
        name_label.setStyleSheet("color: #475569; font-size: 11px; font-weight: 600;")
        layout.addWidget(name_label)

        self.name_edit = QLineEdit(meta.label)
        self.name_edit.setPlaceholderText("표시할 카테고리명")
        self.name_edit.setStyleSheet(
            "QLineEdit { border: 1px solid #cbd5e1; border-radius: 8px; padding: 8px 10px; "
            "font-size: 13px; color: #0f172a; background: #ffffff; }"
            "QLineEdit:focus { border-color: #0f766e; }"
        )
        layout.addWidget(self.name_edit)

        icon_label = QLabel("아이콘 선택")
        icon_label.setStyleSheet("color: #475569; font-size: 11px; font-weight: 600;")
        layout.addWidget(icon_label)

        self.icon_list = QListWidget()
        self.icon_list.setViewMode(QListWidget.IconMode)
        self.icon_list.setMovement(QListWidget.Static)
        self.icon_list.setResizeMode(QListWidget.Adjust)
        self.icon_list.setGridSize(QSize(92, 40))
        self.icon_list.setSpacing(6)
        self.icon_list.setFixedHeight(172)
        self.icon_list.setStyleSheet(
            "QListWidget { border: 1px solid #e2e8f0; border-radius: 8px; background: #f8fafc; padding: 6px; }"
            "QListWidget::item { border-radius: 8px; padding: 6px; color: #334155; }"
            "QListWidget::item:selected { background: #0f766e; color: #ffffff; }"
        )
        for emoji, label in _CATEGORY_ICON_CHOICES:
            item = QListWidgetItem(f"{emoji} {label}")
            item.setData(Qt.UserRole, emoji)
            self.icon_list.addItem(item)
            if emoji == meta.emoji:
                self.icon_list.setCurrentItem(item)
        if self.icon_list.currentItem() is None and self.icon_list.count() > 0:
            self.icon_list.setCurrentRow(0)
        self.icon_list.currentItemChanged.connect(self._on_icon_changed)
        layout.addWidget(self.icon_list)

        # 삭제 버튼 — 좌측, 빨강
        btn_row = QHBoxLayout()
        self.delete_btn = QPushButton("🗑 삭제")
        self.delete_btn.setStyleSheet(
            "QPushButton { background: #fef2f2; color: #b91c1c; "
            "border: 1px solid #fecaca; border-radius: 8px; "
            "padding: 6px 14px; font-weight: 600; }"
            "QPushButton:hover { background: #fee2e2; }"
        )
        self.delete_btn.clicked.connect(self._on_delete_clicked)
        btn_row.addWidget(self.delete_btn)
        btn_row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        btn_row.addWidget(buttons)
        layout.addLayout(btn_row)

    def _on_delete_clicked(self) -> None:
        ans = QMessageBox.question(
            self, "카테고리 삭제",
            f"'{self.name_edit.text().strip() or self._meta_code}' 카테고리를 칩 목록에서 제거할까요?\n\n"
            "이 카테고리에 속해 있던 항목은 모두 '기타' 로 이동합니다.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ans == QMessageBox.Yes:
            self._delete_requested = True
            self.accept()

    def is_delete_requested(self) -> bool:
        return self._delete_requested

    def _on_icon_changed(self, current: Optional[QListWidgetItem], _previous: Optional[QListWidgetItem]) -> None:
        if current is not None:
            self._selected_emoji = str(current.data(Qt.UserRole) or self._selected_emoji)

    def values(self) -> tuple[str, str]:
        label = self.name_edit.text().strip()
        return label, self._selected_emoji


class _FixedCostsService:
    """매월 반복되는 고정비 (구독료, 통신비, 임대료 등) 저장/조회.

    저장 위치: ~/.smartinventory/fixed_costs.json
    각 항목: {id: int, name: str, day_of_month: int (1-31), amount: int, memo: str, active: bool}
    """

    def __init__(self) -> None:
        self._items: List[Dict[str, Any]] = []
        self._load()

    @staticmethod
    def _path() -> Path:
        d = Path.home() / ".smartinventory"
        d.mkdir(parents=True, exist_ok=True)
        return d / "fixed_costs.json"

    def _load(self) -> None:
        try:
            import json as _j
            p = self._path()
            if not p.exists():
                self._items = []
                return
            data = _j.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self._items = [
                    {
                        "id": int(x.get("id") or 0),
                        "name": str(x.get("name") or ""),
                        "day_of_month": max(1, min(31, int(x.get("day_of_month") or 1))),
                        "amount": int(x.get("amount") or 0),
                        "memo": str(x.get("memo") or ""),
                        "active": bool(x.get("active", True)),
                    }
                    for x in data if isinstance(x, dict)
                ]
        except Exception:  # noqa: BLE001
            self._items = []

    def _save(self) -> None:
        try:
            import json as _j
            self._path().write_text(
                _j.dumps(self._items, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass

    def list_items(self) -> List[Dict[str, Any]]:
        return [dict(it) for it in self._items]

    def add(self, name: str, day_of_month: int, amount: int, memo: str = "") -> int:
        new_id = (max((it["id"] for it in self._items), default=0) or 0) + 1
        self._items.append({
            "id": new_id,
            "name": name.strip() or "(이름 없음)",
            "day_of_month": max(1, min(31, int(day_of_month or 1))),
            "amount": int(amount or 0),
            "memo": memo.strip(),
            "active": True,
        })
        self._save()
        return new_id

    def update(self, item_id: int, **kwargs) -> None:
        for it in self._items:
            if it["id"] == item_id:
                for k, v in kwargs.items():
                    if k == "day_of_month":
                        it[k] = max(1, min(31, int(v or 1)))
                    elif k == "amount":
                        it[k] = int(v or 0)
                    elif k == "active":
                        it[k] = bool(v)
                    else:
                        it[k] = str(v or "")
                self._save()
                return

    def delete(self, item_id: int) -> None:
        self._items = [it for it in self._items if it["id"] != item_id]
        self._save()

    def total_active(self) -> int:
        return sum(int(it.get("amount") or 0) for it in self._items if it.get("active", True))


class _FixedCostsTable(QWidget):
    """고정비 입력/관리 테이블. changed Signal 으로 합계 재계산 트리거."""

    changed = Signal()

    def __init__(self, service: _FixedCostsService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._service = service
        self._suspend = False

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        # 안내 + 합계
        head = QHBoxLayout()
        info = QLabel(
            "💡 매달 반복되는 고정비를 입력하세요. 일자(매월 며칠)와 금액을 적으면 "
            "이번 달 총 지출에 자동으로 합산됩니다."
        )
        info.setStyleSheet(
            "QLabel { background: #fef9c3; border: 1px solid #fde68a; border-radius: 6px; "
            "padding: 6px 10px; color: #854d0e; font-size: 11px; }"
        )
        info.setWordWrap(True)
        head.addWidget(info, 1)
        v.addLayout(head)

        # 입력 row
        add_row = QHBoxLayout()
        add_row.setSpacing(6)
        self.day_edit = QLineEdit()
        self.day_edit.setPlaceholderText("일자 (1~31)")
        self.day_edit.setFixedWidth(90)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("항목명 (예: 임대료, 인터넷)")
        self.amount_edit = QLineEdit()
        self.amount_edit.setPlaceholderText("금액 (원)")
        self.amount_edit.setFixedWidth(140)
        self.memo_edit = QLineEdit()
        self.memo_edit.setPlaceholderText("메모(선택)")
        self.add_btn = QPushButton("＋ 추가")
        self.add_btn.setStyleSheet(
            "QPushButton { background: #0f172a; color: #ffffff; border: none; "
            "border-radius: 6px; padding: 6px 14px; font-weight: 600; }"
            "QPushButton:hover { background: #1e293b; }"
        )
        self.add_btn.clicked.connect(self._on_add)
        for w in (self.day_edit, self.name_edit, self.amount_edit, self.memo_edit):
            w.setStyleSheet(
                "QLineEdit { border: 1px solid #cbd5e1; border-radius: 6px; padding: 6px 8px; "
                "background: #ffffff; }"
                "QLineEdit:focus { border-color: #0f172a; }"
            )
        # Enter 로도 추가
        self.amount_edit.returnPressed.connect(self._on_add)
        self.memo_edit.returnPressed.connect(self._on_add)
        add_row.addWidget(self.day_edit)
        add_row.addWidget(self.name_edit, 2)
        add_row.addWidget(self.amount_edit)
        add_row.addWidget(self.memo_edit, 2)
        add_row.addWidget(self.add_btn)
        v.addLayout(add_row)

        # 테이블
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["사용", "일자", "항목명", "금액", "메모", "삭제"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setStyleSheet(
            "QTableWidget { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px;"
            " gridline-color: #eef2f7; outline: 0; }"
            "QHeaderView::section { background: #f8fafc; border: none;"
            " border-right: 1px solid #eef2f7;"
            " border-bottom: 1px solid #e2e8f0; padding: 8px; font-weight: 600; color: #475569; }"
            "QTableWidget::item { padding: 6px 8px; border: none;"
            " border-right: 1px solid #f1f5f9; }"
        )
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(4, QHeaderView.Stretch)
        h.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.table.cellChanged.connect(self._on_cell_changed)
        v.addWidget(self.table, 1)

        self._render()

    def _on_add(self) -> None:
        try:
            day = int(self.day_edit.text().strip() or "0")
        except ValueError:
            day = 0
        if day < 1 or day > 31:
            QMessageBox.warning(self, "일자 오류", "일자는 1~31 사이의 숫자여야 합니다.")
            return
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "항목명 필요", "항목명을 입력하세요.")
            return
        try:
            amount = int(self.amount_edit.text().strip().replace(",", "") or "0")
        except ValueError:
            QMessageBox.warning(self, "금액 오류", "금액은 숫자로 입력하세요.")
            return
        if amount <= 0:
            QMessageBox.warning(self, "금액 오류", "금액은 0원보다 커야 합니다.")
            return
        memo = self.memo_edit.text().strip()
        self._service.add(name, day, amount, memo)
        self.day_edit.clear()
        self.name_edit.clear()
        self.amount_edit.clear()
        self.memo_edit.clear()
        self._render()
        self.changed.emit()
        self.day_edit.setFocus()

    def _render(self) -> None:
        self._suspend = True
        try:
            items = self._service.list_items()
            items.sort(key=lambda x: (x.get("day_of_month") or 0, x.get("name") or ""))
            self.table.setRowCount(len(items))
            for r, it in enumerate(items):
                # 사용 체크박스
                chk = QCheckBox()
                chk.setChecked(bool(it.get("active", True)))
                chk.toggled.connect(
                    lambda checked, iid=it["id"]: self._on_active_toggled(iid, checked)
                )
                wrap = QWidget()
                wlay = QHBoxLayout(wrap)
                wlay.setContentsMargins(0, 0, 0, 0)
                wlay.setAlignment(Qt.AlignCenter)
                wlay.addWidget(chk)
                self.table.setCellWidget(r, 0, wrap)

                # 일자
                day_item = QTableWidgetItem(str(it.get("day_of_month") or ""))
                day_item.setTextAlignment(Qt.AlignCenter)
                day_item.setData(Qt.UserRole, it["id"])
                self.table.setItem(r, 1, day_item)

                # 항목명
                name_item = QTableWidgetItem(str(it.get("name") or ""))
                name_item.setData(Qt.UserRole, it["id"])
                self.table.setItem(r, 2, name_item)

                # 금액
                amt_item = QTableWidgetItem(f"{int(it.get('amount') or 0):,}")
                amt_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                af = QFont(); af.setBold(True); amt_item.setFont(af)
                amt_item.setData(Qt.UserRole, it["id"])
                self.table.setItem(r, 3, amt_item)

                # 메모
                memo_item = QTableWidgetItem(str(it.get("memo") or ""))
                memo_item.setData(Qt.UserRole, it["id"])
                self.table.setItem(r, 4, memo_item)

                # 삭제 버튼
                del_btn = QPushButton("🗑")
                del_btn.setCursor(Qt.PointingHandCursor)
                del_btn.setStyleSheet(
                    "QPushButton { background: #fef2f2; color: #b91c1c; "
                    "border: 1px solid #fecaca; border-radius: 6px; "
                    "padding: 2px 8px; font-weight: 600; }"
                    "QPushButton:hover { background: #fee2e2; }"
                )
                del_btn.clicked.connect(
                    lambda _ck=False, iid=it["id"]: self._on_delete(iid)
                )
                wrap2 = QWidget()
                wlay2 = QHBoxLayout(wrap2)
                wlay2.setContentsMargins(2, 0, 2, 0)
                wlay2.addWidget(del_btn)
                self.table.setCellWidget(r, 5, wrap2)
                self.table.setRowHeight(r, 30)
        finally:
            self._suspend = False

    def _on_active_toggled(self, item_id: int, checked: bool) -> None:
        self._service.update(item_id, active=bool(checked))
        self.changed.emit()

    def _on_delete(self, item_id: int) -> None:
        ans = QMessageBox.question(
            self, "고정비 삭제", "이 고정비 항목을 삭제할까요?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        self._service.delete(item_id)
        self._render()
        self.changed.emit()

    def _on_cell_changed(self, row: int, col: int) -> None:
        if self._suspend:
            return
        if col not in (1, 2, 3, 4):
            return
        item = self.table.item(row, col)
        if item is None:
            return
        item_id = item.data(Qt.UserRole)
        if item_id is None:
            return
        new_text = item.text().strip()
        try:
            if col == 1:
                day = int(new_text or "0")
                if day < 1 or day > 31:
                    raise ValueError("1~31")
                self._service.update(item_id, day_of_month=day)
            elif col == 2:
                self._service.update(item_id, name=new_text)
            elif col == 3:
                amt = int(new_text.replace(",", "") or "0")
                if amt < 0:
                    raise ValueError("음수 불가")
                self._service.update(item_id, amount=amt)
            elif col == 4:
                self._service.update(item_id, memo=new_text)
        except Exception:  # noqa: BLE001
            QMessageBox.warning(self, "값 오류", "유효하지 않은 값입니다.")
            self._render()
            return
        self._render()
        self.changed.emit()


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


class _CoupangMatchDetailDialog(QDialog):
    """카드 사용내역에 매칭된 쿠팡 구매내역 상세 팝업."""

    def __init__(
        self,
        usage: CardUsage,
        group: Optional[PurchaseGroup],
        items: List[Any],
        order: Optional[PurchaseOrder],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("쿠팡 매칭 상세")
        self.resize(1100, 820)
        self.setMinimumSize(720, 520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        # 1) 카드 사용 정보
        card_box = QFrame()
        card_box.setStyleSheet(
            "QFrame { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; }"
        )
        card_lay = QVBoxLayout(card_box)
        card_lay.setContentsMargins(12, 10, 12, 10)
        card_lay.setSpacing(4)

        head_font = QFont(); head_font.setBold(True); head_font.setPointSize(11)
        head = QLabel("💳 카드 사용 내역")
        head.setFont(head_font)
        head.setStyleSheet("color: #0f172a;")
        card_lay.addWidget(head)

        amt = int(usage.amount or 0)
        amt_color = "#ef4444" if amt < 0 else "#0f172a"
        store = (usage.store_name or "(가맹점명 미상)").strip()
        info_html = (
            f"<div style='font-size:12px; color:#334155;'>"
            f"<b style='color:#0f172a; font-size:13px;'>{store}</b><br>"
            f"<span style='color:#64748b;'>{_format_used_at_short(usage.used_at)}"
            f" · {_mask_card_number(usage.card_num)}</span><br>"
            f"<span style='color:{amt_color}; font-weight:600;'>{_fmt_money(amt)}</span>"
            f"</div>"
        )
        info_label = QLabel(info_html)
        info_label.setTextFormat(Qt.RichText)
        info_label.setWordWrap(True)
        card_lay.addWidget(info_label)
        layout.addWidget(card_box)

        # 2) 매칭된 주문 정보
        if group is None:
            none_lab = QLabel("⚠ 매칭된 쿠팡 구매내역이 없습니다.")
            none_lab.setStyleSheet(
                "QLabel { background: #fef3c7; border: 1px solid #fde68a; border-radius: 8px; "
                "padding: 10px 12px; color: #92400e; font-size: 12px; }"
            )
            layout.addWidget(none_lab)
        else:
            order_box = QFrame()
            order_box.setStyleSheet(
                "QFrame { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; }"
            )
            order_lay = QVBoxLayout(order_box)
            order_lay.setContentsMargins(12, 10, 12, 10)
            order_lay.setSpacing(4)

            head2 = QLabel("🔗 매칭된 쿠팡 주문")
            head2.setFont(head_font)
            head2.setStyleSheet("color: #0f172a;")
            order_lay.addWidget(head2)

            lines: List[str] = []
            lines.append(f"<b style='color:#0f172a;'>{group.title}</b>")
            sub_parts: List[str] = []
            if group.order_date:
                sub_parts.append(f"주문일 {group.order_date}")
            if group.item_count:
                sub_parts.append(f"품목 {group.item_count}건")
            if order is not None:
                if order.order_no:
                    sub_parts.append(f"주문번호 {order.order_no}")
                if order.status:
                    sub_parts.append(f"상태 {order.status}")
            if sub_parts:
                lines.append(
                    f"<span style='color:#64748b; font-size:11px;'>{' · '.join(sub_parts)}</span>"
                )
            # 카드청구액 합계 — 품목 중 취소완료/주문취소 표제는 음수로 처리해 제외.
            # 기존 group.total_amount 는 atomic 으로 합쳐진 단일 값이라 이미 정확하지만,
            # 품목 단에서 취소건이 섞여 있으면 sum 이 실제 카드 청구과 어긋날 수 있어 재계산.
            cancel_tokens = ("[취소완료]", "[주문취소]", "[결제취소]", "[구매취소]", "[반품완료]")
            def _item_signed(rec) -> int:
                t = (getattr(rec, "title", "") or "").strip()
                a = int(getattr(rec, "amount", 0) or 0)
                if any(tok in t for tok in cancel_tokens):
                    return -abs(a)
                return a
            items_signed_total = sum(_item_signed(it) for it in items) if items else None
            display_total = (
                items_signed_total if items_signed_total is not None
                else int(group.total_amount or 0)
            )
            color_total = "#ef4444" if display_total < 0 else "#0f172a"
            tot_line = (
                f"카드청구액 합계: <b style='color:{color_total};'>{display_total:,}원</b>"
            )
            if items_signed_total is not None and items_signed_total != int(group.total_amount or 0):
                tot_line += (
                    f" <span style='color:#94a3b8; font-size:11px;'>"
                    f"(원장 {int(group.total_amount or 0):,}원, 취소품목 제외 후 {display_total:,}원)</span>"
                )
            elif order is not None:
                if order.payment_total is not None and order.payment_total != group.total_amount:
                    tot_line += (
                        f" <span style='color:#64748b; font-size:11px;'>"
                        f"(결제총액 {int(order.payment_total):,}원"
                    )
                    if order.cash_used and int(order.cash_used) >= 100:
                        tot_line += f" − 캐시 {int(order.cash_used):,}원"
                    tot_line += ")</span>"
            lines.append(f"<div style='margin-top:4px;'>{tot_line}</div>")

            # 카드 금액과 일치 여부 — 취소완료 품목 제외한 signed total 기준으로 비교
            ref_total = (
                items_signed_total if items_signed_total is not None
                else int(group.total_amount or 0)
            )
            diff = amt - int(ref_total)
            if diff == 0:
                badge = (
                    "<div style='margin-top:6px;'>"
                    "<span style='background:#dcfce7; color:#166534; padding:2px 8px; "
                    "border-radius:10px; font-size:11px; font-weight:600;'>"
                    "✓ 금액 일치</span></div>"
                )
            else:
                sign = "+" if diff > 0 else ""
                badge = (
                    "<div style='margin-top:6px;'>"
                    f"<span style='background:#fef3c7; color:#92400e; padding:2px 8px; "
                    f"border-radius:10px; font-size:11px; font-weight:600;'>"
                    f"⚠ 차액 {sign}{diff:,}원</span></div>"
                )
            lines.append(badge)

            order_label = QLabel("<br>".join(lines))
            order_label.setTextFormat(Qt.RichText)
            order_label.setWordWrap(True)
            order_lay.addWidget(order_label)
            layout.addWidget(order_box)

        # 3) 품목 테이블
        items_label = QLabel(f"📦 품목 ({len(items)}건)")
        items_label.setFont(head_font)
        items_label.setStyleSheet("color: #0f172a;")
        layout.addWidget(items_label)

        items_table = QTableWidget(0, 4)
        items_table.setHorizontalHeaderLabels(["주문일", "주문번호", "상품/내역", "금액"])
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
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        items_table.setRowCount(len(items))
        # 표시 단계에서도 취소품목은 음수로 강제 (원장 amount 가 양수라도)
        cancel_tokens_disp = ("[취소완료]", "[주문취소]", "[결제취소]", "[구매취소]", "[반품완료]")
        for r, rec in enumerate(items):
            raw_amt = int(getattr(rec, "amount", 0) or 0)
            title_text = (getattr(rec, "title", "") or "").strip()
            is_cancel_item = any(tok in title_text for tok in cancel_tokens_disp)
            if is_cancel_item:
                ramt = -abs(raw_amt)
            else:
                ramt = raw_amt
            cancelled = ramt < 0
            d_item = QTableWidgetItem(getattr(rec, "order_date", "") or "")
            o_item = QTableWidgetItem(getattr(rec, "order_no", "") or "")
            t_item = QTableWidgetItem(title_text)
            t_item.setToolTip(getattr(rec, "title", "") or "")
            a_item = QTableWidgetItem(_fmt_money(ramt))
            a_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            af = QFont(); af.setBold(True); a_item.setFont(af)
            if cancelled:
                a_item.setForeground(QBrush(QColor("#ef4444")))
                t_item.setForeground(QBrush(QColor("#ef4444")))
                if is_cancel_item:
                    a_item.setToolTip("취소완료 품목 — 카드청구액 합계 계산에서 제외(상쇄)")
            items_table.setItem(r, 0, d_item)
            items_table.setItem(r, 1, o_item)
            items_table.setItem(r, 2, t_item)
            items_table.setItem(r, 3, a_item)
        if not items:
            items_table.setRowCount(1)
            ph = QTableWidgetItem("(상세 품목 데이터가 없습니다 — 구매내역 동기화 필요)")
            ph.setForeground(QBrush(QColor("#94a3b8")))
            items_table.setSpan(0, 0, 1, 4)
            items_table.setItem(0, 0, ph)
        layout.addWidget(items_table, 1)

        btn_row = QDialogButtonBox(QDialogButtonBox.Close)
        btn_row.rejected.connect(self.reject)
        btn_row.accepted.connect(self.accept)
        layout.addWidget(btn_row)


class _GaugeBar(QWidget):
    """카테고리 분할 가로 게이지 바.

    segments: [(color_hex, weight), ...]
    percent_of_max: 0.0 ~ 1.0  → 가로 길이 비율
    """

    def __init__(self, segments: List[tuple[str, int]], percent_of_max: float) -> None:
        super().__init__()
        self._segments = segments
        self._pct = max(0.0, min(1.0, percent_of_max))
        self.setFixedHeight(6)
        self.setMinimumWidth(40)

    def paintEvent(self, _e) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        h = self.height()
        # 배경
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#e2e8f0"))
        p.drawRoundedRect(0, 0, w, h, h / 2, h / 2)
        if self._pct <= 0 or not self._segments:
            return
        bar_w = int(w * self._pct)
        if bar_w <= 0:
            return
        total = sum(max(0, s[1]) for s in self._segments) or 1
        x = 0
        for color, weight in self._segments:
            seg_w = int(bar_w * (max(0, weight) / total))
            if seg_w <= 0:
                continue
            p.setBrush(QColor(color))
            p.drawRect(x, 0, seg_w, h)
            x += seg_w
        # 라운딩 오버레이 — 단순화: 전체 바를 마스크 처리
        # (간단히 좌우 끝만 둥글게)


class _CalendarDayCell(QFrame):
    """캘린더 한 칸 (일자, 총액, 게이지, 퍼센트). 클릭 시 day_clicked(day) emit."""

    day_clicked = Signal(int)

    def __init__(
        self,
        day: int,
        amount: int,
        segments: List[tuple[str, int]],
        percent: float,
        is_placeholder: bool = False,
    ) -> None:
        super().__init__()
        self._day = day
        self._is_placeholder = is_placeholder
        self.setObjectName("calCell")
        if is_placeholder:
            self.setStyleSheet(
                "#calCell { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; }"
            )
        else:
            self.setStyleSheet(
                "#calCell { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; }"
                "#calCell:hover { background: #f8fafc; border-color: #94a3b8; }"
            )
            self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(110)
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(4)

        day_label = QLabel(f"{day}일")
        df = QFont(); df.setBold(True); df.setPointSize(10)
        day_label.setFont(df)
        day_label.setStyleSheet("color: #0f172a;" if not is_placeholder else "color: #cbd5e1;")
        v.addWidget(day_label)

        if is_placeholder or amount <= 0:
            empty = QLabel("-")
            empty.setStyleSheet("color: #cbd5e1; font-size: 11px;")
            v.addWidget(empty)
            v.addStretch(1)
            gauge = _GaugeBar([], 0.0)
            v.addWidget(gauge)
            pct = QLabel("0%")
            pct.setAlignment(Qt.AlignRight)
            pct.setStyleSheet("color: #cbd5e1; font-size: 10px;")
            v.addWidget(pct)
            return

        amt_label = QLabel(f"{amount:,}원")
        amt_label.setStyleSheet("color: #0f172a; font-size: 11px;")
        v.addWidget(amt_label)
        v.addStretch(1)
        gauge = _GaugeBar(segments, percent)
        v.addWidget(gauge)
        pct = QLabel(f"{int(round(percent * 100))}%")
        pct.setAlignment(Qt.AlignRight)
        pct.setStyleSheet("color: #94a3b8; font-size: 10px;")
        v.addWidget(pct)

    def mousePressEvent(self, event):  # type: ignore[override]
        if not self._is_placeholder and event.button() == Qt.LeftButton and self._day > 0:
            self.day_clicked.emit(int(self._day))
            event.accept()
            return
        super().mousePressEvent(event)


class _BgPaintingDelegate(QStyledItemDelegate):
    """item.setBackground() 가 QSS QTableWidget::item 에 의해 무시되는 문제 우회.

    paint 단계에서 직접 배경을 칠한다.
    - reviewed 행: 항상 연한 녹색 페인트 (선택 상태와 무관)
    - 선택된 행: 위에 반투명 darken 오버레이로 구분 (QSS :selected bg 는 사용 안 함)
    """

    def paint(self, painter, option, index):  # type: ignore[override]
        bg = index.data(Qt.BackgroundRole)
        brush: Optional[QBrush] = None
        if isinstance(bg, QBrush) and bg.style() != Qt.NoBrush:
            brush = bg
        elif isinstance(bg, QColor) and bg.isValid():
            brush = QBrush(bg)
        if brush is not None:
            painter.save()
            painter.fillRect(option.rect, brush)
            painter.restore()
        # super 의 selection / focus bg QSS 페인팅 비활성화 — 우리가 직접 오버레이로 처리
        selected = bool(option.state & QStyle.State_Selected)
        from PySide6.QtWidgets import QStyleOptionViewItem
        opt = QStyleOptionViewItem(option)
        opt.state = option.state & ~QStyle.State_Selected & ~QStyle.State_HasFocus
        super().paint(painter, opt, index)
        if selected:
            painter.save()
            painter.fillRect(option.rect, QColor(15, 23, 42, 28))  # 약 11% darken
            painter.restore()


class _MonthYearPicker(QWidget):
    """연도/월 분리 콤보박스 — QDateEdit 호환 API (date(), setDate(), dateChanged) 제공.

    side="start" 면 1일, "end" 면 말일을 반환하는 QDate 를 만든다.
    """

    dateChanged = Signal(QDate)

    def __init__(self, *, side: str, year: int, month: int) -> None:
        super().__init__()
        self._side = side
        self._suspend = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.year_combo = QComboBox()
        # ±5 년 범위 제공 (필요시 사용자가 조회로 갱신해도 무방)
        cur_year = date.today().year
        for y in range(cur_year - 6, cur_year + 2):
            self.year_combo.addItem(f"{y}년", y)
        self.month_combo = QComboBox()
        for m in range(1, 13):
            self.month_combo.addItem(f"{m:02d}월", m)

        self._set_combo(self.year_combo, year)
        self._set_combo(self.month_combo, month)

        self.year_combo.currentIndexChanged.connect(self._emit_changed)
        self.month_combo.currentIndexChanged.connect(self._emit_changed)

        layout.addWidget(self.year_combo)
        layout.addWidget(self.month_combo)

    @staticmethod
    def _set_combo(combo: QComboBox, value: int) -> None:
        idx = combo.findData(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _emit_changed(self, *_: object) -> None:
        if self._suspend:
            return
        self.dateChanged.emit(self.date())

    def date(self) -> QDate:
        y = int(self.year_combo.currentData() or date.today().year)
        m = int(self.month_combo.currentData() or 1)
        if self._side == "start":
            return QDate(y, m, 1)
        last_day = calendar.monthrange(y, m)[1]
        return QDate(y, m, last_day)

    def setDate(self, qd: QDate) -> None:
        self._suspend = True
        try:
            self._set_combo(self.year_combo, qd.year())
            self._set_combo(self.month_combo, qd.month())
        finally:
            self._suspend = False


# ---------------------------------------------------------------------------
# 메인 탭
# ---------------------------------------------------------------------------


class CardUsageTab(QWidget):
    _CAT_ORDER_PREF_KEY = "card_usage.category_order"
    _CAT_CUSTOM_PREF_KEY = "card_usage.category_customizations"

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._config = config
        self.client = BarobillCardClient.from_app_config(config)
        self._all_items: List[CardUsage] = []
        self._displayed_items: List[CardUsage] = []
        self._categories_index: Dict[str, str] = {}  # use_key → category code
        self._selected_category: Optional[str] = None
        self._last_synced_at: Optional[datetime] = None
        self._view_mode: str = "table"   # "table" | "calendar"
        self._suspend_memo_signal: bool = False
        self._sort_col: int = 0   # 기본: 날짜
        self._sort_asc: bool = False   # 기본: 내림차순
        self._review_mode: bool = False
        self._reviewed_keys: set[str] = set()  # 메모리상 검토 완료 마킹
        self._excluded_keys: set[str] = self._load_excluded_keys()  # 사용자 수동 제외 (집계 제외)
        self._coupang_match_index: Dict[str, PurchaseGroup] = {}  # use_key/id → matched group
        # 캘린더 셀 클릭 → 테이블에서 해당 일자 단일 필터
        self._jump_to_date: Optional[date] = None
        # 캘린더 모드에서 보고 있는 월 (period 필터와 독립; 네비게이션 버튼으로 변경)
        self._cal_view_year: Optional[int] = None
        self._cal_view_month: Optional[int] = None
        # Pi 데이터 API: 카드내역과 구매내역 모두 라즈베리에 저장
        self.pi = PiDataClient(
            getattr(config, "monitor_url", None),
            gist_raw_url=getattr(config, "monitor_url_gist", "") or "",
        )
        self._category_customizations: Dict[str, Dict[str, str]] = self._load_category_customizations()
        self._category_order: List[str] = self._load_category_order()
        self._category_shortcuts: List[QShortcut] = []

        # ===== 헤더 =====
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(8)
        title = QLabel("카드사용내역")
        tf = QFont(); tf.setBold(True); tf.setPointSize(15)
        title.setFont(tf)
        self.last_sync_label = QLabel("· 동기화: -")
        self.last_sync_label.setStyleSheet("color: #94a3b8; font-size: 11px;")
        header.addWidget(title)
        header.addWidget(self.last_sync_label)
        header.addStretch(1)

        self.refresh_chk = QCheckBox("즉시 갱신")
        self.refresh_chk.setToolTip("바로빌이 카드사로부터 새로 받아오도록 강제")

        self.sync_btn = QPushButton("🔄 바로빌")
        self.sync_btn.setStyleSheet(
            "QPushButton { background: #0f172a; color: #ffffff; border: none; "
            "border-radius: 6px; padding: 5px 12px; font-weight: 600; font-size: 12px; }"
            "QPushButton:hover { background: #1e293b; }"
            "QPushButton:disabled { background: #cbd5e1; }"
        )
        self.sync_btn.clicked.connect(self._on_sync)

        self.match_btn = QPushButton("🔗 쿠팡 매칭")
        self.match_btn.setStyleSheet(
            "QPushButton { background: #ef4444; color: #ffffff; border: none; "
            "border-radius: 6px; padding: 5px 12px; font-weight: 600; font-size: 12px; }"
            "QPushButton:hover { background: #dc2626; }"
        )
        self.match_btn.clicked.connect(self._on_match_coupang)

        header.addWidget(self.refresh_chk)
        header.addWidget(self.sync_btn)
        header.addWidget(self.match_btn)

        layout.addLayout(header)

        # ===== 고정비 서비스 (요약카드 합계 계산에 필요해 미리 생성) =====
        self._fixed_service = _FixedCostsService()

        # ===== 통계 카드 =====
        cards_row = QHBoxLayout()
        cards_row.setSpacing(8)
        self.card_total = _SummaryCard("이번 달 총 지출")
        self.card_count = _SummaryCard("거래 건수")
        self.card_avg = _SummaryCard("일평균 지출")
        # 손익분기 매출 — 마진율 40% 가정. (총지출 + 고정비) / 0.4
        self.card_breakeven = _SummaryCard("손익분기 매출 (마진 40%)")
        for c in (self.card_total, self.card_count, self.card_avg, self.card_breakeven):
            c.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            c.setFixedHeight(40)
            cards_row.addWidget(c, 1)
        layout.addLayout(cards_row)

        # ===== 상태 배너 (에러/경고/진행중일 때만 표시) =====
        self.status_banner = QLabel("")
        self.status_banner.setStyleSheet(
            "QLabel { background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 6px; "
            "padding: 6px 10px; color: #166534; font-size: 11px; }"
        )
        self.status_banner.setMinimumHeight(28)
        self.status_banner.setVisible(False)
        layout.addWidget(self.status_banner)

        # ===== 카테고리 칩 (가로 공간 부족 시 자동 줄바꿈) =====
        self.category_chips: Dict[str, _CategoryChip] = {}
        self.category_chip_wrap = _FlowWrap(h_spacing=8, v_spacing=6)
        for meta in self._ordered_category_metas():
            chip = _CategoryChip(meta.code, meta.label, meta.emoji, meta.bg_color)
            chip.clicked.connect(lambda _checked, c=meta.code: self._on_chip_clicked(c))
            chip.moved.connect(self._on_category_chip_moved)
            chip.edit_requested.connect(self._edit_category)
            self.category_chips[meta.code] = chip
            self.category_chip_wrap.add_widget(chip)
        self.clear_filter_btn = QPushButton("✕ 필터 해제")
        self.clear_filter_btn.setCursor(Qt.PointingHandCursor)
        self.clear_filter_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; color: #64748b; "
            "padding: 6px 8px; font-size: 12px; }"
            "QPushButton:hover { color: #0f172a; }"
        )
        self.clear_filter_btn.clicked.connect(self._on_clear_filter)
        self.category_chip_wrap.add_widget(self.clear_filter_btn)
        layout.addWidget(self.category_chip_wrap)
        self._refresh_category_shortcut_labels()
        self._install_category_shortcuts()

        # ===== 필터 영역 =====
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        start_d, end_d = self._this_month_range()
        # 월 단위 선택 — 연도/월 분리 콤보박스 (start = 1일, end = 말일 반환)
        self.start_edit = _MonthYearPicker(
            side="start", year=start_d.year, month=start_d.month
        )
        self.end_edit = _MonthYearPicker(
            side="end", year=end_d.year, month=end_d.month
        )

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

        # 기간 변경 → 자동 조회 (연/월 콤보 둘 다 바뀔 수 있어 250ms debounce)
        self._range_debounce = QTimer(self)
        self._range_debounce.setSingleShot(True)
        self._range_debounce.setInterval(250)
        self._range_debounce.timeout.connect(self._on_refresh)
        self.start_edit.dateChanged.connect(lambda _d: self._range_debounce.start())
        self.end_edit.dateChanged.connect(lambda _d: self._range_debounce.start())

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

        # 뷰 모드 토글 (테이블 / 카드 / 캘린더)
        self._view_btns: Dict[str, QPushButton] = {}
        view_group = QFrame()
        view_group.setStyleSheet(
            "QFrame { background: #f1f5f9; border: 1px solid #e2e8f0; border-radius: 8px; }"
        )
        vg_layout = QHBoxLayout(view_group)
        vg_layout.setContentsMargins(2, 2, 2, 2)
        vg_layout.setSpacing(0)
        for code, label in (("table", "테이블"), ("calendar", "캘린더"), ("fixed", "고정비")):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _ck=False, c=code: self._on_view_mode(c))
            self._view_btns[code] = btn
            vg_layout.addWidget(btn)
        self._view_btns[self._view_mode].setChecked(True)
        self._refresh_view_mode_styles()

        # 리뷰 모드 토글 — ON 일 때 행을 더블클릭하면 검토 완료(녹색)로 표시
        self.review_btn = QPushButton("리뷰 모드 OFF")
        self.review_btn.setCheckable(True)
        self.review_btn.setCursor(Qt.PointingHandCursor)
        self.review_btn.setToolTip(
            "리뷰 모드 ON: 행을 더블클릭하면 검토 완료(녹색)로 표시됩니다."
        )
        self.review_btn.clicked.connect(self._toggle_review_mode)
        self._refresh_review_btn_style()

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
        list_header.addWidget(view_group)
        list_header.addWidget(self.review_btn)
        list_header.addWidget(self.sort_btn)
        layout.addLayout(list_header)

        # ===== 본문 (Stacked: 테이블 / 캘린더) =====
        self.body_stack = QStackedWidget()

        # --- 테이블 뷰 ---
        # 컬럼 순서: 날짜, 카테고리, 가맹점, 금액, 카드, 검토, 쿠팡매칭, 메모
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(["날짜", "카테고리", "가맹점", "금액", "카드", "검토", "쿠팡매칭", "메모"])
        # 더블클릭은 매칭 상세 팝업/리뷰 토글에 사용 → 편집은 Enter/F2 로만 시작
        self.table.setEditTriggers(QAbstractItemView.EditKeyPressed)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setTabKeyNavigation(False)
        self.table.installEventFilter(self)
        # alternating row colors 는 per-item setBackground (검토 완료 녹색)을 덮어쓸 수 있어 비활성화
        self.table.setAlternatingRowColors(False)
        # QSS 가 item 배경을 무시하는 케이스 대응 — delegate 로 명시적 페인팅
        self._bg_delegate = _BgPaintingDelegate(self.table)
        self.table.setItemDelegate(self._bg_delegate)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setStyleSheet(
            "QTableWidget { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px;"
            " gridline-color: #eef2f7; outline: 0; }"
            "QHeaderView::section { background: #f8fafc; border: none;"
            " border-right: 1px solid #eef2f7;"
            " border-bottom: 1px solid #e2e8f0; padding: 8px; font-weight: 600; color: #475569; }"
            "QTableWidget::item { padding: 6px 8px; border: none;"
            " border-right: 1px solid #f1f5f9; }"
            "QTableWidget::item:selected { background: #f1f5f9; color: #0f172a; }"
            "QTableWidget::item:focus { background: #f1f5f9; color: #0f172a; }"
        )
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)  # 날짜
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)  # 카테고리
        h.setSectionResizeMode(2, QHeaderView.Stretch)            # 가맹점
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)  # 금액
        h.setSectionResizeMode(4, QHeaderView.ResizeToContents)  # 카드
        h.setSectionResizeMode(5, QHeaderView.Fixed)              # 검토
        self.table.setColumnWidth(5, 56)
        h.setSectionResizeMode(6, QHeaderView.ResizeToContents)  # 쿠팡매칭
        h.setSectionResizeMode(7, QHeaderView.Stretch)            # 메모
        h.setSectionsClickable(True)
        h.setSortIndicatorShown(True)
        h.setSortIndicator(self._sort_col, Qt.AscendingOrder if self._sort_asc else Qt.DescendingOrder)
        h.sectionClicked.connect(self._on_header_clicked)
        self.table.cellDoubleClicked.connect(self._on_table_double_clicked)
        self.table.cellClicked.connect(self._on_table_cell_clicked)
        self.table.cellChanged.connect(self._on_table_cell_changed)
        # 우클릭 컨텍스트 메뉴 (제외하기 등)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)
        # 금액 컬럼 호버 커서 변경
        self.table.viewport().setMouseTracking(True)
        self.table.viewport().installEventFilter(self)
        self.body_stack.addWidget(self.table)  # idx 0 = table

        # --- 캘린더 뷰 ---
        cal_scroll = QScrollArea()
        cal_scroll.setWidgetResizable(True)
        cal_scroll.setStyleSheet("QScrollArea { border: none; background: #f8fafc; }")
        self._cal_inner = QWidget()
        self._cal_inner_layout = QVBoxLayout(self._cal_inner)
        self._cal_inner_layout.setContentsMargins(8, 8, 8, 8)
        self._cal_inner_layout.setSpacing(8)
        cal_scroll.setWidget(self._cal_inner)
        self.body_stack.addWidget(cal_scroll)  # idx 1 = calendar

        # --- 고정비 뷰 ---
        self.fixed_view = _FixedCostsTable(self._fixed_service)
        self.fixed_view.changed.connect(self._on_fixed_costs_changed)
        self.body_stack.addWidget(self.fixed_view)  # idx 2 = fixed

        # 초기 페이지: 테이블
        _initial_idx = {"table": 0, "calendar": 1, "fixed": 2}.get(self._view_mode, 0)
        self.body_stack.setCurrentIndex(_initial_idx)
        layout.addWidget(self.body_stack, 1)

        if not self.client.is_configured():
            miss = ", ".join(self.client.config.missing_fields())
            self.status_banner.setText(
                f"⚠ 바로빌 설정 누락: {miss}. credentials.json 의 barobill 섹션을 확인하세요."
            )
            self.status_banner.setStyleSheet(
                "QLabel { background: #fef3c7; border: 1px solid #fde68a; border-radius: 6px; "
                "padding: 6px 10px; color: #92400e; font-size: 11px; }"
            )
            self.status_banner.setVisible(True)

        # 시작 시 Pi 캐시에서 즉시 로드 — UI 가 그려진 직후 1회 (창 띄우기 안 늦게)
        QTimer.singleShot(0, self._load_from_pi_cache)

    # ----- helpers -----

    @staticmethod
    def _default_category_order() -> List[str]:
        return [meta.code for meta in DEFAULT_CATEGORIES]

    @staticmethod
    def _local_ui_prefs_path() -> Path:
        return Path.home() / ".smartinventory" / "ui_prefs.json"

    def _read_local_ui_prefs(self) -> Dict[str, Any]:
        path = self._local_ui_prefs_path()
        try:
            if not path.exists():
                return {}
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _write_local_ui_pref(self, key: str, value: Any) -> None:
        path = self._local_ui_prefs_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = self._read_local_ui_prefs()
            data[key] = value
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def _load_category_customizations(self) -> Dict[str, Dict[str, str]]:
        raw: Any = None
        try:
            raw = self.pi.get_ui_pref(self._CAT_CUSTOM_PREF_KEY)
        except Exception:  # noqa: BLE001
            raw = None
        if not isinstance(raw, dict):
            raw = self._read_local_ui_prefs().get(self._CAT_CUSTOM_PREF_KEY)
        if not isinstance(raw, dict):
            return {}
        valid = set(self._default_category_order())
        result: Dict[str, Dict[str, str]] = {}
        for code, cfg in raw.items():
            if str(code) not in valid or not isinstance(cfg, dict):
                continue
            label = str(cfg.get("label") or "").strip()
            emoji = str(cfg.get("emoji") or "").strip()
            entry: Dict[str, str] = {}
            if label:
                entry["label"] = label[:24]
            if emoji:
                entry["emoji"] = emoji[:8]
            if entry:
                result[str(code)] = entry
        return result

    def _save_category_customizations(self) -> None:
        payload = {code: dict(cfg) for code, cfg in self._category_customizations.items() if cfg}
        self._write_local_ui_pref(self._CAT_CUSTOM_PREF_KEY, payload)
        try:
            self.pi.set_ui_pref(self._CAT_CUSTOM_PREF_KEY, payload)
        except Exception:  # noqa: BLE001
            pass

    def _category_meta(self, code: str) -> CategoryMeta:
        base = category_meta(code)
        custom = self._category_customizations.get(base.code, {})
        return CategoryMeta(
            base.code,
            custom.get("label") or base.label,
            custom.get("emoji") or base.emoji,
            base.bg_color,
        )

    def _load_category_order(self) -> List[str]:
        default = self._default_category_order()
        order: list[str] = []
        try:
            remote = self.pi.get_ui_pref(self._CAT_ORDER_PREF_KEY)
            if isinstance(remote, list):
                order = [str(x) for x in remote]
        except Exception:  # noqa: BLE001
            order = []
        if not order:
            local = self._read_local_ui_prefs().get(self._CAT_ORDER_PREF_KEY)
            if isinstance(local, list):
                order = [str(x) for x in local]
        valid = set(default)
        normalized = [code for code in order if code in valid]
        for code in default:
            if code not in normalized:
                normalized.append(code)
        return normalized

    def _ordered_category_metas(self):
        valid = {meta.code for meta in DEFAULT_CATEGORIES}
        return [self._category_meta(code) for code in self._category_order if code in valid]

    def _save_category_order(self) -> None:
        self._write_local_ui_pref(self._CAT_ORDER_PREF_KEY, list(self._category_order))
        try:
            self.pi.set_ui_pref(self._CAT_ORDER_PREF_KEY, list(self._category_order))
        except Exception:  # noqa: BLE001
            pass

    def _apply_category_chip_order(self) -> None:
        widgets: list[QWidget] = [
            self.category_chips[code]
            for code in self._category_order
            if code in self.category_chips
        ]
        widgets.append(self.clear_filter_btn)
        self.category_chip_wrap.set_order(widgets)
        self._refresh_category_shortcut_labels()

    def _refresh_category_shortcut_labels(self) -> None:
        sums: Dict[str, int] = {}
        for it in self._all_items:
            if self._is_excluded(it):
                continue
            cat = self._categories_index.get(it.use_key or it.id or "", "OTHER")
            sums[cat] = sums.get(cat, 0) + max(0, int(it.amount or 0))
        for idx, code in enumerate(self._category_order):
            chip = self.category_chips.get(code)
            if chip is None:
                continue
            meta = self._category_meta(code)
            chip.set_display(meta.label, meta.emoji, meta.bg_color)
            chip.set_shortcut_label(f"F{idx + 1}" if idx < 12 else "")
            chip.set_amount(sums.get(code, 0))

    def _install_category_shortcuts(self) -> None:
        for idx in range(12):
            shortcut = QShortcut(QKeySequence(f"F{idx + 1}"), self)
            shortcut.activated.connect(lambda i=idx: self._on_category_shortcut(i))
            self._category_shortcuts.append(shortcut)

    def _selected_usage_for_shortcut(self) -> Optional[CardUsage]:
        row = self.table.currentRow()
        if 0 <= row < len(self._displayed_items):
            return self._displayed_items[row]
        return None

    def _on_category_shortcut(self, index: int) -> None:
        if index < 0 or index >= len(self._category_order):
            return
        focus = QApplication.focusWidget()
        if self.table.state() == QAbstractItemView.EditingState or isinstance(focus, (QLineEdit, QComboBox)):
            return
        code = self._category_order[index]
        usage = self._selected_usage_for_shortcut()
        if usage is not None and self.body_stack.currentIndex() == 0:
            self._change_category(usage, code)
            meta = self._category_meta(code)
            self.status_banner.setText(f"{meta.emoji} {meta.label} 카테고리로 변경했습니다. ({self._shortcut_name(index)})")
            self.status_banner.setStyleSheet(
                "QLabel { background: #ecfdf5; border: 1px solid #bbf7d0; border-radius: 6px; "
                "padding: 6px 10px; color: #047857; font-size: 11px; }"
            )
            self.status_banner.setVisible(True)
            return
        self._select_category_filter(code)

    @staticmethod
    def _shortcut_name(index: int) -> str:
        return f"F{index + 1}"

    def _on_category_chip_moved(self, source_code: str, target_code: str) -> None:
        if source_code not in self._category_order or target_code not in self._category_order:
            return
        self._category_order.remove(source_code)
        target_idx = self._category_order.index(target_code)
        self._category_order.insert(target_idx, source_code)
        self._apply_category_chip_order()
        self._save_category_order()
        self._refresh_chip_amounts()

    def _edit_category(self, code: str) -> None:
        meta = self._category_meta(code)
        dialog = _CategoryEditDialog(meta, self)
        if dialog.exec() != QDialog.Accepted:
            return
        # 삭제 요청 처리
        if dialog.is_delete_requested():
            self._delete_category(code)
            return
        label, emoji = dialog.values()
        if not label:
            QMessageBox.warning(self, "카테고리명 필요", "카테고리명을 입력하세요.")
            return
        base = category_meta(code)
        custom: Dict[str, str] = {}
        if label != base.label:
            custom["label"] = label
        if emoji != base.emoji:
            custom["emoji"] = emoji
        if custom:
            self._category_customizations[code] = custom
        else:
            self._category_customizations.pop(code, None)
        self._save_category_customizations()
        self._refresh_chip_amounts()
        self._render_list()
        self.status_banner.setText(f"{emoji} {label} 카테고리 이름/아이콘을 저장했습니다.")
        self.status_banner.setStyleSheet(
            "QLabel { background: #ecfdf5; border: 1px solid #bbf7d0; border-radius: 6px; "
            "padding: 6px 10px; color: #047857; font-size: 11px; }"
        )
        self.status_banner.setVisible(True)

    def _delete_category(self, code: str) -> None:
        """카테고리를 칩 목록에서 제거 + 해당 카테고리의 항목을 'OTHER' 로 재할당."""
        if code == "OTHER":
            QMessageBox.information(self, "삭제 불가", "기타(OTHER) 카테고리는 삭제할 수 없습니다.")
            return
        # 1) 순서 목록에서 제거
        if code in self._category_order:
            self._category_order = [c for c in self._category_order if c != code]
            self._save_category_order()
        # 2) 커스텀(이름/아이콘) 정리
        self._category_customizations.pop(code, None)
        self._save_category_customizations()
        # 3) 해당 카테고리의 in-memory 항목들 OTHER 로 이동 + Pi 동기화
        affected_keys = [k for k, v in self._categories_index.items() if v == code]
        for k in affected_keys:
            self._categories_index[k] = "OTHER"
        # in-memory CardUsage 객체도 동기화 + Pi 저장
        for it in self._all_items:
            ukey = it.use_key or it.id or ""
            if ukey in affected_keys:
                it.category = "OTHER"
                self._save_usage_change(it, category="OTHER")
        # 4) UI 칩 위젯 제거 (기존 위젯이 떠있어서 즉시 사라지게)
        chip = self.category_chips.pop(code, None)
        if chip is not None:
            try:
                self.category_chip_wrap.remove_widget(chip)
            except Exception:  # noqa: BLE001
                chip.setParent(None)
                chip.deleteLater()
        self._install_category_shortcuts()
        self._refresh_chip_amounts()
        self._render_list()
        self._refresh_summary_cards()
        self.status_banner.setText(
            f"🗑 '{code}' 카테고리를 제거했습니다 — {len(affected_keys)}건이 '기타' 로 이동"
        )
        self.status_banner.setStyleSheet(
            "QLabel { background: #fef9c3; border: 1px solid #fde68a; border-radius: 6px; "
            "padding: 6px 10px; color: #854d0e; font-size: 11px; }"
        )
        self.status_banner.setVisible(True)

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
        """쿠팡 매칭 (UI 버튼) — 결과 다이얼로그 표시."""
        result = self._run_coupang_match(silent=False)
        if result is None:
            return
        matched, candidates, ambiguous, source_msg = result
        QMessageBox.information(
            self, "쿠팡 매칭 결과",
            f"쿠팡 카드결제 {candidates}건 중 {matched}건 매칭"
            f"{f' (모호 {ambiguous}건은 가까운 날짜 우선)' if ambiguous else ''}.\n"
            f"매칭 소스: {source_msg}",
        )

    def _run_coupang_match(self, *, silent: bool) -> Optional[tuple[int, int, int, str]]:
        """쿠팡 주문 단위 매칭 실행. 반환: (matched, candidates, ambiguous, source_msg) 또는 None.

        silent=True 면 다이얼로그/경고 없이 조용히 동작 (자동 매칭 용).
        """
        if not self._all_items:
            if not silent:
                QMessageBox.information(self, "쿠팡 매칭", "먼저 바로빌 동기화로 카드내역을 불러오세요.")
            return None

        # ── 1순위: purchase_orders (Pi 우선, 로컬 fallback) ──
        orders: List[PurchaseOrder] = []
        if self.pi.is_configured:
            try:
                orders = self.pi.list_purchase_orders(channel="coupang", limit=5000)
            except Exception:  # noqa: BLE001
                orders = []
        if not orders:
            try:
                store = PurchaseHistoryStore()
                orders = store.load_orders(channel="coupang", limit=5000)
            except Exception:  # noqa: BLE001
                orders = []
        # payment_total 이 없는 주문은 매칭 불가능 → 제외
        orders = [o for o in orders if o.payment_total is not None and o.payment_total > 0]

        groups: List[PurchaseGroup] = []
        # 주문별 후보 금액 매핑 — 매칭 시 card_amount 와 payment_total 둘 다 시도해서
        # 어느 한쪽이든 카드내역과 일치하면 매칭. 과거 cash_used 오집계로 card_amount
        # 가 잘못 저장된 주문도 payment_total 로 매칭됨.
        amounts_by_group_key: Dict[str, set[int]] = {}
        if orders:
            for o in orders:
                cash_used = int(o.cash_used or 0)
                # 100원 미만 cash_used 는 과거 파서 false positive — 무시
                if 0 < cash_used < 100:
                    cash_used = 0
                card_amt = o.card_amount if o.card_amount is not None else o.payment_total
                if card_amt is None or card_amt <= 0:
                    continue
                title_extra = ""
                if cash_used > 0:
                    title_extra = f" · 캐시 {cash_used:,}원"
                title = (
                    f"주문 {o.order_no}"
                    + (f" · {o.item_count}건" if o.item_count else "")
                    + title_extra
                    + (f" [{o.status}]" if o.status else "")
                )
                gk = f"coupang|order|{o.order_no}"
                # 후보 금액 — 표시는 card_amt 로, 매칭은 둘 다 시도
                cand: set[int] = {int(card_amt)}
                if o.payment_total and int(o.payment_total) > 0:
                    cand.add(int(o.payment_total))
                amounts_by_group_key[gk] = cand
                groups.append(PurchaseGroup(
                    channel=o.channel,
                    order_date=o.order_date,
                    title=title,
                    total_amount=int(card_amt),
                    item_count=o.item_count,
                    items=[],
                    group_key=gk,
                ))
            source_msg = f"주문 {len(orders)}개 (카드청구액/총결제 둘 다 시도)"
        else:
            # ── 2순위 fallback: 품목 합산 ──
            recs: List = []
            if self.pi.is_configured:
                try:
                    recs = self.pi.list_purchase_records(channel="coupang", limit=5000)
                except Exception:  # noqa: BLE001
                    recs = []
            if not recs:
                try:
                    store = PurchaseHistoryStore()
                    recs = store.load_records(channel="coupang", limit=2000)
                except Exception as exc:  # noqa: BLE001
                    if not silent:
                        QMessageBox.warning(self, "쿠팡 매칭", f"쿠팡 구매내역 로드 실패: {exc}")
                    return None
            if not recs:
                if not silent:
                    QMessageBox.information(
                        self, "쿠팡 매칭",
                        "DB 에 쿠팡 구매내역이 없습니다. 구매내역 탭에서 먼저 동기화하세요.",
                    )
                return None
            groups = group_records_by_order(recs)
            source_msg = f"품목 합산 그룹 {len(groups)}개 (구매내역 {len(recs)}건, ⚠ 배송비/할인 미반영)"
        # group_date(date) → list[PurchaseGroup]
        by_date: Dict[date, List[PurchaseGroup]] = {}
        for g in groups:
            try:
                gd = datetime.strptime(g.order_date or "", "%Y-%m-%d").date()
            except Exception:  # noqa: BLE001
                continue
            by_date.setdefault(gd, []).append(g)

        # 카드 매칭: 가맹점에 쿠팡/coupang 포함 + 양수 금액
        used_groups: set[str] = set()
        matched = 0
        ambiguous = 0
        candidates = 0
        new_index: Dict[str, PurchaseGroup] = {}
        for usage in self._all_items:
            store_l = (usage.store_name or "").lower()
            amt = int(usage.amount or 0)
            if amt <= 0:
                continue
            if "쿠팡" not in (usage.store_name or "") and "coupang" not in store_l:
                continue
            candidates += 1
            udt = _parse_used_at(usage.used_at)
            if udt is None:
                continue
            ud = udt.date()
            # ±3일 내 같은 금액 그룹 찾기.
            # purchase_orders 경로: card_amount 와 payment_total 둘 다 시도 (cash_used
            # 오집계 대비). 품목 fallback 경로: total_amount 만 비교.
            window: List[PurchaseGroup] = []
            for delta in range(-3, 4):
                d = ud + timedelta(days=delta)
                for g in by_date.get(d, []):
                    if g.group_key in used_groups:
                        continue
                    cand = amounts_by_group_key.get(g.group_key)
                    if cand is not None:
                        if amt in cand:
                            window.append(g)
                    elif g.total_amount == amt:
                        window.append(g)
            if not window:
                continue
            if len(window) > 1:
                # 가장 가까운 날짜 1개 선택
                window.sort(key=lambda g: abs(
                    (datetime.strptime(g.order_date or "1900-01-01", "%Y-%m-%d").date() - ud).days
                ))
                ambiguous += 1
            chosen = window[0]
            used_groups.add(chosen.group_key)
            key = usage.use_key or usage.id or ""
            if key:
                new_index[key] = chosen
            usage.coupang_purchase_id = chosen.group_key
            matched += 1

        self._coupang_match_index.update(new_index)

        # Pi 에도 매칭 결과 저장 (best-effort)
        if self.pi.is_configured and matched > 0:
            try:
                changed_items = [it for it in self._all_items if it.coupang_purchase_id]
                if changed_items:
                    self.pi.upload_card_usages(changed_items)
            except Exception:  # noqa: BLE001
                pass

        # 화면 갱신
        try:
            self._render_list()
        except Exception:  # noqa: BLE001
            pass

        return (matched, candidates, ambiguous, source_msg)

    def _on_chip_clicked(self, code: str) -> None:
        self._select_category_filter(code)

    def _select_category_filter(self, code: str) -> None:
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

    # ----- 뷰 모드 / 리뷰 모드 -----

    def _on_view_mode(self, code: str) -> None:
        if code not in ("table", "calendar", "fixed"):
            return
        self._view_mode = code
        for c, btn in self._view_btns.items():
            btn.setChecked(c == code)
        self._refresh_view_mode_styles()
        idx = {"table": 0, "calendar": 1, "fixed": 2}[code]
        self.body_stack.setCurrentIndex(idx)
        # 캘린더 모드로 돌아가면 점프 필터 해제 (전체 재표시)
        if code == "calendar":
            self._jump_to_date = None
        if code != "fixed":
            self._render_list()

    def _on_fixed_costs_changed(self) -> None:
        """고정비 추가/수정/삭제 → 요약카드 즉시 재계산."""
        try:
            self._refresh_summary_cards()
        except Exception:  # noqa: BLE001
            pass

    def _on_calendar_day_clicked(self, year: int, month: int, day: int) -> None:
        """캘린더 셀 클릭 → 테이블 뷰로 전환 + 해당 일자만 표시."""
        try:
            target = date(year, month, day)
        except Exception:  # noqa: BLE001
            return
        self._jump_to_date = target
        # 테이블 뷰로 전환
        self._view_mode = "table"
        for c, btn in self._view_btns.items():
            btn.setChecked(c == "table")
        self._refresh_view_mode_styles()
        self.body_stack.setCurrentIndex(0)
        self._render_list()
        # 상태 배너로 알림 + 해제 안내
        self.status_banner.setText(
            f"📅 {target.isoformat()} 일자 카드내역만 표시 중 — 다시 캘린더를 누르거나 검색을 비우면 해제"
        )
        self.status_banner.setStyleSheet(
            "background: #e0f2fe; color: #075985; border: 1px solid #7dd3fc;"
            " border-radius: 6px; padding: 6px 10px; font-size: 12px;"
        )
        self.status_banner.setVisible(True)

    def _refresh_view_mode_styles(self) -> None:
        for code, btn in self._view_btns.items():
            if btn.isChecked():
                btn.setStyleSheet(
                    "QPushButton { background: #ffffff; color: #0f172a; border: 1px solid #cbd5e1; "
                    "border-radius: 6px; padding: 4px 12px; font-size: 11px; font-weight: 600; }"
                )
            else:
                btn.setStyleSheet(
                    "QPushButton { background: transparent; color: #64748b; border: none; "
                    "border-radius: 6px; padding: 4px 12px; font-size: 11px; font-weight: 500; }"
                    "QPushButton:hover { color: #0f172a; }"
                )

    def _toggle_review_mode(self) -> None:
        self._review_mode = not self._review_mode
        self.review_btn.setChecked(self._review_mode)
        self._refresh_review_btn_style()
        self._render_list()

    def _refresh_review_btn_style(self) -> None:
        if self._review_mode:
            self.review_btn.setText("리뷰 모드 ON")
            self.review_btn.setStyleSheet(
                "QPushButton { background: #0f172a; color: #ffffff; border: none; "
                "border-radius: 6px; padding: 4px 12px; font-size: 11px; font-weight: 600; }"
            )
        else:
            self.review_btn.setText("리뷰 모드 OFF")
            self.review_btn.setStyleSheet(
                "QPushButton { background: #ffffff; color: #475569; border: 1px solid #e2e8f0; "
                "border-radius: 6px; padding: 4px 12px; font-size: 11px; font-weight: 500; }"
                "QPushButton:hover { border-color: #cbd5e1; }"
            )

    def _is_reviewed(self, usage: CardUsage) -> bool:
        key = usage.use_key or usage.id or ""
        if key and key in self._reviewed_keys:
            return True
        if usage.reviewed:
            return True
        # 메모에 내용이 있으면 자동으로 검토 완료 처리
        if (usage.memo or "").strip():
            return True
        return False

    # ----- 사용자 수동 제외 (집계 제외) -----

    @staticmethod
    def _excluded_keys_path() -> Path:
        from pathlib import Path as _P
        d = _P.home() / ".smartinventory"
        d.mkdir(parents=True, exist_ok=True)
        return d / "card_usage_excluded.json"

    def _load_excluded_keys(self) -> set[str]:
        try:
            import json as _j
            p = self._excluded_keys_path()
            if not p.exists():
                return set()
            data = _j.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return {str(x) for x in data if x}
        except Exception:  # noqa: BLE001
            pass
        return set()

    def _save_excluded_keys(self) -> None:
        try:
            import json as _j
            self._excluded_keys_path().write_text(
                _j.dumps(sorted(self._excluded_keys), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass

    def _is_excluded(self, usage: CardUsage) -> bool:
        key = usage.use_key or usage.id or ""
        return bool(key) and key in self._excluded_keys

    def _set_excluded(self, usage: CardUsage, excluded: bool) -> None:
        key = usage.use_key or usage.id or ""
        if not key:
            return
        if excluded:
            self._excluded_keys.add(key)
        else:
            self._excluded_keys.discard(key)
        self._save_excluded_keys()

    # ----- 데이터 가져오기 -----

    def _load_from_pi_cache(self) -> None:
        """창 시작 시 Pi DB 에서 카드사용내역 즉시 로드 (네트워크 호출 없음).

        - Pi 가 설정돼 있고 데이터가 있으면 그걸로 화면 초기 채움
        - 사용자는 필요할 때 '바로빌 동기화' 로 최신화
        - 실패해도 조용히 무시 (UI 는 빈 상태)
        """
        if not self.pi.is_configured:
            return
        start = self._q_to_iso(self.start_edit.date())
        end = self._q_to_iso(self.end_edit.date())
        try:
            items = self.pi.list_card_usages(
                start_date=start, end_date=end, card_num=None, limit=20000,
            )
        except Exception:  # noqa: BLE001
            return
        if not items:
            return

        # target_cards: 로드된 데이터에서 추출
        target_cards = sorted({(it.card_num or "") for it in items if it.card_num})
        self._refresh_card_combo([c for c in target_cards if c])

        self._categories_index = {
            (it.use_key or it.id or ""): (
                it.category
                or classify_category(
                    it.store_name, (it.raw or {}).get("UseStoreBizType") if it.raw else None
                )
            )
            for it in items
        }
        # Pi 가 가진 reviewed 플래그를 in-memory keyset 에도 동기화
        self._reviewed_keys = {
            (it.use_key or it.id or "") for it in items if bool(getattr(it, "reviewed", False))
        }

        self._all_items = items
        self._last_synced_at = datetime.now()
        self.last_sync_label.setText(
            f"Pi 캐시 로드: {len(items)}건 (마지막 동기화 시점은 '바로빌 동기화' 시 갱신)"
        )
        self.status_banner.setVisible(False)

        try:
            self._refresh_summary_cards()
            self._refresh_chip_amounts()
            self._render_list()
        except Exception:  # noqa: BLE001
            pass

        # Pi 캐시에는 이미 매칭 결과(coupang_purchase_id) 가 들어있을 수 있으므로
        # 여기서는 자동 매칭을 한 번 더 돌려 신규 항목까지 커버 (조용히)
        try:
            self._run_coupang_match(silent=True)
        except Exception:  # noqa: BLE001
            pass

    def _fetch(self) -> None:
        """바로빌 동기화 — 동기 호출 (UI 잠깐 멈춤).

        QThread 워커 + 시그널 dispatch 가 PyInstaller frozen 환경에서 가끔 죽어서
        단순 동기 호출로 변경. 보통 91건 조회에 1-2초.
        """
        start = self._q_to_iso(self.start_edit.date())
        end = self._q_to_iso(self.end_edit.date())
        refresh = self.refresh_chk.isChecked()
        card = self.card_combo.currentData() or None

        self.status_banner.setText(f"🔄 조회 중... ({start} ~ {end})")
        self.status_banner.setStyleSheet(
            "QLabel { background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 6px; "
            "padding: 6px 10px; color: #1e40af; font-size: 11px; }"
        )
        self.status_banner.setVisible(True)
        self._set_busy(True)
        QApplication.processEvents()  # 상태 메시지 즉시 표시

        try:
            data = self.client.fetch_card_usages(
                start_date=start, end_date=end,
                card_num=card, refresh_before_fetch=refresh,
            )
        except BarobillError as exc:
            self._set_busy(False)
            self.refresh_chk.setChecked(False)
            self.status_banner.setText(f"❌ [{exc.code or ''}] {exc}")
            self.status_banner.setStyleSheet(
                "QLabel { background: #fef2f2; border: 1px solid #fecaca; border-radius: 6px; "
                "padding: 6px 10px; color: #991b1b; font-size: 11px; }"
            )
            self.status_banner.setVisible(True)
            return
        except Exception as exc:  # noqa: BLE001
            self._set_busy(False)
            self.refresh_chk.setChecked(False)
            self.status_banner.setText(f"❌ {exc}")
            self.status_banner.setStyleSheet(
                "QLabel { background: #fef2f2; border: 1px solid #fecaca; border-radius: 6px; "
                "padding: 6px 10px; color: #991b1b; font-size: 11px; }"
            )
            self.status_banner.setVisible(True)
            return

        self._set_busy(False)
        self.refresh_chk.setChecked(False)

        logs: List[CardUsage] = data.get("logs") or []
        target_cards: List[str] = data.get("targetCards") or []

        self._refresh_card_combo(target_cards)

        # Pi 업로드 (best-effort): 사용자 편집(메모/카테고리/검토)은 Pi 측 COALESCE 로 보존
        pi_changed = -2  # -2: 미시도, -1: 실패, >=0: 변경 row 수
        if self.pi.is_configured and logs:
            try:
                pi_changed = self.pi.upload_card_usages(logs)
            except (PiDataError, Exception):  # noqa: BLE001
                pi_changed = -1
            if pi_changed >= 0:
                # Pi 의 보존된 메모/검토/매칭값을 머지
                try:
                    remote = self.pi.list_card_usages(
                        start_date=start, end_date=end, card_num=card, limit=20000,
                    )
                    if remote:
                        by_key = {(r.use_key or r.id or ""): r for r in remote}
                        merged: List[CardUsage] = []
                        for it in logs:
                            k = it.use_key or it.id or ""
                            r = by_key.get(k)
                            if r is not None:
                                if it.raw is not None:
                                    r.raw = it.raw
                                merged.append(r)
                            else:
                                merged.append(it)
                        logs = merged
                except Exception:  # noqa: BLE001
                    pass

        # categories_index 는 Pi 머지 이후에 재빌드 — Pi 가 가진 사용자 저장 카테고리(it.category)
        # 가 우선, 없으면 가맹점/업종 기반 자동 분류 fallback. 머지 이전에 빌드하면 사용자
        # 카테고리가 사라지는 버그 발생.
        self._categories_index = {
            (it.use_key or it.id or ""): (
                it.category
                or classify_category(
                    it.store_name, (it.raw or {}).get("UseStoreBizType") if it.raw else None
                )
            )
            for it in logs
        }
        # reviewed_keys 도 Pi 데이터로 동기화
        self._reviewed_keys = {
            (it.use_key or it.id or "") for it in logs if bool(getattr(it, "reviewed", False))
        }

        self._all_items = logs
        self._last_synced_at = datetime.now()
        pi_suffix = ""
        if self.pi.is_configured:
            if pi_changed == -1:
                pi_suffix = " · 라즈베리 통신 실패"
            elif pi_changed >= 0:
                pi_suffix = f" · 라즈베리 동기 {pi_changed}건"
        self.last_sync_label.setText(
            f"최근 동기화: {self._last_synced_at.strftime('%Y. %m. %d. %p %I:%M')}{pi_suffix}"
        )

        # 성공 시 배너 숨김 (요약카드/last_sync_label 로 충분)
        self.status_banner.setVisible(False)

        try:
            self._refresh_summary_cards()
            self._refresh_chip_amounts()
            self._render_list()
        except Exception as exc:  # noqa: BLE001
            import traceback
            self.status_banner.setText(f"❌ 화면 갱신 오류: {exc}")
            print("[CardUsageTab] render error:", traceback.format_exc())

        # 동기화 후 쿠팡 자동 매칭 (조용히) — 다이얼로그 없이 last_sync_label 에 결과만 추가
        try:
            res = self._run_coupang_match(silent=True)
            if res is not None:
                matched, candidates, _amb, _src = res
                cur = self.last_sync_label.text()
                self.last_sync_label.setText(
                    f"{cur} · 쿠팡매칭 {matched}/{candidates}건"
                )
        except Exception:  # noqa: BLE001
            pass

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
        # 사용자 수동 제외건은 집계에서 빼기
        items = [it for it in self._all_items if not self._is_excluded(it)]
        # 이번 달
        today = date.today()
        first = today.replace(day=1)
        month_items = []
        for it in items:
            d = _parse_used_at(it.used_at)
            if d and d.date() >= first and d.date() <= today:
                month_items.append(it)
        # 음수(취소) 차감 이미 들어있음 (final amount 가 음수)
        positive_total = sum(int(it.amount or 0) for it in month_items if (it.amount or 0) > 0)
        cancel_total = abs(sum(int(it.amount or 0) for it in month_items if (it.amount or 0) < 0))
        variable = positive_total - cancel_total

        # 고정비 합계 (활성 항목만, 매월 동일)
        fixed = self._fixed_service.total_active() if hasattr(self, "_fixed_service") else 0

        net = variable + fixed
        self.card_total.set_value(
            f"{net:,}원",
            f"지출 {variable:,} · 고정비 {fixed:,} · {first.strftime('%m.%d')}~{today.strftime('%m.%d')}",
        )

        confirmed = sum(1 for it in month_items if (it.amount or 0) != 0)
        empty = len(month_items) - confirmed
        self.card_count.set_value(
            f"{len(month_items)}건",
            f"금액확인 {confirmed}건 · 미확인 {empty}건",
        )

        days_passed = max(1, (today - first).days + 1)
        avg = net // days_passed
        self.card_avg.set_value(f"{avg:,}원", f"{days_passed}일 기준 (고정비 포함)")

        # 손익분기 매출 — 마진율 40% 기준: net = 0.4 × 매출 → 매출 = net / 0.4
        margin = 0.40
        if net > 0:
            be = int(net / margin)
            # 이달 일수 기준 일매출 (해당 월 전체 일수, days_passed 가 아니라 월 전체)
            import calendar as _cal
            days_in_month = _cal.monthrange(today.year, today.month)[1]
            daily_be = be // days_in_month
            be_man = be // 10000
            be_remain = be % 10000
            be_human = (
                f"{be_man:,}만 {be_remain:,}원" if be_remain else f"{be_man:,}만원"
            ) if be_man else f"{be:,}원"
            self.card_breakeven.set_value(
                f"{be:,}원",
                f"마진 40% · 일매출 {daily_be:,}원/{days_in_month}일 ({be_human})",
            )
        else:
            self.card_breakeven.set_value("-", "지출 없음")

    def _refresh_chip_amounts(self) -> None:
        sums: Dict[str, int] = {}
        for it in self._all_items:
            if self._is_excluded(it):
                continue
            cat = self._categories_index.get(it.use_key or it.id or "", "OTHER")
            sums[cat] = sums.get(cat, 0) + max(0, int(it.amount or 0))
        for idx, code in enumerate(self._category_order):
            chip = self.category_chips.get(code)
            if chip is None:
                continue
            meta = self._category_meta(code)
            chip.set_display(meta.label, meta.emoji, meta.bg_color)
            chip.set_shortcut_label(f"F{idx + 1}" if idx < 12 else "")
            chip.set_amount(sums.get(code, 0))

    # ----- 리스트 렌더 (디스패처) -----

    def _filtered_sorted_items(self) -> List[CardUsage]:
        items = list(self._all_items)

        # 캘린더에서 일자 클릭으로 점프한 경우: 해당 일자만 표시
        if self._jump_to_date is not None:
            target = self._jump_to_date
            day_items: List[CardUsage] = []
            for it in items:
                d = _parse_used_at(it.used_at)
                if d is not None and d.date() == target:
                    day_items.append(it)
            items = day_items

        # 카테고리 필터
        if self._selected_category:
            items = [
                it for it in items
                if self._categories_index.get(it.use_key or it.id or "", "OTHER") == self._selected_category
            ]

        # 검색
        q = self.store_filter.text().strip()
        if q:
            items = self._apply_search(items, q)

        # 리뷰 모드는 필터가 아니라 클릭 토글 동작 — 모든 항목 표시
        # (검토 완료 항목은 녹색 배경으로 구분)

        # 정렬 (헤더 클릭에 따라 컬럼/방향 결정)
        col = self._sort_col
        if col == self.COL_DATE:
            key_fn = lambda it: (it.used_at or "")
        elif col == self.COL_CATEGORY:
            key_fn = lambda it: self._categories_index.get(it.use_key or it.id or "", "OTHER")
        elif col == self.COL_STORE:
            key_fn = lambda it: (it.store_name or "")
        elif col == self.COL_AMOUNT:
            key_fn = lambda it: int(it.amount or 0)
        elif col == self.COL_CARD:
            key_fn = lambda it: (it.card_num or "")
        elif col == self.COL_REVIEW:
            key_fn = lambda it: (1 if self._is_reviewed(it) else 0)
        elif col == self.COL_COUPANG:
            key_fn = lambda it: (1 if getattr(it, "coupang_purchase_id", None) else 0)
        elif col == self.COL_MEMO:
            key_fn = lambda it: (it.memo or "")
        else:
            key_fn = lambda it: (it.used_at or "")
        items.sort(key=key_fn, reverse=not self._sort_asc)
        return items

    def _render_list(self) -> None:
        items = self._filtered_sorted_items()
        self._displayed_items = items
        reviewed_n = sum(1 for it in items if self._is_reviewed(it))
        suffix = f" · 검토 {reviewed_n}/{len(items)}"
        self.list_count_label.setText(f"거래내역 {len(items)}건{suffix}")

        if self._view_mode == "calendar":
            self._render_calendar(items)
        else:
            self._render_table(items)

    # 컬럼 인덱스: 0=날짜, 1=카테고리, 2=가맹점, 3=금액, 4=카드, 5=검토, 6=쿠팡매칭, 7=메모
    COL_DATE = 0
    COL_CATEGORY = 1
    COL_STORE = 2
    COL_AMOUNT = 3
    COL_CARD = 4
    COL_REVIEW = 5
    COL_COUPANG = 6
    COL_MEMO = 7

    def _find_offsetting_pairs(self, items: List[CardUsage]) -> set:
        """양수 결제 + 같은 가맹점·같은 금액 음수 취소가 정확히 짝을 이루는 행 식별.

        반환: 짝이 맞춰진 row 의 (use_key/id) set — 양쪽 모두 포함.
        """
        # (store, abs_amount) → (positives, negatives)
        idx: dict = {}
        for it in items:
            store = (it.store_name or "").strip()
            amt = int(it.amount or 0)
            if amt == 0 or not store:
                continue
            key = (store, abs(amt))
            slot = idx.setdefault(key, ([], []))
            (slot[0] if amt > 0 else slot[1]).append(it)
        offset_keys: set[str] = set()
        for key, (pos, neg) in idx.items():
            n = min(len(pos), len(neg))
            for i in range(n):
                p_key = pos[i].use_key or pos[i].id or ""
                n_key = neg[i].use_key or neg[i].id or ""
                if p_key:
                    offset_keys.add(p_key)
                if n_key:
                    offset_keys.add(n_key)
        return offset_keys

    def _render_table(self, items: List[CardUsage]) -> None:
        self._suspend_memo_signal = True
        try:
            offset_keys = self._find_offsetting_pairs(items)
            self.table.setRowCount(0)
            self.table.setRowCount(len(items))
            for r, usage in enumerate(items):
                cat_code = self._categories_index.get(usage.use_key or usage.id or "", "OTHER")
                cm = self._category_meta(cat_code)
                amount_int = int(usage.amount or 0)
                cancelled = amount_int < 0
                reviewed = self._is_reviewed(usage)
                matched = bool(getattr(usage, "coupang_purchase_id", None))
                offset_canceled = (usage.use_key or usage.id or "") in offset_keys

                # 날짜
                date_item = QTableWidgetItem(_format_used_at_short(usage.used_at))
                date_item.setForeground(QBrush(QColor("#475569")))
                # 카테고리
                cat_item = QTableWidgetItem(f"{cm.emoji} {cm.label}")
                cat_item.setForeground(QBrush(QColor("#334155")))
                # 가맹점 (매칭된 건은 🔗 prefix)
                store_text = (usage.store_name or "(가맹점명 미상)").strip()
                if matched:
                    store_text = f"🔗 {store_text}"
                store_item = QTableWidgetItem(store_text)
                store_item.setForeground(QBrush(QColor("#ef4444" if cancelled else "#0f172a")))
                f = QFont(); f.setBold(True); store_item.setFont(f)
                if matched:
                    store_item.setToolTip("매칭된 쿠팡 구매내역 — 더블클릭으로 상세보기")
                # 금액
                amt_item = QTableWidgetItem(_fmt_money(amount_int))
                amt_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                af = QFont(); af.setBold(True); amt_item.setFont(af)
                amt_item.setForeground(QBrush(QColor("#ef4444" if cancelled else "#0f172a")))
                # 카드
                card_item = QTableWidgetItem(_mask_card_number(usage.card_num))
                card_item.setForeground(QBrush(QColor("#64748b")))
                # 쿠팡매칭 (읽기 전용 — 아이콘만 표시. 더블클릭으로 상세보기)
                if matched:
                    key = usage.use_key or usage.id or ""
                    chosen_group = self._coupang_match_index.get(key)
                    coupang_text = "🔗"
                    title_for_tip = chosen_group.title if chosen_group is not None else "매칭됨"
                    coupang_tooltip = f"{title_for_tip} — 더블클릭으로 상세보기"
                else:
                    coupang_text = ""
                    coupang_tooltip = ""
                coupang_item = QTableWidgetItem(coupang_text)
                coupang_item.setTextAlignment(Qt.AlignCenter)
                coupang_item.setForeground(QBrush(QColor("#0f172a" if matched else "#94a3b8")))
                if coupang_tooltip:
                    coupang_item.setToolTip(coupang_tooltip)
                # 메모 (편집 가능)
                memo_item = QTableWidgetItem(usage.memo or "")
                memo_item.setForeground(QBrush(QColor("#475569")))
                memo_item.setFlags(memo_item.flags() | Qt.ItemIsEditable)
                memo_item.setToolTip("더블클릭으로 메모 입력 · Enter 로 저장")

                # 비메모 셀은 편집 불가
                for it in (date_item, cat_item, store_item, amt_item, card_item, coupang_item):
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)

                # 결제+취소 짝이 맞아 0원 처리된 행: 취소선 + 빨강 (볼드 해제)
                if offset_canceled:
                    excl_color = QBrush(QColor("#ef4444"))
                    for it in (date_item, cat_item, store_item, amt_item, card_item, coupang_item, memo_item):
                        it.setForeground(excl_color)
                        sf = QFont(it.font())
                        sf.setStrikeOut(True)
                        sf.setBold(False)
                        it.setFont(sf)
                    if matched:
                        store_item.setToolTip(
                            (store_item.toolTip() or "") + "\n(결제+취소가 짝지어 0원 처리)"
                        )
                    else:
                        store_item.setToolTip("결제+취소가 짝지어 0원 처리")

                # 사용자 수동 제외 행: 취소선 + 빨강 (볼드 해제) + tooltip
                excluded = self._is_excluded(usage)
                if excluded:
                    excl_color = QBrush(QColor("#ef4444"))
                    for it in (date_item, cat_item, store_item, amt_item, card_item, coupang_item, memo_item):
                        it.setForeground(excl_color)
                        sf = QFont(it.font())
                        sf.setStrikeOut(True)
                        sf.setBold(False)
                        it.setFont(sf)
                    store_item.setToolTip(
                        (store_item.toolTip() or "") + "\n🚫 제외됨 (집계 안 함) · 우클릭으로 해제"
                    )

                # 검토 완료 시 연한 녹색 배경
                if reviewed:
                    bg = QBrush(QColor("#f0fdf4"))
                    for it in (date_item, cat_item, store_item, amt_item, card_item, coupang_item, memo_item):
                        it.setBackground(bg)

                # 검토 버튼 (placeholder item — 배경/편집불가)
                review_placeholder = QTableWidgetItem("")
                review_placeholder.setFlags(Qt.ItemIsEnabled)
                if reviewed:
                    review_placeholder.setBackground(QBrush(QColor("#f0fdf4")))

                self.table.setItem(r, self.COL_DATE, date_item)
                self.table.setItem(r, self.COL_CATEGORY, cat_item)
                self.table.setItem(r, self.COL_STORE, store_item)
                self.table.setItem(r, self.COL_AMOUNT, amt_item)
                self.table.setItem(r, self.COL_CARD, card_item)
                self.table.setItem(r, self.COL_REVIEW, review_placeholder)
                self.table.setItem(r, self.COL_COUPANG, coupang_item)
                self.table.setItem(r, self.COL_MEMO, memo_item)

                # 검토 버튼 — 커스텀 페인팅 정사각형 체크박스
                review_btn = _ReviewCheckButton(reviewed)
                key = usage.use_key or usage.id or ""
                review_btn.clicked.connect(
                    lambda _checked=False, k=key: self._on_review_btn_clicked(k)
                )
                # 셀 안에서 가운데 정렬되도록 컨테이너로 감싸기
                review_wrap = QWidget()
                wrap_layout = QHBoxLayout(review_wrap)
                wrap_layout.setContentsMargins(0, 0, 0, 0)
                wrap_layout.setSpacing(0)
                wrap_layout.addWidget(review_btn, 0, Qt.AlignCenter)
                self.table.setCellWidget(r, self.COL_REVIEW, review_wrap)
        finally:
            self._suspend_memo_signal = False

    # ----- 더블클릭 / 편집 핸들러 -----

    def eventFilter(self, obj, event):  # type: ignore[override]
        if obj is self.table and event.type() == QEvent.KeyPress:
            key = event.key()
            if key in (Qt.Key_Return, Qt.Key_Enter):
                # 편집 중이면 기본동작(커밋)에 맡김
                if self.table.state() == QAbstractItemView.EditingState:
                    return False
                row = self.table.currentRow()
                if 0 <= row < len(self._displayed_items):
                    memo_item = self.table.item(row, self.COL_MEMO)
                    if memo_item is not None:
                        self.table.setCurrentCell(row, self.COL_MEMO)
                        self.table.editItem(memo_item)
                        return True
            if key == Qt.Key_Space:
                # 편집 중이면 공백 입력은 그대로 통과
                if self.table.state() == QAbstractItemView.EditingState:
                    return False
                row = self.table.currentRow()
                if 0 <= row < len(self._displayed_items):
                    self._toggle_reviewed(self._displayed_items[row])
                    return True
        # 금액/쿠팡매칭 컬럼 호버 시 손가락 커서로 변경 (매칭된 행만)
        if obj is self.table.viewport() and event.type() == QEvent.MouseMove:
            idx = self.table.indexAt(event.pos())
            change_to = None
            if idx.isValid() and idx.column() in (self.COL_AMOUNT, self.COL_COUPANG):
                row = idx.row()
                if 0 <= row < len(self._displayed_items):
                    usage = self._displayed_items[row]
                    if getattr(usage, "coupang_purchase_id", None):
                        change_to = Qt.PointingHandCursor
            if change_to is not None:
                self.table.viewport().setCursor(change_to)
            else:
                self.table.viewport().unsetCursor()
            return False
        return super().eventFilter(obj, event)

    def _on_table_cell_clicked(self, row: int, col: int) -> None:
        """금액/쿠팡매칭 셀 단일 클릭으로 매칭 상세 다이얼로그 열기."""
        if col not in (self.COL_AMOUNT, self.COL_COUPANG):
            return
        if row < 0 or row >= len(self._displayed_items):
            return
        usage = self._displayed_items[row]
        if not getattr(usage, "coupang_purchase_id", None):
            return
        self._open_match_dialog(usage)

    def _on_table_context_menu(self, pos) -> None:
        """우클릭 → 카테고리 변경 / 제외 토글 메뉴."""
        idx = self.table.indexAt(pos)
        if not idx.isValid():
            return
        row = idx.row()
        if row < 0 or row >= len(self._displayed_items):
            return
        usage = self._displayed_items[row]
        cur_code = self._categories_index.get(usage.use_key or usage.id or "", "OTHER")
        menu = QMenu(self.table)
        # 카테고리 변경 (서브메뉴)
        cat_menu = menu.addMenu("🏷 카테고리 변경")
        for idx, meta in enumerate(self._ordered_category_metas()):
            shortcut = f"F{idx + 1} · " if idx < 12 else ""
            label = f"{shortcut}{meta.emoji} {meta.label}"
            if meta.code == cur_code:
                label = f"● {label}"
            a = cat_menu.addAction(label)
            a.triggered.connect(
                lambda _checked=False, u=usage, code=meta.code: self._change_category(u, code)
            )
        menu.addSeparator()
        # 제외 토글
        is_excl = self._is_excluded(usage)
        action_text = "✓ 제외 해제" if is_excl else "🚫 제외하기 (집계 제외)"
        act = menu.addAction(action_text)
        act.triggered.connect(lambda _checked=False, u=usage: self._toggle_excluded(u))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _change_category(self, usage: CardUsage, new_code: str) -> None:
        key = usage.use_key or usage.id or ""
        if not key:
            return
        cur = self._categories_index.get(key, "OTHER")
        if cur == new_code:
            return
        self._categories_index[key] = new_code
        usage.category = new_code
        self._save_usage_change(usage, category=new_code)
        # 집계/카테고리 칩/리스트 모두 갱신
        self._render_list()
        self._refresh_summary_cards()
        self._refresh_chip_amounts()

    def _toggle_excluded(self, usage: CardUsage) -> None:
        new_state = not self._is_excluded(usage)
        self._set_excluded(usage, new_state)
        # 집계/표시 모두 갱신
        self._render_list()
        self._refresh_summary_cards()
        self._refresh_chip_amounts()

    def _on_table_double_clicked(self, row: int, col: int) -> None:
        if row < 0 or row >= len(self._displayed_items):
            return
        usage = self._displayed_items[row]

        # 리뷰 모드 ON 이면 메모/검토/쿠팡매칭 컬럼 외 더블클릭으로 검토 토글
        if self._review_mode and col not in (self.COL_MEMO, self.COL_REVIEW, self.COL_COUPANG):
            self._toggle_reviewed(usage)
            return

        # 메모 컬럼: 더블클릭으로 즉시 편집 시작
        if col == self.COL_MEMO:
            item = self.table.item(row, self.COL_MEMO)
            if item is not None:
                self.table.editItem(item)
            return
        # 검토 컬럼: 위젯 자체 처리 (체크박스)
        if col == self.COL_REVIEW:
            return

        # 그 외 → 매칭 상세 다이얼로그
        self._open_match_dialog(usage)

    def _on_table_cell_changed(self, row: int, col: int) -> None:
        if self._suspend_memo_signal:
            return
        if col != self.COL_MEMO:
            return
        if row < 0 or row >= len(self._displayed_items):
            return
        item = self.table.item(row, col)
        if item is None:
            return
        new_text = item.text().strip()
        usage = self._displayed_items[row]
        if (usage.memo or "") == new_text:
            return
        usage.memo = new_text
        # 메모가 있으면 자동으로 검토 완료 처리
        auto_reviewed: Optional[bool] = None
        if new_text and not usage.reviewed:
            usage.reviewed = True
            key = usage.use_key or usage.id or ""
            if key:
                self._reviewed_keys.add(key)
            auto_reviewed = True
        self._save_usage_change(usage, memo=new_text, reviewed=auto_reviewed)
        # 메모/검토 컬럼 기준 정렬일 때만 전체 재렌더, 그 외는 행만 갱신해 포커스/스크롤 유지
        needs_full_render = self._sort_col in (self.COL_MEMO, self.COL_REVIEW)
        if auto_reviewed and not needs_full_render:
            self._update_row_visual(usage)
        elif needs_full_render:
            self._render_list()
        # 편집 종료 후 같은 셀에 포커스/선택 유지
        QTimer.singleShot(0, lambda r=row: self._restore_memo_focus(r))

    def _save_usage_change(
        self,
        usage: CardUsage,
        *,
        memo: Optional[str] = None,
        reviewed: Optional[bool] = None,
        category: Optional[str] = None,
    ) -> None:
        """단건 변경을 Pi 에 즉시 저장 (best-effort).

        실패는 status_banner 에 노출 — 조용한 실패가 사용자 혼란 야기하던 문제 해소.
        """
        if not self.pi.is_configured:
            try:
                self.status_banner.setText(
                    "⚠ Pi 미설정 — 카테고리/검토/메모 변경이 라즈베리에 저장되지 않습니다 "
                    "(credentials.json 의 monitor.url 설정 필요)"
                )
                self.status_banner.setStyleSheet(
                    "QLabel { background: #fef3c7; border: 1px solid #fcd34d; border-radius: 6px; "
                    "padding: 6px 10px; color: #92400e; font-size: 11px; }"
                )
                self.status_banner.setVisible(True)
            except Exception:  # noqa: BLE001
                pass
            return
        use_key = usage.use_key or usage.id
        if not use_key:
            return
        kwargs: Dict[str, Any] = {}
        if memo is not None:
            if memo == "":
                kwargs["clear_memo"] = True
            else:
                kwargs["memo"] = memo
        if reviewed is not None:
            kwargs["reviewed"] = bool(reviewed)
        if category is not None:
            kwargs["category"] = category
        if not kwargs:
            return
        try:
            self.pi.patch_card_usage(use_key, **kwargs)
        except Exception as exc:  # noqa: BLE001
            try:
                fields = ", ".join(kwargs.keys())
                self.status_banner.setText(
                    f"⚠ 라즈베리 저장 실패 ({fields}): {type(exc).__name__}: {exc}"
                )
                self.status_banner.setStyleSheet(
                    "QLabel { background: #fef2f2; border: 1px solid #fecaca; border-radius: 6px; "
                    "padding: 6px 10px; color: #991b1b; font-size: 11px; }"
                )
                self.status_banner.setVisible(True)
            except Exception:  # noqa: BLE001
                pass

    def _restore_memo_focus(self, row: int) -> None:
        if row < 0 or row >= self.table.rowCount():
            return
        self.table.setCurrentCell(row, self.COL_MEMO)
        self.table.setFocus()

    def _on_header_clicked(self, col: int) -> None:
        """컬럼 헤더 클릭 → 정렬 토글."""
        if col == self._sort_col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            # 날짜/금액은 기본 내림차순, 그 외는 오름차순
            self._sort_asc = col not in (self.COL_DATE, self.COL_AMOUNT)
        self.table.horizontalHeader().setSortIndicator(
            col, Qt.AscendingOrder if self._sort_asc else Qt.DescendingOrder
        )
        self._render_list()

    @staticmethod
    def _style_review_btn(btn: QPushButton, reviewed: bool) -> None:
        btn.setText("✓" if reviewed else "")
        btn.setStyleSheet(
            "QPushButton { background: #ffffff; color: #0f172a; "
            "border: 1px solid #94a3b8; border-radius: 4px; "
            "font-size: 12px; font-weight: 700; }"
            "QPushButton:hover { background: #f1f5f9; border-color: #64748b; }"
        )

    def _make_review_btn(self, usage: CardUsage) -> QPushButton:
        btn = QPushButton()
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFocusPolicy(Qt.NoFocus)  # 클릭 시 포커스 이동에 따른 스크롤 방지
        btn.setFixedSize(28, 22)
        self._style_review_btn(btn, self._is_reviewed(usage))
        key = usage.use_key or usage.id or ""
        btn.clicked.connect(lambda _checked=False, k=key: self._on_review_btn_clicked(k))
        return btn

    def _update_row_visual(self, usage: CardUsage) -> None:
        """단일 행만 갱신 (전체 재정렬/재렌더 없이) → 행 위치/포커스/스크롤 유지."""
        target_key = usage.use_key or usage.id or ""
        if not target_key:
            return
        row = -1
        for i, u in enumerate(self._displayed_items):
            if (u.use_key or u.id or "") == target_key:
                row = i
                break
        if row < 0:
            return

        reviewed = self._is_reviewed(usage)
        bg = QBrush(QColor("#f0fdf4")) if reviewed else QBrush()

        # 스크롤/포커스 보존
        sb = self.table.verticalScrollBar()
        scroll_pos = sb.value()

        self._suspend_memo_signal = True
        try:
            for c in (
                self.COL_DATE, self.COL_CATEGORY, self.COL_STORE,
                self.COL_AMOUNT, self.COL_CARD, self.COL_COUPANG, self.COL_MEMO,
            ):
                item = self.table.item(row, c)
                if item is None:
                    continue
                if reviewed:
                    item.setBackground(bg)
                else:
                    item.setData(Qt.BackgroundRole, None)
            # 메모 텍스트 동기화
            memo_item = self.table.item(row, self.COL_MEMO)
            if memo_item is not None and memo_item.text() != (usage.memo or ""):
                memo_item.setText(usage.memo or "")
            # 검토 placeholder 배경
            rp = self.table.item(row, self.COL_REVIEW)
            if rp is not None:
                if reviewed:
                    rp.setBackground(bg)
                else:
                    rp.setData(Qt.BackgroundRole, None)
            # 버튼 위젯은 재생성하지 않고 기존 위젯만 스타일 갱신 (포커스 이동 방지)
            existing = self.table.cellWidget(row, self.COL_REVIEW)
            chk = existing.findChild(_ReviewCheckButton) if existing is not None else None
            if isinstance(chk, _ReviewCheckButton):
                chk.set_reviewed(reviewed)
            elif isinstance(existing, _ReviewCheckButton):
                existing.set_reviewed(reviewed)
            elif isinstance(existing, QPushButton):
                self._style_review_btn(existing, reviewed)
            else:
                self.table.setCellWidget(row, self.COL_REVIEW, self._make_review_btn(usage))
        finally:
            self._suspend_memo_signal = False

        # 스크롤 복원
        sb.setValue(scroll_pos)

        # 검토 카운트 라벨 갱신
        reviewed_n = sum(1 for it in self._displayed_items if self._is_reviewed(it))
        total_n = len(self._displayed_items)
        self.list_count_label.setText(f"거래내역 {total_n}건 · 검토 {reviewed_n}/{total_n}")

    def _on_review_btn_clicked(self, key: str) -> None:
        if not key:
            return
        for it in self._all_items:
            if (it.use_key or it.id) == key:
                self._toggle_reviewed(it)
                return

    def _toggle_reviewed(self, usage: CardUsage) -> None:
        # 메모 유무는 무시하고 reviewed 플래그 자체를 토글
        new_state = not bool(usage.reviewed)
        usage.reviewed = new_state
        key = usage.use_key or usage.id or ""
        if key:
            if new_state:
                self._reviewed_keys.add(key)
            else:
                self._reviewed_keys.discard(key)
        self._save_usage_change(usage, reviewed=new_state)
        # 정렬 컬럼이 검토일 때만 전체 재렌더, 그 외는 단일 행만 갱신해 위치/포커스 유지
        if self._sort_col == self.COL_REVIEW:
            self._render_list()
        else:
            self._update_row_visual(usage)

    def _open_match_dialog(self, usage: CardUsage) -> None:
        group = self._coupang_match_index.get(usage.use_key or usage.id or "")
        items: List[Any] = []
        order: Optional[PurchaseOrder] = None

        if group is None and getattr(usage, "coupang_purchase_id", None):
            # 매칭 인덱스가 비어있어도 group_key 로 재구성
            order_no = self._extract_order_no(usage.coupang_purchase_id or "")
            if order_no:
                order, items = self._lookup_coupang_order(order_no)
                if order is not None:
                    group = PurchaseGroup(
                        channel="coupang",
                        order_date=order.order_date,
                        title=(
                            f"주문 {order.order_no}"
                            + (f" · {order.item_count}건" if order.item_count else "")
                            + (f" [{order.status}]" if order.status else "")
                        ),
                        total_amount=int(order.card_amount or order.payment_total or 0),
                        item_count=order.item_count or len(items),
                        items=[],
                        group_key=usage.coupang_purchase_id or f"coupang|order|{order_no}",
                    )

        if group is not None:
            order_no = self._extract_order_no(group.group_key)
            if order_no:
                ord_obj, recs = self._lookup_coupang_order(order_no)
                if order is None:
                    order = ord_obj
                if recs:
                    items = recs
            if not items and group.items:
                items = list(group.items)

        dlg = _CoupangMatchDetailDialog(usage, group, items, order, parent=self)
        dlg.exec()

    @staticmethod
    def _extract_order_no(group_key: str) -> str:
        if not group_key:
            return ""
        parts = group_key.split("|")
        if len(parts) >= 3 and parts[1] == "order":
            return parts[-1]
        return ""

    def _lookup_coupang_order(self, order_no: str) -> tuple[Optional[PurchaseOrder], List[Any]]:
        """주문번호로 PurchaseOrder + 품목 records 를 조회 (Pi 우선, 로컬 fallback)."""
        order: Optional[PurchaseOrder] = None
        items: List[Any] = []
        # Pi
        if self.pi.is_configured:
            try:
                orders = self.pi.list_purchase_orders(channel="coupang", limit=5000)
                for o in orders:
                    if (o.order_no or "") == order_no:
                        order = o
                        break
            except Exception:  # noqa: BLE001
                pass
            try:
                recs = self.pi.list_purchase_records(channel="coupang", limit=5000)
                items = [r for r in recs if (getattr(r, "order_no", "") or "") == order_no]
            except Exception:  # noqa: BLE001
                pass
        # Local fallback
        if order is None or not items:
            try:
                store = PurchaseHistoryStore()
                if order is None:
                    for o in store.load_orders(channel="coupang", limit=5000):
                        if (o.order_no or "") == order_no:
                            order = o
                            break
                if not items:
                    items = [
                        r for r in store.load_records(channel="coupang", limit=5000)
                        if (getattr(r, "order_no", "") or "") == order_no
                    ]
            except Exception:  # noqa: BLE001
                pass
        # 같은 주문 내 동일 품목이 여러 행 저장된 케이스(Pi fingerprint 변경 이력 등)를
        # 표시 단계에서 제거.
        return order, dedupe_order_items(items)

    def _period_month_bounds(self) -> tuple[tuple[int, int], tuple[int, int]]:
        """기간 위젯 기준 (start_year, start_month), (end_year, end_month)."""
        sd = self.start_edit.date()
        ed = self.end_edit.date()
        return (sd.year(), sd.month()), (ed.year(), ed.month())

    def _clamp_cal_view_month(self) -> None:
        (sy, sm), (ey, em) = self._period_month_bounds()
        ord_min = sy * 12 + (sm - 1)
        ord_max = ey * 12 + (em - 1)
        cur = int(self._cal_view_year) * 12 + (int(self._cal_view_month) - 1)
        cur = max(ord_min, min(ord_max, cur))
        self._cal_view_year = cur // 12
        self._cal_view_month = (cur % 12) + 1

    def _can_shift_cal_view(self, delta: int) -> bool:
        (sy, sm), (ey, em) = self._period_month_bounds()
        ord_min = sy * 12 + (sm - 1)
        ord_max = ey * 12 + (em - 1)
        cur = int(self._cal_view_year) * 12 + (int(self._cal_view_month) - 1)
        nxt = cur + delta
        return ord_min <= nxt <= ord_max

    def _shift_cal_view(self, delta: int) -> None:
        if not self._can_shift_cal_view(delta):
            return
        cur = int(self._cal_view_year) * 12 + (int(self._cal_view_month) - 1) + delta
        self._cal_view_year = cur // 12
        self._cal_view_month = (cur % 12) + 1
        self._render_list()

    def _render_calendar(self, items: List[CardUsage]) -> None:
        # 기존 캘린더 클리어
        while self._cal_inner_layout.count():
            child = self._cal_inner_layout.takeAt(0)
            w = child.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
            else:
                lay = child.layout()
                if lay is not None:
                    self._clear_layout(lay)

        # 표시할 월 결정 — _cal_view_year/month (없으면 start_edit 의 월로 초기화).
        # 기간 범위 [start_edit, end_edit] 안에서만 이동 가능하도록 클램프.
        if self._cal_view_year is None or self._cal_view_month is None:
            sd = self.start_edit.date()
            self._cal_view_year, self._cal_view_month = sd.year(), sd.month()
        self._clamp_cal_view_month()
        year, month = int(self._cal_view_year), int(self._cal_view_month)
        first_weekday, days_in_month = calendar.monthrange(year, month)
        # Python: monday=0 → 우리 캘린더는 일요일 시작이므로 col_offset 계산
        # 일=0, 월=1, ..., 토=6
        py_to_sun_first = (first_weekday + 1) % 7  # mon(0)→1, tue(1)→2, ..., sun(6)→0

        # 일자별 카테고리 합계 집계
        day_totals: Dict[int, int] = {}
        day_cat_sums: Dict[int, Dict[str, int]] = {}
        for it in items:
            d = _parse_used_at(it.used_at)
            if d is None:
                continue
            if d.year != year or d.month != month:
                continue
            amt = int(it.amount or 0)
            if amt <= 0:
                continue  # 취소건은 게이지 제외
            day = d.day
            day_totals[day] = day_totals.get(day, 0) + amt
            cat = self._categories_index.get(it.use_key or it.id or "", "OTHER")
            day_cat_sums.setdefault(day, {})
            day_cat_sums[day][cat] = day_cat_sums[day].get(cat, 0) + amt

        max_total = max(day_totals.values()) if day_totals else 0

        # 헤더(설명 + 월)
        info = QLabel(
            f"일별 사용액 게이지 (카테고리 분할, 최대 {max_total:,}원 = 100%)"
            if max_total > 0 else "일별 사용액 게이지 — 데이터 없음"
        )
        info.setStyleSheet(
            "QLabel { background: #fefce8; border: 1px solid #fde68a; border-radius: 8px; "
            "padding: 8px 12px; color: #854d0e; font-size: 11px; }"
        )
        self._cal_inner_layout.addWidget(info)

        # 월 네비게이션 (◀ yyyy년 M월 ▶) — period 범위 안에서만 이동
        nav_row = QHBoxLayout()
        nav_row.setSpacing(6)
        prev_btn = QPushButton("◀")
        next_btn = QPushButton("▶")
        nav_btn_style = (
            "QPushButton { background: #ffffff; border: 1px solid #e2e8f0; "
            "border-radius: 6px; padding: 4px 12px; font-size: 13px; color: #475569; }"
            "QPushButton:hover { background: #f1f5f9; }"
            "QPushButton:disabled { color: #cbd5e1; background: #f8fafc; }"
        )
        prev_btn.setStyleSheet(nav_btn_style)
        next_btn.setStyleSheet(nav_btn_style)
        prev_btn.setCursor(Qt.PointingHandCursor)
        next_btn.setCursor(Qt.PointingHandCursor)
        prev_btn.setEnabled(self._can_shift_cal_view(-1))
        next_btn.setEnabled(self._can_shift_cal_view(+1))
        prev_btn.clicked.connect(lambda: self._shift_cal_view(-1))
        next_btn.clicked.connect(lambda: self._shift_cal_view(+1))
        title = QLabel(f"{year}년 {month}월")
        tf = QFont(); tf.setBold(True); tf.setPointSize(13)
        title.setFont(tf)
        title.setStyleSheet("color: #0f172a; padding: 4px 8px;")
        nav_row.addWidget(prev_btn)
        nav_row.addWidget(title)
        nav_row.addWidget(next_btn)
        nav_row.addStretch(1)
        self._cal_inner_layout.addLayout(nav_row)

        # 그리드
        grid_box = QFrame()
        grid_box.setStyleSheet("QFrame { background: transparent; }")
        grid = QGridLayout(grid_box)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        # 컬럼 균등
        for c in range(7):
            grid.setColumnStretch(c, 1)

        # 요일 헤더
        weekdays = ["일", "월", "화", "수", "목", "금", "토"]
        for c, wd in enumerate(weekdays):
            lab = QLabel(wd)
            lab.setAlignment(Qt.AlignCenter)
            color = "#ef4444" if c == 0 else ("#3b82f6" if c == 6 else "#64748b")
            lab.setStyleSheet(f"color: {color}; font-size: 11px; font-weight: 600; padding: 4px;")
            grid.addWidget(lab, 0, c)

        # 일자 셀
        row = 1
        col = py_to_sun_first
        # 첫 주 placeholder
        for c in range(col):
            ph = _CalendarDayCell(0, 0, [], 0.0, is_placeholder=True)
            ph.day_placeholder = True  # marker
            # placeholder 라도 day 라벨이 어색하므로 빈 프레임으로 대체
            grid.addWidget(self._calendar_blank_cell(), row, c)

        for day in range(1, days_in_month + 1):
            amt = day_totals.get(day, 0)
            cat_sums = day_cat_sums.get(day, {})
            # 세그먼트 (큰 카테고리부터)
            segs: List[tuple[str, int]] = []
            for code, sub in sorted(cat_sums.items(), key=lambda kv: -kv[1]):
                segs.append((self._cat_color(code), sub))
            pct = (amt / max_total) if max_total > 0 else 0.0
            cell = _CalendarDayCell(day, amt, segs, pct, is_placeholder=False)
            cell.day_clicked.connect(
                lambda d, y=year, m=month: self._on_calendar_day_clicked(y, m, d)
            )
            grid.addWidget(cell, row, col)
            col += 1
            if col >= 7:
                col = 0
                row += 1

        # 마지막 주 placeholder
        if col != 0:
            for c in range(col, 7):
                grid.addWidget(self._calendar_blank_cell(), row, c)

        self._cal_inner_layout.addWidget(grid_box)
        self._cal_inner_layout.addStretch(1)

    def _calendar_blank_cell(self) -> QWidget:
        f = QFrame()
        f.setMinimumHeight(110)
        f.setStyleSheet(
            "QFrame { background: #f1f5f9; border: 1px solid #e2e8f0; border-radius: 8px; }"
        )
        return f

    @staticmethod
    def _clear_layout(lay) -> None:
        while lay.count():
            child = lay.takeAt(0)
            w = child.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    @staticmethod
    def _cat_color(code: str) -> str:
        # 카테고리 메인 컬러 (게이지용 — 채도 높임)
        m = {
            "CAFE":          "#f59e0b",
            "FOOD":          "#fb923c",
            "BAKERY":        "#fbbf24",
            "TRANSPORT":     "#3b82f6",
            "SHOPPING":      "#22c55e",
            "CONVENIENCE":   "#10b981",
            "FUEL":          "#f97316",
            "FINANCE":       "#a855f7",
            "TELECOM":       "#6366f1",
            "OFFICE":        "#64748b",
            "MEDICAL":       "#14b8a6",
            "EDUCATION":     "#0ea5e9",
            "ENTERTAINMENT": "#ec4899",
            "OTHER":         "#94a3b8",
        }
        return m.get(code, "#94a3b8")

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
        try:
            self.pi.close()
        except Exception:  # noqa: BLE001
            pass


__all__ = ["CardUsageTab"]
