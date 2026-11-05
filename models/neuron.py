"""models.neuron —— 神经元：五类记忆 + PAD 三维 + 双时态 + 置信度 + 层级 + 可塑性变量。

双时态（架构 §六）：
    valid_from/valid_to            现实时间：事实在现实中何时成立
    transaction_from/transaction_to 系统认知时间：取 HLC token，回答"我10月时认为X是什么"

PAD 与可塑性：
    pad 是写入时估算的三维情绪坐标；strength/stability/difficulty 是 FSRS 变量，
    由 reinforce 事件流在读取时确定性计算，**不作为随机状态独立存储**。
"""
from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from config import get_config
from core.ids import new_uuid7
from core.interfaces import MemoryType, Namespace, Pyramid, Record, Tier

__all__ = ["Neuron", "PAD"]


def _now_ms() -> int:
    return int(time.time() * 1000)


class PAD(BaseModel):
    """愉悦度 / 唤醒度 / 支配度，各 ∈ [-1, 1]。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    p: float = 0.0
    a: float = 0.0
    d: float = 0.0

    @field_validator("p", "a", "d")
    @classmethod
    def _clamp(cls, value: float) -> float:
        if value < -1.0 or value > 1.0:
            raise ValueError(f"PAD 分量必须落在 [-1,1]，收到 {value}")
        return float(value)

    def salience(self) -> float:
        """情绪显著性：高唤醒（|A| 大）的记忆在召回中被加权（架构 §七.3 w4）。

        唤醒度权重进 config（`recall.pad_salience_arousal_weight`），便于 eval 调参。
        """
        arousal_weight = get_config().recall.pad_salience_arousal_weight
        magnitude = ((self.p**2 + self.a**2 + self.d**2) / 3.0) ** 0.5
        return arousal_weight * abs(self.a) + (1.0 - arousal_weight) * magnitude

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.p, self.a, self.d)


class Neuron(BaseModel):
    """一条记忆。id 用 UUIDv7，天然时间有序，可直接做事件幂等键。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    uuid7_id: str = Field(default_factory=new_uuid7)
    device_id: str = "dev-unknown"
    agent_id: str = "agent-unknown"
    memory_type: MemoryType = "fact"
    content: str = Field(min_length=1)

    pad: PAD = Field(default_factory=PAD)
    affinity: float = Field(default=0.0, ge=-1.0, le=1.0)   # 好感度慢变量
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)  # 信任梯度

    valid_from_ms: int = Field(default_factory=_now_ms)
    valid_to_ms: int | None = None
    transaction_from: str = ""      # HLC token，落库时由组合根注入
    transaction_to: str | None = None

    tier: Tier = "buffer"
    pyramid: Pyramid = "raw"
    strength: float = 1.0
    stability: float = 1.0
    difficulty: float = 5.0
    recall_count: int = 0
    last_recall_ms: int = 0          # FSRS 衰减起点；0 表示从未被召回

    namespace: Namespace = "private"
    is_active: bool = True
    created_at_ms: int = Field(default_factory=_now_ms)

    # ── 与存储层的桥接（记录是 Mapping，不含 Pydantic 类型）──────────────
    def to_record(self) -> dict[str, Any]:
        """展平为存储记录：id / pad_pad_a_pad_d / tx_from_tx_to。"""
        return {
            "id": self.uuid7_id,
            "device_id": self.device_id,
            "agent_id": self.agent_id,
            "memory_type": self.memory_type,
            "content": self.content,
            "pad_p": self.pad.p,
            "pad_a": self.pad.a,
            "pad_d": self.pad.d,
            "affinity": self.affinity,
            "confidence": self.confidence,
            "valid_from_ms": self.valid_from_ms,
            "valid_to_ms": self.valid_to_ms,
            "tx_from": self.transaction_from,
            "tx_to": self.transaction_to,
            "tier": self.tier,
            "pyramid": self.pyramid,
            "strength": self.strength,
            "stability": self.stability,
            "difficulty": self.difficulty,
            "recall_count": self.recall_count,
            "last_recall_ms": self.last_recall_ms,
            "namespace": self.namespace,
            "is_active": self.is_active,
            "created_at_ms": self.created_at_ms,
        }

    @classmethod
    def from_record(cls, record: Record) -> "Neuron":
        data = dict(record)
        pad = PAD(
            p=float(data.pop("pad_p", 0.0)),
            a=float(data.pop("pad_a", 0.0)),
            d=float(data.pop("pad_d", 0.0)),
        )
        return cls(
            uuid7_id=str(data.pop("id", data.pop("uuid7_id", ""))),
            pad=pad,
            transaction_from=str(data.pop("tx_from", "") or ""),
            transaction_to=data.pop("tx_to", None),
            **{key: value for key, value in data.items() if key in cls.model_fields},
        )

    def with_pad(self, pad: PAD) -> "Neuron":
        """复制并替换 PAD（杏仁核精算后回填）。"""
        return self.model_copy(update={"pad": pad})
