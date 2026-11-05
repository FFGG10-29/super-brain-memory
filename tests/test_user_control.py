"""tests/test_user_control.py —— 用户控制闭环 / 事件时间轴 / 敏感与核心记忆 / 索引化。"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from config import BrainConfig, ModelConfig, StorageConfig, SyncConfig  # noqa: E402
from ui.desktop.backend import DesktopBackend  # noqa: E402

__all__: list[str] = []


@pytest.fixture
def backend(tmp_path: Path):
    config = BrainConfig(
        storage=StorageConfig(
            sqlite_path=tmp_path / "r.db",
            lancedb_path=tmp_path / "v.lance",
            kuzu_path=tmp_path / "g.kuzu",
            lmdb_path=tmp_path / "kv.lmdb",
            duckdb_path=tmp_path / "t.db",
        ),
        model=ModelConfig(dense_dim=8, use_cuda=False, llm_gguf_dir=tmp_path / "models"),
        sync=SyncConfig(device_id="dev-user-control", local_repo_dir=tmp_path / "sync"),
    )
    instance = DesktopBackend(config)
    instance.save_memory("测试记忆：周五发版出现严重冲突")
    try:
        yield instance
    finally:
        instance.close()


def test_correct_creates_new_version_and_preserves_original(backend: DesktopBackend) -> None:
    old = backend.memories(limit=10)[0]
    result = backend.correct_memory(
        old["id"], {"content": "纠正后：发版流程已改为双人复核", "tier": "core"},
        reason="原文有误",
    )
    assert result["ok"] is True
    new_id = str(result["id"])
    assert new_id != old["id"]

    relational = backend._app.storage.relational
    sealed = relational.get("neuron", old["id"])
    assert sealed["is_active"] is False
    assert sealed["content"] == old["content"], "原值必须完整保留供审计"
    assert sealed["tx_to"]

    current = relational.get("neuron", new_id)
    assert current["content"] == "纠正后：发版流程已改为双人复核"
    assert current["tier"] == "core"

    audit = backend.memory_audit(new_id)
    assert any(item["type"] == "supersede" and "content" in item["changed"] for item in audit)
    assert any(item["type"] == "ingest" for item in audit)


def test_forget_removes_from_recall_but_keeps_audit(backend: DesktopBackend) -> None:
    old_id = str(backend.memories(limit=1)[0]["id"])
    result = backend.forget_memory(old_id, reason="用户不希望保留")
    assert result["ok"] is True
    assert backend._app.storage.relational.get("neuron", old_id)["is_active"] is False
    assert all(str(row["id"]) != old_id for row in backend.memories(limit=50))
    assert any(item["type"] == "tombstone" for item in backend.memory_audit(old_id))


def test_event_timeline_and_type_filter(backend: DesktopBackend) -> None:
    all_events = backend.event_timeline()
    assert all_events, "写入至少产生 ingest 事件"
    ingests = backend.event_timeline(types=("ingest",))
    assert ingests and all(item["type"] == "ingest" for item in ingests)
    assert all(item["ts_ms"] >= 0 for item in ingests)
    assert len(backend.event_types()) == 7


def test_sensitive_and_core_memory(backend: DesktopBackend) -> None:
    memory_id = str(backend.memories(limit=1)[0]["id"])
    backend.set_sensitive(memory_id, True)
    assert memory_id in backend.sensitive_ids()
    assert backend.memory_inspect(memory_id)["sensitive"] is True
    backend.set_sensitive(memory_id, False)
    assert memory_id not in backend.sensitive_ids()

    blocks = backend.core_memory_blocks()
    assert set(blocks) == {"human", "persona", "preferences", "constraints"}
    saved = backend.save_core_memory_blocks({"human": "用户偏好直接给结论"})
    assert saved["ok"] is True
    assert backend.core_memory_blocks()["human"] == "用户偏好直接给结论"


def test_cognitive_sectors_and_namespaces(backend: DesktopBackend) -> None:
    sectors = backend.cognitive_sectors()
    assert len(sectors) == 5
    assert sum(sectors.values()) >= 1
    assert "private" in backend.namespaces()
    assert backend.recall("发版", namespaces=("不存在的命名空间",)).hits == []


def test_text_inverted_index_replaces_full_scan(tmp_path: Path) -> None:
    from core.interfaces import VectorRecord
    from storage.vector_db import LanceVectorDB

    db = LanceVectorDB(tmp_path / "v.lance", dense_dim=4)
    db.upsert([
        VectorRecord(id="a", dense=[1, 0, 0, 0], text="发版冲突复盘记录",
                     sparse={"t1": 0.5}, metadata={"namespace": "private"}),
        VectorRecord(id="b", dense=[0, 1, 0, 0], text="周末爬山与摄影",
                     sparse={"t2": 0.3}, metadata={"namespace": "private"}),
    ])
    index = db._sync_sparse_index()
    assert index is not None
    assert index.text_search("发版", None, 10) == ["a"]
    assert index.text_search("摄影", None, 10) == ["b"]
    assert index.text_search("完全不存在", None, 10) == []
    db.delete(["a"])
    assert index.text_search("发版", None, 10) == []
