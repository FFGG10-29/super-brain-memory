"""brain.synaptic_plasticity —— 可塑性：FSRS 遗忘曲线 / 强化曲线。

纪律（架构 §8.2 确定性要求）：S/R 不作为存储的随机状态，而是由 reinforce 事件流
在读取时确定性计算 —— 同一事件日志在任何设备重放出完全一致的可塑性数值。

模型：
    R(t) = (1 + Δt_days / (9·S))^(-power)          幂律遗忘（FSRS 家族的稳定度形式）
    成功召回：S' = S · (1 + (1-R)) · (1 + arousal_boost·|A|)   间隔效应 + 情绪增强
    D' = max(1, D - 0.1)，strength 回弹到 max(R, 0.8)
高唤醒（|A| 大）的记忆稳定度增长更快、衰减更慢 —— 对应"闪光灯记忆"效应。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from config import BrainConfig, PlasticityConfig, get_config
from models.neuron import Neuron

__all__ = ["PlasticityState", "SynapticPlasticity"]

_MS_PER_DAY = 86_400_000.0
_STABILITY_SCALE = 9.0  # FSRS 经验常数：S=1 天时约 9 天衰减到 R≈0.5


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class PlasticityState:
    retrievability: float
    stability: float
    difficulty: float
    strength: float


class SynapticPlasticity:
    def __init__(self, config: BrainConfig | None = None) -> None:
        self._config = config or get_config()

    @property
    def params(self) -> PlasticityConfig:
        return self._config.plasticity

    def elapsed_days(self, neuron: Neuron, now_ms: int) -> float:
        anchor = neuron.last_recall_ms or neuron.valid_from_ms or neuron.created_at_ms
        return max(0.0, (int(now_ms) - int(anchor)) / _MS_PER_DAY)

    def retrievability(self, neuron: Neuron, *, now_ms: int | None = None) -> float:
        now = _now_ms() if now_ms is None else int(now_ms)
        stability = max(neuron.stability, 1e-6)
        elapsed = self.elapsed_days(neuron, now)
        return (1.0 + elapsed / (_STABILITY_SCALE * stability)) ** (-self.params.fsrs_power)

    def state(self, neuron: Neuron, *, now_ms: int | None = None) -> PlasticityState:
        retrievability = self.retrievability(neuron, now_ms=now_ms)
        return PlasticityState(
            retrievability=retrievability,
            stability=neuron.stability,
            difficulty=neuron.difficulty,
            strength=retrievability,
        )

    def decayed(self, neuron: Neuron, *, now_ms: int | None = None) -> Neuron:
        """读取时视图：strength 换成当前可提取性（不落库，保持投影确定性）。"""
        return neuron.model_copy(update={"strength": self.retrievability(neuron, now_ms=now_ms)})

    def reinforce(self, neuron: Neuron, *, now_ms: int | None = None) -> Neuron:
        """成功召回 → 稳定度增长（间隔效应），返回新 Neuron（不落库，由海马体发事件）。"""
        now = _now_ms() if now_ms is None else int(now_ms)
        retrievability = self.retrievability(neuron, now_ms=now)
        arousal = abs(neuron.pad.a)
        growth = (1.0 + (1.0 - retrievability)) * (1.0 + self.params.fsrs_arousal_boost * arousal)
        return neuron.model_copy(
            update={
                "stability": max(neuron.stability, 1e-6) * growth,
                "difficulty": max(1.0, neuron.difficulty - 0.1),
                "strength": max(retrievability, 0.8),
                "recall_count": neuron.recall_count + 1,
                "last_recall_ms": now,
            }
        )
