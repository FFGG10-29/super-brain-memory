"""ui.desktop.pages.settings_page —— 配置中心：预设 + 全量参数自动表单。

模型接入（本地/云端、API key、本地模型文件）在独立的「模型配置」页。
打包成软件后所有参数在这里改，写用户目录差异补丁，下次启动自动恢复；
引擎开关等启动期参数点「应用并重启内核」即时生效，无需重装。
"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ui.desktop.backend import DesktopBackend
from ui.desktop.settings import SettingsStore
from ui.desktop.user_config import PRESET_NOTES, preset_names
from ui.desktop.widgets import Card, ConfigEditor

__all__ = ["SettingsPage"]

_PRESET_TITLES = {
    "balanced": "均衡默认（出厂 · 中肯推荐）",
    "igpu": "核显 / 无独显（省电流畅）",
    "dgpu": "独显（全火力）",
}

_UI_EXCLUDE = {
    "background_color", "node_color", "emotion_node_color", "line_color", "pulse_color",
    "stardust_color", "label_color", "center_x", "center_y", "center_pull",
    "repulsion_distance", "repulsion_force", "friction", "proximity_line_alpha",
    "normal_node_radius", "emotion_node_radius",
}


# 默认折叠的精调参数（勾选「显示高级参数」展开；出厂值已经过评测）
_ADVANCED_FIELDS: dict[str, set[str]] = {
    "召回与精排": {
        "rrf_k_oversample", "rrf_constant", "w_rrf", "w_freshness", "w_synapse",
        "w_pad", "w_confidence", "p95_target_ms", "pad_salience_arousal_weight",
    },
    "遗忘与强化（FSRS）": {
        "fsrs_difficulty_default", "fsrs_stability_default", "fsrs_decay", "fsrs_power",
        "reinforce_threshold", "consolidate_confidence", "recall_layer_ceiling",
        "review_queue_ceiling", "fsrs_arousal_boost",
    },
    "分层流动": {"archive_retrievability_threshold"},
    "丘脑准入与去重": {
        "significance_length_weight", "significance_pad_weight",
        "significance_confidence_weight", "significance_length_ref", "dedupe_window",
    },
    "情感阈值（PAD / 冷静期）": {"affinity_alpha", "pad_smoothing"},
    "同步参数": {"segment_max_bytes", "schema_version"},
    "存储引擎": {
        "sqlite_busy_timeout_ms", "wal_autocheckpoint",
        "lmdb_map_size_mb", "lmdb_max_buckets", "duckdb_threads",
    },
}


class SettingsPage(QtWidgets.QWidget):
    appearance_changed = QtCore.Signal()

    def __init__(self, backend: DesktopBackend, store: SettingsStore) -> None:
        super().__init__()
        self._backend = backend
        self._store = store

        # ── 预设 ──────────────────────────────────────────────────────────
        self._preset = QtWidgets.QComboBox()
        for name in preset_names():
            self._preset.addItem(_PRESET_TITLES.get(name, name), name)
        self._preset_note = QtWidgets.QLabel("")
        self._preset_note.setObjectName("hint")
        self._preset_note.setWordWrap(True)
        self._preset.currentIndexChanged.connect(self._show_preset_note)
        apply_preset = QtWidgets.QPushButton("应用预设")
        apply_preset.setObjectName("primary")
        apply_preset.setToolTip("把该预设的一组参数写入配置并立即生效；存储/模型参数需再点「应用并重启内核」")
        apply_preset.clicked.connect(self._apply_preset)
        preset_row = QtWidgets.QHBoxLayout()
        preset_row.addWidget(QtWidgets.QLabel("一键预设"))
        preset_row.addWidget(self._preset, 1)
        preset_row.addWidget(apply_preset)
        preset_card = Card("预设方案 —— 按你的硬件选一个起点，之后任意参数都可微调（模型在「模型配置」页）")
        preset_card.body.addLayout(preset_row)
        preset_card.body.addWidget(self._preset_note)
        self._show_preset_note()

        # ── 全量参数（自动表单）──────────────────────────────────────────
        self._editor = self._build_editor()
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._editor)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)

        # ── 操作 ──────────────────────────────────────────────────────────
        save = QtWidgets.QPushButton("保存配置")
        save.setObjectName("primary")
        save.setToolTip("把所有参数写入用户配置文件并即时生效；悬停任意参数名可查看它管什么")
        save.clicked.connect(self.save)
        restart = QtWidgets.QPushButton("应用并重启内核")
        restart.setObjectName("primary")
        restart.setToolTip("引擎开关等启动期参数需要这一步；记忆数据不会丢")
        restart.clicked.connect(self.restart_kernel)
        reset = QtWidgets.QPushButton("恢复出厂默认")
        reset.setObjectName("danger")
        reset.setToolTip("清除全部自定义参数，回到出厂中肯值；记忆数据不受影响")
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
        layout.addWidget(scroll, 1)
        layout.addLayout(actions)
        layout.addWidget(self._status)

    def _build_editor(self) -> ConfigEditor:
        config = self._backend.config
        return ConfigEditor(
            (
                ("存储引擎", config.storage),
                ("语义合并与聚类", config.merge),
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
            advanced=_ADVANCED_FIELDS,
        )

    # ── 动作 ──────────────────────────────────────────────────────────────
    def _show_preset_note(self) -> None:
        name = str(self._preset.currentData())
        self._preset_note.setText(PRESET_NOTES.get(name, ""))

    def _apply_preset(self) -> None:
        name = str(self._preset.currentData())
        result = self._backend.apply_preset(name)
        self._status.setText(str(result["message"]))
        if result.get("ok"):
            self._reload_editor()
            self.appearance_changed.emit()

    def save(self) -> None:
        result = self._backend.save_settings_patch(self._editor.collect())
        self._status.setText(str(result["message"]))
        self.appearance_changed.emit()

    def restart_kernel(self) -> None:
        result = self._backend.restart()
        self._status.setText(str(result["message"]))
        if result.get("ok"):
            self.appearance_changed.emit()

    def reset_defaults(self) -> None:
        result = self._backend.reset_settings()
        self._status.setText(str(result["message"]))
        self._reload_editor()

    def _reload_editor(self) -> None:
        """预设 / 恢复出厂后：用当前 config 值重建表单。"""
        old = self._editor
        self._editor = self._build_editor()
        scroll = self.findChild(QtWidgets.QScrollArea)
        if scroll is not None:
            scroll.setWidget(self._editor)
        old.deleteLater()
