"""ui.panels.tier_distribution —— 分层分布 + 金字塔分布（两个正交维度）。"""
from __future__ import annotations

from core.interfaces import RelationalRepository

__all__ = ["data"]


def data(relational: RelationalRepository, *, limit: int = 1000) -> dict:
    rows = relational.query("neuron", limit=limit)
    tiers: dict[str, int] = {}
    pyramid: dict[str, int] = {}
    for row in rows:
        tier = str(row.get("tier", "buffer"))
        level = str(row.get("pyramid", "raw"))
        tiers[tier] = tiers.get(tier, 0) + 1
        pyramid[level] = pyramid.get(level, 0) + 1
    return {"tiers": tiers, "pyramid": pyramid, "total": len(rows)}
