"""brain.memory_browser —— 记忆查看服务（桌面/Web UI 共用）。

把主库记录整形成只读视图，UI 不接触 RelationalRepository，也不认识 SQLite。
"""
from __future__ import annotations

from core.interfaces import RelationalRepository

__all__ = ["MemoryBrowser"]


class MemoryBrowser:
    def __init__(self, relational: RelationalRepository) -> None:
        self._relational = relational

    def list(
        self, *, limit: int = 80, tier: str | None = None, memory_type: str | None = None
    ) -> list[dict[str, Any]]:
        rows = [
            {
                "id": str(row.get("id", "")),
                "content": str(row.get("content", "")),
                "memory_type": row.get("memory_type", "fact"),
                "tier": row.get("tier", "buffer"),
                "pad": (row.get("pad_p", 0.0), row.get("pad_a", 0.0), row.get("pad_d", 0.0)),
                "confidence": row.get("confidence", 0.0),
                "namespace": row.get("namespace", "private"),
                "valid_from_ms": row.get("valid_from_ms", 0),
                "valid_to_ms": row.get("valid_to_ms"),
                "tx_from": row.get("tx_from", ""),
                "is_active": row.get("is_active", True),
            }
            for row in self._relational.query("neuron", limit=limit)
        ]
        if tier:
            rows = [row for row in rows if row["tier"] == tier]
        if memory_type:
            rows = [row for row in rows if row["memory_type"] == memory_type]
        return rows

    def get(self, memory_id: str) -> dict[str, Any] | None:
        row = self._relational.get("neuron", memory_id)
        return dict(row) if row is not None else None

    @staticmethod
    def detail(row: dict[str, Any]) -> str:
        pad = (row.get("pad_p", 0.0), row.get("pad_a", 0.0), row.get("pad_d", 0.0))
        return (
            f"ID        {row.get('id')}\n"
            f"正文      {row.get('content')}\n"
            f"类型/分层 {row.get('memory_type')} / {row.get('tier')} / {row.get('pyramid')}\n"
            f"PAD       P{float(pad[0]):+.2f}  A{float(pad[1]):+.2f}  D{float(pad[2]):+.2f}\n"
            f"置信度    {row.get('confidence')}     好感度 {row.get('affinity')}\n"
            f"命名空间  {row.get('namespace')}   设备 {row.get('device_id')}\n"
            f"事件时间  {row.get('valid_from_ms')} → {row.get('valid_to_ms') or '至今'}\n"
            f"认知时间  {row.get('tx_from')} → {row.get('tx_to') or '至今'}\n"
            f"可塑性    S={row.get('stability')}  R-count={row.get('recall_count')}\n"
            f"活跃      {'是' if row.get('is_active', True) else '否（已封存）'}"
        )
