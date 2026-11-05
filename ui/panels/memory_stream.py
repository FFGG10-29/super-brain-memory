"""ui.panels.memory_stream —— 实时记忆流数据源（SSE 推送时只传本函数结果）。"""
from __future__ import annotations

from core.interfaces import RelationalRepository

__all__ = ["data"]


def data(relational: RelationalRepository, *, limit: int = 20) -> dict:
    rows = relational.query("neuron", limit=limit)
    return {
        "items": [
            {
                "id": str(row.get("id", "")),
                "content": str(row.get("content", "")),
                "memory_type": row.get("memory_type", "fact"),
                "tier": row.get("tier", "buffer"),
                "pad": [row.get("pad_p", 0.0), row.get("pad_a", 0.0), row.get("pad_d", 0.0)],
                "ts_ms": row.get("valid_from_ms", 0),
            }
            for row in rows
        ],
        "count": relational.count("neuron"),
    }
