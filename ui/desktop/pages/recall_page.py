"""ui.desktop.pages.recall_page —— 召回测试台：看通道 / 看分数 / 一键点亮星云。"""
from __future__ import annotations

import time

from PySide6 import QtCore, QtWidgets

from ui.desktop.backend import DesktopBackend, RecallView
from ui.desktop.inspector import BarChart

__all__ = ["RecallPage"]

_COLUMNS = ("分数", "通道", "PAD (P/A/D)", "命中记忆")
_CHANNELS = (
    ("dense", "语义向量", "#6EC6FF"),
    ("sparse", "稀疏词元", "#B08CFF"),
    ("fts", "全文检索", "#3DDC97"),
    ("graph", "图谱多跳", "#FFB84D"),
    ("timeline", "时间线", "#FF8FB1"),
    ("profile", "用户画像", "#7FE0D4"),
)


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
        self._namespace = QtWidgets.QComboBox()
        self._namespace.addItem("全部命名空间")
        self._namespace.addItems(backend.namespaces())
        run = QtWidgets.QPushButton("召回")
        run.setObjectName("primary")
        run.clicked.connect(self.run_recall)
        activate = QtWidgets.QPushButton("在星云中激活")
        activate.clicked.connect(self._activate)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(self._query, 1)
        top.addWidget(QtWidgets.QLabel("top_k"))
        top.addWidget(self._top_k)
        top.addWidget(self._namespace)
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

        self._table.itemSelectionChanged.connect(self._explain_hit)

        # ── 检索即解释：通道分解 / 单条归因 / 注入上下文 ──────────────────
        self._channel_chart = BarChart()
        self._channel_note = QtWidgets.QLabel("—")
        self._channel_note.setWordWrap(True)
        self._channel_note.setObjectName("hint")
        self._weights = QtWidgets.QLabel("—")
        self._weights.setWordWrap(True)
        self._weights.setObjectName("hint")
        channel_tab = QtWidgets.QWidget()
        channel_layout = QtWidgets.QVBoxLayout(channel_tab)
        channel_layout.addWidget(QtWidgets.QLabel("各路召回命中数（条）"))
        channel_layout.addWidget(self._channel_chart)
        channel_layout.addWidget(self._channel_note)
        channel_layout.addWidget(self._weights)
        channel_layout.addStretch(1)

        self._why = QtWidgets.QTextEdit()
        self._why.setReadOnly(True)
        self._why.setPlaceholderText("点击上方任意一条命中，查看它为什么被召回")

        self._injection = QtWidgets.QTextEdit()
        self._injection.setReadOnly(True)
        self._injection.setPlaceholderText("召回结果将作为上下文注入（L0-L4 收敛注入）")

        self._explain_tabs = QtWidgets.QTabWidget()
        self._explain_tabs.addTab(channel_tab, "通道解释")
        self._explain_tabs.addTab(self._why, "为什么命中")
        self._explain_tabs.addTab(self._injection, "注入上下文")
        self._explain_tabs.setMaximumHeight(300)

        self._status = QtWidgets.QLabel("—")
        self._status.setObjectName("hint")

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 12)
        layout.addLayout(top)
        layout.addWidget(self._table, 1)
        layout.addWidget(self._explain_tabs)
        layout.addWidget(self._status)

    def run_recall(self) -> RecallView:
        query = self._query.text().strip()
        if not query:
            return RecallView(query=query)
        top_k = self._top_k.value()
        selected = self._namespace.currentText()
        namespaces = () if selected == "全部命名空间" else (selected,)
        started = time.perf_counter()
        self._status.setText("召回中…（六路融合 + 精排）")
        self._backend.tasks.submit(
            lambda: self._backend.recall(query, limit=top_k, namespaces=namespaces),
            lambda view: self._on_recalled(view, started),
            lambda message: self._status.setText(f"召回失败：{message}"),
        )
        return RecallView(query=query)

    def _on_recalled(self, view: RecallView, started: float) -> None:
        self._view = view
        self.render(view, (time.perf_counter() - started) * 1000)

    def render(self, view: RecallView, cost_ms: float) -> None:
        self._table.setRowCount(len(view.hits))
        sensitive = set(self._backend.sensitive_ids())
        for index, hit in enumerate(view.hits):
            pad = hit["pad"]
            cells = (
                f"{float(hit['score']):.4f}",
                "+".join(hit["channels"]) or "—",
                f"{float(pad[0]):+.2f} / {float(pad[1]):+.2f} / {float(pad[2]):+.2f}",
                "••••••（敏感记忆已遮蔽）" if str(hit.get("id")) in sensitive
                else str(hit["content"])[:90],
            )
            for column, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                if column == 0:
                    item.setData(QtCore.Qt.ItemDataRole.UserRole, hit["id"])
                self._table.setItem(index, column, item)
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self._view = view
        audit = dict(view.audit or {})
        counts = dict(audit.get("counts") or {})
        injected = dict(audit.get("injected") or {})
        self._channel_chart.set_bars(
            [(label, float(counts.get(key, 0)), color) for key, label, color in _CHANNELS]
        )
        self._channel_note.setText(
            "　".join(
                f"{label} {counts.get(key, 0)}→{injected.get(key, 0)}"
                for key, label, _color in _CHANNELS
            ) + "　（召回数 → 最终注入数）"
        )
        weights = dict(audit.get("weights") or {})
        self._weights.setText(
            "融合权重：RRF {rrf} / 新鲜度 {freshness} / 突触 {synapse} / "
            "情绪 {pad} / 置信度 {confidence}　·　交叉编码精排：{rerank}".format(
                rrf=weights.get("rrf", "—"), freshness=weights.get("freshness", "—"),
                synapse=weights.get("synapse", "—"), pad=weights.get("pad", "—"),
                confidence=weights.get("confidence", "—"),
                rerank="已启用" if audit.get("rerank") else "已关闭",
            )
        )
        self._injection.setPlainText(view.injection or "（无注入上下文）")
        source = "降级（字符 n-gram）" if view.degraded else "BGE-M3"
        tokens = max(0, len(view.injection or "") // 2)
        self._status.setText(
            f"命中 {len(view.hits)} 条 · {cost_ms:.0f} ms · 向量源 {source} · "
            f"注入约 {tokens} tokens（按字符粗估）"
        )
        self._explain_hit()

    def _explain_hit(self) -> None:
        """选中一条命中 → 解释它凭什么被召回（可解释检索）。"""
        view = self._view
        if view is None:
            return
        rows = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        if not rows or rows[0].row() >= len(view.hits):
            self._why.setPlainText("点击上方任意一条命中，查看它为什么被召回")
            return
        hit = view.hits[rows[0].row()]
        audit = dict(view.audit or {})
        similarities = dict(audit.get("similarities") or {})
        activations = dict(view.activations)
        weights = dict(audit.get("weights") or {})
        pad = hit.get("pad", (0.0, 0.0, 0.0))
        lines = [
            f"记忆 ID　　{hit.get('id', '—')}",
            f"最终得分　{float(hit.get('score', 0)):.4f}",
            f"命中通道　{', '.join(hit.get('channels') or []) or '—'}",
            f"向量余弦　{float(similarities.get(str(hit.get('id')), 0.0)):.4f}（BGE-M3 dense）",
            f"星云激活　{float(activations.get(str(hit.get('id')), 0.0)):.4f}",
            f"PAD　　　 {float(pad[0]):+.2f} / {float(pad[1]):+.2f} / {float(pad[2]):+.2f}",
            "── 分数构成（配置权重）──",
            f"多通道 RRF 融合　× {weights.get('rrf', '—')}",
            f"新鲜度衰减　　　× {weights.get('freshness', '—')}",
            f"突触可提取性　　× {weights.get('synapse', '—')}",
            f"情绪显著度　　　× {weights.get('pad', '—')}",
            f"记忆置信度　　　× {weights.get('confidence', '—')}",
            f"交叉编码精排　　{'已启用（与融合分各占一半）' if audit.get('rerank') else '已关闭'}",
            "",
            "••••••（敏感，可在记忆检查器中显式揭示）"
            if str(hit.get("id")) in set(self._backend.sensitive_ids())
            else str(hit.get("content") or "")[:300],
        ]
        self._why.setPlainText("\n".join(lines))

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
