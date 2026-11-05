"""ui.desktop.pages.data_page —— 展示数据：六个面板的原始快照 + 导出。"""
from __future__ import annotations

import json

from PySide6 import QtWidgets

from ui.desktop.backend import DesktopBackend

__all__ = ["DataPage"]

_PANELS = (
    ("engine_health", "引擎健康"),
    ("memory_stream", "记忆流"),
    ("pad_axis", "PAD 轴"),
    ("synapse_topology", "突触拓扑"),
    ("tier_distribution", "分层分布"),
    ("sync_status", "同步状态"),
)


class DataPage(QtWidgets.QWidget):
    def __init__(self, backend: DesktopBackend) -> None:
        super().__init__()
        self._backend = backend
        self._editors: dict[str, QtWidgets.QTextEdit] = {}

        self._tabs = QtWidgets.QTabWidget()
        for key, title in _PANELS:
            editor = QtWidgets.QTextEdit()
            editor.setReadOnly(True)
            editor.setLineWrapMode(QtWidgets.QTextEdit.LineWrapMode.NoWrap)
            editor.setStyleSheet("font-family: Consolas, 'Cascadia Mono', monospace; font-size: 11px;")
            self._editors[key] = editor
            self._tabs.addTab(editor, title)

        refresh = QtWidgets.QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        export_json = QtWidgets.QPushButton("导出全量快照 JSON")
        export_json.setObjectName("primary")
        export_json.clicked.connect(self._export_json)
        export_memories = QtWidgets.QPushButton("导出记忆 NDJSON")
        export_memories.clicked.connect(self._export_memories)
        actions = QtWidgets.QHBoxLayout()
        actions.addWidget(refresh)
        actions.addWidget(export_json)
        actions.addWidget(export_memories)
        actions.addStretch(1)

        self._status = QtWidgets.QLabel("—")
        self._status.setObjectName("hint")

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 12)
        layout.addLayout(actions)
        layout.addWidget(self._tabs, 1)
        layout.addWidget(self._status)
        self.refresh()

    def refresh(self) -> None:
        snapshot = self._backend.snapshot()
        for key, editor in self._editors.items():
            editor.setPlainText(json.dumps(snapshot.get(key, {}), ensure_ascii=False, indent=2, default=str))
        self._status.setText("快照已刷新（只读；数据源 ui.panels 纯函数，不碰数据库文件）")

    def _export_json(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "导出全量快照", "brain_snapshot.json", "JSON (*.json)"
        )
        if path:
            self._status.setText(f"已导出：{self._backend.export_dashboard(path)}")

    def _export_memories(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "导出记忆", "memories.ndjson", "NDJSON (*.ndjson)"
        )
        if path:
            self._status.setText(f"已导出记忆 NDJSON：{self._backend.export_memories(path)}")
