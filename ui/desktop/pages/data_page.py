"""ui.desktop.pages.data_page —— 展示数据：六个可视化面板 + 原始 JSON + 导出。

原先六个面板一律打印 JSON 文本；现借鉴 OpenMemory / claude-mem 的看板：
引擎卡片、实时记忆流、PAD 情绪曲线、突触关系表、分层金字塔、设备游标。
原始 JSON 仍保留在独立标签页，便于调试与导出，不丢可审计性。
"""
from __future__ import annotations

import json
import time
from typing import Any

from PySide6 import QtCore, QtWidgets

from ui.desktop.backend import DesktopBackend
from ui.desktop.inspector import BarChart, Sparkline

__all__ = ["DataPage"]

_TIER_COLORS = ("#6EC6FF", "#B08CFF", "#3DDC97", "#FFB84D", "#FF8FB1", "#7FE0D4")
_PAD_COLORS = ("#FF6B9D", "#FFB84D", "#6EC6FF")
_SECTOR_COLORS = (
    ("情景 episodic", "#6EC6FF"), ("语义 semantic", "#3DDC97"),
    ("程序 procedural", "#B08CFF"), ("情绪 emotional", "#FF6B9D"),
    ("画像 profile", "#FFB84D"),
)


def _fmt_ms(value: Any) -> str:
    try:
        ms = int(value or 0)
    except (TypeError, ValueError):
        return "—"
    if not ms:
        return "—"
    return time.strftime("%m-%d %H:%M", time.localtime(ms / 1000))


def _table(headers: tuple[str, ...]) -> QtWidgets.QTableWidget:
    widget = QtWidgets.QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(list(headers))
    widget.verticalHeader().setVisible(False)
    widget.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.setAlternatingRowColors(True)
    widget.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
    return widget


def _fill_table(
    table: QtWidgets.QTableWidget, rows: list[tuple[Any, ...]], stretch: int | None = None
) -> None:
    table.setRowCount(len(rows))
    for index, row in enumerate(rows):
        for column, value in enumerate(row):
            table.setItem(index, column, QtWidgets.QTableWidgetItem(str(value)))
    table.resizeColumnsToContents()
    if stretch is not None:
        table.horizontalHeader().setSectionResizeMode(
            stretch, QtWidgets.QHeaderView.ResizeMode.Stretch
        )


class DataPage(QtWidgets.QWidget):
    def __init__(self, backend: DesktopBackend) -> None:
        super().__init__()
        self._backend = backend

        self._tabs = QtWidgets.QTabWidget()
        self._tabs.addTab(self._build_engines(), "引擎健康")
        self._tabs.addTab(self._build_stream(), "记忆流")
        self._tabs.addTab(self._build_pad(), "PAD 情绪曲线")
        self._tabs.addTab(self._build_synapses(), "突触关系")
        self._tabs.addTab(self._build_tiers(), "分层分布")
        self._tabs.addTab(self._build_sync(), "同步状态")
        self._tabs.addTab(self._build_sectors(), "认知分区")
        self._tabs.addTab(self._build_core(), "Core Memory")
        self._tabs.addTab(self._build_raw(), "原始 JSON")

        refresh = QtWidgets.QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        export_json = QtWidgets.QPushButton("导出全量快照 JSON")
        export_json.setObjectName("primary")
        export_json.clicked.connect(self._export_json)
        export_memories = QtWidgets.QPushButton("导出记忆 NDJSON")
        export_memories.clicked.connect(self._export_memories)
        actions = QtWidgets.QHBoxLayout()
        actions.addWidget(refresh)
        actions.addWidget(export_json)
        actions.addWidget(export_memories)
        actions.addStretch(1)

        self._status = QtWidgets.QLabel("—")
        self._status.setObjectName("hint")

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 12)
        layout.addLayout(actions)
        layout.addWidget(self._tabs, 1)
        layout.addWidget(self._status)
        self.refresh()

    # ── 面板构造 ──────────────────────────────────────────────────────────
    def _build_engines(self) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget()
        self._engine_layout = QtWidgets.QGridLayout(widget)
        self._engine_layout.setSpacing(8)
        return widget

    def _build_stream(self) -> QtWidgets.QWidget:
        self._stream_table = _table(("时间", "类型", "分层", "PAD (P/A/D)", "内容"))
        return self._stream_table

    def _build_pad(self) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        self._pad_summary = QtWidgets.QLabel("—")
        self._pad_summary.setWordWrap(True)
        self._pad_chart = Sparkline(y_range=(-1.0, 1.0))
        layout.addWidget(self._pad_summary)
        layout.addWidget(QtWidgets.QLabel("Pleasure / Arousal / Dominance 随时间的轨迹"))
        layout.addWidget(self._pad_chart, 1)
        return widget

    def _build_synapses(self) -> QtWidgets.QWidget:
        self._synapse_table = _table(("源记忆", "关系谓词", "目标记忆", "权重"))
        return self._synapse_table

    def _build_tiers(self) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        self._tier_chart = BarChart()
        self._pyramid_chart = BarChart()
        layout.addWidget(QtWidgets.QLabel("记忆分层（缓冲 / 核心 / 归档）"))
        layout.addWidget(self._tier_chart)
        layout.addWidget(QtWidgets.QLabel("认知金字塔层级"))
        layout.addWidget(self._pyramid_chart)
        layout.addStretch(1)
        return widget

    def _build_sync(self) -> QtWidgets.QWidget:
        self._sync_table = _table(("字段", "值"))
        return self._sync_table

    def _build_sectors(self) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        self._sector_chart = BarChart()
        self._sector_note = QtWidgets.QLabel("")
        self._sector_note.setWordWrap(True)
        self._sector_note.setObjectName("hint")
        layout.addWidget(QtWidgets.QLabel("五类认知分区（由 memory_type 归类，借鉴 OpenMemory 的 sectors）"))
        layout.addWidget(self._sector_chart)
        layout.addWidget(self._sector_note)
        layout.addStretch(1)
        return widget

    def _build_core(self) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        note = QtWidgets.QLabel(
            "可编辑核心记忆块（对齐 Letta ADE 的 Core Memory Blocks）。"
            "内容存 KV，编辑会写一条 policy 事件，跨设备重放可复现。"
        )
        note.setWordWrap(True)
        note.setObjectName("hint")
        self._core_editors: dict[str, QtWidgets.QPlainTextEdit] = {}
        form = QtWidgets.QFormLayout()
        for block, label in (
            ("human", "关于用户"), ("persona", "Agent 人设"),
            ("preferences", "长期偏好"), ("constraints", "约束与边界"),
        ):
            editor = QtWidgets.QPlainTextEdit()
            editor.setMaximumHeight(76)
            editor.setPlaceholderText(f"{label}：会被作为稳定的背景信息")
            self._core_editors[block] = editor
            form.addRow(f"{label}：", editor)
        save = QtWidgets.QPushButton("保存 Core Memory")
        save.setObjectName("primary")
        save.clicked.connect(self._save_core)
        self._core_status = QtWidgets.QLabel("")
        self._core_status.setObjectName("hint")
        layout.addWidget(note)
        layout.addLayout(form)
        layout.addWidget(save)
        layout.addWidget(self._core_status)
        layout.addStretch(1)
        return widget

    def _save_core(self) -> None:
        blocks = {key: editor.toPlainText() for key, editor in self._core_editors.items()}
        result = self._backend.save_core_memory_blocks(blocks)
        self._core_status.setText("已保存并写入审计事件" if result.get("ok") else "保存失败")

    def _build_raw(self) -> QtWidgets.QWidget:
        self._raw = QtWidgets.QTextEdit()
        self._raw.setReadOnly(True)
        self._raw.setLineWrapMode(QtWidgets.QTextEdit.LineWrapMode.NoWrap)
        self._raw.setStyleSheet("font-family: Consolas, 'Cascadia Mono', monospace; font-size: 11px;")
        return self._raw

    # ── 数据填充 ──────────────────────────────────────────────────────────
    def refresh(self) -> None:
        snapshot = self._backend.snapshot()
        self._fill_engines(snapshot.get("engine_health", {}))
        self._fill_stream(snapshot.get("memory_stream", {}))
        self._fill_pad(snapshot.get("pad_axis", {}))
        self._fill_synapses(snapshot.get("synapse_topology", {}))
        self._fill_tiers(snapshot.get("tier_distribution", {}))
        self._fill_sync(snapshot.get("sync_status", {}))
        self._fill_sectors()
        self._fill_core()
        self._raw.setPlainText(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str))
        self._status.setText("快照已刷新（只读；数据源 ui.panels 纯函数，不碰数据库文件）")

    @staticmethod
    def _clear(layout: QtWidgets.QLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                old = item.widget()
                old.hide()
                old.setParent(None)
                old.deleteLater()

    def _fill_engines(self, payload: dict) -> None:
        self._clear(self._engine_layout)
        statuses = list(payload.get("engines") or [])
        for index, status in enumerate(statuses):
            ok = bool(status.get("ok"))
            color = "#3DDC97" if ok else "#FF6B6B"
            card = QtWidgets.QFrame()
            card.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
            card.setStyleSheet(f"QFrame {{ border-left: 3px solid {color}; padding: 6px; }}")
            box = QtWidgets.QVBoxLayout(card)
            name = QtWidgets.QLabel(str(status.get("engine") or status.get("name") or "引擎"))
            name.setStyleSheet("font-weight: 600;")
            box.addWidget(name)
            box.addWidget(QtWidgets.QLabel(f"状态：{'正常' if ok else '异常'}　延迟 {status.get('latency_ms', '—')} ms"))
            if status.get("version"):
                box.addWidget(QtWidgets.QLabel(f"版本：{status['version']}"))
            if status.get("detail"):
                detail = QtWidgets.QLabel(str(status["detail"]))
                detail.setWordWrap(True)
                detail.setObjectName("hint")
                box.addWidget(detail)
            extra = status.get("extra") or {}
            if extra:
                note = QtWidgets.QLabel(json.dumps(extra, ensure_ascii=False, default=str)[:120])
                note.setWordWrap(True)
                note.setObjectName("hint")
                box.addWidget(note)
            self._engine_layout.addWidget(card, index // 3, index % 3)
        if not statuses:
            self._engine_layout.addWidget(QtWidgets.QLabel("无引擎状态数据"), 0, 0)
        # 多余高度交给空行，避免卡片被网格拉成细长条
        self._engine_layout.setRowStretch(len(statuses) // 3 + 1, 1)

    def _fill_stream(self, payload: dict) -> None:
        items = payload.get("items") or []
        rows = []
        for item in items:
            pad = item.get("pad") or (0.0, 0.0, 0.0)
            rows.append((
                _fmt_ms(item.get("ts_ms")),
                item.get("memory_type", "—"),
                item.get("tier", "—"),
                f"{float(pad[0]):+.2f} / {float(pad[1]):+.2f} / {float(pad[2]):+.2f}",
                str(item.get("content") or "")[:100],
            ))
        _fill_table(self._stream_table, rows, stretch=4)

    def _fill_pad(self, payload: dict) -> None:
        series = list(payload.get("series") or [])
        summary = dict(payload.get("summary") or {})
        mean = summary.get("pad") or (0.0, 0.0, 0.0)
        peak = summary.get("peak") or {}
        self._pad_summary.setText(
            f"样本 {summary.get('samples', len(series))} 个 · "
            f"均值 P{float(mean[0]):+.2f} / A{float(mean[1]):+.2f} / D{float(mean[2]):+.2f}"
            + (f" · 最强烈：A{float(peak.get('a', 0)):+.2f} @ {_fmt_ms(peak.get('ts_ms'))}" if peak else "")
        )
        keys = (("p", "P 愉悦度", _PAD_COLORS[0]), ("a", "A 唤醒度", _PAD_COLORS[1]),
                ("d", "D 支配度", _PAD_COLORS[2]))
        self._pad_chart.set_series(
            [(label, color, [float(point.get(key, 0.0)) for point in series]) for key, label, color in keys]
        )

    def _fill_synapses(self, payload: dict) -> None:
        edges = payload.get("edges") or []
        rows = [
            (str(edge.get("source") or "")[:18], edge.get("predicate", "related"),
             str(edge.get("target") or "")[:18], f"{float(edge.get('weight') or 0):.2f}")
            for edge in edges
        ]
        _fill_table(self._synapse_table, rows, stretch=1)

    def _fill_tiers(self, payload: dict) -> None:
        for key, chart in (("tiers", self._tier_chart), ("pyramid", self._pyramid_chart)):
            values = dict(payload.get(key) or {})
            chart.set_bars([
                (str(name), float(count), _TIER_COLORS[index % len(_TIER_COLORS)])
                for index, (name, count) in enumerate(sorted(values.items()))
            ])

    def _fill_sectors(self) -> None:
        counts = self._backend.cognitive_sectors()
        self._sector_chart.set_bars([
            (name, float(counts.get(name, 0)), color) for name, color in _SECTOR_COLORS
        ])
        self._sector_note.setText(
            "情景=经历与事件，语义=客观事实，程序=代码与流程，情绪=带 PAD 的感受，画像=稳定偏好。\n"
            "分区只是展示视角，不改变 tier / pyramid 的实际分层与召回逻辑。"
        )

    def _fill_core(self) -> None:
        blocks = self._backend.core_memory_blocks()
        for block, editor in self._core_editors.items():
            editor.setPlainText(str(blocks.get(block, "")))

    def _fill_sync(self, payload: dict) -> None:
        rows: list[tuple[Any, ...]] = []
        for key, value in payload.items():
            if isinstance(value, dict):
                for device, cursor in value.items():
                    rows.append((f"游标 · {device}", str(cursor)))
            else:
                rows.append((str(key), str(value)))
        _fill_table(self._sync_table, rows, stretch=1)

    # ── 导出 ──────────────────────────────────────────────────────────────
    def _export_json(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "导出全量快照", "brain_snapshot.json", "JSON (*.json)"
        )
        if path:
            self._status.setText(f"已导出：{self._backend.export_dashboard(path)}")

    def _export_memories(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "导出记忆", "memories.ndjson", "NDJSON (*.ndjson)"
        )
        if path:
            self._status.setText(f"已导出记忆 NDJSON：{self._backend.export_memories(path)}")
