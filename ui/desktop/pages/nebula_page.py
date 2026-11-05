"""ui.desktop.pages.nebula_page —— 主视图：星云 + 记忆标签 + 迷你地图 + 浮动操作。

交互：
- 滚轮缩放、拖拽平移、「返回全景」fitInView；
- 顶栏搜索 → `activate(query)`：真实召回 → 中心齐射光脉冲 → 高亮命中标签；
- 记忆页双击 → `focus(memory_id)`：居中并高亮该神经元。
"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from config import BrainConfig
from ui.desktop.backend import DesktopBackend
from ui.desktop.neurons import NebulaView
from ui.desktop.system_stats import get_system_stats
from ui.desktop.widgets import MetricGauge, MiniMap

__all__ = ["NebulaPage"]


class NebulaPage(QtWidgets.QWidget):
    node_selected = QtCore.Signal(str)

    def __init__(self, backend: DesktopBackend, config: BrainConfig) -> None:
        super().__init__()
        self._backend = backend
        self._config = config
        self._space = "全部"
        self._source = "—"

        self.view = NebulaView(config)
        self.minimap = MiniMap(self.view.view_scene, self)

        self._overlay = QtWidgets.QLabel(self)
        self._overlay.setStyleSheet(
            "color: #9FB6D4; background: rgba(7,10,16,170); border: 1px solid #18202E;"
            "border-radius: 8px; padding: 6px 10px; font-size: 11px;"
        )
        self._overlay.move(16, 14)

        self._hits = QtWidgets.QLabel(self)
        self._hits.setStyleSheet("color: #4FC3F7; background: transparent; font-size: 11px;")
        self._hits.move(18, 52)

        self._manage = QtWidgets.QPushButton("管理数据", self)
        self._manage.clicked.connect(lambda: self.window().open_data_directory())
        self._fullscreen = QtWidgets.QPushButton("全屏预览", self)
        self._fullscreen.setObjectName("primary")
        self._fullscreen.clicked.connect(lambda: self.window().toggle_fullscreen())
        for button in (self._manage, self._fullscreen):
            button.setFixedHeight(28)

        self.view.horizontalScrollBar().valueChanged.connect(self._sync_minimap)
        self.view.verticalScrollBar().valueChanged.connect(self._sync_minimap)
        self.view.node_selected.connect(self.node_selected.emit)
        self._refresh_timer = QtCore.QTimer(self)
        self._refresh_timer.timeout.connect(self._tick)
        self._refresh_timer.start(500)

        legend = QtWidgets.QLabel(
            '图例：<span style="color:#FF3264;">●</span> 情绪 / 强唤醒　'
            '<span style="color:#4FC3F7;">●</span> 普通记忆　'
            '<span style="color:#96E1FF;">━━</span> 突触关系（粗）　'
            '<span style="color:#96E1FF;">─</span> 语义相近（细）　'
            '· 环=分层（核心在内）　扇区=记忆类型　角度=形成时间<br>'
            '· 悬停节点 / 连线看摘要，点击节点打开记忆检查器'
        )
        legend.setObjectName("hint")
        legend.setWordWrap(True)
        legend.setContentsMargins(12, 2, 12, 4)

        # 顶部仪表：CPU / 内存 / 显卡（后台线程采样，2 秒刷新一次，不阻塞绘制）
        self._cpu_gauge = MetricGauge("CPU", "#4FC3F7")
        self._mem_gauge = MetricGauge("内存", "#B08CFF")
        self._gpu_gauge = MetricGauge("显卡", "#3DDC97")
        gauges = QtWidgets.QHBoxLayout()
        gauges.setContentsMargins(10, 8, 10, 2)
        gauges.setSpacing(10)
        for gauge in (self._cpu_gauge, self._mem_gauge, self._gpu_gauge):
            gauges.addWidget(gauge, 1)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(gauges)
        layout.addWidget(self.view, 1)
        layout.addWidget(legend)

        self._stats = get_system_stats()
        self._stats_timer = QtCore.QTimer(self)
        self._stats_timer.timeout.connect(self._refresh_gauges)
        self._stats_timer.start(2000)
        self._refresh_gauges()

        self.reload()

    # ── 顶部仪表 ──────────────────────────────────────────────────────────
    def _refresh_gauges(self) -> None:
        snapshot = self._stats.latest()
        cpu = snapshot.get("cpu_percent")
        self._cpu_gauge.set_value(cpu, "—" if cpu is None else f"{cpu:.0f}%")

        memory = snapshot.get("memory_percent")
        used = snapshot.get("memory_used_gb")
        total = snapshot.get("memory_total_gb")
        mem_text = "—" if memory is None else f"{memory:.0f}%"
        if used is not None and total is not None:
            mem_text = f"{used:.1f}/{total:.1f} GB"
        self._mem_gauge.set_value(memory, mem_text)

        gpu = snapshot.get("gpu")
        if gpu:
            self._gpu_gauge.set_value(
                gpu["util_percent"],
                f"{gpu['util_percent']:.0f}% · 显存 {gpu['memory_used_mb']:.0f}/{gpu['memory_total_mb']:.0f}MB",
            )
            self._gpu_gauge.set_tooltip(
                f"{gpu['name']} · {gpu['temperature_c']:.0f}°C · "
                f"显存 {gpu['memory_percent']:.0f}%"
            )
        else:
            self._gpu_gauge.set_value(None, "无 N 卡 / 不可用")
            self._gpu_gauge.set_tooltip("nvidia-smi 不可用：核显机器或未安装驱动")

    def stop_stats(self) -> None:
        self._stats_timer.stop()

    # ── 数据 ──────────────────────────────────────────────────────────────
    def set_space(self, space: str) -> None:
        self._space = space
        self.reload()

    def reload(self) -> None:
        graph = self._backend.graph(space=self._space)
        self.view.set_graph(graph)
        self._source = "字符 n-gram（降级）" if graph.degraded else "BGE-M3 稠密向量"
        self.view.fitInView(self.view.view_scene.sceneRect(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)
        self.view.scale(1.15, 1.15)
        self.minimap.refresh()
        self.minimap.raise_()
        self._sync_minimap()
        self._hits.setText("")
        self._update_overlay()

    def reset_view(self) -> None:
        self.view.fitInView(self.view.view_scene.sceneRect(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)
        self._sync_minimap()

    def stats(self) -> dict:
        base = self.view.stats()
        return {**base, "space": self._space, "source": self._source}

    def _update_overlay(self) -> None:
        stats = self.stats()
        self._overlay.setText(
            f"空间 {stats['space']} · 节点 {stats['nodes']} · 突触 {stats['edges']} · "
            f"光点 {stats['pulses']} · {stats['source']}"
        )
        self._overlay.adjustSize()

    def _tick(self) -> None:
        self._update_overlay()

    def _sync_minimap(self) -> None:
        self.minimap.set_view_area(self.view.view_area())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        margin = 18
        button_y = self.height() - 28 - margin
        self._fullscreen.adjustSize()
        self._manage.adjustSize()
        self._fullscreen.move(self.width() - self._fullscreen.width() - margin, button_y)
        self._manage.move(
            self._fullscreen.x() - self._manage.width() - 10, button_y
        )
        self.minimap.move(
            self.width() - self.minimap.width() - margin,
            button_y - self.minimap.height() - 12,
        )
        self.minimap.raise_()
        self._manage.raise_()
        self._fullscreen.raise_()
        self._sync_minimap()

    # ── 检索联动（顶栏搜索 / 召回页）─────────────────────────────────────
    def activate(self, query: str) -> dict:
        self._hits.setText(f"「{query}」召回中…")
        self._backend.tasks.submit(
            lambda: self._backend.recall(query),
            lambda view: self._on_activated(query, view),
            lambda message: self._hits.setText(f"召回失败：{message}"),
        )
        return {"hits": 0, "pulses": 0, "degraded": False}

    def _on_activated(self, query: str, view) -> None:
        emitted = self.view.emit_from_center(view.activations)
        self.view.highlight([hit["id"] for hit in view.hits], dim_others=False)
        self._hits.setText(f"「{query}」命中 {len(view.hits)} · 光脉冲 {emitted}")

    def focus(self, memory_id: str) -> bool:
        found = self.view.focus_on(memory_id)
        if found:
            self._hits.setText(f"定位 {memory_id[:8]}")
        return found

    def demo_pulse(self) -> int:
        graph = self._backend.graph(space=self._space)
        if not graph.edges:
            self._hits.setText("当前空间没有突触边：先写入几条相关记忆")
            return 0
        edges = sorted(graph.edges, key=lambda item: item.weight, reverse=True)[:3]
        emitted = 0
        for edge in edges:
            emitted += self.view.emit_pulses(edge.source, [(edge.target, edge.weight)])
        self._hits.setText(f"演示：{len(edges)} 条最强突触同时传导")
        return emitted
