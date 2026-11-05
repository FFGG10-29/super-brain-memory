"""ui.desktop.neurons —— 突触激活的视觉具象化：神经元节点 + Lerp 光脉冲。

- `NeuronNode`：径向渐变发光；收到脉冲后 `flash()` 变白并逐渐衰减（神经递质释放）。
- `LightPulse`：每帧线性插值（Lerp）沿起点→终点当前坐标移动，速度由余弦相似度决定；
  即使节点在漂移也始终跟随最新位置，不会脱节。
- `NebulaView`：真实数据入口 `set_graph()`；检索入口 `emit_pulses()` / `emit_from_center()`。
  一次查询可同时向 N 条命中记忆发射光点，整屏像神经网络被激活一样闪耀。

所有动画参数来自 config.UiConfig / ActivationConfig，视图代码不含魔法数字。
"""
from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence

from PySide6 import QtCore, QtGui, QtWidgets

from brain.activation import ActivationGraph, ActivationNode
from config import BrainConfig, get_config

__all__ = ["LightPulse", "NebulaView", "NeuronNode"]


def _stable_position(node_id: str, center_x: float, center_y: float) -> tuple[float, float]:
    """同一记忆在任何设备上都落在同一位置（稳定哈希，重放/截图可复现）。"""
    digest = hashlib.blake2b(node_id.encode("utf-8"), digest_size=8).digest()
    seed = int.from_bytes(digest, "big")
    angle = (seed % 10000) / 10000.0 * 2 * math.pi
    radius = 120.0 + (seed >> 16) % 220
    return center_x + math.cos(angle) * radius, center_y + math.sin(angle) * radius


class NeuronNode(QtWidgets.QGraphicsEllipseItem):
    def __init__(self, node: ActivationNode, x: float, y: float, *, config: BrainConfig) -> None:
        params = config.ui
        self.radius = params.emotion_node_radius if node.is_emotion else params.normal_node_radius
        # item 边界取最大发光半径（含 flash 时的扩张），否则光晕会被裁剪成实心圆
        glow_extent = self.radius * (params.glow_multiplier + 2)
        super().__init__(-glow_extent, -glow_extent, glow_extent * 2, glow_extent * 2)
        self.node_id = node.id
        self.data = node
        self.setPos(x, y)
        self.vx = 0.0
        self.vy = 0.0
        self.base_color = QtGui.QColor(params.emotion_node_color if node.is_emotion else params.node_color)
        self.flash_intensity = 0.0
        self.flash_count = 0                  # 累计被激活次数（终点闪烁可观测）
        self._config = config
        self._update_brush()
        self.setToolTip(f"{node.label}\nPAD{node.pad} / {node.tier}")

    def _update_brush(self) -> None:
        params = self._config.ui
        glow_radius = self.radius * (params.glow_multiplier + self.flash_intensity * 2)
        gradient = QtGui.QRadialGradient(0, 0, glow_radius)
        color = self.base_color
        core = QtGui.QColor(255, 255, 255) if self.flash_intensity > 0.5 else color
        gradient.setColorAt(0.0, core)
        gradient.setColorAt(0.4, QtGui.QColor(color.red(), color.green(), color.blue(), 150))
        gradient.setColorAt(1.0, QtGui.QColor(0, 0, 0, 0))
        self.setBrush(QtGui.QBrush(gradient))
        self.setPen(QtGui.QPen(QtCore.Qt.PenStyle.NoPen))

    def flash(self) -> None:
        self.flash_intensity = 1.0
        self.flash_count += 1
        self._update_brush()

    def update_physics(self, nodes: Sequence["NeuronNode"]) -> None:
        params = self._config.ui
        center = QtCore.QPointF(params.center_x, params.center_y)
        self.vx += (center.x() - self.x()) * params.center_pull
        self.vy += (center.y() - self.y()) * params.center_pull
        for other in nodes:
            if other is self:
                continue
            dx, dy = self.x() - other.x(), self.y() - other.y()
            distance = math.sqrt(dx * dx + dy * dy)
            if 0 < distance < params.repulsion_distance:
                force = (params.repulsion_distance - distance) * params.repulsion_force
                self.vx += (dx / distance) * force
                self.vy += (dy / distance) * force
        self.vx *= params.friction
        self.vy *= params.friction
        self.setPos(self.x() + self.vx, self.y() + self.vy)
        if self.flash_intensity > 0:
            self.flash_intensity = max(0.0, self.flash_intensity - params.flash_decay)
            self._update_brush()


class LightPulse(QtWidgets.QGraphicsEllipseItem):
    def __init__(self, start_node: NeuronNode, end_node: NeuronNode, *, similarity: float, config: BrainConfig) -> None:
        radius = config.ui.pulse_radius
        super().__init__(-radius, -radius, radius * 2, radius * 2)
        self.start_node = start_node
        self.end_node = end_node
        self.progress = 0.0
        activation = config.activation
        self.speed = activation.pulse_speed_base + activation.pulse_speed_similarity_gain * max(0.0, min(1.0, similarity))
        self.similarity = similarity

        gradient = QtGui.QRadialGradient(0, 0, radius)
        gradient.setColorAt(0, QtGui.QColor(config.ui.pulse_color))
        brightness = int(120 + 135 * max(0.0, min(1.0, similarity)))
        gradient.setColorAt(0.5, QtGui.QColor(0, 255, 255, brightness))
        gradient.setColorAt(1, QtGui.QColor(0, 0, 0, 0))
        self.setBrush(QtGui.QBrush(gradient))
        self.setPen(QtGui.QPen(QtCore.Qt.PenStyle.NoPen))
        self.setZValue(1)

    def update_position(self) -> bool:
        self.progress += self.speed
        if self.progress >= 1.0:
            self.end_node.flash()
            return False
        start, end = self.start_node, self.end_node
        x = start.x() + (end.x() - start.x()) * self.progress
        y = start.y() + (end.y() - start.y()) * self.progress
        self.setPos(x, y)
        return True


class NebulaView(QtWidgets.QGraphicsView):
    """星云视口：真实记忆节点 + 相似度突触线 + 检索驱动光脉冲。"""

    def __init__(self, config: BrainConfig | None = None, *, auto_animate: bool = True) -> None:
        super().__init__()
        self._config = config or get_config()
        self.scene = QtWidgets.QGraphicsScene(self)
        self.setScene(self.scene)
        self.setStyleSheet(f"background-color: {self._config.ui.background_color}; border: none;")
        self.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)

        self.nodes: list[NeuronNode] = []
        self._node_by_id: dict[str, NeuronNode] = {}
        self._edges: list[tuple[str, str, float, str]] = []
        self._line_items: dict[tuple[str, str], QtWidgets.QGraphicsLineItem] = {}
        self.pulses: list[LightPulse] = []
        self.last_arrivals: list[str] = []
        self._origin: NeuronNode | None = None

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.animate)
        if auto_animate:
            self.timer.start(self._config.ui.timer_interval_ms)

    # ── 真实数据入口 ──────────────────────────────────────────────────────
    def set_graph(self, graph: ActivationGraph) -> None:
        self.scene.clear()
        # 固定画布：否则 sceneRect 会随节点包围盒变化，渲染时坐标被整体缩放/平移
        params = self._config.ui
        self.scene.setSceneRect(0, 0, params.center_x * 2, params.center_y * 2)
        self.nodes.clear()
        self._node_by_id.clear()
        self._line_items.clear()
        self.pulses.clear()
        self._edges = [(edge.source, edge.target, edge.weight, edge.kind) for edge in graph.edges]
        params = self._config.ui
        for node in graph.nodes:
            x, y = _stable_position(node.id, params.center_x, params.center_y)
            item = NeuronNode(node, x, y, config=self._config)
            self.scene.addItem(item)
            self.nodes.append(item)
            self._node_by_id[node.id] = item
        self._ensure_origin()
        self.update_lines()

    def _ensure_origin(self) -> None:
        """查询中心节点：所有"检索激活"光脉冲的发射源。"""
        from brain.activation import ActivationNode

        params = self._config.ui
        origin_node = ActivationNode(
            id="__query__", label="查询", content="", memory_type="query",
            tier="core", pad=(0.0, 0.0, 0.0), confidence=1.0, is_emotion=False,
        )
        origin = NeuronNode(origin_node, params.center_x, params.center_y, config=self._config)
        origin.base_color = QtGui.QColor("#FFD166")
        origin.setZValue(2)
        origin._update_brush()
        self.scene.addItem(origin)
        self._origin = origin

    def node(self, node_id: str) -> NeuronNode | None:
        return self._node_by_id.get(node_id)

    # ── 脉冲发射（接真实检索）────────────────────────────────────────────
    def emit_pulses(self, source_id: str | None, targets: Sequence[tuple[str, float]]) -> int:
        """从 source 向多条命中记忆同时发射光点；targets 为 (node_id, 余弦相似度)。"""
        source = self._node_by_id.get(source_id or "") or self._origin
        if source is None:
            return 0
        emitted = 0
        for node_id, similarity in targets:
            target = self._node_by_id.get(node_id)
            if target is None or target is source:
                continue
            if len(self.pulses) >= self._config.activation.max_pulses:
                break
            pulse = LightPulse(source, target, similarity=similarity, config=self._config)
            self.scene.addItem(pulse)
            self.pulses.append(pulse)
            emitted += 1
        return emitted

    def emit_from_center(self, targets: Sequence[tuple[str, float]]) -> int:
        """一次查询 → N 条命中记忆，光点从中心齐射（高阶玩法）。"""
        return self.emit_pulses(None, targets)

    # ── 连线与动画 ────────────────────────────────────────────────────────
    def update_lines(self) -> None:
        """两类线：视觉近邻线（淡）+ 语义/突触边（亮，来自真实余弦相似度）。"""
        params = self._config.ui
        color = QtGui.QColor(params.line_color)
        wanted: set[tuple[str, str]] = set()

        def ensure_line(key: tuple[str, str], first: NeuronNode, second: NeuronNode) -> QtWidgets.QGraphicsLineItem:
            wanted.add(key)
            item = self._line_items.get(key)
            if item is None:
                item = QtWidgets.QGraphicsLineItem()
                item.setZValue(-1)
                self.scene.addItem(item)
                self._line_items[key] = item
            item.setLine(first.x(), first.y(), second.x(), second.y())
            return item

        # 1) 近邻线：复刻"深空星云"视觉，随节点漂移动态刷新
        for index, first in enumerate(self.nodes):
            for second in self.nodes[index + 1 :]:
                distance = math.hypot(first.x() - second.x(), first.y() - second.y())
                if distance >= params.link_distance:
                    continue
                key = (min(first.node_id, second.node_id), max(first.node_id, second.node_id))
                alpha = int(params.proximity_line_alpha * (1 - distance / params.link_distance))
                line = ensure_line(key, first, second)
                line.setPen(QtGui.QPen(QtGui.QColor(color.red(), color.green(), color.blue(), max(20, alpha)), 1))

        # 2) 语义/突触边：真实检索相似度，覆盖同名近邻线并加亮加粗
        for source_id, target_id, weight, kind in self._edges:
            source, target = self._node_by_id.get(source_id), self._node_by_id.get(target_id)
            if source is None or target is None:
                continue
            key = (min(source_id, target_id), max(source_id, target_id))
            alpha = int(255 * max(0.0, min(1.0, weight)))
            line = ensure_line(key, source, target)
            line.setPen(
                QtGui.QPen(QtGui.QColor(120, 255, 220, alpha), 2.5 if kind == "synapse" else 1.8)
            )

        for key, item in list(self._line_items.items()):
            if key not in wanted:
                self.scene.removeItem(item)
                del self._line_items[key]

    def animate(self) -> None:
        for node in self.nodes:
            node.update_physics(self.nodes)
        self.update_lines()
        for pulse in self.pulses[:]:
            if not pulse.update_position():
                self.last_arrivals.append(pulse.end_node.node_id)
                self.scene.removeItem(pulse)
                self.pulses.remove(pulse)

    def pulse_count(self) -> int:
        return len(self.pulses)
