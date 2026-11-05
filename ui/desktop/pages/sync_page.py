"""ui.desktop.pages.sync_page —— 同步与备份：仓库配置 / 立即同步 / 快照。"""
from __future__ import annotations

import time

from PySide6 import QtWidgets

from ui.desktop.backend import DesktopBackend
from ui.desktop.settings import SettingsStore
from ui.desktop.settings_panel import SettingsPanel
from ui.desktop.widgets import Card, KpiCard

__all__ = ["SyncPage"]


class SyncPage(QtWidgets.QWidget):
    def __init__(self, backend: DesktopBackend, store: SettingsStore) -> None:
        super().__init__()
        self._backend = backend

        self._pending = KpiCard("待重放事件", "—")
        self._snapshots = KpiCard("本地快照", "—")
        self._repo = KpiCard("远端仓库", "未配置")
        self._last_hlc = KpiCard("Journal 最后 HLC", "—")
        kpis = QtWidgets.QHBoxLayout()
        for card in (self._pending, self._snapshots, self._repo, self._last_hlc):
            kpis.addWidget(card)

        sync_now = QtWidgets.QPushButton("立即同步（pull → replay → push）")
        sync_now.setObjectName("primary")
        sync_now.clicked.connect(self.sync_now)
        snapshot = QtWidgets.QPushButton("创建快照")
        snapshot.clicked.connect(self.create_snapshot)
        refresh = QtWidgets.QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        actions = QtWidgets.QHBoxLayout()
        actions.addWidget(sync_now)
        actions.addWidget(snapshot)
        actions.addWidget(refresh)
        actions.addStretch(1)

        self._table = QtWidgets.QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(("快照", "创建时间", "HLC 切点", "记录数"))
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Stretch)

        self._log = QtWidgets.QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(120)
        self._log.setStyleSheet("font-family: Consolas, 'Cascadia Mono', monospace; font-size: 11px;")

        snapshot_card = Card("快照（状态；事件日志仍是唯一真源）")
        snapshot_card.body.addWidget(self._table)

        config_card = Card("PAT 与仓库配置（钥匙串存储，不入库）")
        config_card.body.addWidget(SettingsPanel(store))

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 12)
        layout.addLayout(kpis)
        layout.addLayout(actions)
        layout.addWidget(snapshot_card, 1)
        layout.addWidget(config_card)
        layout.addWidget(self._log)
        self.refresh()

    def refresh(self) -> None:
        snapshot = self._backend.snapshot()
        sync = snapshot["sync_status"]
        self._pending.set_value(sync.get("pending_events", 0))
        self._pending.set_accent("#FFCB6B" if sync.get("pending_events") else "#7EE787")
        self._last_hlc.set_value(str(sync.get("journal_last_hlc") or "—"))
        items = self._backend.snapshots()
        self._snapshots.set_value(len(items))
        repo = self._backend.config.sync.repo_url
        self._repo.set_value("已配置" if repo else "未配置")
        self._repo.set_accent("#7EE787" if repo else "#FFCB6B")

        self._table.setRowCount(len(items))
        for row, item in enumerate(items):
            counts = sum(int(value) for value in (item["counts"] or {}).values())
            created = (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(item["created_at_ms"] / 1000))
                if item["created_at_ms"]
                else "—"
            )
            for column, text in enumerate((item["name"], created, item["hlc_cutoff"], str(counts))):
                self._table.setItem(row, column, QtWidgets.QTableWidgetItem(text))
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Stretch)

    def sync_now(self) -> None:
        report = self._backend.sync_once()
        self._log.append(
            f"[{time.strftime('%H:%M:%S')}] 同步 {'OK' if report['ok'] else 'FAIL'} · "
            f"pull={report['pulled']} replay={report['replayed']} skip={report['skipped']} "
            f"push={report['pushed']} repo={report['has_repo']}"
            + (f" · errors={report['errors']}" if report["errors"] else "")
        )
        self.refresh()

    def create_snapshot(self) -> None:
        result = self._backend.create_snapshot()
        self._log.append(f"[{time.strftime('%H:%M:%S')}] 快照已创建：{result.get('path')}")
        self.refresh()
