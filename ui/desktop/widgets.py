"""ui.desktop.widgets —— 通用卡片、KPI、迷你地图、自动配置表单。"""
from __future__ import annotations

import types as _types
import typing
from collections.abc import Sequence
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

from pydantic import BaseModel
from PySide6 import QtCore, QtGui, QtWidgets

__all__ = ["Card", "ConfigEditor", "KpiCard", "MiniMap", "divider", "FIELD_LABELS"]


def divider() -> QtWidgets.QFrame:
    line = QtWidgets.QFrame()
    line.setObjectName("divider")
    line.setFixedHeight(1)
    return line


class Card(QtWidgets.QFrame):
    """标题 + 内容区的圆角面板。内容请加到 `card.body`。"""

    def __init__(self, title: str = "", parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(10)
        if title:
            label = QtWidgets.QLabel(title)
            label.setObjectName("cardTitle")
            layout.addWidget(label)
        self.body = QtWidgets.QVBoxLayout()
        self.body.setSpacing(8)
        layout.addLayout(self.body)


class KpiCard(QtWidgets.QFrame):
    def __init__(self, caption: str, value: str = "—", parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(2)
        self._value = QtWidgets.QLabel(value)
        self._value.setObjectName("kpiValue")
        self._caption = QtWidgets.QLabel(caption)
        self._caption.setObjectName("kpiCaption")
        layout.addWidget(self._value)
        layout.addWidget(self._caption)

    def set_value(self, value: object) -> None:
        self._value.setText(str(value))

    def set_accent(self, color: str) -> None:
        self._value.setStyleSheet(f"color: {color};")


class MiniMap(QtWidgets.QGraphicsView):
    """右下角缩略图：共享主场景 + 视口矩形指示。"""

    def __init__(self, scene: QtWidgets.QGraphicsScene, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(scene, parent)
        self.setFixedSize(190, 126)
        self.setInteractive(False)
        self.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
        for policy in (QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff,):
            self.setHorizontalScrollBarPolicy(policy)
            self.setVerticalScrollBarPolicy(policy)
        self.setStyleSheet(
            "background-color: rgba(7, 10, 16, 205); border: 1px solid #1E2B3F; border-radius: 8px;"
        )
        self._area = QtCore.QRectF()

    def refresh(self) -> None:
        self.fitInView(self.sceneRect(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)
        self.viewport().update()

    def set_view_area(self, rect: QtCore.QRectF) -> None:
        self._area = QtCore.QRectF(rect)
        self.viewport().update()

    def showEvent(self, event: QtGui.QShowEvent) -> None:  # noqa: N802
        super().showEvent(event)
        self.refresh()

    def drawForeground(self, painter: QtGui.QPainter, rect: QtCore.QRectF | QtCore.QRect) -> None:  # noqa: N802
        if self._area.isEmpty():
            return
        painter.setPen(QtGui.QPen(QtGui.QColor(79, 195, 247, 170), 1))
        painter.setBrush(QtGui.QBrush(QtGui.QColor(79, 195, 247, 18)))
        painter.drawRect(self._area)


# ── 自动配置表单 ──────────────────────────────────────────────────────────
FIELD_LABELS: dict[str, str] = {
    # 存储
    "sqlite_path": "主库路径", "lancedb_path": "向量库路径", "kuzu_path": "图谱路径",
    "lmdb_path": "KV 路径", "duckdb_path": "时序库路径", "sqlite_busy_timeout_ms": "忙等超时 (ms)",
    "wal_autocheckpoint": "WAL 检查点", "lmdb_map_size_mb": "LMDB 容量 (MB)", "lmdb_max_buckets": "KV 子库数",
    "duckdb_threads": "时序线程数", "engines_enabled": "引擎开关",
    # 模型
    "dense_dim": "向量维度", "embedding_model": "Embedding 模型", "reranker_model": "精排模型",
    "llm_model": "本地 LLM 文件", "llm_gguf_dir": "模型目录", "use_cuda": "启用 CUDA",
    "llm_gpu_layers": "GPU 层数 (-1=全部)", "onnx_providers": "ONNX 提供方",
    "model_mode": "模型来源", "cloud_base_url": "云端 API 地址", "cloud_embedding_model": "云端 Embedding 模型",
    "cloud_llm_model": "云端对话模型", "cloud_embed_dim": "云端向量维度", "cloud_timeout_sec": "云端超时 (s)",
    "cloud_api_key_env": "Key 环境变量名",
    # 召回
    "rrf_k_oversample": "超采样倍数", "rrf_constant": "RRF 常数", "w_rrf": "名次权重", "w_freshness": "新鲜度权重",
    "w_synapse": "突触权重", "w_pad": "情绪权重", "w_confidence": "置信度权重", "top_k": "返回条数",
    "p95_target_ms": "延迟目标 (ms)", "pad_salience_arousal_weight": "唤醒度占比", "rerank_enabled": "启用精排",
    # 记忆与遗忘
    "fsrs_difficulty_default": "初始难度 D", "fsrs_stability_default": "初始稳定度 S", "fsrs_decay": "衰减底数",
    "reinforce_threshold": "巩固访问阈值", "consolidate_confidence": "核心层准入阈值",
    "recall_layer_ceiling": "回忆层上限", "review_queue_ceiling": "复核队列阈值", "fsrs_power": "衰减指数",
    "fsrs_arousal_boost": "高唤醒加成", "tiers": "分层列表", "pyramid": "金字塔层级",
    "buffer_no_sync": "工作记忆不同步", "archive_retrievability_threshold": "归档可提取阈值",
    # 丘脑
    "min_content_chars": "最短字符数", "dedupe_hamming_threshold": "去重汉明阈值",
    "dedupe_cosine_threshold": "向量去重阈值", "significance_threshold": "显著性准入线",
    "rate_limit_per_minute": "限速 (/min，空=默认)", "significance_length_weight": "信息量权重",
    "significance_pad_weight": "情绪权重", "significance_confidence_weight": "置信度权重",
    "significance_length_ref": "信息量饱和长度", "dedupe_window": "去重窗口",
    # 情感
    "affinity_alpha": "好感低通系数", "pad_smoothing": "PAD 平滑",
    "negative_arousal_threshold": "负唤醒冷静线", "negative_valence_threshold": "负效价线",
    "calm_period_ms": "冷静期 (ms)", "calm_recall_penalty": "冷静期召回折扣",
    # 同步 / 网关
    "sync_interval_sec": "同步间隔 (s)", "write_debounce_sec": "写入防抖 (s)",
    "segment_max_bytes": "事件段滚动阈值", "snapshot_monthly": "月度快照", "schema_version": "架构版本",
    "use_age_encrypt": "事件 age 加密", "remote_name": "远端名", "device_id": "设备标识",
    "mcp_host": "MCP 地址", "mcp_port": "MCP 端口", "http_host": "HTTP 地址", "http_port": "HTTP 端口",
    "default_rate_limit": "默认限速 (/min)", "registry_enabled": "启用注册中心",
    # 外观
    "timer_interval_ms": "帧间隔 (ms)", "max_labels": "同屏标签数", "stardust_count": "星尘数量",
    "dim_opacity": "非高亮透明度", "label_font_size": "标签字号", "glow_multiplier": "光晕倍率",
    "pulse_radius": "光点半径", "flash_decay": "闪烁衰减", "link_distance": "近邻连线半径",
    "window_width": "窗口宽", "window_height": "窗口高",
}


class ConfigEditor(QtWidgets.QWidget):
    """把 pydantic 子模型自动渲染成分组表单。

    - 新增 config 字段零 UI 代码：类型驱动控件（bool→勾选、int/float→旋钮、
      Literal→下拉、list→逗号分隔、dict[str,bool]→逐键勾选）；
    - `collect()` 输出 {section: {field: value}} 补丁，交给 DesktopBackend 持久化；
    - 密钥类字段由调用方 exclude，绝不进表单/配置文件。
    """

    def __init__(
        self,
        sections: Sequence[tuple[str, BaseModel]],
        *,
        excludes: dict[str, set[str]] | None = None,
    ) -> None:
        super().__init__()
        self._bindings: list[tuple[str, str, Any]] = []  # (section, field, getter)
        excludes = excludes or {}
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        for title, model in sections:
            card = Card(title)
            form = QtWidgets.QFormLayout()
            form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
            hints = get_type_hints(type(model))
            fields = type(model).model_fields
            for name in fields:
                if name in excludes.get(title, set()):
                    continue
                widget, getter = self._build_editor(hints.get(name, str), getattr(model, name), name)
                label = QtWidgets.QLabel(FIELD_LABELS.get(name, name))
                label.setToolTip(f"{title}.{name}")
                form.addRow(label, widget)
                self._bindings.append((title, name, getter))
            card.body.addLayout(form)
            layout.addWidget(card)
        layout.addStretch(1)

    # ── 控件工厂 ──────────────────────────────────────────────────────────
    def _build_editor(self, annotation: Any, value: Any, field_name: str) -> tuple[QtWidgets.QWidget, Any]:
        origin = get_origin(annotation)
        if annotation is bool:
            box = QtWidgets.QCheckBox()
            box.setChecked(bool(value))
            return box, box.isChecked
        if origin is typing.Literal:
            combo = QtWidgets.QComboBox()
            combo.addItems([str(item) for item in get_args(annotation)])
            combo.setCurrentText(str(value))
            return combo, combo.currentText
        if origin is dict:
            return self._build_dict_editor(annotation, value, field_name)
        if origin in (list, set) or annotation in (list, set):
            edit = QtWidgets.QLineEdit(", ".join(str(item) for item in (value or [])))
            return edit, lambda: [part.strip() for part in edit.text().split(",") if part.strip()]
        if origin is typing.Union or isinstance(annotation, _types.UnionType):
            non_none = [item for item in get_args(annotation) if item is not type(None)]
            if non_none == [int]:
                edit = QtWidgets.QLineEdit("" if value is None else str(value))
                return edit, lambda: int(edit.text()) if edit.text().strip() else None
        if annotation is int:
            spin = QtWidgets.QSpinBox()
            spin.setRange(-2_000_000_000, 2_000_000_000)
            spin.setValue(int(value))
            return spin, spin.value
        if annotation is float:
            spin = QtWidgets.QDoubleSpinBox()
            spin.setRange(-1_000_000.0, 1_000_000.0)
            spin.setDecimals(4)
            spin.setValue(float(value))
            return spin, spin.value
        # str / Path / 其它兜底
        edit = QtWidgets.QLineEdit("" if value is None else str(value))
        if "path" in field_name.lower() or "dir" in field_name.lower() or isinstance(value, Path):
            edit.setPlaceholderText(str(Path.home()))
        return edit, (lambda: Path(edit.text()) if isinstance(value, Path) else edit.text())

    def _build_dict_editor(self, annotation: Any, value: dict[str, Any], field_name: str):
        key_type, value_type = (get_args(annotation) + (str, bool))[:2]
        container = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        getters: dict[str, Any] = {}
        for key in value:
            row = QtWidgets.QHBoxLayout()
            row.addWidget(QtWidgets.QLabel(key))
            widget, getter = self._build_editor(value_type, value[key], field_name)
            row.addWidget(widget)
            row.addStretch(1)
            layout.addLayout(row)
            getters[key] = getter
        return container, lambda: {key: getter() for key, getter in getters.items()}

    # ── 收集 ──────────────────────────────────────────────────────────────
    def collect(self) -> dict[str, dict[str, Any]]:
        patch: dict[str, dict[str, Any]] = {}
        for section, field_name, getter in self._bindings:
            patch.setdefault(section, {})[field_name] = getter()
        return patch
