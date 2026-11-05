"""ui.desktop.pages.settings_page —— 配置中心：预设 + 全量参数自动表单。

模型接入（本地/云端、API key、本地模型文件）在独立的「模型配置」页。
打包成软件后所有参数在这里改，写用户目录差异补丁，下次启动自动恢复；
引擎开关等启动期参数点「应用并重启内核」即时生效，无需重装。
"""
from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtWidgets

from ui.desktop.autostart import is_enabled as is_autostart_enabled
from ui.desktop.autostart import set_enabled as set_autostart
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


_STORAGE_PATHS = (
    "sqlite_path", "lancedb_path", "kuzu_path", "lmdb_path", "duckdb_path",
)


class SettingsPage(QtWidgets.QWidget):
    appearance_changed = QtCore.Signal()

    def __init__(self, backend: DesktopBackend, store: SettingsStore) -> None:
        super().__init__()
        self._backend = backend
        self._store = store

        # ── 存储位置（换盘 / 迁移）─────────────────────────────────────────
        self._storage_root = QtWidgets.QLineEdit(str(backend.storage_root()))
        self._storage_root.setToolTip("五个引擎的库都在这个目录里；换目录会同时改路径、复制数据并重启内核")
        pick = QtWidgets.QPushButton("浏览…")
        pick.clicked.connect(self._pick_storage_root)
        self._migrate = QtWidgets.QCheckBox("同时迁移现有数据（推荐：先关内核再复制，避免半截文件）")
        self._migrate.setChecked(True)
        apply_storage = QtWidgets.QPushButton("切换目录并重启内核")
        apply_storage.setObjectName("primary")
        apply_storage.clicked.connect(self._apply_storage_root)
        self._storage_status = QtWidgets.QLabel(f"当前生效：{backend.storage_root()}")
        self._storage_status.setObjectName("hint")
        self._storage_status.setWordWrap(True)
        storage_row = QtWidgets.QHBoxLayout()
        storage_row.addWidget(self._storage_root, 1)
        storage_row.addWidget(pick)
        storage_card = Card("数据保存位置（一个目录，改一次五个库全跟着走）")
        storage_note = QtWidgets.QLabel(
            "六个引擎的数据都放在这一个目录下（下图列出实际文件），不需要分别填五个路径。"
            "点「切换目录并重启内核」会一次完成：写入配置 → 关内核 → 复制数据 → 重启 → 回读；失败自动回滚。"
        )
        storage_note.setObjectName("hint")
        storage_note.setWordWrap(True)
        self._derived = QtWidgets.QLabel()
        self._derived.setObjectName("hint")
        self._derived.setWordWrap(True)
        self._derived.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self._refresh_derived_paths()
        self._storage_root.textChanged.connect(self._refresh_derived_paths)
        storage_card.body.addWidget(storage_note)
        storage_card.body.addLayout(storage_row)
        storage_card.body.addWidget(self._derived)
        storage_card.body.addWidget(self._migrate)
        storage_card.body.addWidget(apply_storage)
        storage_card.body.addWidget(self._storage_status)

        # ── 启动与驻留 ────────────────────────────────────────────────────
        self._autostart = QtWidgets.QCheckBox("开机自启（登录后直接驻留托盘，不弹窗）")
        self._autostart.setToolTip(
            "写入当前用户的 Run 注册表项，命令带 --tray：开机只在托盘里跑，点图标才进前端。\n"
            "任何时候叉掉窗口都只是隐藏到托盘，引擎与 MCP 接入继续在线；"
            "彻底退出请右键托盘图标 →「完全退出」。"
        )
        self._autostart.setChecked(is_autostart_enabled())
        self._autostart.toggled.connect(self._toggle_autostart)
        residency_note = QtWidgets.QLabel(
            "关闭窗口 = 隐藏到托盘（后台常驻，MCP / HTTP 不中断）；"
            "重复双击图标会把已在运行的窗口唤到前台，不会再起第二个进程。"
        )
        residency_note.setObjectName("hint")
        residency_note.setWordWrap(True)
        residency_card = Card("启动与驻留")
        residency_card.body.addWidget(self._autostart)
        residency_card.body.addWidget(residency_note)

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
        layout.addWidget(storage_card)
        layout.addWidget(residency_card)
        layout.addWidget(preset_card)
        layout.addWidget(scroll, 1)
        layout.addLayout(actions)
        layout.addWidget(self._status)

    def _build_editor(self) -> ConfigEditor:
        config = self._backend.config
        return ConfigEditor(
            (
                # (卡片标题, 配置键, 子模型)：配置键必须与 BrainConfig 字段名一致，否则保存不落盘
                ("存储引擎", "storage", config.storage),
                ("语义合并与聚类", "merge", config.merge),
                ("召回与精排", "recall", config.recall),
                ("遗忘与强化（FSRS）", "plasticity", config.plasticity),
                ("分层流动", "tier", config.tier),
                ("丘脑准入与去重", "thalamus", config.thalamus),
                ("情感阈值（PAD / 冷静期）", "amygdala", config.amygdala),
                ("同步参数", "sync", config.sync),
                ("网关", "gateway", config.gateway),
                ("星云激活", "activation", config.activation),
                ("外观与动画", "ui", config.ui),
            ),
            excludes={
                "同步参数": {"repo_url"},
                "网关": {"mcp_token_secret"},
                # 存储路径只给一个「数据保存位置」入口：五个库都在同一目录，改一次全部跟随
                "存储引擎": set(_STORAGE_PATHS),
                "外观与动画": _UI_EXCLUDE,
            },
            advanced=_ADVANCED_FIELDS,
        )

    # ── 动作 ──────────────────────────────────────────────────────────────
    def _refresh_derived_paths(self) -> None:
        root = self._storage_root.text().strip() or str(self._backend.storage_root())
        base = Path(root)
        items = [
            base / "relational.db", base / "vector.lance", base / "graph.kuzu",
            base / "kv.lmdb", base / "timeseries.db", base / "brain-sync",
        ]
        self._derived.setText("这个目录下会用到：" + "　".join(str(item) for item in items))

    def _pick_storage_root(self) -> None:
        current = self._storage_root.text().strip() or str(self._backend.storage_root())
        chosen = QtWidgets.QFileDialog.getExistingDirectory(self, "选择数据目录", current)
        if chosen:
            self._storage_root.setText(chosen)

    def _apply_storage_root(self) -> None:
        target = self._storage_root.text().strip()
        if not target:
            self._storage_status.setText("请先选择目标目录")
            return
        self._storage_status.setText("正在切换：关闭内核 → 复制数据 → 重启……")
        QtWidgets.QApplication.processEvents()
        result = self._backend.set_storage_root(target, migrate=self._migrate.isChecked())
        self._storage_status.setText(str(result["message"]))
        if result.get("ok"):
            self._storage_root.setText(str(result["root"]))
            self._reload_editor()          # 五个路径字段同步成新目录
            self.appearance_changed.emit()

    def _toggle_autostart(self, checked: bool) -> None:
        if set_autostart(checked):
            self._status.setText(
                "已开启开机自启（登录后驻留托盘）" if checked else "已关闭开机自启"
            )
            return
        # 写注册表失败：把勾选状态拨回去，别让界面撒谎
        self._autostart.blockSignals(True)
        self._autostart.setChecked(not checked)
        self._autostart.blockSignals(False)
        self._status.setText(
            "开机自启设置失败：请检查当前用户是否有权限写 "
            r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
        )

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
        message = str(result["message"])
        # 存储路径属于启动期参数，且换了库不会自动搬数据 —— 必须说清楚，否则用户以为「没反应」
        if any(str(name).startswith("storage.") for name in result.get("restart_needed") or []):
            message += "\n存储路径已改：点「应用并重启内核」后使用新库，旧数据不会自动搬移（需手工拷 data 目录）。"
        self._status.setText(message)
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
            # setWidget 会把旧 widget 交给 Qt 销毁，随后再 deleteLater 会抛
            # RuntimeError: Internal C++ object already deleted —— 重建两次必现。
            scroll.setWidget(self._editor)
        try:
            old.deleteLater()
        except RuntimeError:  # noqa: PERF203  已被 Qt 接管销毁
            pass

