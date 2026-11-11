"""storage.hypergraph_db —— 超图逻辑层（Kùzu 编码 + HyperNetX 高阶分析）。

物理复用 graph_db 的 Kùzu Database（同进程不能开第二个 kuzu.Database，见 ADR-003）：
    超边 → HyperEvent 节点 + MEMBER 边 → 二部图编码
高阶分析（s-连通、中心度）在内存做：装了 hypernetx 就用它，没装用内置并查集
（结果确定性排序，保证跨设备重放一致）。
"""
from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

from core.errors import StorageError
from core.interfaces import GraphRepository, HealthStatus, HypergraphRepository, Record

__all__ = ["GraphHypergraph", "hypernetx_available"]


def hypernetx_available() -> bool:
    try:
        import hypernetx  # noqa: F401  # pyright: ignore[reportMissingImports]
    except Exception:  # noqa: BLE001
        return False
    return True


def _normalize_members(raw: Any) -> list[str]:
    if not raw:
        return []
    members: list[str] = []
    for item in raw:
        if isinstance(item, str):
            members.append(item)
        elif isinstance(item, Mapping):
            value = item.get("id") or item.get("entity_id") or item.get("name")
            if value:
                members.append(str(value))
        else:
            members.append(str(item))
    # 去重但保持稳定顺序
    return list(dict.fromkeys(members))


class GraphHypergraph(HypergraphRepository):
    """超边逻辑层：写入走二部图编码，读取做成员展开与高阶连通分析。"""

    name = "hypergraph:kuzu"

    def __init__(self, graph: GraphRepository, *, relational: Any | None = None) -> None:
        self._graph = graph
        # W4 / Q14：骨架字段放主库 hyperedge 记录的 payload（JSON），不动 Hyperedge 模型
        # （extra="forbid"），也不给 Kùzu HyperEvent 加列。未注入时只提供降级骨架。
        self._relational = relational

    # ── 写入 ──────────────────────────────────────────────────────────────
    def add(self, hyperedge: Record) -> None:
        hyperedge_id = str(hyperedge.get("id") or hyperedge.get("uuid7_id") or "")
        if not hyperedge_id:
            raise StorageError("超边缺少 id")
        label = str(hyperedge.get("label") or hyperedge.get("content") or "")
        occurred_at_ms = int(hyperedge.get("occurred_at_ms") or hyperedge.get("ts_ms") or 0)
        self._graph.upsert_hyperedge_node(hyperedge_id, label=label, occurred_at_ms=occurred_at_ms)
        for entity_id in _normalize_members(hyperedge.get("members") or hyperedge.get("member_ids")):
            self._graph.upsert_entity(entity_id, name=entity_id)
            self._graph.add_member(hyperedge_id, entity_id)

    # ── 读取 ──────────────────────────────────────────────────────────────
    def members(self, hyperedge_id: str) -> list[str]:
        return self._graph.members(hyperedge_id)

    def hyperedges_of(self, entity_id: str, *, limit: int = 20) -> list[str]:
        return self._graph.hyperedges_of(entity_id, limit=limit)

    def get_event_skeleton(self, hyperedge_id: str) -> str:
        """Q14 / P4.2：把超边拼成可直接注入 prompt 的结构化文本块（不调 LLM）。

        优先读主库 hyperedge 记录的 `skeleton` payload；历史数据没有骨架时
        降级为「成员列表 + 提示」，保证任何超边都能作为叙事单元参与召回。
        """
        skeleton: dict[str, Any] = {}
        if self._relational is not None:
            try:
                row = self._relational.get("hyperedge", hyperedge_id)
                if isinstance(row, dict) and isinstance(row.get("skeleton"), dict):
                    skeleton = dict(row["skeleton"])
            except Exception:  # noqa: BLE001  主库不可读时走降级骨架
                skeleton = {}
        members = self.members(hyperedge_id)
        if not skeleton:
            lines = [f"【事件】{hyperedge_id[:8]}（历史数据，无骨架元数据）",
                     "【成员】" + "；".join(f"{member[:8]}" for member in members),
                     "【情绪】未知", "【后续】无"]
            return "\n".join(lines)
        parts = [f"【事件】{skeleton.get('time', '—')} "
                 f"{skeleton.get('subject', '—')} {skeleton.get('action', '—')}"]
        summaries = skeleton.get("member_summaries") or {}
        parts.append("【成员】" + "；".join(
            f"{key}：{value}" for key, value in summaries.items()
        ) if summaries else "【成员】" + "；".join(members))
        pad = skeleton.get("pad") or [0.0, 0.0, 0.0]
        parts.append(f"【情绪】P={pad[0]} A={pad[1]} D={pad[2]}")
        parts.append(f"【后续】{skeleton.get('follow_up') or '无'}")
        return "\n".join(parts)

    def incidence(self, hyperedge_ids: Sequence[str] | None = None) -> Mapping[str, list[str]]:
        """关联矩阵的稀疏表示：{超边 id: [成员 id...]}。"""
        if hyperedge_ids is None:
            raise StorageError(
                "incidence() 需要显式超边列表：全量枚举走 sync/snapshot 或 graph.count_edges('MEMBER')"
            )
        return {str(hid): self._graph.members(str(hid)) for hid in hyperedge_ids}

    # ── 高阶分析 ──────────────────────────────────────────────────────────
    def s_connected_components(self, hyperedge_ids: Sequence[str] | None = None) -> list[list[str]]:
        """成员共享超边即连通；返回排序后的分量列表（确定性）。"""
        if hyperedge_ids is None:
            raise StorageError("s_connected_components() 需要显式超边列表")
        incidence = self.incidence(hyperedge_ids)

        if hypernetx_available():
            try:
                import hypernetx as hnx  # pyright: ignore[reportMissingImports]

                hypergraph = hnx.Hypergraph(
                    {hid: set(members) for hid, members in incidence.items() if members}
                )
                components = [sorted(map(str, comp)) for comp in hypergraph.connected_components()]
                return sorted(components)
            except Exception:  # noqa: BLE001  分析失败退回内置实现，不影响主流程
                pass

        return _union_find_components(incidence)

    # ── 健康检查 ──────────────────────────────────────────────────────────
    def health(self) -> HealthStatus:
        started = time.perf_counter()
        try:
            members = self._graph.count_edges("MEMBER")
            latency = (time.perf_counter() - started) * 1000
            return HealthStatus(
                engine=self.name,
                ok=True,
                latency_ms=latency,
                version="hypernetx" if hypernetx_available() else "builtin",
                detail=f"member_edges={members}",
                extra={"backend": "graph:" + type(self._graph).__name__},
            )
        except Exception as exc:  # noqa: BLE001
            return HealthStatus(
                engine=self.name,
                ok=False,
                latency_ms=(time.perf_counter() - started) * 1000,
                detail=f"{type(exc).__name__}: {exc}",
            )


def _union_find_components(incidence: Mapping[str, Sequence[str]]) -> list[list[str]]:
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: str, b: str) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_b] = root_a

    for members in incidence.values():
        members = list(members)
        for member in members:
            find(member)
        for first, second in zip(members, members[1:], strict=False):
            union(first, second)

    buckets: dict[str, list[str]] = {}
    for node in parent:
        buckets.setdefault(find(node), []).append(node)
    return sorted(sorted(group) for group in buckets.values())
