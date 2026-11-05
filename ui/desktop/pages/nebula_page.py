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
from ui.desktop.widgets import MiniMap

__all__ = ["NebulaPage"]


class NebulaPage(QtWidgets.QWidget):
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
        self._refresh_timer = QtCore.QTimer(self)
        self._refresh_timer.timeout.connect(self._tick)
        self._refresh_timer.start(500)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)

        self.reload()

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
        view = self._backend.recall(query)
        emitted = self.view.emit_from_center(view.activations)
        self.view.highlight([hit["id"] for hit in view.hits], dim_others=False)
        self._hits.setText(f"「{query}」命中 {len(view.hits)} · 光脉冲 {emitted}")
        return {"hits": len(view.hits), "pulses": emitted, "degraded": view.degraded}

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
