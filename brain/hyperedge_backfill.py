"""brain.hyperedge_backfill —— P4.0 补边（v4.1 W4，从 P5 前置到扩散之前）。

裁决依据：A7 / R2 / Q14
- 扩散依赖突触边；历史 hyperedge 只来自 Agent 上报的 cognition，存量常常为零。
- 补边规则：**同 topic + 时间窗 + 2-gram 相似度** 贪心聚簇，为同簇成员建
  `HyperEvent + MEMBER` 边（Kùzu 二部图编码），不改 schema。
- 骨架字段放**主库 hyperedge 记录的 payload JSON**（`skeleton` 键），
  `Hyperedge` 模型是 `extra="forbid"`，Kùzu HyperEvent 也不加列。
- 「后续」按时间相邻超边的 label 生成，跨 hyperedge 不按时间窗口切断因果链。
- 幂等：超边 id = sha1(topic + 排序后的成员 id + 起始时间)，重复跑只 MERGE，不产生重复边。
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from config import BrainConfig, get_config
from storage.hypergraph_db import GraphHypergraph

__all__ = ["Cluster", "GraphDensity", "HyperedgeBackfill", "graph_density"]


def _bigram(text: str) -> dict[str, float]:
    cleaned = "".join(str(text or "").split())
    vector: dict[str, float] = {}
    for index in range(max(1, len(cleaned) - 1)):
        key = cleaned[index: index + 2]
        vector[key] = vector.get(key, 0.0) + 1.0
    return vector


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    a, b = (left, right) if len(left) <= len(right) else (right, left)
    dot = sum(value * b.get(key, 0.0) for key, value in a.items())
    norm_a = sum(value * value for value in a.values()) ** 0.5
    norm_b = sum(value * value for value in b.values()) ** 0.5
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


@dataclass
class Cluster:
    topic: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    vectors: list[dict[str, float]] = field(default_factory=list)
    label: str = ""

    @property
    def started_at_ms(self) -> int:
        return min(int(self._ts(row)) for row in self.rows)

    @staticmethod
    def _ts(row: dict[str, Any]) -> int:
        return int(row.get("valid_from_ms") or row.get("created_at_ms") or 0)

    @property
    def member_ids(self) -> list[str]:
        return [str(row.get("id") or "") for row in self.rows]

    def stable_id(self) -> str:
        raw = f"{self.topic}|{','.join(sorted(self.member_ids))}|{self.started_at_ms}"
        return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


@dataclass
class GraphDensity:
    nodes: int
    relates_edges: int
    member_edges: int
    avg_degree: float
    threshold: float = 2.0

    @property
    def sparse(self) -> bool:
        return self.avg_degree < self.threshold

    def as_dict(self) -> dict[str, Any]:
        return {
            "nodes": self.nodes, "relates_edges": self.relates_edges,
            "member_edges": self.member_edges,
            "avg_degree": round(self.avg_degree, 3),
            "threshold": self.threshold, "sparse": self.sparse,
            "advice": ("先跑 graph.backfill 补边，扩散降 1 跳" if self.sparse
                       else "图密度足够，可开 2 跳扩散"),
        }


def graph_density(graph: Any, config: BrainConfig | None = None) -> GraphDensity:
    """P4.1 前置评估：平均度数 < 2 时扩散会大面积为空。"""
    cfg = config or get_config()
    nodes = int(graph.count_nodes("Entity"))
    relates = int(graph.count_edges("RELATES"))
    members = int(graph.count_edges("MEMBER"))
    avg = (2.0 * relates + members) / max(1, nodes)
    return GraphDensity(nodes=nodes, relates_edges=relates, member_edges=members,
                        avg_degree=avg,
                        threshold=float(cfg.hyperedge_backfill.sparse_degree_threshold))


class HyperedgeBackfill:
    def __init__(self, *, relational: Any, graph: Any,
                 config: BrainConfig | None = None) -> None:
        self._relational = relational
        self._graph = graph
        self._config = config or get_config()
        self._hypergraph = GraphHypergraph(graph, relational=relational)

    # ── 聚簇 ──────────────────────────────────────────────────────────────
    def _active_neurons(self, limit: int) -> list[dict[str, Any]]:
        rows = self._relational.query("neuron", limit=limit, since_ms=0)
        return [dict(row) for row in rows if row.get("is_active", True)]

    def cluster(self, *, limit: int = 500) -> list[Cluster]:
        cfg = self._config.hyperedge_backfill
        window_ms = int(cfg.window_hours) * 3600 * 1000
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in self._active_neurons(limit):
            topic = str(row.get("namespace") or "topic:default")
            if not topic.startswith("topic:"):
                topic = "topic:default"
            groups.setdefault(topic, []).append(row)

        clusters: list[Cluster] = []
        for topic, rows in sorted(groups.items()):
            rows.sort(key=Cluster._ts)
            current: list[Cluster] = []
            for row in rows:
                vector = _bigram(str(row.get("content") or ""))
                stamp = Cluster._ts(row)
                target: Cluster | None = None
                for cluster in current:
                    if stamp - cluster.started_at_ms > window_ms:
                        continue
                    if any(_cosine(vector, existing) >= float(cfg.similarity_threshold)
                           for existing in cluster.vectors):
                        target = cluster
                        break
                if target is None:
                    target = Cluster(topic=topic)
                    current.append(target)
                target.rows.append(row)
                target.vectors.append(vector)
            clusters.extend(item for item in current
                            if len(item.rows) >= int(cfg.min_members))
            if len(clusters) >= int(cfg.max_clusters):
                break
        clusters.sort(key=lambda item: item.started_at_ms)
        for index, cluster in enumerate(clusters):
            cluster.label = f"事件：{cluster.topic}（{len(cluster.rows)} 条）"
        return clusters[:int(cfg.max_clusters)]

    # ── 骨架（Q14）───────────────────────────────────────────────────────
    def _skeleton(self, cluster: Cluster, follow_up: str) -> dict[str, Any]:
        cfg = self._config.hyperedge_backfill
        chars = max(20, int(cfg.skeleton_chars))
        summaries = {
            str(row.get("id") or "")[:8]: str(row.get("content") or "").strip()[:chars]
            for row in cluster.rows
        }
        pad = [0.0, 0.0, 0.0]
        for index, axis in enumerate(("pad_p", "pad_a", "pad_d")):
            values = [float(row.get(axis) or 0.0) for row in cluster.rows]
            pad[index] = round(sum(values) / max(1, len(values)), 3)
        moment = time.strftime("%Y-%m-%d %H:%M",
                               time.localtime(cluster.started_at_ms / 1000))
        return {
            "time": moment,
            "subject": cluster.topic,
            "action": cluster.label,
            "member_summaries": summaries,
            "pad": pad,
            "follow_up": follow_up,
        }

    # ── 落库 ──────────────────────────────────────────────────────────────
    def run(self, *, limit: int = 500) -> dict[str, Any]:
        cfg = self._config.hyperedge_backfill
        if not cfg.enabled:
            return {"ok": False, "message": "hyperedge_backfill.enabled=false",
                    "clusters": 0, "hyperedges": []}
        before = graph_density(self._graph, self._config)
        clusters = self.cluster(limit=limit)
        labels = [cluster.label for cluster in clusters]
        written: list[str] = []
        for index, cluster in enumerate(clusters):
            hyperedge_id = cluster.stable_id()
            follow_up = labels[index + 1] if index + 1 < len(labels) else ""
            skeleton = self._skeleton(cluster, follow_up)
            record = {
                "id": hyperedge_id,
                "label": cluster.label,
                "members": cluster.member_ids,
                "occurred_at_ms": cluster.started_at_ms,
                "namespace": cluster.topic,
                "tier": "topic",
                "is_active": True,
                # 骨架放 payload JSON，不动 Hyperedge 模型 / Kùzu schema
                "skeleton": skeleton,
                "pyramid_subtype": "event",
            }
            self._relational.upsert("hyperedge", record)
            self._hypergraph.add({
                "id": hyperedge_id, "label": cluster.label,
                "members": cluster.member_ids,
                "occurred_at_ms": cluster.started_at_ms,
            })
            written.append(hyperedge_id)
        after = graph_density(self._graph, self._config)
        return {
            "ok": True, "clusters": len(clusters), "hyperedges": written,
            "density_before": before.as_dict(), "density_after": after.as_dict(),
        }

    def skeleton_text(self, hyperedge_id: str) -> str:
        return self._hypergraph.get_event_skeleton(hyperedge_id)
