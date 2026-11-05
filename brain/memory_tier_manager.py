"""brain.memory_tier_manager —— 分层：缓冲 → 核心 → 召回 → 归档。

与金字塔正交：tier 按热度/可提取性流动，pyramid 按抽象度夜间升维，互不混用。
不变量：
- tier=buffer 只留本地（不同步）；晋升出 buffer 才产生 tier_change 跨设备事件。
- 归档不删除，只是退出常规召回（prefrontal 已过滤）。
- 核心层不因衰减被自动归档（需人工/复盘降级）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from config import BrainConfig, get_config
from core.interfaces import EventLog, RelationalRepository, Tier
from models.neuron import Neuron
from brain.synaptic_plasticity import SynapticPlasticity

__all__ = ["MemoryTierManager", "TierSweepReport"]

_ARCHIVE = "archive"


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class TierDecision:
    neuron_id: str
    from_tier: str
    to_tier: str
    reason: str


@dataclass
class TierSweepReport:
    scanned: int = 0
    decisions: list[TierDecision] = field(default_factory=list)
    events_emitted: int = 0

    @property
    def archived(self) -> list[str]:
        return [item.neuron_id for item in self.decisions if item.to_tier == _ARCHIVE]


class MemoryTierManager:
    def __init__(
        self,
        relational: RelationalRepository,
        plasticity: SynapticPlasticity,
        *,
        event_log: EventLog | None = None,
        config: BrainConfig | None = None,
    ) -> None:
        self._relational = relational
        self._plasticity = plasticity
        self._event_log = event_log
        self._config = config or get_config()

    def decide(self, neuron: Neuron, *, now_ms: int | None = None) -> tuple[Tier, str]:
        params = self._config.plasticity
        retrievability = self._plasticity.retrievability(neuron, now_ms=now_ms)

        if neuron.confidence >= params.consolidate_confidence and neuron.recall_count >= params.reinforce_threshold:
            return "core", "高置信 + 多次召回"
        if neuron.tier == "core":
            return "core", "核心层不自动降级"
        if retrievability < self._config.tier.archive_retrievability_threshold:
            return _ARCHIVE, f"可提取性 {retrievability:.2f} 低于归档阈值"
        if neuron.confidence < params.review_queue_ceiling:
            return "recall", "低置信度入召回层复核队列"
        if neuron.tier == "buffer" and retrievability > 0.5:
            return "recall", "缓冲层巩固为召回层"
        return neuron.tier, "保持"

    def sweep(self, *, limit: int = 500, now_ms: int | None = None) -> TierSweepReport:
        now = _now_ms() if now_ms is None else int(now_ms)
        report = TierSweepReport()
        for record in self._relational.query("neuron", limit=limit):
            neuron = Neuron.from_record(record)
            if neuron.tier == _ARCHIVE:
                continue
            report.scanned += 1
            target, reason = self.decide(neuron, now_ms=now)
            if target == neuron.tier:
                continue
            self._relational.set_tier("neuron", neuron.uuid7_id, target)
            decision = TierDecision(neuron.uuid7_id, neuron.tier, target, reason)
            report.decisions.append(decision)
            if self._emit(neuron, decision, now):
                report.events_emitted += 1
        return report

    def _emit(self, neuron: Neuron, decision: TierDecision, now_ms: int) -> bool:
        """缓冲层不同步：只有晋升出 buffer（或跨非缓冲层迁移）才发事件。"""
        if self._event_log is None:
            return False
        if decision.from_tier == "buffer" and decision.to_tier == "buffer":
            return False
        if self._config.tier.buffer_no_sync and decision.from_tier == "buffer" and decision.to_tier == "recall":
            # buffer→recall 只是本地巩固，尚未值得跨设备；core/archive 才发
            return False
        self._event_log.append(
            {
                "event_id": f"{neuron.uuid7_id}:tier:{now_ms}",
                "type": "tier_change",
                "agent_id": neuron.agent_id,
                "hlc": f"{now_ms}:0:{self._config.sync.device_id}",
                "payload": {
                    "neuron_id": neuron.uuid7_id,
                    "from": decision.from_tier,
                    "to": decision.to_tier,
                    "reason": decision.reason,
                },
            }
        )
        return True
