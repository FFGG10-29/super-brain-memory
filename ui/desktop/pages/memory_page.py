"""ui.desktop.pages.memory_page —— 记忆管理：列表 / 过滤 / 详情 / 写入 / 定位星云。"""
from __future__ import annotations

import time

from PySide6 import QtCore, QtWidgets

from ui.desktop.backend import DesktopBackend

__all__ = ["MemoryPage", "MemoryPanel"]

_COLUMNS = ("时间", "类型", "分层", "PAD (P/A/D)", "内容", "置信度")


class MemoryPage(QtWidgets.QWidget):
    focus_requested = QtCore.Signal(str)

    def __init__(self, backend: DesktopBackend) -> None:
        super().__init__()
        self._backend = backend
        self._rows: list[dict[str, Any]] = []

        self._search = QtWidgets.QLineEdit()
        self._search.setPlaceholderText("在已载入记忆中过滤…")
        self._search.textChanged.connect(self._apply_filter)
        self._tier = QtWidgets.QComboBox()
        self._tier.addItem("全部分层")
        self._type = QtWidgets.QComboBox()
        self._type.addItem("全部类型")
        self._tier.currentIndexChanged.connect(self._apply_filter)
        self._type.currentIndexChanged.connect(self._apply_filter)
        refresh = QtWidgets.QPushButton("刷新")
        refresh.clicked.connect(self.refresh)

        filters = QtWidgets.QHBoxLayout()
        filters.addWidget(self._search, 1)
        filters.addWidget(self._tier)
        filters.addWidget(self._type)
        filters.addWidget(refresh)

        self._table = QtWidgets.QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.horizontalHeader().setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self._table.itemSelectionChanged.connect(self._on_select)
        self._table.itemDoubleClicked.connect(self._on_double_click)

        self._detail = QtWidgets.QTextEdit()
        self._detail.setReadOnly(True)
        copy_id = QtWidgets.QPushButton("复制记忆 ID")
        copy_id.clicked.connect(self._copy_id)
        locate = QtWidgets.QPushButton("在星云中定位")
        locate.setObjectName("primary")
        locate.clicked.connect(self._locate)
        detail_actions = QtWidgets.QHBoxLayout()
        detail_actions.addWidget(copy_id)
        detail_actions.addWidget(locate)
        detail_actions.addStretch(1)

        detail_box = QtWidgets.QWidget()
        detail_layout = QtWidgets.QVBoxLayout(detail_box)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.addWidget(QtWidgets.QLabel("记忆详情 · 双时态 / PAD / 可塑性"))
        detail_layout.addWidget(self._detail, 1)
        detail_layout.addLayout(detail_actions)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        table_box = QtWidgets.QWidget()
        table_layout = QtWidgets.QVBoxLayout(table_box)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.addLayout(filters)
        table_layout.addWidget(self._table)
        splitter.addWidget(table_box)
        splitter.addWidget(detail_box)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        self._new_memory = QtWidgets.QLineEdit()
        self._new_memory.setPlaceholderText("写入一条新记忆（回车提交，走完整丘脑路由 + 去重）")
        self._new_memory.returnPressed.connect(self._save)
        save = QtWidgets.QPushButton("写入记忆")
        save.setObjectName("primary")
        save.clicked.connect(self._save)
        write_row = QtWidgets.QHBoxLayout()
        write_row.addWidget(self._new_memory, 1)
        write_row.addWidget(save)

        self._status = QtWidgets.QLabel("—")
        self._status.setObjectName("hint")

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 12)
        layout.addWidget(splitter, 1)
        layout.addLayout(write_row)
        layout.addWidget(self._status)
        self.refresh()

    # ── 数据 ──────────────────────────────────────────────────────────────
    def refresh(self) -> None:
        self._rows = self._backend.memories(limit=500)
        tiers = sorted({str(row["tier"]) for row in self._rows})
        types = sorted({str(row["memory_type"]) for row in self._rows})
        self._tier.blockSignals(True)
        self._type.blockSignals(True)
        self._tier.clear()
        self._tier.addItem("全部分层")
        self._tier.addItems(tiers)
        self._type.clear()
        self._type.addItem("全部类型")
        self._type.addItems(types)
        self._tier.blockSignals(False)
        self._type.blockSignals(False)
        self._apply_filter()

    def _apply_filter(self) -> None:
        tier = self._tier.currentText()
        memory_type = self._type.currentText()
        keyword = self._search.text().strip().lower()
        rows = [
            row
            for row in self._rows
            if (tier == "全部分层" or row["tier"] == tier)
            and (memory_type == "全部类型" or row["memory_type"] == memory_type)
            and (not keyword or keyword in str(row["content"]).lower())
        ]
        self._fill(rows)
        self._status.setText(f"显示 {len(rows)} / {len(self._rows)} 条记忆")

    def _fill(self, rows: list[dict[str, Any]]) -> None:
        self._table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            pad = row["pad"]
            cells = (
                _format_time(int(row.get("valid_from_ms") or 0)),
                str(row["memory_type"]),
                str(row["tier"]),
                f"{float(pad[0]):+.2f} / {float(pad[1]):+.2f} / {float(pad[2]):+.2f}",
                str(row["content"])[:80],
                f"{float(row.get('confidence') or 0):.2f}",
            )
            for column, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                if column == 0:
                    item.setData(QtCore.Qt.ItemDataRole.UserRole, row["id"])
                self._table.setItem(index, column, item)
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.Stretch)

    def _selected_id(self) -> str:
        rows = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        if not rows:
            return ""
        item = self._table.item(rows[0].row(), 0)
        return str(item.data(QtCore.Qt.ItemDataRole.UserRole)) if item else ""

    def _on_select(self) -> None:
        memory_id = self._selected_id()
        if memory_id:
            self._detail.setPlainText(self._backend.memory_detail(memory_id))

    def _on_double_click(self, *_args) -> None:
        memory_id = self._selected_id()
        if memory_id:
            self.focus_requested.emit(memory_id)

    def _copy_id(self) -> None:
        memory_id = self._selected_id()
        if memory_id:
            QtWidgets.QApplication.clipboard().setText(memory_id)
            self._status.setText(f"已复制 {memory_id}")

    def _locate(self) -> None:
        memory_id = self._selected_id()
        if memory_id:
            self.focus_requested.emit(memory_id)

    def _save(self) -> None:
        content = self._new_memory.text().strip()
        if not content:
            return
        result = self._backend.save_memory(content)
        self._new_memory.clear()
        if not result["applied"]:
            self._status.setText(f"被丘脑拒绝：{result['reason']}")
            return
        self.refresh()
        self._status.setText(f"写入成功 {result['id'][:8]}（新神经元已点亮）")
        self.focus_requested.emit(result["id"])


def _format_time(ms: int) -> str:
    if not ms:
        return "—"
    return time.strftime("%m-%d %H:%M", time.localtime(ms / 1000))


MemoryPanel = MemoryPage
