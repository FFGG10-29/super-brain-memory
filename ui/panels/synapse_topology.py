"""ui.panels.synapse_topology —— 突触拓扑数据源（节点/边，供前端图布局）。"""
from __future__ import annotations

from core.interfaces import RelationalRepository

__all__ = ["data"]


def data(relational: RelationalRepository, *, limit: int = 100) -> dict:
    rows = relational.query("synapse", limit=limit)
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    for row in rows:
        subject, obj = str(row.get("subject_id", "")), str(row.get("object_id", ""))
        if not subject or not obj:
            continue
        nodes.setdefault(subject, {"id": subject, "label": subject})
        nodes.setdefault(obj, {"id": obj, "label": obj})
        edges.append({
            "source": subject,
            "target": obj,
            "predicate": row.get("predicate", "related"),
            "weight": row.get("weight", 1.0),
        })
    return {"nodes": list(nodes.values()), "edges": edges}
