"""阶段 0：六引擎行为验证（建表 / 幂等 / 预过滤 / 只读纪律）。"""
from __future__ import annotations

import sqlite3

import pytest

from core.errors import StorageError
from core.ids import HLCStamp
from core.interfaces import RecallFilter, VectorRecord

DIM = 8


def _vector(seed: int) -> list[float]:
    return [1.0 if i == seed else 0.0 for i in range(DIM)]


def _neuron(entity_id: str, *, active: bool = True, text: str = "周五发版导致争吵") -> dict:
    return {
        "id": entity_id,
        "device_id": "dev-test",
        "agent_id": "agent-a",
        "memory_type": "emotion",
        "content": text,
        "namespace": "private",
        "tier": "buffer",
        "pyramid": "raw",
        "confidence": 0.62,
        "affinity": -0.1,
        "pad_p": -0.7,
        "pad_a": 0.8,
        "pad_d": 0.1,
        "valid_from_ms": 1000,
        "is_active": active,
    }


# ── SQLite 主库 ────────────────────────────────────────────────────────────
def test_relational_wal_enabled(brain_config):
    from storage.relational_db import SqliteRelationalDB

    db = SqliteRelationalDB(brain_config.storage.sqlite_path)
    try:
        conn = sqlite3.connect(str(brain_config.storage.sqlite_path))
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        conn.close()
    finally:
        db.close()


def test_relational_upsert_is_idempotent_and_fts_works(storage):
    relational = storage.relational
    record = _neuron("n1")
    relational.upsert("neuron", record)
    relational.upsert("neuron", record)
    assert relational.count("neuron") == 1, "重复写入必须零冗余"

    fetched = relational.get("neuron", "n1")
    assert fetched["content"] == record["content"]
    assert fetched["pad_a"] == pytest.approx(0.8)

    hits = relational.full_text_search("neuron", "发版")
    assert [hit["id"] for hit in hits] == ["n1"], "CJK 两字查询要能命中（LIKE 兜底）"


def test_relational_deactivate_seals_without_deleting(storage):
    relational = storage.relational
    relational.upsert("neuron", _neuron("n2"))
    relational.deactivate("neuron", "n2", valid_to_ms=2000, tx_to=HLCStamp(2000, 1, "dev-test"))

    sealed = relational.get("neuron", "n2")
    assert sealed is not None, "封存不是删除：记录必须可追溯"
    assert sealed["is_active"] is False
    assert sealed["valid_to_ms"] == 2000
    assert sealed["tx_to"] == "2000:1:dev-test"
    assert relational.count("neuron") == 0
    assert relational.count("neuron", active_only=False) == 1


def test_relational_outbox_roundtrip(storage):
    relational = storage.relational
    relational.outbox_enqueue("evt-1", "vector", {"kind": "neuron", "id": "n1"})
    assert relational.outbox_pending_count() == 1

    claimed = relational.outbox_claim(limit=10)
    assert len(claimed) == 1 and claimed[0].event_id == "evt-1"
    assert relational.outbox_pending_count() == 0, "claim 后不能再被重复领取"

    relational.outbox_ack([claimed[0].outbox_id])
    assert relational.outbox_claim(limit=10) == []


def test_relational_event_dedupe(storage):
    relational = storage.relational
    assert relational.mark_event_applied("evt-dup") is True
    assert relational.mark_event_applied("evt-dup") is False, "重放幂等：第二次必须 False"


# ── LanceDB 向量库 ─────────────────────────────────────────────────────────
def test_vector_prefilter_excludes_inactive(storage):
    vector = storage.vector
    vector.upsert(
        [
            VectorRecord(id="v1", dense=_vector(0), text="release conflict 发版",
                         sparse={"发版": 0.9}, metadata={"is_active": True, "namespace": "private"}),
            VectorRecord(id="v2", dense=_vector(1), text="archived note",
                         sparse={"发版": 0.5}, metadata={"is_active": False, "namespace": "private"}),
        ]
    )
    assert vector.count() == 2

    hits = vector.search_dense(_vector(1), limit=5, where=RecallFilter(active_only=True))
    assert [hit.id for hit in hits] == ["v1"], "预过滤必须先于向量检索（is_active=0 不得出现）"

    everything = vector.search_dense(_vector(1), limit=5, where=RecallFilter(active_only=False))
    assert len(everything) == 2


def test_vector_sparse_and_fulltext_channels(storage):
    vector = storage.vector
    vector.upsert(
        [
            VectorRecord(id="v1", dense=_vector(0), text="release conflict 发版",
                         sparse={"发版": 0.9}, metadata={"is_active": True}),
            VectorRecord(id="v2", dense=_vector(1), text="unrelated", sparse={}, metadata={"is_active": True}),
        ]
    )
    sparse_hits = vector.search_sparse({"发版": 1.0}, limit=5, where=RecallFilter(active_only=True))
    assert [hit.id for hit in sparse_hits] == ["v1"]

    text_hits = vector.search_text("release", limit=5, where=RecallFilter(active_only=True))
    assert [hit.id for hit in text_hits] == ["v1"]


def test_vector_dimension_mismatch_rejected(brain_config):
    from core.errors import SchemaVersionMismatch
    from storage.vector_db import LanceVectorDB

    first = LanceVectorDB(brain_config.storage.lancedb_path, dense_dim=DIM)
    first.close()
    with pytest.raises(SchemaVersionMismatch):
        LanceVectorDB(brain_config.storage.lancedb_path, dense_dim=DIM * 2)


# ── Kùzu 图谱 + 超图 ───────────────────────────────────────────────────────
def test_graph_neighbors_and_supersede(storage):
    graph = storage.graph
    graph.upsert_entity("alice", name="Alice")
    graph.upsert_entity("bob", name="Bob")
    graph.add_relation("alice", "knows", "bob")

    neighbors = graph.neighbors("alice")
    assert [row["id"] for row in neighbors] == ["bob"]
    assert graph.count_edges("RELATES") == 1

    graph.supersede("bob", "alice", tx_from=HLCStamp(3000, 2, "dev-test"))
    assert graph.count_edges("SUPERSEDES") == 1, "冲突消解必须在图谱留演进边"
    # 重复 MERGE 不产生重复边（重放幂等）
    graph.supersede("bob", "alice", tx_from=HLCStamp(3000, 2, "dev-test"))
    assert graph.count_edges("SUPERSEDES") == 1


def test_hypergraph_bipartite_encoding(storage):
    hypergraph = storage.hypergraph
    hypergraph.add({"id": "he1", "label": "冲突事件", "occurred_at_ms": 1234,
                    "members": ["alice", "bob"]})

    assert sorted(hypergraph.members("he1")) == ["alice", "bob"]
    assert hypergraph.hyperedges_of("alice") == ["he1"]
    assert hypergraph.s_connected_components(["he1"]) == [["alice", "bob"]]
    assert storage.graph.count_edges("MEMBER") == 2


# ── LMDB KV ────────────────────────────────────────────────────────────────
def test_kv_put_scan_and_counter(storage):
    kv = storage.kv
    kv.put_json("profile", "user:1", {"tone": "direct", "lang": "zh"})
    assert kv.get_json("profile", "user:1")["tone"] == "direct"

    kv.put("profile", "user:2", b"raw")
    keys = [key for key, _ in kv.scan_prefix("profile", "user:")]
    assert keys == ["user:1", "user:2"]

    assert kv.incr("ratelimit", "agent-a", ttl_ms=60_000) == 1
    assert kv.incr("ratelimit", "agent-a", ttl_ms=60_000) == 2

    kv.delete("profile", "user:2")
    assert kv.get("profile", "user:2") is None


# ── DuckDB 时序 ────────────────────────────────────────────────────────────
def test_timeseries_pad_track_append_and_fetch(storage):
    timeseries = storage.timeseries
    timeseries.append(
        "pad_track",
        [
            {"ts_ms": 1000, "neuron_id": "n1", "device_id": "dev-test", "agent_id": "agent-a",
             "p": -0.7, "a": 0.8, "d": 0.1, "source": "llm"},
            {"ts_ms": 2000, "neuron_id": "n1", "device_id": "dev-test", "agent_id": "agent-a",
             "p": -0.3, "a": 0.2, "d": 0.1, "source": "llm"},
        ],
    )
    rows = timeseries.fetch("pad_track", entity_id="n1", limit=10)
    assert [row["ts_ms"] for row in rows] == [1000, 2000]
    assert rows[0]["p"] == pytest.approx(-0.7)

    # 趋势分析走只读入口
    stats = timeseries.query(
        "SELECT avg(a) AS avg_a FROM pad_track WHERE neuron_id = ?", ["n1"]
    )
    assert stats[0]["avg_a"] == pytest.approx(0.5)


def test_timeseries_rejects_writes_through_analysis_entry(storage):
    with pytest.raises(StorageError):
        storage.timeseries.query("DELETE FROM pad_track")
    with pytest.raises(StorageError):
        storage.timeseries.query("DROP TABLE pad_track")
