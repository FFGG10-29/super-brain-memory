"""v3 验收：inbox 租约状态机、journal group commit/补齐、受理延迟、Worker 端到端。"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from config import BrainConfig, ModelConfig, StorageConfig, SyncConfig
from v3.daemon import V3Daemon
from v3.inbox import Inbox
from v3.journal import V3Journal


@pytest.fixture()
def v3_config(tmp_path):
    return BrainConfig(
        storage=StorageConfig(
            sqlite_path=tmp_path / "projection.db", lancedb_path=tmp_path / "vector.lance",
            kuzu_path=tmp_path / "graph.kuzu", lmdb_path=tmp_path / "kv.lmdb",
            duckdb_path=tmp_path / "timeseries.db",
        ),
        model=ModelConfig(dense_dim=8, use_cuda=False, llm_gguf_dir=tmp_path / "models"),
        sync=SyncConfig(device_id="v3-test", local_repo_dir=tmp_path / "sync"),
    )


def _event(i: int) -> dict:
    return {"event_id": f"evt-{i:04d}", "content": f"测试记忆 {i}", "agent_id": "tester", "namespace": "private"}


# ── inbox 状态机 ──────────────────────────────────────────────────────────
def test_inbox_claim_is_exclusive_across_workers(tmp_path):
    inbox = Inbox(tmp_path / "q.db")
    for index in range(10):
        assert inbox.enqueue(_event(index)) is True
    assert inbox.enqueue(_event(0)) is False, "event_id 必须幂等"

    first = inbox.claim("worker-A", limit=6)
    second = inbox.claim("worker-B", limit=6)
    assert len(first) == 6 and len(second) == 4
    assert {row["event_id"] for row in first}.isdisjoint({row["event_id"] for row in second})
    assert inbox.counts()["processing"] == 10


def test_lease_recycle_and_cas_finish(tmp_path):
    inbox = Inbox(tmp_path / "q.db")
    inbox.enqueue(_event(1))
    claimed = inbox.claim("worker-A", limit=1, lease_s=0.01)[0]
    assert inbox.finish(claimed["event_id"], "worker-B", ok=True) is False, "非持有者不能完成（CAS）"
    time.sleep(0.03)
    assert inbox.recycle_expired() == 1, "租约超时必须回收"
    reclaimed = inbox.claim("worker-B", limit=1)[0]
    assert reclaimed["event_id"] == claimed["event_id"]
    assert inbox.finish(reclaimed["event_id"], "worker-B", ok=True) is True
    assert inbox.counts().get("done") == 1


# ── journal ───────────────────────────────────────────────────────────────
def test_journal_group_commit_concurrent_and_reconcile(tmp_path):
    journal = V3Journal(tmp_path / "event-log", device_id="test")
    inbox = Inbox(tmp_path / "q.db")
    errors: list[Exception] = []

    def writer(index: int) -> None:
        try:
            journal.append(f"并发写入 {index}", agent_id="tester")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert not errors
    events = journal.iter_events()
    assert len(events) == 20, "每条都必须 fsync 后才返回"
    assert journal.last_event_id() == events[-1]["event_id"]

    # 模拟崩溃窗口：只入队 3 条，其余靠 reconcile 补齐
    for event in events[:3]:
        inbox.enqueue(event)
    assert journal.reconcile(inbox) == 17
    assert inbox.counts()["pending"] == 20
    journal.close()
    inbox.close()


# ── 受理路径 + Worker 端到端 ─────────────────────────────────────────────
def test_ack_does_not_wait_for_cognition_and_worker_drains(v3_config, tmp_path):
    daemon = V3Daemon(tmp_path / "v3", config=v3_config, use_llm=False)
    daemon.start(serve_http=False, serve_ws=False)
    try:
        acks = [daemon.handle_event(f"异步链路验证 {i} 号记忆", agent_id="tester") for i in range(3)]
        assert all(ack["accepted"] for ack in acks)
        assert max(ack["ack_ms"] for ack in acks) < 500, "受理不得等待模型/投影"
        assert len(daemon.journal.iter_events()) == 3
        # Worker 后台消费：等它追平
        deadline = time.time() + 15
        while time.time() < deadline and daemon.inbox.counts().get("done", 0) < 3:
            time.sleep(0.1)
        assert daemon.inbox.counts().get("done", 0) == 3, "Worker 必须 drain 到空"
        assert daemon.health()["worker"]["processed"] == 3
        assert len(daemon.journal.iter_events()) == 3, "Worker 不得把事件再写回 journal"
        assert daemon.memories(limit=20), "投影表里应能读到新记忆"
    finally:
        daemon.stop()


def test_http_events_endpoint_returns_202_accepted(v3_config, tmp_path):
    http_port = 18768
    daemon = V3Daemon(tmp_path / "v3", config=v3_config, http_port=http_port, use_llm=False)
    daemon.start(serve_http=True, serve_ws=False)
    try:
        body = json.dumps({"content": "通过 HTTP 受理的记忆", "agent_id": "mcp"}).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{http_port}/v1/events", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 本地回环
            assert response.status == 202
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["accepted"] is True and payload["event_id"]
        with urllib.request.urlopen(f"http://127.0.0.1:{http_port}/v1/health", timeout=5) as response:  # noqa: S310
            health = json.loads(response.read().decode("utf-8"))
        assert health["ok"] is True and health["journal_events"] >= 1
    finally:
        daemon.stop()
