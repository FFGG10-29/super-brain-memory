"""ui.desktop.pages.system_page —— 系统概览：引擎健康 / 记忆规模 / 调度任务。"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ui.desktop.backend import DesktopBackend
from ui.desktop.widgets import Card, KpiCard

__all__ = ["SystemPage"]


class SystemPage(QtWidgets.QWidget):
    def __init__(self, backend: DesktopBackend) -> None:
        super().__init__()
        self._backend = backend

        self._engines_kpi = KpiCard("引擎健康", "—")
        self._memories_kpi = KpiCard("记忆总数", "—")
        self._pad_kpi = KpiCard("PAD 采样点", "—")
        self._pending_kpi = KpiCard("待重放事件", "—")
        kpis = QtWidgets.QHBoxLayout()
        for card in (self._engines_kpi, self._memories_kpi, self._pad_kpi, self._pending_kpi):
            kpis.addWidget(card)

        self._engine_table = _table(("引擎", "状态", "延迟 ms", "说明"))
        self._task_table = _table(("任务", "时间", "上次执行"))
        run_task = QtWidgets.QPushButton("立即执行选中任务")
        run_task.setObjectName("primary")
        run_task.clicked.connect(self._run_selected_task)
        refresh = QtWidgets.QPushButton("刷新")
        refresh.clicked.connect(self.refresh)

        engine_card = Card("六引擎健康")
        engine_card.body.addWidget(self._engine_table)
        task_card = Card("昼夜调度（Circadian）")
        task_card.body.addWidget(self._task_table)
        task_card.body.addWidget(run_task)

        self._info = QtWidgets.QLabel("—")
        self._info.setObjectName("hint")
        self._status = QtWidgets.QLabel("")
        self._status.setObjectName("accent")

        columns = QtWidgets.QHBoxLayout()
        columns.addWidget(engine_card, 1)
        columns.addWidget(task_card, 1)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 12)
        layout.addLayout(kpis)
        layout.addLayout(columns, 1)
        layout.addWidget(self._info)
        layout.addWidget(self._status)
        layout.addWidget(refresh, 0, QtCore.Qt.AlignmentFlag.AlignRight)
        self.refresh()

    def refresh(self) -> None:
        snapshot = self._backend.snapshot()
        engines = snapshot["engine_health"]["engines"]
        ok_count = sum(1 for engine in engines if engine["ok"])
        self._engines_kpi.set_value(f"{ok_count}/{len(engines)}")
        self._engines_kpi.set_accent("#7EE787" if ok_count == len(engines) else "#FF5370")
        self._memories_kpi.set_value(snapshot["memory_stream"].get("count", 0))
        self._pad_kpi.set_value(snapshot["pad_axis"]["summary"].get("samples", 0))
        pending = snapshot["sync_status"].get("pending_events", 0)
        self._pending_kpi.set_value(pending)
        self._pending_kpi.set_accent("#FFCB6B" if pending else "#7EE787")

        self._engine_table.setRowCount(len(engines))
        for row, engine in enumerate(engines):
            cells = (
                engine["engine"],
                "OK" if engine["ok"] else "FAIL",
                f"{float(engine.get('latency_ms') or 0):.2f}",
                str(engine.get("detail") or "")[:60],
            )
            for column, text in enumerate(cells):
                self._engine_table.setItem(row, column, QtWidgets.QTableWidgetItem(text))
        self._engine_table.resizeColumnsToContents()

        tasks = self._backend.circadian_tasks()
        self._task_table.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            for column, text in enumerate((task["name"], task["at"], task["last_run"])):
                self._task_table.setItem(row, column, QtWidgets.QTableWidgetItem(text))
        self._task_table.resizeColumnsToContents()

        config = self._backend.config
        self._info.setText(
            f"设备 {config.sync.device_id} · schema v{config.sync.schema_version} · "
            f"数据目录 {config.storage.sqlite_path.parent} · 记忆分层 "
            + ", ".join(f"{k}={v}" for k, v in snapshot["tier_distribution"].items())
        )

    def _run_selected_task(self) -> None:
        rows = self._task_table.selectionModel().selectedRows() if self._task_table.selectionModel() else []
        if not rows:
            self._status.setText("先在任务表里选中一行")
            return
        name = self._task_table.item(rows[0].row(), 0).text()
        result = self._backend.run_task(name)
        self._status.setText(f"{'OK' if result['ok'] else 'FAIL'} {name}：{result['message']}")
        self.refresh()


def _table(headers: tuple[str, ...]) -> QtWidgets.QTableWidget:
    table = QtWidgets.QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(list(headers))
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
    table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
    return table
