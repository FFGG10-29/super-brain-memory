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

        self._sync_btn = QtWidgets.QPushButton("立即同步（pull → replay → push）")
        self._sync_btn.setObjectName("primary")
        self._sync_btn.clicked.connect(self.sync_now)
        sync_now = self._sync_btn
        snapshot = QtWidgets.QPushButton("创建快照")
        snapshot.clicked.connect(self.create_snapshot)
        backfill = QtWidgets.QPushButton("把本地记忆补成事件")
        backfill.setToolTip(
            "同步只搬事件日志（data\\brain-sync），不搬整个 data 目录。\n"
            "本机以前写入、且当时没进事件流的记忆，用这个按钮补写成 ingest 事件后再同步。"
        )
        backfill.clicked.connect(self.backfill_events)
        refresh = QtWidgets.QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        actions = QtWidgets.QHBoxLayout()
        actions.addWidget(sync_now)
        actions.addWidget(backfill)
        actions.addWidget(snapshot)
        actions.addWidget(refresh)
        actions.addStretch(1)

        # 同步结果显示在按钮正下方：日志框在页面底部，小窗口下会被挤到看不见，
        # 用户点完按钮看不到任何反馈就会以为「没反应」。
        self._status = QtWidgets.QLabel("未同步过")
        self._status.setObjectName("accent")
        self._status.setWordWrap(True)

        self._scope_note = QtWidgets.QLabel(
            "同步范围：只同步**事件日志**（data\\brain-sync\\devices\\…）与快照索引，不同步 data 目录里的"
            "二进制库（Kùzu / LanceDB 无法合并）。另一台设备拉到事件后本地重放，重建同一份记忆。\n"
            "另外默认「工作记忆不跨设备同步」（buffer_no_sync）——新写入先在 buffer 层，"
            "要立刻跨设备可先回填事件，或在「分层流动」里关掉该开关。"
        )
        self._scope_note.setObjectName("hint")
        self._scope_note.setWordWrap(True)

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
        layout.addWidget(self._scope_note)
        layout.addLayout(actions)
        layout.addWidget(self._status)
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
        # git pull/push 可能几秒到几十秒：放后台线程，按钮立刻给反馈，界面不冻结
        self._sync_btn.setEnabled(False)
        self._status.setText("同步中…（pull → replay → push）")
        self._backend.tasks.submit(self._backend.sync_once, self._on_sync_done, self._on_sync_failed)

    def _on_sync_done(self, report: dict) -> None:
        self._sync_btn.setEnabled(True)
        if not report.get("has_repo"):
            message = (
                "未同步：还没有本地 git 仓库。请先在上方填仓库地址并保存（保存后会自动初始化），"
                "或检查 PAT 是否已保存"
            )
        elif report.get("errors"):
            message = "同步失败：" + "；".join(str(item) for item in report["errors"])[:300]
        elif report.get("pushed"):
            message = (
                f"同步完成：pull={report['pulled']} replay={report['replayed']} "
                f"skip={report['skipped']} push=已推送"
            )
        else:
            message = "同步完成：没有新的本地事件可推送（先点「把本地记忆补成事件」再同步）"
        self._status.setText(message)
        self._log.append(
            f"[{time.strftime('%H:%M:%S')}] {'OK' if report.get('ok') else 'FAIL'} · "
            f"pull={report.get('pulled')} replay={report.get('replayed')} "
            f"skip={report.get('skipped')} push={report.get('pushed')} "
            f"repo={report.get('has_repo')}"
            + (f" · errors={report.get('errors')}" if report.get("errors") else "")
        )
        self.refresh()

    def _on_sync_failed(self, message: str) -> None:
        self._sync_btn.setEnabled(True)
        self._status.setText(f"同步失败：{message}")
        self._log.append(f"[{time.strftime('%H:%M:%S')}] 同步失败：{message}")

    def backfill_events(self) -> None:
        result = self._backend.backfill_events()
        self._log.append(f"[{time.strftime('%H:%M:%S')}] 回填：{result['message']}")
        self.refresh()

    def create_snapshot(self) -> None:
        result = self._backend.create_snapshot()
        self._log.append(f"[{time.strftime('%H:%M:%S')}] 快照已创建：{result.get('path')}")
        self.refresh()
