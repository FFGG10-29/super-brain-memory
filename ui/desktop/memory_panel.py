"""ui.desktop.memory_panel —— 记忆查看界面（检索/详情/写入 + 光脉冲联动）。

联动点：
- 检索：一次召回 → 从查询中心向 N 条命中记忆齐射光点，速度=余弦相似度；
- 双击某条记忆：向与它最相似的若干记忆发射光点（突触激活扩散）；
- 单击：右侧详情显示 PAD / 双时态 / 分层 / 可塑性。
"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ui.desktop.backend import DesktopBackend
from ui.desktop.neurons import NebulaView

__all__ = ["MemoryPanel"]


class MemoryPanel(QtWidgets.QWidget):
    def __init__(self, backend: DesktopBackend, nebula: NebulaView) -> None:
        super().__init__()
        self._backend = backend
        self._nebula = nebula
        self._rows: dict[str, dict] = {}

        search = QtWidgets.QLineEdit()
        search.setPlaceholderText("输入查询：召回记忆并点亮突触…")
        search.returnPressed.connect(self.do_search)
        search_button = QtWidgets.QPushButton("检索 / 激活")
        search_button.clicked.connect(self.do_search)
        refresh_button = QtWidgets.QPushButton("刷新图谱")
        refresh_button.clicked.connect(self.refresh)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(search, 1)
        top.addWidget(search_button)
        top.addWidget(refresh_button)

        self._list = QtWidgets.QListWidget()
        self._list.itemClicked.connect(self.show_detail)
        self._list.itemDoubleClicked.connect(self.activate_related)
        self._detail = QtWidgets.QTextEdit()
        self._detail.setReadOnly(True)
        self._status = QtWidgets.QLabel("就绪")
        self._status.setWordWrap(True)

        new_memory = QtWidgets.QLineEdit()
        new_memory.setPlaceholderText("写入一条新记忆（回车提交）")
        new_memory.returnPressed.connect(lambda: self.save_memory(new_memory))
        save_button = QtWidgets.QPushButton("写入")
        save_button.clicked.connect(lambda: self.save_memory(new_memory))
        bottom = QtWidgets.QHBoxLayout()
        bottom.addWidget(new_memory, 1)
        bottom.addWidget(save_button)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        list_panel = QtWidgets.QWidget()
        list_layout = QtWidgets.QVBoxLayout(list_panel)
        list_layout.addLayout(top)
        list_layout.addWidget(self._list)
        detail_panel = QtWidgets.QWidget()
        detail_layout = QtWidgets.QVBoxLayout(detail_panel)
        detail_layout.addWidget(QtWidgets.QLabel("记忆详情 / 注入上下文"))
        detail_layout.addWidget(self._detail)
        detail_layout.addLayout(bottom)
        splitter.addWidget(list_panel)
        splitter.addWidget(detail_panel)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(splitter)
        layout.addWidget(self._status)
        self._search = search
        self.refresh()

    # ── 数据 ──────────────────────────────────────────────────────────────
    def refresh(self) -> None:
        self._nebula.set_graph(self._backend.graph())
        self._show(self._backend.memories(), "已载入真实记忆节点")

    def _show(self, rows: list[dict], status: str) -> None:
        self._rows = {row["id"]: row for row in rows}
        self._list.clear()
        for row in rows:
            pad = row["pad"]
            label = f"[{row['memory_type']}/{row['tier']}] P{float(pad[0]):+.1f} A{float(pad[1]):+.1f}  {row['content'][:42]}"
            item = QtWidgets.QListWidgetItem(label)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, row["id"])
            self._list.addItem(item)
        self._status.setText(f"{status} · 共 {len(rows)} 条 · 节点 {len(self._nebula.nodes)} 个")

    # ── 检索 → 光脉冲 ────────────────────────────────────────────────────
    def do_search(self) -> None:
        query = self._search.text().strip()
        if not query:
            return
        view = self._backend.recall(query)
        emitted = self._nebula.emit_from_center(view.activations)
        self._show(
            [
                {
                    "id": hit["id"], "content": hit["content"], "memory_type": "hit",
                    "tier": "-", "pad": hit["pad"], "confidence": hit["score"],
                }
                for hit in view.hits
            ],
            f"查询「{query}」激活 {emitted} 条突触 · 向量源={'降级' if view.degraded else 'BGE-M3'}",
        )
        if view.hits:
            self._detail.setPlainText(view.injection or "（无注入上下文）")

    def activate_related(self, item: QtWidgets.QListWidgetItem) -> None:
        memory_id = str(item.data(QtCore.Qt.ItemDataRole.UserRole))
        targets = self._backend.related(memory_id)
        emitted = self._nebula.emit_pulses(memory_id, targets)
        self._status.setText(f"突触扩散：{memory_id[:8]} → {emitted} 个相关记忆")

    def show_detail(self, item: QtWidgets.QListWidgetItem) -> None:
        memory_id = str(item.data(QtCore.Qt.ItemDataRole.UserRole))
        self._detail.setPlainText(self._backend.memory_detail(memory_id))

    def save_memory(self, field: QtWidgets.QLineEdit) -> None:
        content = field.text().strip()
        if not content:
            return
        result = self._backend.save_memory(content)
        field.clear()
        if result["applied"]:
            self.refresh()
            node = self._nebula.node(result["id"])
            if node is not None:
                node.flash()
            self._status.setText(f"写入成功：{result['id'][:8]}（新神经元已点亮）")
        else:
            self._status.setText(f"写入被丘脑拒绝：{result['reason']}")
