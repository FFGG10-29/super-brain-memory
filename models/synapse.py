"""models.synapse —— 突触：实体二元关系边 + 权重 + 时序。

冲突消解产生的 `supersedes` 演进边也是突触（predicate="supersedes"），
保证"旧事实不被物理删除、演进链条可追溯"（架构 §七.7）。
"""
from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.ids import new_uuid7
from core.interfaces import Namespace, Record, Tier

__all__ = ["Synapse"]


def _now_ms() -> int:
    return int(time.time() * 1000)


class Synapse(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str = Field(default_factory=new_uuid7)
    device_id: str = "dev-unknown"
    agent_id: str = "agent-unknown"

    subject_id: str
    predicate: str = Field(min_length=1)
    object_id: str
    weight: float = Field(default=1.0, ge=0.0)
    polarity: float = Field(default=0.0, ge=-1.0, le=1.0)  # 正/负向关系

    namespace: Namespace = "private"
    tier: Tier = "buffer"

    valid_from_ms: int = Field(default_factory=_now_ms)
    valid_to_ms: int | None = None
    transaction_from: str = ""
    transaction_to: str | None = None
    is_active: bool = True

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "device_id": self.device_id,
            "agent_id": self.agent_id,
            "subject_id": self.subject_id,
            "predicate": self.predicate,
            "object_id": self.object_id,
            "weight": self.weight,
            "polarity": self.polarity,
            "namespace": self.namespace,
            "tier": self.tier,
            "valid_from_ms": self.valid_from_ms,
            "valid_to_ms": self.valid_to_ms,
            "tx_from": self.transaction_from,
            "tx_to": self.transaction_to,
            "is_active": self.is_active,
        }

    @classmethod
    def from_record(cls, record: Record) -> "Synapse":
        data = dict(record)
        return cls(
            id=str(data.pop("id", "")),
            transaction_from=str(data.pop("tx_from", "") or ""),
            transaction_to=data.pop("tx_to", None),
            **{key: value for key, value in data.items() if key in cls.model_fields},
        )
