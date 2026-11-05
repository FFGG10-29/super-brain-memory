"""models.hyperedge —— 超边：多实体复杂事件（高阶情景 / 多 Agent 协作）。

物理编码：Kùzu 里"超边 = 事件节点 + 成员边"（二部图）；本模型是逻辑表示，
`members` 即关联矩阵的一行，高阶分析（s-连通等）由 storage.hypergraph_db 负责。
"""
from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.ids import new_uuid7
from core.interfaces import Namespace, Record, Tier
from models.neuron import PAD

__all__ = ["Hyperedge"]


def _now_ms() -> int:
    return int(time.time() * 1000)


class Hyperedge(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str = Field(default_factory=new_uuid7)
    device_id: str = "dev-unknown"
    agent_id: str = "agent-unknown"
    label: str = Field(min_length=1)
    members: list[str] = Field(default_factory=list)
    pad: PAD = Field(default_factory=PAD)
    occurred_at_ms: int = Field(default_factory=_now_ms)

    namespace: Namespace = "private"
    tier: Tier = "buffer"
    valid_from_ms: int = Field(default_factory=_now_ms)
    valid_to_ms: int | None = None
    transaction_from: str = ""
    transaction_to: str | None = None
    is_active: bool = True

    @field_validator("members")
    @classmethod
    def _dedupe_members(cls, value: list[str]) -> list[str]:
        # 顺序稳定去重：高阶分析结果必须确定性，否则跨设备重放不一致
        return list(dict.fromkeys(member for member in value if member))

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "device_id": self.device_id,
            "agent_id": self.agent_id,
            "label": self.label,
            "members": list(self.members),
            "pad_p": self.pad.p,
            "pad_a": self.pad.a,
            "pad_d": self.pad.d,
            "occurred_at_ms": self.occurred_at_ms,
            "namespace": self.namespace,
            "tier": self.tier,
            "valid_from_ms": self.valid_from_ms,
            "valid_to_ms": self.valid_to_ms,
            "tx_from": self.transaction_from,
            "tx_to": self.transaction_to,
            "is_active": self.is_active,
        }

    @classmethod
    def from_record(cls, record: Record) -> "Hyperedge":
        data = dict(record)
        pad = PAD(
            p=float(data.pop("pad_p", 0.0)),
            a=float(data.pop("pad_a", 0.0)),
            d=float(data.pop("pad_d", 0.0)),
        )
        return cls(
            id=str(data.pop("id", "")),
            pad=pad,
            transaction_from=str(data.pop("tx_from", "") or ""),
            transaction_to=data.pop("tx_to", None),
            **{key: value for key, value in data.items() if key in cls.model_fields},
        )
