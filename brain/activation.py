"""brain.activation —— 突触激活服务：把"向量检索"翻译成可视化语言。

给 PySide6 星云视图提供两样真实数据：
    1. 静态突触线：记忆两两余弦相似度 ≥ 阈值 或 图谱已有关系边；
    2. 动态光脉冲：一次召回命中后，用"查询向量 ↔ 命中记忆向量"的余弦
       决定光点速度/亮度（相似度越高，传导越快越亮）。

向量来源：
    - embedder 可用 → BGE-M3 稠密向量（1024 维）
    - 不可用    → 字符 2-gram TF 稀疏向量（确定性、跨设备一致，明确标 degraded）
接口不变，模型到位自动替换；绝不生成随机假相似度。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from config import BrainConfig, get_config
from core.errors import EngineUnavailable
from core.interfaces import Embedder, RelationalRepository

__all__ = ["ActivationEdge", "ActivationGraph", "ActivationNode", "ActivationService"]

Vector = dict[str, float]


@dataclass(frozen=True)
class ActivationNode:
    id: str
    label: str
    content: str
    memory_type: str
    tier: str
    pad: tuple[float, float, float]
    confidence: float
    is_emotion: bool


@dataclass(frozen=True)
class ActivationEdge:
    source: str
    target: str
    weight: float
    kind: str = "semantic"                    # semantic | synapse


@dataclass
class ActivationGraph:
    nodes: list[ActivationNode] = field(default_factory=list)
    edges: list[ActivationEdge] = field(default_factory=list)
    source: str = "bge-m3"
    degraded: bool = False

    @property
    def node_ids(self) -> set[str]:
        return {node.id for node in self.nodes}


class ActivationService:
    def __init__(
        self,
        relational: RelationalRepository,
        embedder: Embedder | None = None,
        config: BrainConfig | None = None,
    ) -> None:
        self._relational = relational
        self._embedder = embedder
        self._config = config or get_config()
        self._vectors: dict[str, Vector] = {}

    @property
    def params(self):
        return self._config.activation

    # ── 向量化 ────────────────────────────────────────────────────────────
    def _ngram_vector(self, text: str) -> Vector:
        cleaned = "".join(text.split())
        vector: Vector = {}
        for index in range(max(1, len(cleaned) - 1)):
            key = cleaned[index : index + 2]
            vector[key] = vector.get(key, 0.0) + 1.0
        return vector

    def _vectorize(self, texts: list[str]) -> tuple[list[Vector], str, bool]:
        if self._embedder is not None:
            try:
                embeddings = self._embedder.embed_documents(texts)
                return (
                    [{f"d{index}": float(value) for index, value in enumerate(item.dense)} for item in embeddings],
                    "bge-m3",
                    False,
                )
            except EngineUnavailable:
                pass
        return ([self._ngram_vector(text) for text in texts], "char-2gram", True)

    @staticmethod
    def cosine(left: Vector, right: Vector) -> float:
        if not left or not right:
            return 0.0
        if len(left) > len(right):
            left, right = right, left
        dot = sum(value * right.get(key, 0.0) for key, value in left.items())
        norm_left = math.sqrt(sum(value * value for value in left.values()))
        norm_right = math.sqrt(sum(value * value for value in right.values()))
        return dot / (norm_left * norm_right) if norm_left and norm_right else 0.0

    # ── 图谱 ──────────────────────────────────────────────────────────────
    def build(self) -> ActivationGraph:
        records = self._relational.query("neuron", limit=self.params.max_nodes)
        if not records:
            return ActivationGraph(nodes=[], edges=[], source="empty", degraded=self._embedder is None)
        texts = [str(record.get("content", "")) for record in records]
        vectors, source, degraded = self._vectorize(texts)
        nodes: list[ActivationNode] = []
        for record, vector in zip(records, vectors, strict=False):
            node_id = str(record.get("id", ""))
            if not node_id:
                continue
            self._vectors[node_id] = vector
            pad = (
                float(record.get("pad_p", 0.0)),
                float(record.get("pad_a", 0.0)),
                float(record.get("pad_d", 0.0)),
            )
            nodes.append(
                ActivationNode(
                    id=node_id,
                    label=str(record.get("content", ""))[:18],
                    content=str(record.get("content", "")),
                    memory_type=str(record.get("memory_type", "fact")),
                    tier=str(record.get("tier", "buffer")),
                    pad=pad,
                    confidence=float(record.get("confidence", 0.5)),
                    is_emotion=(
                        str(record.get("memory_type", "")) == "emotion" or abs(pad[0]) > 0.4 or abs(pad[1]) > 0.6
                    ),
                )
            )

        edges = self._semantic_edges(nodes)
        edges.extend(self._synapse_edges(nodes))
        return ActivationGraph(nodes=nodes, edges=edges, source=source, degraded=degraded)

    def _semantic_edges(self, nodes: list[ActivationNode]) -> list[ActivationEdge]:
        threshold = self.params.similarity_threshold
        per_node: dict[str, list[ActivationEdge]] = {}
        for index, first in enumerate(nodes):
            for second in nodes[index + 1 :]:
                weight = self.cosine(self._vectors[first.id], self._vectors[second.id])
                if weight < threshold:
                    continue
                per_node.setdefault(first.id, []).append(ActivationEdge(first.id, second.id, weight))
                per_node.setdefault(second.id, []).append(ActivationEdge(first.id, second.id, weight))
        selected: dict[tuple[str, str], ActivationEdge] = {}
        for node_id, candidates in per_node.items():
            for edge in sorted(candidates, key=lambda item: item.weight, reverse=True)[
                : self.params.max_edges_per_node
            ]:
                selected[(edge.source, edge.target)] = edge
        return list(selected.values())

    def _synapse_edges(self, nodes: list[ActivationNode]) -> list[ActivationEdge]:
        """图谱二元关系边：主体/客体名与记忆正文匹配才连线（确定性，不造边）。"""
        known = {node.id: node.content for node in nodes}
        edges: list[ActivationEdge] = []
        for record in self._relational.query("synapse", limit=200):
            source = next((nid for nid, content in known.items() if str(record.get("subject_id", "")) in content), None)
            target = next((nid for nid, content in known.items() if str(record.get("object_id", "")) in content), None)
            if source and target and source != target:
                edges.append(ActivationEdge(source, target, float(record.get("weight", 1.0)), "synapse"))
        return edges

    # ── 召回联动 ──────────────────────────────────────────────────────────
    def activation_scores(self, query: str, hit_ids: list[str]) -> list[tuple[str, float]]:
        """返回值 ∈ [0,1] 的"查询→命中"激活强度；缺向量时按 n-gram 现算。"""
        query_vector = self._vectorize([query])[0][0]
        scores: list[tuple[str, float]] = []
        for hit_id in hit_ids:
            vector = self._vectors.get(hit_id)
            if vector is None:
                record = self._relational.get("neuron", hit_id)
                vector = self._ngram_vector(str(record.get("content", ""))) if record else {}
                self._vectors[hit_id] = vector
            scores.append((hit_id, max(0.0, min(1.0, self.cosine(query_vector, vector)))))
        return scores
