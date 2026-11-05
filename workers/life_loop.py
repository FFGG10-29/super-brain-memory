"""workers.life_loop —— 默认模式网络：夜间复盘、图谱修剪、遗忘清扫、晨报。

纪律：只读时序库/主库，改写必须走海马体或 MemoryTierManager 发事件；
      不物理删除任何记录（归档与弱边标记都保留可追溯性）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from config import BrainConfig, get_config
from core.interfaces import GraphRepository, RelationalRepository, TimeSeriesRepository
from brain.memory_tier_manager import MemoryTierManager

__all__ = ["EmotionalReview", "LifeLoop", "NightlyReport"]

_MS_PER_DAY = 86_400_000.0


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class EmotionalReview:
    samples: int
    mean_p: float
    mean_a: float
    mean_d: float
    trend: str
    delta_p: float
    anomalies: list[dict[str, Any]] = field(default_factory=list)

    def as_brief(self) -> str:
        if self.samples == 0:
            return "情绪复盘：无样本（当天没有写入）"
        trend_text = {
            "improving": "情绪回升",
            "worsening": "情绪走低",
            "stable": "情绪平稳",
            "insufficient": "样本不足",
        }.get(self.trend, self.trend)
        lines = [
            f"情绪复盘：{self.samples} 条样本，均值 P{self.mean_p:+.2f}/A{self.mean_a:+.2f}/D{self.mean_d:+.2f} → {trend_text}",
        ]
        if self.anomalies:
            lines.append(f"异常波动 {len(self.anomalies)} 处，已标记待复查")
        return "\n".join(lines)


@dataclass
class NightlyReport:
    emotional: EmotionalReview | None = None
    archived: list[str] = field(default_factory=list)
    tier_changes: int = 0
    weak_relations: list[str] = field(default_factory=list)
    consolidations: int = 0

    def as_brief(self) -> str:
        lines = ["【晨报】"]
        if self.emotional:
            lines.append(self.emotional.as_brief())
        lines.append(f"分层迁移 {self.tier_changes} 条，归档 {len(self.archived)} 条")
        lines.append(f"金字塔固化 {self.consolidations} 个主题，弱关系标记 {len(self.weak_relations)} 条")
        return "\n".join(lines)


class LifeLoop:
    def __init__(
        self,
        timeseries: TimeSeriesRepository,
        *,
        relational: RelationalRepository | None = None,
        graph: GraphRepository | None = None,
        tier_manager: MemoryTierManager | None = None,
        config: BrainConfig | None = None,
    ) -> None:
        self._timeseries = timeseries
        self._relational = relational
        self._graph = graph
        self._tier_manager = tier_manager
        self._config = config or get_config()

    def emotional_review(self, *, since_ms: int | None = None, agent_id: str | None = None) -> EmotionalReview:
        since = _now_ms() - int(_MS_PER_DAY * 1000) if since_ms is None else int(since_ms)
        where = "WHERE ts_ms >= ?"
        params: list[object] = [since]
        if agent_id:
            where += " AND agent_id = ?"
            params.append(agent_id)
        rows = self._timeseries.query(
            f"SELECT ts_ms, p, a, d FROM pad_track {where} ORDER BY ts_ms", params
        )
        if not rows:
            return EmotionalReview(0, 0.0, 0.0, 0.0, "insufficient", 0.0)

        count = len(rows)
        mean_p = sum(float(row["p"]) for row in rows) / count
        mean_a = sum(float(row["a"]) for row in rows) / count
        mean_d = sum(float(row["d"]) for row in rows) / count
        half = max(1, count // 2)
        first_half, second_half = rows[:half], (rows[half:] or rows[-1:])
        delta_p = (
            sum(float(row["p"]) for row in second_half) / len(second_half)
            - sum(float(row["p"]) for row in first_half) / len(first_half)
        )
        if count < 2:
            trend = "insufficient"
        elif delta_p > 0.15:
            trend = "improving"
        elif delta_p < -0.15:
            trend = "worsening"
        else:
            trend = "stable"
        anomalies = [
            {
                "ts_ms": int(row["ts_ms"]),
                "p": float(row["p"]),
                "a": float(row["a"]),
                "reason": "高负唤醒",
            }
            for row in rows
            if float(row["p"]) <= self._config.amygdala.negative_valence_threshold
            and float(row["a"]) >= self._config.amygdala.negative_arousal_threshold
        ]
        return EmotionalReview(count, mean_p, mean_a, mean_d, trend, delta_p, anomalies[:20])

    def forget_sweep(self, *, limit: int = 500) -> list[str]:
        """衰减清扫：由分层管理器按可提取性归档（不物理删除）。"""
        if self._tier_manager is None:
            return []
        report = self._tier_manager.sweep(limit=limit)
        return report.archived

    def graph_prune(self, *, limit: int = 200) -> list[str]:
        """图谱修剪：找出无邻居的弱关系主体，输出待修剪清单（不删边，供复盘确认）。

        Kùzu 当前接口不暴露删除，且事件日志是全量重建真源 —— 直接删图会与重放不一致，
        因此这里只标记，由后续快照阶段统一裁决。
        """
        if self._graph is None or self._relational is None:
            return []
        weak: list[str] = []
        for record in self._relational.query("synapse", limit=limit):
            subject = str(record.get("subject_id", ""))
            if not subject or subject in weak:
                continue
            try:
                if not self._graph.neighbors(subject, hops=1, limit=1):
                    weak.append(subject)
            except Exception:  # noqa: BLE001  单个实体查询失败不阻断修剪
                continue
        return weak

    def nightly(self, *, consolidations: int = 0, agent_id: str | None = None) -> NightlyReport:
        report = NightlyReport(emotional=self.emotional_review(agent_id=agent_id))
        report.archived = self.forget_sweep()
        report.tier_changes = len(report.archived)
        report.weak_relations = self.graph_prune()
        report.consolidations = consolidations
        return report

    def morning_brief(self, *, agent_id: str | None = None) -> str:
        return self.nightly(agent_id=agent_id).as_brief()
