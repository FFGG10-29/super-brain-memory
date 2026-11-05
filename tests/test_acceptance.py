"""七阶段剩余验收项：P95 / 三类客户端 / 断网零丢失 / 冷启动重建 / 真实 HTTP 端口。"""
from __future__ import annotations

import json
import threading
import time
import urllib.request

import pytest

from eval.golden_set import SAMPLE_CASES, evaluate, save_baseline
from gateway.http_adapter import HTTPAdapter
from gateway.mcp_server import MCPToolkit
from gateway.registry import AgentRegistry
from core.ids import HLCStamp
from main import build_storage
from sync.event_store import EventStore
from sync.git_worker import GitSyncWorker
from sync.journal import NdjsonJournal
from sync.projections import EventProjector
from sync.snapshot import SnapshotManager
from tests.test_phase3_to6 import PAYLOAD, _neuron_record, _projector, _two_configs


def _gateway(brain):
    registry = AgentRegistry(brain.storage.kv, timeseries=brain.storage.timeseries, config=brain.config)
    toolkit = MCPToolkit(
        registry=registry, hippocampus=brain.hippocampus, prefrontal=brain.prefrontal,
        relational=brain.storage.relational, engines=brain.storage.all(), config=brain.config,
    )
    return registry, toolkit


def test_recall_p95_under_800ms(brain_factory, brain_config):
    brain = brain_factory(llm_payload=PAYLOAD)
    for index in range(12):
        brain.hippocampus.remember(f"性能基线第 {index} 条发版记录", agent_id="agent-a")
    latencies = []
    for index in range(20):
        started = time.perf_counter()
        brain.prefrontal.recall(f"发版记录 {index}", agent_id="agent-a")
        latencies.append((time.perf_counter() - started) * 1000)
    p95 = sorted(latencies)[int(len(latencies) * 0.95) - 1]
    assert p95 < brain_config.recall.p95_target_ms, f"CPU 单次召回 P95={p95:.1f}ms 超标"


def test_golden_set_baseline_saved(brain_factory, tmp_path):
    brain = brain_factory(llm_payload=PAYLOAD)
    brain.hippocampus.remember("周五发版冲突的复盘记录", agent_id="agent-a")
    metrics = evaluate(brain.prefrontal, SAMPLE_CASES, agent_id="agent-a", k=5)
    path = save_baseline(tmp_path / "baseline.json", metrics, label="stage2-cpu")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["label"] == "stage2-cpu"
    assert 0.0 <= saved["metrics"]["recall@5"] <= 1.0
    assert 0.0 <= saved["metrics"]["mrr"] <= 1.0


def test_three_client_types_each_one_connection_string(brain_factory):
    """MCP×2 + HTTP×1，每个只拿一条 token 即可通；命名空间隔离生效。"""
    brain = brain_factory(llm_payload=PAYLOAD)
    registry, toolkit = _gateway(brain)
    mcp_a = registry.register("mcp-client-a", scopes=("private",))
    mcp_b = registry.register("mcp-client-b", scopes=("private",))
    http_c = registry.register("http-client-c", scopes=("group",))
    adapter = HTTPAdapter(registry=registry, toolkit=toolkit, config=brain.config)

    for index, (_, token) in enumerate((mcp_a, mcp_b)):
        headers = {"Authorization": f"Bearer {token}"}
        saved = toolkit.call(
            "save_memory", {"content": f"MCP 客户端 {index} 的一条独立记忆"}, headers=headers
        )
        assert saved["applied"] is True, "内容需各自不同，否则丘脑去重会正确拒收"
        assert "hits" in toolkit.call("recall_memory", {"query": "MCP"}, headers=headers)

    headers_c = {"Authorization": f"Bearer {http_c[1]}"}
    status, saved = adapter.handle("POST", "/save", headers=headers_c, body={"text": "HTTP 客户端记忆"})
    assert status == 200 and saved["applied"] is True
    assert adapter.handle("POST", "/search", headers=headers_c, body={"query": "HTTP"})[0] == 200
    # group 客户端不能读写 private 命名空间
    denied = toolkit.call("save_memory", {"content": "越权写入", "namespace": "private"}, headers=headers_c)
    assert "error" in denied


def test_offline_reconnect_zero_loss(tmp_path):
    """断网期间本地照写，恢复后一次重放全部到位。"""
    cfg_a, cfg_b = _two_configs(tmp_path)
    root = tmp_path / "journal"
    remote_journal = NdjsonJournal(root, device_id="dev-a")
    for index in range(5):
        remote_journal.append(
            {"event_id": f"offline-{index}", "type": "ingest", "hlc": f"{1000 + index}:0:dev-a",
             "agent_id": "agent-a", "payload": {"neuron": _neuron_record(f"N{index}", f"离线事件 {index}")}}
        )
    store = EventStore(root)
    storage = build_storage(cfg_b)
    try:
        worker = GitSyncWorker(root=root, device_id="dev-b", event_store=store,
                               projector=_projector(cfg_b, storage))
        first = worker.sync_once()
        assert first.ok and first.replayed == 5
        for index in range(5):
            assert storage.relational.get("neuron", f"N{index}") is not None
        # 重连后再来两条，仍零丢失
        for index in range(5, 7):
            remote_journal.append(
                {"event_id": f"online-{index}", "type": "ingest", "hlc": f"{1000 + index}:0:dev-a",
                 "agent_id": "agent-a", "payload": {"neuron": _neuron_record(f"N{index}", f"恢复后事件 {index}")}}
            )
        second = worker.sync_once()
        assert second.replayed == 2
        assert storage.relational.count("neuron", active_only=False) == 7
    finally:
        storage.close()


def test_new_device_cold_start_snapshot_plus_tail_replay(tmp_path):
    """新设备：载入快照 + 重放尾部事件 → 全量重建且 PAD 一致。"""
    cfg_a, cfg_b = _two_configs(tmp_path)
    root = tmp_path / "journal"
    journal = NdjsonJournal(root, device_id="dev-a")
    for index in range(3):
        record = _neuron_record(f"N{index}", f"快照前事件 {index}")
        journal.append({"event_id": f"base-{index}", "type": "ingest",
                        "hlc": f"{1000 + index}:0:dev-a", "agent_id": "agent-a",
                        "payload": {"neuron": record}})

    source = build_storage(cfg_a)
    fresh = build_storage(cfg_b)
    try:
        projector = _projector(cfg_a, source)
        for event in EventStore(root).scan():
            projector.apply(event)
        manager = SnapshotManager(root)
        manager.create(source.relational, hlc_cutoff="1002:0:dev-a", label="snapshot-cut")

        tail = _neuron_record("N-tail", "快照之后的尾部事件")
        tail["pad_a"] = 0.77
        journal.append({"event_id": "tail-1", "type": "ingest", "hlc": "2000:0:dev-a",
                        "agent_id": "agent-a", "payload": {"neuron": tail}})

        assert manager.restore(fresh.relational) == 3
        fresh_projector = _projector(cfg_b, fresh)
        cutoff = HLCStamp.from_token(manager.meta().hlc_cutoff)
        for event in EventStore(root).scan(after=cutoff):
            fresh_projector.apply(event)
        assert fresh.relational.count("neuron", active_only=False) == 4
        restored = fresh.relational.get("neuron", "N-tail")
        assert restored is not None and restored["pad_a"] == pytest.approx(0.77), "PAD 必须跨设备一致"
        assert fresh.timeseries.count("pad_track") == 1, "尾部事件投影必须补 PAD 轨迹"
    finally:
        source.close()
        fresh.close()


def test_http_server_real_port(brain_factory):
    """真实 HTTP 端口冒烟：/health 可达，shutdown 可优雅停。"""
    brain = brain_factory(llm_payload=PAYLOAD)
    registry, toolkit = _gateway(brain)
    adapter = HTTPAdapter(registry=registry, toolkit=toolkit, config=brain.config)
    server = adapter.make_server(host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
            assert response.status == 200
            assert json.loads(response.read().decode("utf-8"))["status"] == "ok"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
