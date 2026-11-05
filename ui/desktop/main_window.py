"""ui.desktop.main_window —— 主窗口：星云视图 + 记忆查看 + 安全配置 + 引擎/同步状态。"""
from __future__ import annotations

import json

from PySide6 import QtCore, QtWidgets

from config import BrainConfig
from ui.desktop.backend import DesktopBackend
from ui.desktop.memory_panel import MemoryPanel
from ui.desktop.neurons import NebulaView
from ui.desktop.settings import SettingsStore
from ui.desktop.settings_panel import SettingsPanel

__all__ = ["MainWindow"]


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, backend: DesktopBackend, store: SettingsStore, config: BrainConfig | None = None) -> None:
        super().__init__()
        self._backend = backend
        self._config = config or backend.config
        self.setWindowTitle("超级大脑 v2.1 · 突触激活星云")
        self.resize(self._config.ui.window_width, self._config.ui.window_height)
        self.setStyleSheet(
            f"QMainWindow, QWidget {{ background-color: {self._config.ui.background_color}; color: #D8E6FF; }}"
            "QPushButton { background-color: #16263F; border: 1px solid #2A4B7C; padding: 5px 12px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #21406B; }"
            "QLineEdit, QTextEdit, QListWidget { background-color: #0B1424; border: 1px solid #213A5E; }"
            "QLabel { color: #A8C6F0; }"
        )

        self._nebula = NebulaView(self._config)
        self._memory_panel = MemoryPanel(backend, self._nebula)
        self._settings_panel = SettingsPanel(store)
        self._status_text = QtWidgets.QTextEdit()
        self._status_text.setReadOnly(True)

        self._tabs = QtWidgets.QTabWidget()
        self._tabs.addTab(self._memory_panel, "记忆查看 / 突触激活")
        self._tabs.addTab(self._settings_panel, "PAT 与同步配置")
        self._tabs.addTab(self._status_text, "引擎与同步状态")

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        splitter.addWidget(self._nebula)
        splitter.addWidget(self._tabs)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        self.setCentralWidget(splitter)

        self._status_label = QtWidgets.QLabel()
        self.statusBar().addWidget(self._status_label)

        toolbar = self.addToolBar("操作")
        refresh_action = toolbar.addAction("刷新图谱")
        refresh_action.triggered.connect(self._memory_panel.refresh)
        pulse_action = toolbar.addAction("演示脉冲")
        pulse_action.triggered.connect(self.demo_pulse)
        status_action = toolbar.addAction("刷新状态")
        status_action.triggered.connect(self.refresh_status)

        self._status_timer = QtCore.QTimer(self)
        self._status_timer.timeout.connect(self.refresh_status)
        self._status_timer.start(5000)
        self.refresh_status()

    # ── 状态 ──────────────────────────────────────────────────────────────
    def refresh_status(self) -> None:
        snapshot = self._backend.snapshot()
        self._status_text.setPlainText(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str))
        engines = snapshot["engine_health"]["engines"]
        ok = sum(1 for item in engines if item["ok"])
        sync = snapshot["sync_status"]
        self._status_label.setText(
            f"引擎 {ok}/{len(engines)} 正常 · 记忆 {snapshot['memory_stream']['count']} 条 · "
            f"PAD 样本 {snapshot['pad_axis']['summary']['samples']} · "
            f"待重放事件 {sync['pending_events']} · 光脉冲 {self._nebula.pulse_count()}"
        )

    def demo_pulse(self) -> None:
        """无检索时也能看效果：挑当前图谱里最相似的一对发射光点。"""
        graph = self._backend.graph()
        if not graph.edges:
            self._status_label.setText("暂无突触边：先写入几条相关记忆")
            return
        edge = max(graph.edges, key=lambda item: item.weight)
        emitted = self._nebula.emit_pulses(edge.source, [(edge.target, edge.weight)])
        self._status_label.setText(f"演示脉冲：相似度 {edge.weight:.2f} → 发射 {emitted} 个光点")

    def closeEvent(self, event) -> None:  # noqa: N802
        self._status_timer.stop()
        self._nebula.timer.stop()
        self._backend.close()
        super().closeEvent(event)
