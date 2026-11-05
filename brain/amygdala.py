"""brain.amygdala —— 杏仁核：PAD 三维 + 好感度低通积分 + 情绪边界。

情绪全链路的"生成/建模"端（架构 情绪记忆全链路 §）：
- PAD 精算：LLM 粗估 → 与历史基线平滑（`pad_smoothing`），避免单次波动改写长期情绪；
- affinity：P 维度低通积分慢变量（`affinity_alpha` 越小越慢变）——单次情绪只是波动；
- 情绪边界：高负唤醒（P≤阈值 且 A≥阈值）触发冷静期，短期压制该 Agent 记忆的召回优先级；
- 轨迹：每次 ingest 由调用方写 DuckDB pad_track（只写不改），杏仁核本身不碰时序库，
  保持"认知层只依赖抽象接口"的边界。

状态持久化：KV 单键（bucket=amygdala, key=agent_id），确定性计算，可重放。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from config import AmygdalaConfig, BrainConfig, get_config
from core.interfaces import KVRepository
from models.neuron import PAD

__all__ = ["AffectiveState", "Amygdala"]

_BUCKET = "amygdala"


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class AffectiveState:
    agent_id: str
    pad: PAD            # 平滑后的情绪基线
    affinity: float     # 好感度慢变量 ∈ [-1,1]
    calm_until_ms: int  # 冷静期截止（0 表示无）
    updated_at_ms: int

    def to_json(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "pad": {"p": self.pad.p, "a": self.pad.a, "d": self.pad.d},
            "affinity": self.affinity,
            "calm_until_ms": self.calm_until_ms,
            "updated_at_ms": self.updated_at_ms,
        }

    @classmethod
    def from_json(cls, data: dict[str, object]) -> "AffectiveState":
        raw_pad = data.get("pad") or {}
        assert isinstance(raw_pad, dict)
        return cls(
            agent_id=str(data.get("agent_id", "")),
            pad=PAD(
                p=float(raw_pad.get("p", 0.0)),
                a=float(raw_pad.get("a", 0.0)),
                d=float(raw_pad.get("d", 0.0)),
            ),
            affinity=float(data.get("affinity", 0.0)),
            calm_until_ms=int(data.get("calm_until_ms", 0)),
            updated_at_ms=int(data.get("updated_at_ms", 0)),
        )


class Amygdala:
    """情绪加工。所有系数读 config，禁止硬编码阈值。"""

    def __init__(self, kv: KVRepository, config: BrainConfig | None = None) -> None:
        self._kv = kv
        self._config = config or get_config()

    @property
    def params(self) -> AmygdalaConfig:
        return self._config.amygdala

    def state(self, agent_id: str) -> AffectiveState:
        raw = self._kv.get_json(_BUCKET, agent_id)
        if not raw:
            return AffectiveState(
                agent_id=agent_id, pad=PAD(), affinity=0.0, calm_until_ms=0, updated_at_ms=0
            )
        return AffectiveState.from_json(raw)

    def ingest(self, agent_id: str, raw_pad: PAD, *, now_ms: int | None = None) -> AffectiveState:
        """把一次情绪事件并入基线：PAD 平滑 + 好感度积分 + 冷静期判定。"""
        now = _now_ms() if now_ms is None else int(now_ms)
        previous = self.state(agent_id)
        params = self.params

        smoothing = params.pad_smoothing
        blended = PAD(
            p=_clamp((1.0 - smoothing) * previous.pad.p + smoothing * raw_pad.p),
            a=_clamp((1.0 - smoothing) * previous.pad.a + smoothing * raw_pad.a),
            d=_clamp((1.0 - smoothing) * previous.pad.d + smoothing * raw_pad.d),
        )
        # 好感度 = P 维度的时间积分慢变量
        affinity = _clamp(previous.affinity + params.affinity_alpha * raw_pad.p)

        calm_until = previous.calm_until_ms
        if raw_pad.p <= params.negative_valence_threshold and raw_pad.a >= params.negative_arousal_threshold:
            calm_until = max(calm_until, now + params.calm_period_ms)

        state = AffectiveState(
            agent_id=agent_id,
            pad=blended,
            affinity=affinity,
            calm_until_ms=calm_until,
            updated_at_ms=now,
        )
        self._kv.put_json(_BUCKET, agent_id, state.to_json())
        return state

    def recall_weight(self, agent_id: str, *, now_ms: int | None = None) -> float:
        """召回偏置：好感度微调 + 冷静期折扣（架构 情绪边界 §）。"""
        now = _now_ms() if now_ms is None else int(now_ms)
        state = self.state(agent_id)
        if state.calm_until_ms > now:
            return self.params.calm_recall_penalty
        # 好感度 ∈ [-1,1] → 权重 ∈ [0.85, 1.15]，只做微调，不翻天
        return 1.0 + 0.15 * state.affinity

    def inject_tone(self, agent_id: str, *, now_ms: int | None = None) -> str:
        """给前额叶的注入语气提示（冷静期 / 好感度 / 中性）。"""
        now = _now_ms() if now_ms is None else int(now_ms)
        state = self.state(agent_id)
        if state.calm_until_ms > now:
            return "该话题刚发生高强度负面事件：收敛措辞，先确认再下结论"
        if state.affinity <= -0.3:
            return "该 Agent 与用户历史交互偏负面：收敛措辞，先确认再下结论"
        if state.affinity >= 0.3:
            return "该 Agent 与用户历史交互积极：可用更直接的技术表达"
        return "中性语气"

    def refine(self, raw_pad: PAD) -> PAD:
        """记忆自身的情绪坐标：保留事件原始色彩（不做基线平滑）。

        平滑只用于 Agent 情绪基线（state.pad）——单次事件不应被历史稀释，
        否则"闪光灯记忆"（高唤醒）在存储端就先被抹平了。
        """
        return PAD(p=raw_pad.p, a=raw_pad.a, d=raw_pad.d)


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))
