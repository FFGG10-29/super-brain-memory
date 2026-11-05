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
import random
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
        self.node = node
        self.setPos(x, y)
        self.vx = 0.0
        self.vy = 0.0
        self.base_color = QtGui.QColor(params.emotion_node_color if node.is_emotion else params.node_color)
        self.flash_intensity = 0.0
        self.flash_count = 0                  # 累计被激活次数（终点闪烁可观测）
        self._config = config
        self._update_brush()
        self.setToolTip(f"{node.content}\nPAD{node.pad} / {node.tier}")

        # LivingMemory 风格记忆标签：小字 + 类型前缀，默认隐藏，由视图按优先级点亮
        self.label_item = QtWidgets.QGraphicsSimpleTextItem(self)
        snippet = " ".join(node.content.split())[:14]
        prefix = "情绪" if node.is_emotion else "记忆"
        self.label_item.setText(f"{prefix} ● {snippet}")
        font = self.label_item.font()
        font.setPointSize(max(6, params.label_font_size))
        self.label_item.setFont(font)
        self.label_item.setBrush(QtGui.QBrush(QtGui.QColor(params.label_color)))
        self.label_item.setPos(self.radius * 1.2, -self.radius * 1.4)
        self.label_item.setZValue(3)
        self.label_item.setVisible(False)

        # 高亮环：只圈住神经元本体，不用整块发光边界（否则圈大得离谱）
        ring = self.radius * 1.7
        self._ring = QtWidgets.QGraphicsEllipseItem(-ring, -ring, ring * 2, ring * 2, self)
        self._ring.setPen(QtGui.QPen(QtGui.QColor(180, 235, 255, 190), 1.2))
        self._ring.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        self._ring.setVisible(False)

    def set_highlighted(self, highlighted: bool) -> None:
        self._ring.setVisible(highlighted)

    def set_dimmed(self, dimmed: bool) -> None:
        opacity = self._config.ui.dim_opacity if dimmed else 1.0
        self.setOpacity(opacity)
        self.label_item.setOpacity(opacity)

    def set_label_visible(self, visible: bool) -> None:
        self.label_item.setVisible(visible)

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
        # 微扰漂移：力导向收敛后仍保持缓慢游动，星云永远是活的
        self.vx += random.uniform(-1.0, 1.0) * params.drift_jitter
        self.vy += random.uniform(-1.0, 1.0) * params.drift_jitter
        self.vx *= params.friction
        self.vy *= params.friction
        self.setPos(self.x() + self.vx, self.y() + self.vy)
        # 神经元自发放电：低频微闪烁，模拟静息态电活动
        if random.random() < params.neuron_fire_probability and self.flash_intensity < 0.2:
            self.flash_intensity = max(self.flash_intensity, 0.35)
            self.flash_count += 1
            self._update_brush()
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
        self.view_scene = QtWidgets.QGraphicsScene(self)
        self.setScene(self.view_scene)
        self.setStyleSheet(f"background-color: {self._config.ui.background_color}; border: none;")
        self.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)

        self.nodes: list[NeuronNode] = []
        self._node_by_id: dict[str, NeuronNode] = {}
        self._edges: list[tuple[str, str, float, str]] = []
        self._line_items: dict[tuple[str, str], QtWidgets.QGraphicsLineItem] = {}
        self.pulses: list[LightPulse] = []
        self.last_arrivals: list[str] = []
        self._origin: NeuronNode | None = None

        self.setDragMode(QtWidgets.QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self._stardust = self._build_stardust()
        self._frame = 0

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.animate)
        if auto_animate:
            self.timer.start(self._config.ui.timer_interval_ms)

    # ── 背景星尘与缩放 ────────────────────────────────────────────────────
    def _build_stardust(self) -> list[tuple[float, float, float]]:
        rng = random.Random(20261105)
        params = self._config.ui
        return [
            (rng.uniform(0, params.center_x * 2), rng.uniform(0, params.center_y * 2), rng.uniform(0.7, 2.1))
            for _ in range(params.stardust_count)
        ]

    def drawBackground(self, painter: QtGui.QPainter, rect: QtCore.QRectF | QtCore.QRect) -> None:  # noqa: N802
        params = self._config.ui
        painter.fillRect(rect, QtGui.QColor(params.background_color))
        color = QtGui.QColor(params.stardust_color)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QBrush(QtGui.QColor(color.red(), color.green(), color.blue(), 165)))
        for x, y, radius in self._stardust:
            if rect.left() <= x <= rect.right() and rect.top() <= y <= rect.bottom():
                painter.drawEllipse(QtCore.QPointF(x, y), radius, radius)

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:  # noqa: N802
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        current = self.transform().m11()
        if 0.45 <= current * factor <= 3.0:
            self.scale(factor, factor)

    # ── 真实数据入口 ──────────────────────────────────────────────────────
    def set_graph(self, graph: ActivationGraph, *, labels: bool = True) -> None:
        self.view_scene.clear()
        # 固定画布：否则 sceneRect 会随节点包围盒变化，渲染时坐标被整体缩放/平移
        params = self._config.ui
        self.view_scene.setSceneRect(0, 0, params.center_x * 2, params.center_y * 2)
        self.nodes.clear()
        self._node_by_id.clear()
        self._line_items.clear()
        self.pulses.clear()
        self._edges = [(edge.source, edge.target, edge.weight, edge.kind) for edge in graph.edges]
        for node in graph.nodes:
            x, y = _stable_position(node.id, params.center_x, params.center_y)
            item = NeuronNode(node, x, y, config=self._config)
            self.view_scene.addItem(item)
            self.nodes.append(item)
            self._node_by_id[node.id] = item
        self._ensure_origin()
        self._refresh_labels(enabled=labels)
        self.update_lines()

    def _refresh_labels(self, *, enabled: bool = True) -> None:
        """按"情绪 > 高置信"优先级点亮有限标签；标签朝画面中心一侧摆放，减少互相压字。"""
        limit = self._config.ui.max_labels
        center_x = self._config.ui.center_x
        ranked = sorted(
            self.nodes, key=lambda node: (node.node.is_emotion, node.node.confidence), reverse=True
        )
        visible = {item.node_id for item in ranked[:limit]} if enabled else set()
        for node in self.nodes:
            on = node.node_id in visible
            node.set_label_visible(on)
            if not on:
                continue
            width = node.label_item.boundingRect().width()
            offset = node.radius * 1.2
            if node.x() > center_x:
                node.label_item.setPos(-width - offset, -node.radius)
            else:
                node.label_item.setPos(offset, -node.radius)

    def stats(self) -> dict[str, object]:
        return {"nodes": len(self.nodes), "edges": len(self._edges), "pulses": len(self.pulses)}

    def view_area(self) -> QtCore.QRectF:
        return self.mapToScene(self.viewport().rect()).boundingRect()

    # ── 高亮 / 定位 ───────────────────────────────────────────────────────
    def highlight(self, node_ids: Sequence[str], *, dim_others: bool = True) -> None:
        wanted = {str(node_id) for node_id in node_ids}
        for node in self.nodes:
            node.set_highlighted(node.node_id in wanted)
            node.set_dimmed(dim_others and bool(wanted) and node.node_id not in wanted)
            if node.node_id in wanted:
                node.set_label_visible(True)

    def clear_highlight(self) -> None:
        for node in self.nodes:
            node.set_highlighted(False)
            node.set_dimmed(False)
        self._refresh_labels()

    def focus_on(self, node_id: str) -> bool:
        node = self.node(node_id)
        if node is None:
            return False
        self.centerOn(node)
        self.highlight([node_id])
        return True

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
        self.view_scene.addItem(origin)
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
            self.view_scene.addItem(pulse)
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
                self.view_scene.addItem(item)
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
                QtGui.QPen(QtGui.QColor(150, 225, 255, alpha), 2.5 if kind == "synapse" else 1.8)
            )

        for key, item in list(self._line_items.items()):
            if key not in wanted:
                self.view_scene.removeItem(item)
                del self._line_items[key]

    def animate(self) -> None:
        self._frame += 1
        for node in self.nodes:
            node.update_physics(self.nodes)
        self._maybe_spontaneous_pulse()
        self.update_lines()
        for pulse in self.pulses[:]:
            if not pulse.update_position():
                self.last_arrivals.append(pulse.end_node.node_id)
                self.view_scene.removeItem(pulse)
                self.pulses.remove(pulse)

    def _maybe_spontaneous_pulse(self) -> None:
        """静息态自发脉冲：随机节点沿突触向邻居放电，星云在无检索时也保持活性。"""
        params = self._config.ui
        if params.spontaneous_pulse_interval_ms <= 0 or not self.nodes:
            return
        interval_frames = max(1, params.spontaneous_pulse_interval_ms // max(1, params.timer_interval_ms))
        if self._frame % interval_frames != 0:
            return
        source = random.choice(self.nodes)
        neighbors = [
            target_id
            for source_id, target_id, _weight, _kind in self._edges
            if source_id == source.node_id
        ] + [
            source_id
            for source_id, target_id, _weight, _kind in self._edges
            if target_id == source.node_id
        ]
        if not neighbors:
            near = sorted(
                (node for node in self.nodes if node is not source),
                key=lambda node: (node.x() - source.x()) ** 2 + (node.y() - source.y()) ** 2,
            )[:3]
            neighbors = [node.node_id for node in near]
        if not neighbors:
            return
        target_id = random.choice(neighbors)
        self.emit_pulses(source.node_id, [(target_id, random.uniform(0.45, 0.85))])

    def pulse_count(self) -> int:
        return len(self.pulses)
