"""ui.desktop.pages.settings_page —— 配置中心：预设 / 全量参数 / 云端模型 / 内核重启。

打包成软件后，所有参数在这里改，写用户目录差异补丁（%APPDATA%/super-brain/config.json），
下次启动自动恢复；存储开关、模型来源等启动期参数点「应用并重启内核」即时生效，无需重装。
密钥（GitHub PAT / 云端 API key）只进系统钥匙串，绝不进配置文件。
"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from config import BrainConfig
from ui.desktop.backend import DesktopBackend
from ui.desktop.settings import SettingsStore
from ui.desktop.user_config import preset_names
from ui.desktop.widgets import Card, ConfigEditor

__all__ = ["SettingsPage"]

_PRESET_TITLES = {"balanced": "均衡默认（出厂）", "performance": "性能全开", "lite": "低配省电"}

_UI_EXCLUDE = {
    "background_color", "node_color", "emotion_node_color", "line_color", "pulse_color",
    "stardust_color", "label_color", "center_x", "center_y", "center_pull",
    "repulsion_distance", "repulsion_force", "friction", "proximity_line_alpha",
    "normal_node_radius", "emotion_node_radius",
}


class SettingsPage(QtWidgets.QWidget):
    appearance_changed = QtCore.Signal()
    kernel_restarted = QtCore.Signal()

    def __init__(self, backend: DesktopBackend, store: SettingsStore) -> None:
        super().__init__()
        self._backend = backend
        self._store = store

        # ── 预设 ──────────────────────────────────────────────────────────
        self._preset = QtWidgets.QComboBox()
        for name in preset_names():
            self._preset.addItem(_PRESET_TITLES.get(name, name), name)
        apply_preset = QtWidgets.QPushButton("应用预设")
        apply_preset.setObjectName("primary")
        apply_preset.clicked.connect(self._apply_preset)
        preset_row = QtWidgets.QHBoxLayout()
        preset_row.addWidget(QtWidgets.QLabel("一键预设"))
        preset_row.addWidget(self._preset, 1)
        preset_row.addWidget(apply_preset)
        preset_card = Card("预设方案（应用后可再微调任意参数）")
        preset_card.body.addLayout(preset_row)

        # ── 云端模型密钥 ──────────────────────────────────────────────────
        self._cloud_key = QtWidgets.QLineEdit()
        self._cloud_key.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self._cloud_key.setPlaceholderText("OpenAI 兼容 API key；留空=不改动")
        save_key = QtWidgets.QPushButton("保存到钥匙串")
        save_key.clicked.connect(self._save_cloud_key)
        status = self._backend.model_status()
        key_hint = QtWidgets.QLabel(
            f"当前状态：云端地址 {'已配置' if status['cloud_ready'] else '未配置'} · "
            f"key {'已存钥匙串/环境变量' if status['cloud_key_present'] else '未配置'}"
        )
        key_hint.setObjectName("hint")
        key_card = Card("云端模型密钥（系统钥匙串；不写配置文件/事件日志）")
        key_row = QtWidgets.QHBoxLayout()
        key_row.addWidget(self._cloud_key, 1)
        key_row.addWidget(save_key)
        key_card.body.addLayout(key_row)
        key_card.body.addWidget(key_hint)

        # ── 全量参数（自动表单）──────────────────────────────────────────
        config = backend.config
        self._editor = ConfigEditor(
            (
                ("存储引擎", config.storage),
                ("模型（本地 / 云端）", config.model),
                ("召回与精排", config.recall),
                ("遗忘与强化（FSRS）", config.plasticity),
                ("分层流动", config.tier),
                ("丘脑准入与去重", config.thalamus),
                ("情感阈值（PAD / 冷静期）", config.amygdala),
                ("同步参数", config.sync),
                ("网关", config.gateway),
                ("星云激活", config.activation),
                ("外观与动画", config.ui),
            ),
            excludes={
                "同步参数": {"repo_url"},
                "网关": {"mcp_token_secret"},
                "外观与动画": _UI_EXCLUDE,
            },
        )
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._editor)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)

        # ── 操作 ──────────────────────────────────────────────────────────
        save = QtWidgets.QPushButton("保存配置")
        save.setObjectName("primary")
        save.clicked.connect(self.save)
        restart = QtWidgets.QPushButton("应用并重启内核")
        restart.setObjectName("primary")
        restart.clicked.connect(self.restart_kernel)
        reset = QtWidgets.QPushButton("恢复出厂默认")
        reset.setObjectName("danger")
        reset.clicked.connect(self.reset_defaults)
        actions = QtWidgets.QHBoxLayout()
        actions.addWidget(save)
        actions.addWidget(restart)
        actions.addWidget(reset)
        actions.addStretch(1)

        self._status = QtWidgets.QLabel("—")
        self._status.setObjectName("hint")
        self._status.setWordWrap(True)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 12)
        layout.addWidget(preset_card)
        layout.addWidget(key_card)
        layout.addWidget(scroll, 1)
        layout.addLayout(actions)
        layout.addWidget(self._status)

    # ── 动作 ──────────────────────────────────────────────────────────────
    def _apply_preset(self) -> None:
        name = str(self._preset.currentData())
        result = self._backend.apply_preset(name)
        self._status.setText(str(result["message"]))
        if result.get("ok"):
            self._reload_editor()
            self.appearance_changed.emit()

    def _save_cloud_key(self) -> None:
        result = self._backend.save_cloud_key(self._cloud_key.text())
        self._cloud_key.clear()
        self._status.setText(str(result["message"]))

    def save(self) -> None:
        result = self._backend.save_settings_patch(self._editor.collect())
        self._status.setText(str(result["message"]))
        self.appearance_changed.emit()

    def restart_kernel(self) -> None:
        result = self._backend.restart()
        self._status.setText(
            f"{result['message']}（当前模型：{self._backend.model_status()['embedder']['kind']}）"
        )
        if result.get("ok"):
            self.kernel_restarted.emit()
            self.appearance_changed.emit()

    def reset_defaults(self) -> None:
        result = self._backend.reset_settings()
        self._status.setText(str(result["message"]))
        self._reload_editor()

    def _reload_editor(self) -> None:
        """用当前 config 值重建表单（预设/重置后刷新显示）。"""
        layout = self.layout()
        assert layout is not None
        layout.replaceWidget(self._editor, QtWidgets.QWidget())
        self._editor.deleteLater()
        config = self._backend.config
        self._editor = ConfigEditor(
            (
                ("存储引擎", config.storage),
                ("模型（本地 / 云端）", config.model),
                ("召回与精排", config.recall),
                ("遗忘与强化（FSRS）", config.plasticity),
                ("分层流动", config.tier),
                ("丘脑准入与去重", config.thalamus),
                ("情感阈值（PAD / 冷静期）", config.amygdala),
                ("同步参数", config.sync),
                ("网关", config.gateway),
                ("星云激活", config.activation),
                ("外观与动画", config.ui),
            ),
            excludes={
                "同步参数": {"repo_url"},
                "网关": {"mcp_token_secret"},
                "外观与动画": _UI_EXCLUDE,
            },
        )
        scroll_index = next(
            (index for index in range(layout.count()) if layout.itemAt(index).widget() is not None
             and isinstance(layout.itemAt(index).widget(), QtWidgets.QScrollArea)),
            None,
        )
        if scroll_index is not None:
            scroll_item = layout.itemAt(scroll_index)
            assert scroll_item is not None
            scroll_widget = scroll_item.widget()
            assert isinstance(scroll_widget, QtWidgets.QScrollArea)
            scroll_widget.setWidget(self._editor)
