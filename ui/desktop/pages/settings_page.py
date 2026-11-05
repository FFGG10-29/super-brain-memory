"""ui.desktop.pages.settings_page —— 可视化参数：刷新率 / 相似度阈值 / 标签数量。

参数落用户级 QSettings 并即时写回 `config`，不散落在视图代码里。
"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ui.desktop.backend import DesktopBackend
from ui.desktop.settings import SettingsStore
from ui.desktop.widgets import Card

__all__ = ["SettingsPage"]


class SettingsPage(QtWidgets.QWidget):
    appearance_changed = QtCore.Signal()

    def __init__(self, backend: DesktopBackend, store: SettingsStore) -> None:
        super().__init__()
        self._backend = backend
        self._store = store
        prefs = store.load_prefs()

        self._interval = QtWidgets.QSpinBox()
        self._interval.setRange(8, 120)
        self._interval.setSuffix(" ms")
        self._interval.setValue(int(prefs["timer_interval_ms"]))
        self._threshold = QtWidgets.QDoubleSpinBox()
        self._threshold.setRange(0.05, 0.95)
        self._threshold.setSingleStep(0.01)
        self._threshold.setDecimals(2)
        self._threshold.setValue(float(prefs["similarity_threshold"]))
        self._labels = QtWidgets.QSpinBox()
        self._labels.setRange(0, 40)
        self._labels.setValue(int(prefs["max_labels"]))

        form = QtWidgets.QFormLayout()
        form.addRow("动画帧间隔", self._interval)
        form.addRow("语义突触阈值（余弦）", self._threshold)
        form.addRow("同屏最多标签", self._labels)

        save = QtWidgets.QPushButton("保存并应用")
        save.setObjectName("primary")
        save.clicked.connect(self.save)
        self._status = QtWidgets.QLabel("—")
        self._status.setObjectName("hint")

        animation_card = Card("星云渲染")
        animation_card.body.addLayout(form)
        animation_card.body.addWidget(save)
        animation_card.body.addWidget(self._status)

        notes = Card("参数说明")
        hint = QtWidgets.QLabel(
            "帧间隔越小越顺滑，但 CPU 占用越高（默认 16 ms ≈ 60fps）。\n"
            "语义突触阈值越低，画出的相似记忆连线越多；阈值只影响可视化，不影响召回分数。\n"
            "标签数量控制同屏文字气泡，按「情绪记忆 > 高置信度」优先级点亮。\n"
            "所有参数写入用户级 QSettings，并即时回写 config，重启后自动恢复。"
        )
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        notes.body.addWidget(hint)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 12)
        layout.addWidget(animation_card)
        layout.addWidget(notes)
        layout.addStretch(1)

    def save(self) -> None:
        prefs = self._store.save_prefs(
            timer_interval_ms=int(self._interval.value()),
            similarity_threshold=float(self._threshold.value()),
            max_labels=int(self._labels.value()),
        )
        self._status.setText(f"已保存：{prefs}")
        self.appearance_changed.emit()
