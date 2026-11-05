"""ui.desktop.inspector —— 记忆检查器与轻量自绘图表。

借鉴业界「让人能信任的记忆」前端范式（Richard Simms 的 8 个交互模式、
可解释记忆的四维置信度、claude-mem 的审计轨迹、Letta ADE 的逐层披露）：
- 记忆要有「身份证」：来源设备/agent、形成时间、双时态、最后使用；
- 置信度用文字 + 证据，不只用颜色，也不伪装成精确概率；
- 陈旧记忆显式标记，历史放在记忆旁边可就地查看；
- 完整原始字段折叠收起，默认只给结论（渐进式披露）。

只用 Qt 原生绘制，不引入图表依赖。
"""
from __future__ import annotations

import time
from typing import Any

from PySide6 import QtCore, QtGui, QtWidgets

from ui.desktop.backend import DesktopBackend

__all__ = ["BarChart", "ConfidenceBar", "MemoryInspector", "Sparkline"]

_BG = "#111826"
_GRID = "#28324A"
_TEXT = "#A9B6CC"


def _level(value: float) -> str:
    if value >= 0.7:
        return "高"
    if value >= 0.4:
        return "中"
    return "低"


def _color(value: float) -> str:
    if value >= 0.7:
        return "#3DDC97"
    if value >= 0.4:
        return "#FFB84D"
    return "#FF6B6B"


def _fmt_ms(value: Any) -> str:
    try:
        ms = int(value or 0)
    except (TypeError, ValueError):
        return "—"
    if not ms:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000))


def _badge(text: str, color: str) -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(text)
    label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
    label.setStyleSheet(
        f"QLabel {{ color: {color}; border: 1px solid {color}; border-radius: 9px;"
        " padding: 2px 8px; font-size: 11px; }"
    )
    return label


class _CorrectionDialog(QtWidgets.QDialog):
    """纠正记忆：只暴露可安全修改的字段，理由写入 journal 审计。"""

    def __init__(self, data: dict[str, Any], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("纠正记忆（将生成新版本并封存原值）")
        self.resize(480, 300)
        self.content = QtWidgets.QLineEdit(str(data.get("content") or ""))
        self.memory_type = QtWidgets.QComboBox()
        self.memory_type.addItems(["fact", "profile", "emotion", "scenario", "code"])
        current_type = str(data.get("memory_type") or "fact")
        if current_type in [self.memory_type.itemText(i) for i in range(self.memory_type.count())]:
            self.memory_type.setCurrentText(current_type)
        self.tier = QtWidgets.QComboBox()
        self.tier.addItems(["buffer", "core", "percept", "archive"])
        current_tier = str(data.get("tier") or "buffer")
        if current_tier in [self.tier.itemText(i) for i in range(self.tier.count())]:
            self.tier.setCurrentText(current_tier)
        self.namespace = QtWidgets.QLineEdit(str(data.get("namespace") or "private"))
        self.confidence = QtWidgets.QDoubleSpinBox()
        self.confidence.setRange(0.0, 1.0)
        self.confidence.setSingleStep(0.05)
        self.confidence.setValue(float(data.get("confidence") or 0.5))
        self.reason = QtWidgets.QLineEdit()
        self.reason.setPlaceholderText("为什么修改？会写进审计轨迹")

        form = QtWidgets.QFormLayout()
        form.addRow("正文：", self.content)
        form.addRow("类型：", self.memory_type)
        form.addRow("分层：", self.tier)
        form.addRow("命名空间：", self.namespace)
        form.addRow("置信度：", self.confidence)
        form.addRow("理由：", self.reason)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def changes(self) -> dict[str, Any]:
        return {
            "content": self.content.text().strip(),
            "memory_type": self.memory_type.currentText(),
            "tier": self.tier.currentText(),
            "namespace": self.namespace.text().strip() or "private",
            "confidence": float(self.confidence.value()),
        }

    def reason_text(self) -> str:
        return self.reason.text().strip()


class ConfidenceBar(QtWidgets.QWidget):
    """综合置信度：文字标签 + 进度条（颜色只是辅助，不单独承载语义）。"""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._label = QtWidgets.QLabel("—")
        self._bar = QtWidgets.QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(9)
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self._label)
        row.addWidget(self._bar, 1)

    def set_value(self, value: float) -> None:
        score = max(0.0, min(1.0, float(value)))
        color = _color(score)
        self._label.setText(f"{_level(score)}置信度 · {score:.2f}")
        self._label.setStyleSheet(f"QLabel {{ color: {color}; font-weight: 600; }}")
        self._bar.setValue(int(score * 100))
        self._bar.setStyleSheet(
            "QProgressBar { background: #1C2536; border: none; border-radius: 4px; }"
            f"QProgressBar::chunk {{ background: {color}; border-radius: 4px; }}"
        )


class Sparkline(QtWidgets.QWidget):
    """多序列折线（PAD 三条线等）。series = [(label, color, [float, ...]), ...]。"""

    def __init__(self, *, y_range: tuple[float, float] | None = None, parent=None) -> None:
        super().__init__(parent)
        self._series: list[tuple[str, str, list[float]]] = []
        self._y_range = y_range
        self.setMinimumHeight(140)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.MinimumExpanding,
        )

    def set_series(self, series: list[tuple[str, str, list[float]]]) -> None:
        self._series = [(str(a), str(b), [float(v) for v in c]) for a, b, c in series]
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QtGui.QColor(_BG))
        area = self.rect().adjusted(34, 10, -10, -24)
        if area.width() <= 0 or area.height() <= 0:
            return
        values = [v for _label, _color, points in self._series for v in points]
        lo, hi = self._y_range or (min(values) if values else -1.0, max(values) if values else 1.0)
        if hi - lo < 1e-9:
            hi = lo + 1.0
        grid = QtGui.QPen(QtGui.QColor(_GRID), 1)
        painter.setPen(grid)
        for frac in (0.0, 0.5, 1.0):
            y = area.top() + area.height() * frac
            painter.drawLine(int(area.left()), int(y), int(area.right()), int(y))
        painter.setPen(QtGui.QPen(QtGui.QColor(_TEXT), 1))
        painter.drawText(2, int(area.top()) + 8, f"{hi:+.1f}")
        painter.drawText(2, int(area.bottom()), f"{lo:+.1f}")
        if 0.0 < hi and 0.0 > lo:
            zero = area.top() + area.height() * (hi / (hi - lo))
            painter.setPen(QtGui.QPen(QtGui.QColor("#4A5872"), 1, QtCore.Qt.PenStyle.DashLine))
            painter.drawLine(int(area.left()), int(zero), int(area.right()), int(zero))
        legend_x = area.left() + 4
        for label, color, points in self._series:
            if len(points) >= 2:
                poly = QtGui.QPolygonF(
                    [
                        QtCore.QPointF(
                            area.left() + area.width() * i / (len(points) - 1),
                            area.top() + area.height() * (1.0 - (v - lo) / (hi - lo)),
                        )
                        for i, v in enumerate(points)
                    ]
                )
                painter.setPen(QtGui.QPen(QtGui.QColor(color), 2))
                painter.drawPolyline(poly)
            elif len(points) == 1:
                y = area.top() + area.height() * (1.0 - (points[0] - lo) / (hi - lo))
                painter.setBrush(QtGui.QColor(color))
                painter.setPen(QtCore.Qt.PenStyle.NoPen)
                painter.drawEllipse(QtCore.QPointF(area.left(), y), 3, 3)
            painter.setPen(QtGui.QColor(color))
            painter.drawText(int(legend_x), self.height() - 6, f"■ {label}")
            legend_x += 62
        if not values:
            painter.setPen(QtGui.QColor(_TEXT))
            painter.drawText(area.center().toPoint(), "暂无数据")


class BarChart(QtWidgets.QWidget):
    """水平条形图。bars = [(label, value, color), ...]。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._bars: list[tuple[str, float, str]] = []
        self.setMinimumHeight(40)

    def set_bars(self, bars: list[tuple[str, float, str]]) -> None:
        self._bars = [(str(a), float(b), str(c)) for a, b, c in bars]
        self.setMinimumHeight(max(40, 26 * len(self._bars) + 12))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QtGui.QColor(_BG))
        if not self._bars:
            painter.setPen(QtGui.QColor(_TEXT))
            painter.drawText(self.rect().center(), "暂无数据")
            return
        top = max(value for _label, value, _color in self._bars) or 1.0
        label_w, value_w = 96, 54
        bar_w = max(10, self.width() - label_w - value_w - 16)
        for index, (label, value, color) in enumerate(self._bars):
            y = 8 + index * 26
            painter.setPen(QtGui.QColor(_TEXT))
            painter.drawText(6, y + 12, label[:14])
            painter.fillRect(label_w, y + 3, bar_w, 12, QtGui.QColor("#1C2536"))
            painter.fillRect(
                label_w, y + 3, int(bar_w * max(0.0, value) / top), 12, QtGui.QColor(color)
            )
            painter.drawText(label_w + bar_w + 6, y + 13, f"{value:.0f}")


class _Section(QtWidgets.QWidget):
    """可折叠分组：默认收起，实现渐进式披露。"""

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self._toggle = QtWidgets.QToolButton()
        self._toggle.setText(title)
        self._toggle.setCheckable(True)
        self._toggle.setChecked(False)
        self._toggle.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._toggle.setArrowType(QtCore.Qt.ArrowType.RightArrow)
        self._toggle.setStyleSheet("QToolButton { border: none; font-weight: 600; }")
        self._body = QtWidgets.QWidget()
        self._body.setVisible(False)
        self._layout = QtWidgets.QVBoxLayout(self._body)
        self._layout.setContentsMargins(12, 2, 0, 2)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 2, 0, 2)
        outer.addWidget(self._toggle)
        outer.addWidget(self._body)
        self._toggle.toggled.connect(self._on_toggled)

    @property
    def body(self) -> QtWidgets.QVBoxLayout:
        return self._layout

    def _on_toggled(self, checked: bool) -> None:
        self._body.setVisible(checked)
        self._toggle.setArrowType(
            QtCore.Qt.ArrowType.DownArrow if checked else QtCore.Qt.ArrowType.RightArrow
        )


def _form(pairs: list[tuple[str, Any]]) -> QtWidgets.QWidget:
    """固定两列网格：QFormLayout 在窄面板里会给中文标签换行导致行重叠。"""
    widget = QtWidgets.QWidget()
    widget.setSizePolicy(
        QtWidgets.QSizePolicy.Policy.Expanding,
        QtWidgets.QSizePolicy.Policy.Minimum,
    )
    grid = QtWidgets.QGridLayout(widget)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(8)
    grid.setVerticalSpacing(3)
    grid.setColumnMinimumWidth(0, 112)
    grid.setColumnStretch(1, 1)
    for row, (key, value) in enumerate(pairs):
        full = str(value)
        shown = full if len(full) <= 24 else full[:21] + "…"
        key_label = QtWidgets.QLabel(f"{key}：")
        key_label.setObjectName("hint")
        key_label.setWordWrap(False)
        key_label.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        value_label = QtWidgets.QLabel(shown)
        value_label.setToolTip(full)
        value_label.setWordWrap(False)
        value_label.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Minimum,
        )
        value_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        grid.addWidget(key_label, row, 0)
        grid.addWidget(value_label, row, 1)
    return widget


class MemoryInspector(QtWidgets.QScrollArea):
    """记忆检查器：把「为什么记得、多可信、什么时候、用在哪」放在同一处。"""

    focus_requested = QtCore.Signal(str)
    locate_requested = QtCore.Signal(str)
    changed = QtCore.Signal(str)          # 纠正/遗忘后通知列表刷新；参数为新记忆 id（可空）

    def __init__(self, backend: DesktopBackend, parent=None) -> None:
        super().__init__(parent)
        self._backend = backend
        self._current_id = ""
        self._current: dict[str, Any] = {}
        self._revealed = False
        self.setWidgetResizable(True)
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)

        content = QtWidgets.QWidget()
        content.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Minimum,
        )
        self._root = QtWidgets.QVBoxLayout(content)
        self._root.setContentsMargins(12, 10, 12, 12)
        self._root.setSpacing(8)

        self._title = QtWidgets.QLabel("选中一条记忆，查看它的来源、置信度与历史")
        self._title.setWordWrap(True)
        self._title.setStyleSheet("font-size: 14px; font-weight: 600;")
        self._root.addWidget(self._title)

        self._reveal = QtWidgets.QPushButton("在当前上下文显示敏感内容")
        self._reveal.setVisible(False)
        self._reveal.clicked.connect(self._on_reveal)
        self._root.addWidget(self._reveal)

        self._badges = QtWidgets.QHBoxLayout()
        self._root.addLayout(self._badges)
        self._sensitive = QtWidgets.QCheckBox("标记为敏感（默认遮蔽正文）")
        self._sensitive.toggled.connect(self._on_sensitive_toggled)
        self._root.addWidget(self._sensitive)

        self._stale = QtWidgets.QLabel("")
        self._stale.setWordWrap(True)
        self._stale.setVisible(False)
        self._root.addWidget(self._stale)

        self._confidence = ConfidenceBar()
        self._root.addWidget(self._confidence)
        self._evidence = QtWidgets.QHBoxLayout()
        self._root.addLayout(self._evidence)

        minimum = QtWidgets.QSizePolicy.Policy.Minimum
        expanding = QtWidgets.QSizePolicy.Policy.Expanding
        self._provenance = QtWidgets.QWidget()
        QtWidgets.QVBoxLayout(self._provenance).setContentsMargins(0, 0, 0, 0)
        self._provenance.setSizePolicy(expanding, minimum)
        self._root.addWidget(self._provenance)
        self._timeline = QtWidgets.QWidget()
        QtWidgets.QVBoxLayout(self._timeline).setContentsMargins(0, 0, 0, 0)
        self._timeline.setSizePolicy(expanding, minimum)
        self._root.addWidget(self._timeline)
        self._plasticity = QtWidgets.QWidget()
        QtWidgets.QVBoxLayout(self._plasticity).setContentsMargins(0, 0, 0, 0)
        self._plasticity.setSizePolicy(expanding, minimum)
        self._root.addWidget(self._plasticity)

        self._related_label = QtWidgets.QLabel("关联记忆（突触）")
        self._related = QtWidgets.QListWidget()
        self._related.setMinimumHeight(72)
        self._related.setMaximumHeight(120)
        self._related.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Minimum,
        )
        self._related.itemDoubleClicked.connect(self._open_related)
        self._root.addWidget(self._related_label)
        self._root.addWidget(self._related)

        locate = QtWidgets.QPushButton("在星云中定位这条记忆")
        locate.setObjectName("primary")
        locate.clicked.connect(lambda: self.locate_requested.emit(self._current_id))
        self._root.addWidget(locate)

        actions = QtWidgets.QHBoxLayout()
        correct = QtWidgets.QPushButton("纠正这条记忆")
        correct.clicked.connect(self._on_correct)
        forget = QtWidgets.QPushButton("遗忘（退出所有召回）")
        forget.clicked.connect(self._on_forget)
        actions.addWidget(correct)
        actions.addWidget(forget)
        actions.addStretch(1)
        self._root.addLayout(actions)
        self._action_status = QtWidgets.QLabel("")
        self._action_status.setObjectName("hint")
        self._action_status.setWordWrap(True)
        self._root.addWidget(self._action_status)

        self._fields = _Section("查看完整原始字段")
        self._fields_text = QtWidgets.QTextEdit()
        self._fields_text.setReadOnly(True)
        self._fields_text.setMaximumHeight(200)
        self._fields.body.addWidget(self._fields_text)
        self._root.addWidget(self._fields)

        self._history = _Section("查看历史（最近召回审计轨迹）")
        self._history_text = QtWidgets.QTextEdit()
        self._history_text.setReadOnly(True)
        self._history_text.setMaximumHeight(180)
        self._history.body.addWidget(self._history_text)
        self._root.addWidget(self._history)

        self._root.addStretch(1)
        self.setWidget(content)
        self._clear()

    # ── 渲染 ──────────────────────────────────────────────────────────────
    @staticmethod
    def _replace(parent: QtWidgets.QWidget, widget: QtWidgets.QWidget) -> QtWidgets.QWidget:
        layout = parent.layout()
        if layout is not None:
            while layout.count():
                item = layout.takeAt(0)
                if item.widget():
                    old = item.widget()
                    old.hide()
                    old.setParent(None)   # 必须立刻脱离父级，否则旧表仍绘制并与新表叠字
                    old.deleteLater()
            layout.addWidget(widget)
            widget.adjustSize()
            parent.updateGeometry()
        return widget

    def _clear(self) -> None:
        self._clear_badges([("未选中记忆", "#71809A")])
        self._confidence.set_value(0.0)
        self._clear_evidence({})
        self._replace(self._provenance, _form([]))
        self._replace(self._timeline, _form([]))
        self._replace(self._plasticity, _form([]))
        self._related.clear()
        self._fields_text.setPlainText("")
        self._history_text.setPlainText("")
        self._stale.setVisible(False)
        self._reveal.setVisible(False)
        self._sensitive.blockSignals(True)
        self._sensitive.setChecked(False)
        self._sensitive.blockSignals(False)
        self._action_status.setText("")
        self._current = {}

    def _clear_badges(self, items: list[tuple[str, str]]) -> None:
        while self._badges.count():
            item = self._badges.takeAt(0)
            if item.widget():
                old = item.widget()
                old.hide()
                old.setParent(None)
                old.deleteLater()
        for text, color in items:
            self._badges.addWidget(_badge(text, color))
        self._badges.addStretch(1)

    def _clear_evidence(self, breakdown: dict[str, float]) -> None:
        while self._evidence.count():
            item = self._evidence.takeAt(0)
            if item.widget():
                old = item.widget()
                old.hide()
                old.setParent(None)
                old.deleteLater()
        labels = (
            ("来源", "provenance"), ("新鲜度", "freshness"),
            ("使用", "usage"), ("证据", "evidence"),
        )
        for title, key in labels:
            value = float(breakdown.get(key, 0.0))
            self._evidence.addWidget(
                _badge(f"{title} {_level(value)}", _color(value))
            )
        self._evidence.addStretch(1)

    def _render_title(self) -> None:
        if self._current.get("sensitive") and not self._revealed:
            self._title.setText("••••••　（敏感记忆已遮蔽，点上方按钮在当前上下文显示）")
        else:
            self._title.setText(str(self._current.get("content") or "（无正文）"))

    def _render_masked(self) -> None:
        detail = str(self._current.get("detail_text") or "")
        if self._current.get("sensitive") and not self._revealed:
            detail = "\n".join(
                "正文      ••••••（敏感内容已遮蔽）" if line.startswith("正文") else line
                for line in detail.splitlines()
            )
        self._fields_text.setPlainText(detail)

    def show_memory(self, memory_id: str) -> None:
        self._current_id = str(memory_id)
        data = self._backend.memory_inspect(memory_id)
        if not data:
            self._clear()
            self._title.setText("记忆不存在或已被封存")
            return
        self._current = dict(data)
        self._revealed = False
        self._action_status.setText("")
        sensitive = bool(data.get("sensitive"))
        self._sensitive.blockSignals(True)
        self._sensitive.setChecked(sensitive)
        self._sensitive.blockSignals(False)
        self._reveal.setVisible(sensitive)
        self._render_title()
        active = bool(data.get("is_active", True))
        items = [
            (str(data.get("memory_type", "fact")), "#6EC6FF"),
            (f"分层 {data.get('tier', 'buffer')}", "#B08CFF"),
            ("活跃" if active else "已封存", "#3DDC97" if active else "#FF6B6B"),
        ]
        if data.get("stale"):
            items.append((f"{data.get('age_days')} 天未确认", "#FFB84D"))
        self._clear_badges(items)

        self._stale.setVisible(bool(data.get("stale")))
        if data.get("stale"):
            self._stale.setText(
                f"⚠ 这条记忆已有 {data.get('age_days')} 天未被使用或确认，"
                "系统仍会按原权重参与召回。请核对内容是否仍然成立。"
            )
            self._stale.setStyleSheet(
                "QLabel { color: #FFB84D; background: #2A2313; border-radius: 4px; padding: 6px; }"
            )

        breakdown = dict(data.get("confidence_breakdown") or {})
        self._confidence.set_value(float(breakdown.get("overall", 0.0)))
        self._clear_evidence(breakdown)

        self._replace(self._provenance, _form([
            ("来源设备", data.get("device_id") or "—"),
            ("记录者", data.get("agent_id") or "—"),
            ("命名空间", data.get("namespace") or "—"),
            ("金字塔层", data.get("pyramid") or "—"),
            ("好感度", data.get("affinity") if data.get("affinity") is not None else "—"),
        ]))
        self._replace(self._timeline, _form([
            ("生效起", _fmt_ms(data.get("valid_from_ms"))),
            ("失效于", "至今" if not data.get("valid_to_ms") else _fmt_ms(data.get("valid_to_ms"))),
            ("写入 HLC", data.get("tx_from") or "—"),
            ("封存 HLC", data.get("tx_to") or "至今"),
            ("创建", _fmt_ms(data.get("created_at_ms"))),
            ("最后使用", _fmt_ms(data.get("last_recall_ms"))),
            ("距今", f"{data.get('age_days', 0)} 天"),
        ]))
        pad = (
            float(data.get("pad_p") or 0.0),
            float(data.get("pad_a") or 0.0),
            float(data.get("pad_d") or 0.0),
        )
        self._replace(self._plasticity, _form([
            ("P / A / D", f"{pad[0]:+.2f} / {pad[1]:+.2f} / {pad[2]:+.2f}"),
            ("置信度 / 强度", f"{data.get('confidence', 0)} / {data.get('strength', 0)}"),
            ("稳定性 / 难度", f"{data.get('stability', 0)} / {data.get('difficulty', 0)}"),
            ("召回次数", data.get("recall_count", 0)),
        ]))

        self._related.clear()
        for item in data.get("related") or []:
            row = QtWidgets.QListWidgetItem(
                f"{str(item.get('content', ''))[:40]}  ·  权重 {item.get('weight')}"
            )
            row.setData(QtCore.Qt.ItemDataRole.UserRole, str(item.get("id")))
            self._related.addItem(row)
        self._render_masked()

        audit = list(data.get("audit") or [])
        lines = [f"── 创建 / 纠正 / 封存轨迹（journal，共 {len(audit)} 条）──"]
        for item in audit:
            lines.append(
                f"[{item.get('type') or '事件'}]  操作者 {item.get('actor') or '—'}  ·  "
                f"理由 {item.get('reason') or '—'}  ·  "
                f"变更 {', '.join(item.get('changed') or []) or '—'}"
            )
        if sensitive:
            lines.append("（敏感记忆：原值快照不在此展开）")
        lines.append("── 最近使用（召回审计）──")
        for row in self._backend.recall_history(8):
            lines.append(
                f"{_fmt_ms(row.get('ts_ms'))}  {str(row.get('query', ''))[:24]}  "
                f"命中 {row.get('hits')}  ·  {row.get('latency_ms')} ms"
            )
        self._history_text.setPlainText("\n".join(lines) if len(lines) > 2 else "暂无事件")

    def _open_related(self, item: QtWidgets.QListWidgetItem) -> None:
        target = str(item.data(QtCore.Qt.ItemDataRole.UserRole) or "")
        if target:
            self.focus_requested.emit(target)

    # ── 用户控制 ──────────────────────────────────────────────────────────
    def _on_reveal(self) -> None:
        self._revealed = True
        self._reveal.setVisible(False)
        self._render_title()
        self._render_masked()

    def _on_sensitive_toggled(self, checked: bool) -> None:
        if not self._current_id:
            return
        self._backend.set_sensitive(self._current_id, bool(checked))
        self._current["sensitive"] = bool(checked)
        self._revealed = False
        self._reveal.setVisible(bool(checked))
        self._render_title()
        self._render_masked()
        self.changed.emit("")

    def _on_correct(self) -> None:
        if not self._current_id or not self._current:
            return
        dialog = _CorrectionDialog(self._current, self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        changes, reason = dialog.changes(), dialog.reason_text()
        memory_id = self._current_id
        self._action_status.setText("纠正中…（生成新版本并投影向量）")
        self._backend.tasks.submit(
            lambda: self._backend.correct_memory(memory_id, changes, reason=reason),
            self._on_corrected,
            lambda message: self._action_status.setText(f"纠正失败：{message}"),
        )

    def _on_corrected(self, result: dict[str, Any]) -> None:
        if not result.get("ok"):
            self._action_status.setText(f"纠正失败：{result.get('message')}")
            return
        self._action_status.setText(str(result.get("message") or "已纠正"))
        self.changed.emit(str(result.get("id") or ""))

    def _on_forget(self) -> None:
        if not self._current_id:
            return
        answer = QtWidgets.QMessageBox.question(
            self,
            "确认遗忘",
            "这条记忆将从所有召回通道移除（向量删除 + 封存）。\n"
            "主库保留封存记录与 journal 事件用于审计，不做物理删除，\n"
            "以保证跨设备重放不会把已遗忘内容复活。\n\n确认继续？",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
        )
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        memory_id = self._current_id
        self._action_status.setText("遗忘中…")
        self._backend.tasks.submit(
            lambda: self._backend.forget_memory(memory_id, reason="用户主动遗忘"),
            self._on_forgot,
            lambda message: self._action_status.setText(f"遗忘失败：{message}"),
        )

    def _on_forgot(self, result: dict[str, Any]) -> None:
        self._action_status.setText(str(result.get("message") or ""))
        if result.get("ok"):
            self.changed.emit("")
