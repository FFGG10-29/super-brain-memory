"""ui.desktop.backend —— 桌面前端唯一的数据出口（服务层桥接）。

UI 只允许 import 本模块；它内部通过组合根 `build_app()` 装配，
再只调用 brain 服务层与 `Dashboard` 纯数据视图，绝不暴露 storage 实现。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from brain.activation import ActivationGraph, ActivationService
from brain.memory_browser import MemoryBrowser
from config import BrainConfig, get_config
from main import build_app
from ui.dashboard import Dashboard

__all__ = ["DesktopBackend", "RecallView"]


@dataclass
class RecallView:
    query: str
    hits: list[dict] = field(default_factory=list)
    activations: list[tuple[str, float]] = field(default_factory=list)
    injection: str = ""
    degraded: bool = False


class DesktopBackend:
    def __init__(self, config: BrainConfig | None = None, *, auto_start: bool = True) -> None:
        self._config = config or get_config()
        self._app = None
        self._activation: ActivationService | None = None
        self._browser: MemoryBrowser | None = None
        self._dashboard: Dashboard | None = None
        if auto_start:
            self.start()

    # ── 生命周期 ──────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._app is not None:
            return
        app = build_app(self._config)
        self._app = app
        self._activation = ActivationService(app.storage.relational, app.embedder, self._config)
        self._browser = MemoryBrowser(app.storage.relational)
        self._dashboard = Dashboard(
            engines=app.storage.all(), relational=app.storage.relational,
            timeseries=app.storage.timeseries, event_store=app.event_store,
            journal=app.journal, root=self._config.sync.local_repo_dir,
            device_id=self._config.sync.device_id,
        )

    def close(self) -> None:
        if self._app is not None:
            self._app.circadian.stop()
            self._app.journal.flush()
            self._app.close()
            self._app = None

    @property
    def config(self) -> BrainConfig:
        return self._config

    # ── 星云数据 ──────────────────────────────────────────────────────────
    def graph(self) -> ActivationGraph:
        self.start()
        assert self._activation is not None
        return self._activation.build()

    def memories(self, *, limit: int = 80) -> list[dict]:
        self.start()
        assert self._browser is not None
        return self._browser.list(limit=limit)

    def memory_detail(self, memory_id: str) -> str:
        self.start()
        assert self._browser is not None
        row = self._browser.get(memory_id)
        return self._browser.detail(row) if row is not None else "记忆不存在或已被封存"

    # ── 检索 → 光脉冲 ────────────────────────────────────────────────────
    def recall(self, query: str, *, limit: int | None = None, agent_id: str = "desktop") -> RecallView:
        self.start()
        assert self._app is not None and self._activation is not None
        if not query.strip():
            return RecallView(query=query)
        graph = self._activation.build()  # 刷新向量缓存，保证新写入的记忆也能被激活
        hits = self._app.prefrontal.recall(
            query, agent_id=agent_id, limit=limit or self._config.recall.top_k
        )
        rows = [
            {
                "id": hit.neuron.uuid7_id,
                "content": hit.neuron.content,
                "score": round(hit.score, 4),
                "channels": list(hit.channels),
                "pad": hit.neuron.pad.as_tuple(),
            }
            for hit in hits
        ]
        similarities = dict(self._activation.activation_scores(query, [row["id"] for row in rows]))
        max_score = max((row["score"] for row in rows), default=1.0) or 1.0
        activations = [
            (
                row["id"],
                round(0.5 * similarities.get(row["id"], 0.0) + 0.5 * (row["score"] / max_score), 4),
            )
            for row in rows
        ]
        return RecallView(
            query=query,
            hits=rows,
            activations=activations,
            injection=self._app.prefrontal.inject(hits, agent_id=agent_id),
            degraded=graph.degraded,
        )

    def related(self, memory_id: str) -> list[tuple[str, float]]:
        """点击某条记忆 → 与它最相似的其他记忆（光点齐射目标）。"""
        self.start()
        assert self._activation is not None
        graph = self._activation.build()
        edges = [edge for edge in graph.edges if edge.source == memory_id or edge.target == memory_id]
        targets = [
            (edge.target if edge.source == memory_id else edge.source, min(1.0, edge.weight))
            for edge in sorted(edges, key=lambda item: item.weight, reverse=True)
        ]
        return targets[: self._config.activation.max_pulses]

    def save_memory(self, content: str, *, agent_id: str = "desktop") -> dict:
        self.start()
        assert self._app is not None
        result = self._app.hippocampus.remember(content, agent_id=agent_id)
        return {"applied": result.applied, "id": result.neuron.uuid7_id, "reason": result.admission.reason}

    # ── 状态面板 ──────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        self.start()
        assert self._dashboard is not None
        return self._dashboard.snapshot()
