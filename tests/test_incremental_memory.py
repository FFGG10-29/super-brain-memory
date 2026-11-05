"""tests/test_incremental_memory.py —— 构图落盘缓存、增量构建、稀疏倒排索引。

对应「索引/缓存是派生、可重建」与「增量 ingest 不重算全图」两条设计：
- 新实例命中缓存时应零 embedding 调用（连大模型都不加载）；
- 新增一条记忆只补算那一条；
- 缓存损坏 / 配置签名变化一律重建；
- 倒排索引的检索结果必须与全表打分一致。
"""
from __future__ import annotations

from pathlib import Path

from brain.activation import ActivationService
from config import ActivationConfig, BrainConfig, ModelConfig, StorageConfig
from core.interfaces import Embedding, VectorRecord
from storage.vector_db import LanceVectorDB

__all__: list[str] = []


def _config(tmp_path: Path) -> BrainConfig:
    return BrainConfig(
        storage=StorageConfig(sqlite_path=tmp_path / "r.db"),
        model=ModelConfig(dense_dim=4, use_cuda=False),
        activation=ActivationConfig(max_nodes=20, similarity_threshold=0.1),
    )


def _row(node_id: str, content: str) -> dict:
    return {
        "id": node_id, "content": content, "memory_type": "fact", "tier": "buffer",
        "pad_p": 0.0, "pad_a": 0.0, "pad_d": 0.0, "confidence": 0.9,
        "namespace": "private", "is_active": True,
    }


class _FakeRelational:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def query(self, kind, *, limit=50, **_kwargs):
        if kind != "neuron":
            return []
        return [dict(row) for row in self.rows[: int(limit)]]

    def get(self, kind, entity_id):
        return next((row for row in self.rows if row["id"] == entity_id), None)


class _CountingEmbedder:
    dense_dim = 4
    model_file = "fake.onnx"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    @property
    def degraded(self) -> bool:
        return False

    def embed_documents(self, texts):
        self.calls.append(list(texts))
        return [Embedding(dense=[1.0, 0.0, 0.0, 0.0], sparse={}) for _ in texts]


def test_cache_hit_is_zero_model_calls(tmp_path: Path) -> None:
    rows = [_row("a", "发版冲突复盘"), _row("b", "深色主题偏好")]
    cfg = _config(tmp_path)
    cache = tmp_path / "activation_cache.json.gz"

    first = _CountingEmbedder()
    g1 = ActivationService(_FakeRelational(rows), first, cfg, cache_path=cache).build()
    assert cache.is_file()
    assert len(first.calls) == 1 and len(first.calls[0]) == 2

    second = _CountingEmbedder()   # 模拟关掉窗口再打开：全新实例、全新 embedder
    svc2 = ActivationService(_FakeRelational(rows), second, cfg, cache_path=cache)
    g2 = svc2.build()
    assert second.calls == [], "命中落盘缓存时不应加载/调用 embedding 模型"
    assert [node.id for node in g2.nodes] == [node.id for node in g1.nodes]
    assert [(edge.source, edge.target) for edge in g2.edges] == [
        (edge.source, edge.target) for edge in g1.edges
    ]
    assert svc2.activation_scores("发版冲突复盘", ["a"])[0][0] == "a"


def test_incremental_only_embeds_new_memory(tmp_path: Path) -> None:
    rows = [_row("a", "发版冲突复盘"), _row("b", "深色主题偏好")]
    cfg = _config(tmp_path)
    cache = tmp_path / "activation_cache.json.gz"
    ActivationService(_FakeRelational(rows), _CountingEmbedder(), cfg, cache_path=cache).build()

    rows.append(_row("c", "新增一条关于咖啡的记忆"))
    second = _CountingEmbedder()
    graph = ActivationService(_FakeRelational(rows), second, cfg, cache_path=cache).build()
    assert len(second.calls) == 1
    assert second.calls[0] == ["新增一条关于咖啡的记忆"], "已缓存的记忆不该重新 embed"
    assert len(graph.nodes) == 3


def test_corrupt_or_stale_cache_is_rebuilt(tmp_path: Path) -> None:
    rows = [_row("a", "发版冲突复盘")]
    cfg = _config(tmp_path)
    cache = tmp_path / "activation_cache.json.gz"
    ActivationService(_FakeRelational(rows), _CountingEmbedder(), cfg, cache_path=cache).build()

    cache.write_bytes(b"not a gzip file")   # 派生缓存损坏：直接重建，不抛给用户
    rebuilt = _CountingEmbedder()
    ActivationService(_FakeRelational(rows), rebuilt, cfg, cache_path=cache).build()
    assert len(rebuilt.calls) == 1

    cfg.activation.similarity_threshold = 0.9   # 构图参数变了，旧边不能复用
    changed = _CountingEmbedder()
    ActivationService(_FakeRelational(rows), changed, cfg, cache_path=cache).build()
    assert len(changed.calls) == 1


def test_sparse_inverted_index_matches_full_scan(tmp_path: Path) -> None:
    db = LanceVectorDB(tmp_path / "v.lance", dense_dim=4)
    db.upsert([
        VectorRecord(id="a", dense=[1, 0, 0, 0], text="发版", sparse={"t1": 0.5, "t2": 0.2},
                     metadata={"namespace": "private"}),
        VectorRecord(id="b", dense=[0, 1, 0, 0], text="咖啡", sparse={"t1": 0.3},
                     metadata={"namespace": "private"}),
        VectorRecord(id="c", dense=[0, 0, 1, 0], text="无关", sparse={"t3": 0.9},
                     metadata={"namespace": "private"}),
    ])
    hits = db.search_sparse({"t1": 1.0, "t2": 0.5}, limit=10, where=None)
    assert [hit.id for hit in hits] == ["a", "b"]
    assert hits[0].score > hits[1].score
    assert (tmp_path / "sparse_index.db").is_file()

    db.delete(["a"])
    assert [hit.id for hit in db.search_sparse({"t1": 1.0}, limit=10, where=None)] == ["b"]
