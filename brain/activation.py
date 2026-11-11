"""brain.activation —— 突触激活服务：把"向量检索"翻译成可视化语言。

给前端星云视图（Flutter 3D 星云）提供两样真实数据：
    1. 静态突触线：记忆两两余弦相似度 ≥ 阈值 或 图谱已有关系边；
    2. 动态光脉冲：一次召回命中后，用"查询向量 ↔ 命中记忆向量"的余弦
       决定光点速度/亮度（相似度越高，传导越快越亮）。

向量来源：
    - embedder 可用 → BGE-M3 稠密向量（1024 维）
    - 不可用    → 字符 2-gram TF 稀疏向量（确定性、跨设备一致，明确标 degraded）
接口不变，模型到位自动替换；绝不生成随机假相似度。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import BrainConfig, get_config
from core.errors import EngineUnavailable
from core.interfaces import Embedder, RelationalRepository

__all__ = ["ActivationEdge", "ActivationGraph", "ActivationNode", "ActivationService"]

Vector = dict[str, float]
_CACHE_SCHEMA = 1


def _content_hash(text: str) -> str:
    """内容哈希：判断一条记忆的正文是否变化，决定要不要重新 embed。"""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _pack_vector(vector: Vector) -> dict[str, Any]:
    """dense 向量压成 float 列表（省掉 1024 个重复 key），稀疏向量原样存。"""
    keys = list(vector)
    if keys and all(key.startswith("d") and key[1:].isdigit() for key in keys):
        packed: list[float | None] = [None] * (max(int(key[1:]) for key in keys) + 1)
        for key, value in vector.items():
            packed[int(key[1:])] = float(value)
        return {"format": "dense", "values": packed}
    return {"format": "sparse", "values": dict(vector)}


def _unpack_vector(payload: Any) -> Vector:
    if not isinstance(payload, dict):
        return {}
    if payload.get("format") == "dense":
        return {
            f"d{index}": float(value)
            for index, value in enumerate(payload.get("values") or [])
            if value is not None
        }
    return {str(key): float(value) for key, value in (payload.get("values") or {}).items()}


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


def _copy_graph(graph: ActivationGraph) -> ActivationGraph:
    return ActivationGraph(
        nodes=list(graph.nodes), edges=list(graph.edges),
        source=graph.source, degraded=graph.degraded,
    )


def _graph_to_dict(graph: ActivationGraph) -> dict[str, Any]:
    return {
        "nodes": [
            {
                "id": node.id, "label": node.label, "content": node.content,
                "memory_type": node.memory_type, "tier": node.tier,
                "pad": list(node.pad), "confidence": node.confidence,
                "is_emotion": node.is_emotion,
            }
            for node in graph.nodes
        ],
        "edges": [
            {"source": edge.source, "target": edge.target,
             "weight": edge.weight, "kind": edge.kind}
            for edge in graph.edges
        ],
        "source": graph.source,
        "degraded": graph.degraded,
    }


def _graph_from_dict(payload: Any) -> ActivationGraph:
    return ActivationGraph(
        nodes=[
            ActivationNode(
                id=str(node["id"]), label=str(node["label"]), content=str(node["content"]),
                memory_type=str(node["memory_type"]), tier=str(node["tier"]),
                pad=tuple(float(value) for value in node["pad"]),
                confidence=float(node["confidence"]),
                is_emotion=bool(node["is_emotion"]),
            )
            for node in payload["nodes"]
        ],
        edges=[ActivationEdge(**edge) for edge in payload["edges"]],
        source=str(payload["source"]), degraded=bool(payload["degraded"]),
    )


class ActivationService:
    def __init__(
        self,
        relational: RelationalRepository,
        embedder: Embedder | None = None,
        config: BrainConfig | None = None,
        *,
        cache_path: Path | None = None,
    ) -> None:
        self._relational = relational
        self._embedder = embedder
        self._config = config or get_config()
        self._vectors: dict[str, Vector] = {}
        self._cache: ActivationGraph | None = None
        self._lock = threading.RLock()
        # 跟随数据库目录：换库 / 测试临时库自动隔离，打包后落在用户数据目录
        self._cache_path = cache_path or (
            Path(self._config.storage.sqlite_path).parent / "activation_cache.json.gz"
        )

    def invalidate(self) -> None:
        """数据变更后只让内存图失效；磁盘缓存保留，build() 按 id+hash 增量刷新。"""
        with self._lock:
            self._cache = None

    @property
    def params(self):
        return self._config.activation

    # ── 落盘缓存（派生数据，可随时删除重建）───────────────────────────
    def _signature(self) -> dict[str, Any]:
        """模型 / 构图参数变了，旧向量就不能复用。

        只能依赖「不加载模型就能拿到」的信息：model_file 是 _ensure_loaded()
        之后才赋值的运行时状态，若放进签名，冷启动读缓存时必然不匹配。
        """
        embedder = self._embedder
        # 用权重文件的大小+mtime 做指纹：换权重自动失效，但不需要加载模型
        onnx = (
            Path(self._config.model.llm_gguf_dir)
            / str(self._config.model.embedding_model)
            / "model.onnx"
        )
        model_digest = ""
        if onnx.is_file():
            stat = onnx.stat()
            model_digest = f"{stat.st_size}:{int(stat.st_mtime)}"
        return {
            "schema": _CACHE_SCHEMA,
            "max_nodes": int(self.params.max_nodes),
            "similarity_threshold": float(self.params.similarity_threshold),
            "max_edges_per_node": int(self.params.max_edges_per_node),
            "embedder": type(embedder).__name__ if embedder else "none",
            "dense_dim": int(embedder.dense_dim) if embedder else 0,
            "embedding_model": str(self._config.model.embedding_model),
            "model_digest": model_digest,
        }

    def _read_cache(self) -> dict[str, Any] | None:
        if not self.params.cache_enabled or not self._cache_path.is_file():
            return None
        try:
            with gzip.open(self._cache_path, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict) or payload.get("signature") != self._signature():
                return None
            return payload
        except (OSError, ValueError, TypeError):
            return None   # 损坏 / 版本不符：丢弃重建，事件日志才是唯一真源

    def _write_cache(self, payload: dict[str, Any]) -> None:
        if not self.params.cache_enabled:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_name(self._cache_path.name + ".tmp")
            with gzip.open(tmp, "wt", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(tmp, self._cache_path)
        except OSError:
            pass   # 缓存写失败不影响主流程

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

    # ── 图谱（增量 + 落盘缓存）───────────────────────────────────────────
    def build(self) -> ActivationGraph:
        """增量构图：缓存命中时零模型调用；只 embed 新增 / 正文变更的记忆。

        query() 按 updated_at_ms DESC 取 max_nodes 条（已建索引），所以即使库里
        有 10 万条，这里也只读几十条，启动开销不随总记忆量增长。
        """
        with self._lock:
            records = self._relational.query("neuron", limit=self.params.max_nodes)
            current: list[tuple[str, str, dict[str, Any]]] = []
            for record in records:
                node_id = str(record.get("id", ""))
                if node_id:
                    current.append((node_id, _content_hash(str(record.get("content", ""))), record))
            if not current:
                self._vectors = {}
                self._cache = ActivationGraph(
                    nodes=[], edges=[], source="empty", degraded=self._embedder is None
                )
                return _copy_graph(self._cache)

            ids = [item[0] for item in current]
            hashes = {item[0]: item[1] for item in current}
            cached = self._read_cache()
            hit = (
                cached is not None
                and cached.get("ids") == ids
                and cached.get("hashes") == hashes
            )
            if hit and self._cache is None and cached is not None:
                try:
                    self._cache = _graph_from_dict(cached["graph"])
                    self._vectors = {
                        key: _unpack_vector(value)
                        for key, value in (cached.get("vectors") or {}).items()
                    }
                except (KeyError, TypeError, ValueError):
                    hit = False
                    self._cache = None
            if hit and self._cache is not None:
                return _copy_graph(self._cache)

            # 增量：未变化的节点直接复用落盘向量，只补算新增 / 改过正文的
            packed = dict(cached.get("vectors") or {}) if cached else {}
            old_hashes = dict(cached.get("hashes") or {}) if cached else {}
            vectors: dict[str, Vector] = {}
            missing: list[tuple[str, str]] = []
            for node_id, digest, record in current:
                if old_hashes.get(node_id) == digest and node_id in packed:
                    vectors[node_id] = _unpack_vector(packed[node_id])
                else:
                    missing.append((node_id, str(record.get("content", ""))))
            if missing:
                computed, source, degraded = self._vectorize([text for _, text in missing])
                for (node_id, _), vector in zip(missing, computed, strict=False):
                    vectors[node_id] = vector
            else:
                source = str(cached.get("source", "bge-m3")) if cached else "bge-m3"
                degraded = bool(cached.get("degraded", False)) if cached else (self._embedder is None)

            self._vectors = {node_id: vectors.get(node_id, {}) for node_id, _, _ in current}
            nodes = self._make_nodes(current)
            edges = self._semantic_edges(nodes)
            edges.extend(self._synapse_edges(nodes))
            graph = ActivationGraph(nodes=nodes, edges=edges, source=source, degraded=degraded)
            self._cache = graph
            self._write_cache({
                "signature": self._signature(),
                "ids": ids,
                "hashes": hashes,
                "vectors": {key: _pack_vector(value) for key, value in vectors.items()},
                "source": source,
                "degraded": degraded,
                "graph": _graph_to_dict(graph),
            })
            return _copy_graph(graph)

    def _make_nodes(self, current: list[tuple[str, str, dict[str, Any]]]) -> list[ActivationNode]:
        nodes: list[ActivationNode] = []
        for node_id, _digest, record in current:
            content = str(record.get("content", ""))
            pad = (
                float(record.get("pad_p", 0.0)),
                float(record.get("pad_a", 0.0)),
                float(record.get("pad_d", 0.0)),
            )
            nodes.append(
                ActivationNode(
                    id=node_id,
                    label=content[:18],
                    content=content,
                    memory_type=str(record.get("memory_type", "fact")),
                    tier=str(record.get("tier", "buffer")),
                    pad=pad,
                    confidence=float(record.get("confidence", 0.5)),
                    is_emotion=(
                        str(record.get("memory_type", "")) == "emotion" or abs(pad[0]) > 0.4 or abs(pad[1]) > 0.6
                    ),
                )
            )
        return nodes

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
