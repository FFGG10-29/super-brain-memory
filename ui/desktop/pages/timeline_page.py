"""ui.desktop.pages.timeline_page —— 事件视角的记忆时间轴。

借鉴 claude-mem Web Viewer 的「实时观察流 + 会话时间线」与 OpenMemory 的
timeline 页：以 journal 的 HLC 全序事件为主体（不是记忆的 valid_from 视角），
按会话间隔分组、按 EventType 筛选、双击事件直达记忆检查器。
"""
from __future__ import annotations

import time

from PySide6 import QtCore, QtWidgets

from ui.desktop.backend import DesktopBackend

__all__ = ["TimelinePage"]

_SESSION_GAP_MS = 30 * 60 * 1000
_TYPE_LABELS = {
    "ingest": "写入", "supersede": "纠正/替代", "reinforce": "强化",
    "tier_change": "分层变更", "consolidate": "巩固", "policy": "配置",
    "tombstone": "遗忘/封存", "recall": "召回",
}


def _fmt(ms: int) -> str:
    if not ms:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000))


class TimelinePage(QtWidgets.QWidget):
    focus_requested = QtCore.Signal(str)

    def __init__(self, backend: DesktopBackend) -> None:
        super().__init__()
        self._backend = backend
        self._types = ["全部事件", *backend.event_types(), "recall"]

        self._type = QtWidgets.QComboBox()
        self._type.addItems(self._types)
        self._type.currentIndexChanged.connect(self.refresh)
        self._auto = QtWidgets.QCheckBox("自动刷新（3 秒）")
        self._auto.setChecked(True)
        refresh = QtWidgets.QPushButton("立即刷新")
        refresh.clicked.connect(self.refresh)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("事件类型"))
        top.addWidget(self._type)
        top.addWidget(self._auto)
        top.addWidget(refresh)
        top.addStretch(1)

        self._tree = QtWidgets.QTreeWidget()
        self._tree.setHeaderLabels(["时间 / 会话", "类型", "操作者", "事件摘要", "关联记忆"])
        self._tree.setAlternatingRowColors(True)
        self._tree.setColumnWidth(0, 180)
        self._tree.setColumnWidth(3, 420)
        self._tree.itemDoubleClicked.connect(self._on_double_click)

        self._status = QtWidgets.QLabel("—")
        self._status.setObjectName("hint")

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 12)
        layout.addLayout(top)
        layout.addWidget(self._tree, 1)
        layout.addWidget(self._status)

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(3000)
        self._timer.timeout.connect(self._auto_refresh)
        self._timer.start()
        self.refresh()

    def stop(self) -> None:
        self._timer.stop()

    def _auto_refresh(self) -> None:
        if self._auto.isChecked() and self.isVisible() and not self._backend.tasks.busy:
            self.refresh()

    def refresh(self) -> None:
        selected = self._type.currentText()
        types = () if selected == "全部事件" else (selected,)
        events = self._backend.event_timeline(types=types, limit=300)
        self._tree.clear()

        groups: list[list[dict]] = []
        current: list[dict] = []
        last_ts = 0
        for event in events:  # event_timeline 已按时间倒序
            ts = int(event.get("ts_ms") or 0)
            if current and last_ts - ts > _SESSION_GAP_MS:
                groups.append(current)
                current = []
            current.append(event)
            last_ts = ts
        if current:
            groups.append(current)

        for group in groups:
            newest, oldest = group[0], group[-1]
            header = QtWidgets.QTreeWidgetItem([
                f"{_fmt(int(oldest.get('ts_ms') or 0))} → {_fmt(int(newest.get('ts_ms') or 0))}",
                f"{len(group)} 条事件", "", "会话段（30 分钟内连续活动）", "",
            ])
            header.setFirstColumnSpanned(True)
            self._tree.addTopLevelItem(header)
            for event in group:
                kind = str(event.get("type") or "")
                memory_id = str(event.get("memory_id") or "")
                child = QtWidgets.QTreeWidgetItem([
                    _fmt(int(event.get("ts_ms") or 0)),
                    _TYPE_LABELS.get(kind, kind),
                    str(event.get("actor") or event.get("device_id") or "")[:18],
                    str(event.get("summary") or kind),
                    memory_id[:12],
                ])
                child.setData(0, QtCore.Qt.ItemDataRole.UserRole, memory_id)
                header.addChild(child)
        self._tree.expandAll()
        self._status.setText(
            f"共 {len(events)} 条事件 / {len(groups)} 个会话段 · "
            f"数据源 journal（append-only + fsync，HLC 全序）"
        )

    def _on_double_click(self, item: QtWidgets.QTreeWidgetItem, _column: int) -> None:
        memory_id = str(item.data(0, QtCore.Qt.ItemDataRole.UserRole) or "")
        if memory_id:
            self.focus_requested.emit(memory_id)
