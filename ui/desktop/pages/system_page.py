"""ui.desktop.pages.system_page —— 系统概览：给非工程师看的健康检查页。

所有引擎 / 任务都带中文注解：它是干什么的、状态代表什么、出问题先看哪里。
"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ui.desktop.backend import DesktopBackend
from ui.desktop.widgets import Card, KpiCard

__all__ = ["SystemPage"]

# (中文名, 通俗解释)
ENGINE_LABELS: dict[str, tuple[str, str]] = {
    "relational:sqlite": ("主记忆库", "所有记忆的唯一事实源：正文、时间、情绪、分层都在这里。它挂了等于失忆，所以永远第一个检查。"),
    "vector:lancedb": ("语义检索库", "按「意思相近」找记忆，是联想能力的来源。状态里 dim=向量维度。"),
    "graph:kuzu": ("关系图谱", "记录「谁和谁有关」：人物、事件之间的连线。状态里 entities=已登记的实体数。"),
    "kv:lmdb": ("状态仓库", "存放你的画像、当前情绪、限流计数这类轻量状态。"),
    "timeseries:duckdb": ("情绪轨迹库", "情绪曲线与访问审计，只增不改（append-only）。"),
    "hypergraph:kuzu": ("超图（逻辑层）", "一次事件涉及多个人/事时的高阶关联。member_edges=0 表示还没有多主体事件。"),
    "clock:hlc": ("混合逻辑时钟", "给每条记忆打全序时间戳，多设备同步不打架。"),
}

# detail 里的术语 → 人话
_DETAIL_TERMS = (
    ("journal=wal", "写入日志=防崩溃模式"),
    ("dim=", "向量维度 "),
    ("entities=", "已登记实体 "),
    ("buckets=", "命名子库 "),
    ("append-only", "只增不改"),
    ("member,edges=", "多主体事件数 "),
)

TASK_LABELS: dict[str, tuple[str, str]] = {
    "nightly_consolidate": ("夜间巩固", "凌晨把常被想起的记忆晋升到核心层、梳理知识金字塔"),
    "decay_sweep": ("遗忘扫描", "按遗忘曲线降低久未使用记忆的「可提取性」，该忘的慢慢淡出"),
    "snapshot": ("快照导出", "凌晨生成一份可快速恢复的记忆快照（事件日志仍是真源）"),
    "morning_brief": ("晨间简报", "早上把昨天的记忆整理成一段摘要"),
}


def _human_detail(engine: str, detail: str) -> str:
    human = str(detail)
    for raw, text in _DETAIL_TERMS:
        if raw in human:
            human = human.replace(raw, text)
    if engine in ENGINE_LABELS:
        human = f"{ENGINE_LABELS[engine][1]}" if not human else f"{human}　·　{ENGINE_LABELS[engine][1]}"
        return human
    return human


class SystemPage(QtWidgets.QWidget):
    def __init__(self, backend: DesktopBackend) -> None:
        super().__init__()
        self._backend = backend

        self._engines_kpi = KpiCard("引擎健康（六个存储分区是否正常）", "—")
        self._memories_kpi = KpiCard("记忆总数", "—")
        self._pad_kpi = KpiCard("情绪采样点（情绪曲线的数据量）", "—")
        self._pending_kpi = KpiCard("待重放事件（等待同步到其它设备）", "—")
        kpis = QtWidgets.QHBoxLayout()
        for card in (self._engines_kpi, self._memories_kpi, self._pad_kpi, self._pending_kpi):
            kpis.addWidget(card)

        self._engine_table = _table(("引擎", "它在干什么", "状态", "延迟 ms", "详情（悬停有解释）"))
        self._task_table = _table(("任务", "时间", "它做什么", "上次执行"))
        run_task = QtWidgets.QPushButton("立即执行选中任务")
        run_task.setObjectName("primary")
        run_task.clicked.connect(self._run_selected_task)
        refresh = QtWidgets.QPushButton("刷新")
        refresh.clicked.connect(self.refresh)

        engine_card = Card("六引擎健康 —— 记忆系统的六个存储分区（悬停行查看它是干什么的）")
        engine_card.body.addWidget(self._engine_table)
        task_card = Card("昼夜调度 —— 每天自动运行的维护任务（像睡眠时的记忆整理）")
        task_card.body.addWidget(self._task_table)
        task_card.body.addWidget(run_task)

        self._info = QtWidgets.QLabel("—")
        self._info.setObjectName("hint")
        self._info.setWordWrap(True)
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
            name, what = ENGINE_LABELS.get(str(engine["engine"]), (str(engine["engine"]), ""))
            cells = (
                name,
                what,
                "正常" if engine["ok"] else "异常",
                f"{float(engine.get('latency_ms') or 0):.2f}",
                str(engine.get("detail") or ""),
            )
            for column, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                item.setToolTip(f"{what}\n原始信息：{engine.get('detail') or '—'}")
                self._engine_table.setItem(row, column, item)
        self._engine_table.resizeColumnsToContents()
        self._engine_table.horizontalHeader().setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.Stretch)

        tasks = self._backend.circadian_tasks()
        self._task_table.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            task_name, what = TASK_LABELS.get(str(task["name"]), (str(task["name"]), ""))
            for column, text in enumerate((task_name, task["at"], what, task["last_run"])):
                item = QtWidgets.QTableWidgetItem(text)
                item.setToolTip(what)
                self._task_table.setItem(row, column, item)
        self._task_table.resizeColumnsToContents()
        self._task_table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)

        config = self._backend.config
        tiers = ", ".join(f"{key} {value} 条" for key, value in snapshot["tier_distribution"].items())
        self._info.setText(
            f"设备标识 {config.sync.device_id}（多端同步时各设备必须不同）· 数据结构版本 v{config.sync.schema_version}\n"
            f"数据目录 {config.storage.sqlite_path.parent}\n"
            f"记忆分层分布：{tiers or '暂无'}"
        )

    def _run_selected_task(self) -> None:
        rows = self._task_table.selectionModel().selectedRows() if self._task_table.selectionModel() else []
        if not rows:
            self._status.setText("先在任务表里选中一行（点击任务名左侧的行）")
            return
        name = str(self._task_table.item(rows[0].row(), 0).text())
        # 表格里显示的是中文名，找回真实任务名
        real_name = next(
            (key for key, (label, _what) in TASK_LABELS.items() if label == name), name
        )
        result = self._backend.run_task(real_name)
        self._status.setText(f"{'执行成功' if result['ok'] else '执行失败'}：{result['message']}")
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
