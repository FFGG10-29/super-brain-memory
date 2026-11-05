"""ui.desktop.pages.recall_page —— 召回测试台：看通道 / 看分数 / 一键点亮星云。"""
from __future__ import annotations

import time

from PySide6 import QtCore, QtWidgets

from ui.desktop.backend import DesktopBackend, RecallView

__all__ = ["RecallPage"]

_COLUMNS = ("分数", "通道", "PAD (P/A/D)", "命中记忆")


class RecallPage(QtWidgets.QWidget):
    activate_requested = QtCore.Signal(str)
    focus_requested = QtCore.Signal(str)

    def __init__(self, backend: DesktopBackend) -> None:
        super().__init__()
        self._backend = backend
        self._view: RecallView | None = None

        self._query = QtWidgets.QLineEdit()
        self._query.setPlaceholderText("输入查询，走完整召回链路（FTS + 向量 + 图谱 + 时间）")
        self._query.returnPressed.connect(self.run_recall)
        self._top_k = QtWidgets.QSpinBox()
        self._top_k.setRange(1, 50)
        self._top_k.setValue(backend.config.recall.top_k)
        run = QtWidgets.QPushButton("召回")
        run.setObjectName("primary")
        run.clicked.connect(self.run_recall)
        activate = QtWidgets.QPushButton("在星云中激活")
        activate.clicked.connect(self._activate)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(self._query, 1)
        top.addWidget(QtWidgets.QLabel("top_k"))
        top.addWidget(self._top_k)
        top.addWidget(run)
        top.addWidget(activate)

        self._table = QtWidgets.QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self._table.itemDoubleClicked.connect(self._focus_hit)

        self._injection = QtWidgets.QTextEdit()
        self._injection.setReadOnly(True)
        self._injection.setPlaceholderText("召回结果将作为上下文注入（L0-L4 收敛注入）")
        self._injection.setMaximumHeight(220)

        self._status = QtWidgets.QLabel("—")
        self._status.setObjectName("hint")

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 12)
        layout.addLayout(top)
        layout.addWidget(self._table, 1)
        layout.addWidget(QtWidgets.QLabel("注入上下文"))
        layout.addWidget(self._injection)
        layout.addWidget(self._status)

    def run_recall(self) -> RecallView:
        query = self._query.text().strip()
        if not query:
            return RecallView(query=query)
        started = time.perf_counter()
        view = self._backend.recall(query, limit=self._top_k.value())
        cost_ms = (time.perf_counter() - started) * 1000
        self._view = view
        self.render(view, cost_ms)
        return view

    def render(self, view: RecallView, cost_ms: float) -> None:
        self._table.setRowCount(len(view.hits))
        for index, hit in enumerate(view.hits):
            pad = hit["pad"]
            cells = (
                f"{float(hit['score']):.4f}",
                "+".join(hit["channels"]) or "—",
                f"{float(pad[0]):+.2f} / {float(pad[1]):+.2f} / {float(pad[2]):+.2f}",
                str(hit["content"])[:90],
            )
            for column, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                if column == 0:
                    item.setData(QtCore.Qt.ItemDataRole.UserRole, hit["id"])
                self._table.setItem(index, column, item)
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self._injection.setPlainText(view.injection or "（无注入上下文）")
        source = "降级（字符 n-gram）" if view.degraded else "BGE-M3"
        self._status.setText(f"命中 {len(view.hits)} 条 · {cost_ms:.0f} ms · 向量源 {source}")

    def _activate(self) -> None:
        query = self._query.text().strip()
        if query:
            self.activate_requested.emit(query)

    def _focus_hit(self, *_args) -> None:
        rows = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        if not rows:
            return
        item = self._table.item(rows[0].row(), 0)
        if item:
            self.focus_requested.emit(str(item.data(QtCore.Qt.ItemDataRole.UserRole)))
