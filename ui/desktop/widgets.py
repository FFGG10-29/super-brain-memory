"""ui.desktop.widgets —— 通用卡片、KPI、迷你地图、自动配置表单。"""
from __future__ import annotations

import types as _types
import typing
from collections.abc import Sequence
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

from pydantic import BaseModel
from PySide6 import QtCore, QtGui, QtWidgets

__all__ = ["Card", "ConfigEditor", "KpiCard", "MiniMap", "Switch", "divider", "FIELD_LABELS"]


class Switch(QtWidgets.QAbstractButton):
    """iOS 风格滑动开关（自绘，主题色跟随 accent）。"""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._pos = 0.0                                   # 滑块位置 0..1
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._animate)

    def setChecked(self, checked: bool) -> None:  # noqa: N802
        """编程式设置必须同步滑块位置，否则初始渲染看起来是"关"。"""
        super().setChecked(checked)
        self._pos = 1.0 if checked else 0.0
        self.update()

    def sizeHint(self) -> QtCore.QSize:  # noqa: N802
        return QtCore.QSize(44, 24)

    def _animate(self) -> None:
        target = 1.0 if self.isChecked() else 0.0
        step = 0.22 if abs(target - self._pos) > 0.02 else 1.0
        self._pos += (target - self._pos) * step
        self._pos = target if abs(target - self._pos) < 0.01 else self._pos
        self.update()
        if abs(self._pos - target) < 0.01:
            self._pos = target
            self._timer.stop()

    def nextCheckState(self) -> None:  # noqa: N802
        super().nextCheckState()
        self._timer.start()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        width, height = self.width(), self.height()
        track_on = QtGui.QColor("#1C5B76")
        track_off = QtGui.QColor("#1A2436")
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QBrush(track_on if self.isChecked() else track_off))
        painter.drawRoundedRect(0, 0, width, height, height / 2, height / 2)
        knob_d = height - 6
        x = 3 + (width - knob_d - 6) * self._pos
        painter.setBrush(QtGui.QBrush(QtGui.QColor("#EAF3FF")))
        painter.drawEllipse(int(x), 3, knob_d, knob_d)


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
    "merge_enabled": "写入时语义合并裁决", "merge_candidate_limit": "裁决候选条数",
    "cluster_summary_enabled": "聚簇主题摘要", "cluster_min_members": "聚簇最少成员",
    "embed_backend": "Embedding 来源", "embed_base_url": "Embedding API 地址", "embed_model": "Embedding 模型名",
    "embed_dim": "云端向量维度", "embed_timeout_sec": "Embedding 超时 (s)", "embed_api_key_env": "Embedding Key 环境变量名",
    "llm_backend": "LLM 来源", "llm_base_url": "LLM API 地址", "llm_model": "LLM 模型名",
    "llm_timeout_sec": "LLM 超时 (s)", "llm_api_key_env": "LLM Key 环境变量名",
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
    "drift_jitter": "节点漂移强度", "spontaneous_pulse_interval_ms": "自发脉冲间隔 (ms)", "neuron_fire_probability": "自发放电概率", "timer_interval_ms": "帧间隔 (ms)", "max_labels": "同屏标签数", "stardust_count": "星尘数量",
    "dim_opacity": "非高亮透明度", "label_font_size": "标签字号", "glow_multiplier": "光晕倍率",
    "pulse_radius": "光点半径", "flash_decay": "闪烁衰减", "link_distance": "近邻连线半径",
    "window_width": "窗口宽", "window_height": "窗口高",
}


FIELD_HINTS: dict[str, str] = {
    # 存储
    "sqlite_path": "主记忆库位置：所有记忆的唯一事实源。改路径等于搬家，旧数据不会自动迁移。",
    "lancedb_path": "向量库位置：按语义相似度找记忆，是「联想」能力的来源。",
    "kuzu_path": "关系图谱位置：记录「谁和谁有关」的网络。",
    "lmdb_path": "KV 仓库位置：你的画像、当前情绪等轻量状态。",
    "duckdb_path": "时序库位置：情绪轨迹与访问审计，只增不改。",
    "sqlite_busy_timeout_ms": "数据库被占用时最多等多久（毫秒）。多端同时写报 database is locked 时可调大。",
    "wal_autocheckpoint": "写入日志多大时自动合并进主库（页数），默认即可。",
    "lmdb_map_size_mb": "KV 仓库的地址空间上限（不是实际占用），记忆很多时可加大。",
    "lmdb_max_buckets": "KV 命名子库数量上限。",
    "duckdb_threads": "时序分析的并行线程数，2 足够。",
    "engines_enabled": "关掉的引擎读写为空但【不丢数据】：事件日志是唯一真源，重新打开可重放重建。",
    # 模型
    "dense_dim": "向量维度，必须与 embedding 模型匹配；修改需重建向量库。",
    "embedding_model": "语义编码模型（BGE-M3），决定「听懂意思」的能力。",
    "reranker_model": "二阶段精排模型，对召回结果排序。",
    "llm_model": "本地小模型文件名（GGUF），用于结构化抽取与总结。",
    "llm_gguf_dir": "本地模型文件所在目录。",
    "use_cuda": "有 NVIDIA 显卡才勾选；核显 / 无独显保持关闭。",
    "llm_gpu_layers": "本地 LLM 放上显卡的层数：-1=全部，0=完全用 CPU。",
    "onnx_providers": "推理后端，保持默认 CPU 即可。",
    "model_mode": "local=只用本地模型；cloud=优先云端 API；auto=本地有模型文件用本地，否则云端。",
    "cloud_base_url": "OpenAI 兼容接口地址，如 https://api.openai.com/v1 或本地 vLLM 地址（不带尾斜杠）。",
    "cloud_embedding_model": "云端向量模型名（如 text-embedding-3-small）；留空=向量继续用本地。",
    "cloud_llm_model": "云端对话模型名（如 deepseek-chat）；留空=抽取总结继续用本地。",
    "cloud_embed_dim": "云端返回的向量维度，必须与「向量维度」一致，否则报错提示重建。",
    "cloud_timeout_sec": "云端请求超时秒数；超时自动退回本地能力，不会卡死。",
    "cloud_api_key_env": "API key 的环境变量名；更推荐点上方「保存到钥匙串」。",
    # 召回
    "rrf_k_oversample": "每路检索多取几倍候选再精排：越大越准越慢。",
    "rrf_constant": "多路融合的平滑常数，60 是论文标准值。",
    "w_rrf": "检索名次在总分里的权重。",
    "w_freshness": "越新的记忆越加分的权重。",
    "w_synapse": "与其它记忆关联越强越加分的权重。",
    "w_pad": "情绪越强烈越加分的权重。",
    "w_confidence": "信息越可信越加分的权重。",
    "top_k": "一次召回最终返回几条记忆。",
    "p95_target_ms": "性能目标：95% 的召回应快于此毫秒数（用于评估对比，不限制实际请求）。",
    "pad_salience_arousal_weight": "情绪显著性里「激动程度」的占比（其余是愉悦度）。",
    "rerank_enabled": "精排明显提升准确度但更费时；核显 / 低配设备建议关闭。",
    # 记忆与遗忘
    "fsrs_difficulty_default": "新记忆的初始难度 D（1-10），越难忘得越快。",
    "fsrs_stability_default": "新记忆的初始稳定度 S（约等于能记住的天数）。",
    "fsrs_decay": "遗忘曲线底数，越小忘得越快。",
    "reinforce_threshold": "一条记忆被想起多少次后触发巩固（强化）。",
    "consolidate_confidence": "置信度高于此值的记忆有资格晋升核心层。",
    "recall_layer_ceiling": "回忆层的置信度上限。",
    "review_queue_ceiling": "可提取性低于此值的记忆进复核队列，提醒你回看。",
    "fsrs_power": "遗忘幂函数的指数，一般不动。",
    "fsrs_arousal_boost": "情绪越激动的事件记得越牢（闪光灯记忆效应）的加成。",
    "tiers": "启用的记忆分层：缓冲（工作记忆）→ 核心 → 回忆 → 归档。",
    "pyramid": "金字塔抽象层级：原始 → 事实 → 主题 → 世界观。",
    "buffer_no_sync": "工作记忆不跨设备同步（省流量，默认开）。",
    "archive_retrievability_threshold": "可提取性低于此值归档：退出常规召回，但还在库里。",
    # 丘脑
    "min_content_chars": "短于该字符数的内容直接拒收（过滤噪音）。",
    "dedupe_hamming_threshold": "与近期记忆的指纹距离 ≤ 该值判定为重复，不再写入。",
    "dedupe_cosine_threshold": "语义几乎相同的判定阈值（调到 1.0≈关闭近似去重）。",
    "significance_threshold": "显著性低于此值只进缓冲区，不产生跨设备同步事件。",
    "rate_limit_per_minute": "每分钟最多写入几条；留空=用网关默认限速。",
    "significance_length_weight": "显著性里信息量（内容长度）的占比。",
    "significance_pad_weight": "显著性里情绪强度的占比。",
    "significance_confidence_weight": "显著性里可信度的占比。",
    "significance_length_ref": "内容到多长算「信息量满」（字符）。",
    "dedupe_window": "与最近多少条记忆比对去重。",
    # 情感
    "affinity_alpha": "好感度平滑系数：越小情绪越稳定，越大越喜怒形于色。",
    "pad_smoothing": "单次事件对整体情绪基线的即时冲击比例。",
    "negative_arousal_threshold": "既负面又激动超过该值 → 触发冷静期。",
    "negative_valence_threshold": "愉悦度低于该值算负面情绪。",
    "calm_period_ms": "冷静期持续多久；期间相关记忆召回打折（避免上头）。",
    "calm_recall_penalty": "冷静期内的召回折扣（0-1，越小压得越狠）。",
    # 同步 / 网关
    "sync_interval_sec": "多久自动与远端同步一次（秒）。",
    "write_debounce_sec": "写入后延迟几秒再推送（把连续写入合并成一次）。",
    "segment_max_bytes": "单个事件段文件多大后滚动新建（约值即可）。",
    "snapshot_monthly": "每月自动生成一次快照（状态备份，事件日志仍是真源）。",
    "schema_version": "数据结构版本号，升级迁移用，不要手动改。",
    "use_age_encrypt": "同步的事件用 age 加密后再进私库（内容更保险，稍耗 CPU）。",
    "remote_name": "git 远端名称，默认 origin，一般不动。",
    "device_id": "本设备在多端同步中的唯一标识，各设备必须不同（默认取主机名）。",
    "mcp_host": "MCP 工具接口监听地址。",
    "mcp_port": "MCP 工具接口端口。",
    "http_host": "HTTP 接口默认监听地址（「接口与端口」页可临时改）。",
    "http_port": "HTTP 接口默认端口（「接口与端口」页可临时改）。",
    "default_rate_limit": "每个 agent 每分钟默认请求上限（防滥用）。",
    "registry_enabled": "关闭后不做令牌校验，仅限本机调试，绝不建议对外开服务时关闭。",
    # 星云
    "similarity_threshold": "余弦相似度超过该值才画「突触线」：越小线越多越密。",
    "max_nodes": "星云里最多画多少个记忆节点。",
    "max_edges_per_node": "每个节点最多保留几条最强连线。",
    "pulse_speed_base": "光点的基础飞行速度。",
    "pulse_speed_similarity_gain": "相似度越高光点飞得越快的加成。",
    "max_pulses": "同屏最多同时飞几个光点。",
    "drift_jitter": "每帧随机微扰力：让节点永不静止，越大星云越「躁」。",
    "spontaneous_pulse_interval_ms": "每隔多久有一个神经元沿突触自发放电（0=关闭）；这是静息态的大脑活动。",
    "neuron_fire_probability": "每个节点每帧自发微闪烁的概率，模拟静息态电活动。",
    "timer_interval_ms": "动画刷新间隔：16≈60fps；感觉卡顿就调大到 33。",
    "max_labels": "同屏最多显示几个记忆标签。",
    "stardust_count": "背景星尘点数，纯装饰。",
    "dim_opacity": "搜索高亮时其它节点变暗的程度。",
    "label_font_size": "记忆标签的字号。",
    "glow_multiplier": "节点光晕大小倍率。",
    "pulse_radius": "光点的大小。",
    "flash_decay": "光点到达后节点高亮的消退速度。",
    "link_distance": "距离多近的两个节点之间画淡色近邻线。",
    "window_width": "启动时窗口宽度。",
    "window_height": "启动时窗口高度。",
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
        advanced: dict[str, set[str]] | None = None,
    ) -> None:
        """advanced：每组里默认折叠的「高级参数」字段名集合，卡片底部有展开开关。"""
        super().__init__()
        self._bindings: list[tuple[str, str, Any]] = []  # (section, field, getter)
        excludes = excludes or {}
        advanced = advanced or {}
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        for title, model in sections:
            card = Card(title)
            form = QtWidgets.QFormLayout()
            form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
            hints = get_type_hints(type(model))
            adv_fields = advanced.get(title, set())
            adv_form: QtWidgets.QFormLayout | None = None
            toggle: QtWidgets.QCheckBox | None = None
            if adv_fields:
                toggle = QtWidgets.QCheckBox("显示高级参数")
                toggle.setToolTip("这些是精调参数，出厂值已经过评测；不确定含义就保持默认")
                adv_container = QtWidgets.QWidget()
                adv_form = QtWidgets.QFormLayout(adv_container)
                adv_form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
                adv_form.setContentsMargins(0, 0, 0, 0)
                adv_container.setVisible(False)
                toggle.toggled.connect(adv_container.setVisible)
            fields = type(model).model_fields
            for name in fields:
                if name in excludes.get(title, set()):
                    continue
                widget, getter = self._build_editor(hints.get(name, str), getattr(model, name), name)
                hint = FIELD_HINTS.get(name, "")
                key = f"{title}.{name}"
                label = QtWidgets.QLabel(FIELD_LABELS.get(name, name))
                label.setToolTip(f"{hint}\n\n参数名：{key}" if hint else key)
                widget.setToolTip(hint)
                target = adv_form if name in adv_fields and adv_form is not None else form
                target.addRow(label, widget)
                self._bindings.append((title, name, getter))
            card.body.addLayout(form)
            if toggle is not None and adv_form is not None:
                assert adv_form.parentWidget() is not None
                card.body.addWidget(toggle)
                card.body.addWidget(adv_form.parentWidget())
            layout.addWidget(card)
        layout.addStretch(1)

    # ── 控件工厂 ──────────────────────────────────────────────────────────
    def _build_editor(self, annotation: Any, value: Any, field_name: str) -> tuple[QtWidgets.QWidget, Any]:
        origin = get_origin(annotation)
        if annotation is bool:
            switch = Switch()
            switch.setChecked(bool(value))
            row = QtWidgets.QWidget()
            row_layout = QtWidgets.QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.addStretch(1)
            row_layout.addWidget(switch)
            return row, switch.isChecked
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
            current = str(value) if isinstance(value, Path) else (edit.text() or str(Path.home()))
            edit.setPlaceholderText(current)
            browse = QtWidgets.QPushButton("浏览…")
            browse.setToolTip("弹出系统文件/文件夹选择器；改路径只影响下次内核重启，且不会自动搬移旧数据")

            def _browse(text_edit: QtWidgets.QLineEdit = edit, sample: str = current) -> None:
                if sample.endswith((".lance", ".kuzu", ".lmdb")):
                    chosen = QtWidgets.QFileDialog.getExistingDirectory(self, "选择存储目录", sample)
                else:
                    chosen, _ = QtWidgets.QFileDialog.getSaveFileName(self, "选择存储文件", sample)
                if chosen:
                    text_edit.setText(chosen)

            browse.clicked.connect(_browse)
            row = QtWidgets.QWidget()
            row_layout = QtWidgets.QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(6)
            row_layout.addWidget(edit, 1)
            row_layout.addWidget(browse)
            return row, (lambda: Path(edit.text()) if isinstance(value, Path) else edit.text())
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
