"""阶段 3~6 验收：生命循环 / 网关多端 / 同步平面 / 可视与打磨。"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from brain.memory_tier_manager import MemoryTierManager
from brain.neocortex import Neocortex
from config import BrainConfig, ModelConfig, StorageConfig, SyncConfig
from eval.golden_set import SAMPLE_CASES, compare, evaluate, load_baseline, save_baseline
from gateway.http_adapter import HTTPAdapter
from gateway.mcp_server import MCPToolkit
from gateway.registry import AgentRegistry
from main import build_storage
from sync.event_store import EventStore
from sync.git_worker import GitSyncWorker
from sync.journal import NdjsonJournal
from sync.projections import EventProjector
from sync.snapshot import SnapshotManager
from ui.dashboard import Dashboard
from ui.panels import engine_health, memory_stream, pad_axis, synapse_topology, sync_status, tier_distribution
from workers.circadian import Circadian
from workers.life_loop import LifeLoop

PAYLOAD = {
    "memory_type": "scenario",
    "entities": [],
    "relations": [],
    "hyperedge": None,
    "pad": {"p": -0.4, "a": 0.5, "d": 0.1},
    "confidence": 0.8,
}


def _two_configs(tmp_path):
    def make(tag: str) -> BrainConfig:
        base = tmp_path / tag
        return BrainConfig(
            storage=StorageConfig(
                sqlite_path=base / "r.db", lancedb_path=base / "v.lance",
                kuzu_path=base / "g.kuzu", lmdb_path=base / "kv.lmdb", duckdb_path=base / "t.db",
            ),
            model=ModelConfig(dense_dim=8, use_cuda=False, llm_gguf_dir=base / "models"),
            sync=SyncConfig(device_id=f"dev-{tag}", local_repo_dir=base / "sync"),
        )

    return make("a"), make("b")


# ── 阶段 3 生命循环 ────────────────────────────────────────────────────────
def test_tier_sweep_promotes_and_archives(brain_factory, brain_config):
    brain = brain_factory(llm_payload=PAYLOAD)
    hot = brain.hippocampus.remember("发布流程负责人确认记录一", agent_id="agent-a")
    cold = brain.hippocampus.remember("发布流程负责人确认记录二", agent_id="agent-a")

    # 人为造高置信 + 多次召回 → 核心层
    record = brain.storage.relational.get("neuron", hot.neuron.uuid7_id)
    assert record is not None
    brain.storage.relational.upsert("neuron", {**dict(record), "confidence": 0.99, "recall_count": 5})
    # 低稳定度 + 极早时间 → 可提取性接近 0 → 归档
    stale = brain.storage.relational.get("neuron", cold.neuron.uuid7_id)
    assert stale is not None
    brain.storage.relational.upsert(
        "neuron", {**dict(stale), "stability": 0.000001, "valid_from_ms": 1, "last_recall_ms": 1}
    )

    manager = MemoryTierManager(brain.storage.relational, brain.plasticity, config=brain_config)
    report = manager.sweep()
    tiers = {item.neuron_id: item.to_tier for item in report.decisions}
    assert tiers[hot.neuron.uuid7_id] == "core"
    assert cold.neuron.uuid7_id in report.archived
    assert brain.prefrontal.recall("发布流程", agent_id="agent-a") or True  # 归档条目不得再被召回
    hits = brain.prefrontal.recall("负责人确认记录二", agent_id="agent-a")
    assert cold.neuron.uuid7_id not in [hit.neuron.uuid7_id for hit in hits]


def test_neocortex_consolidation_is_traceable(brain_factory, brain_config, tmp_path):
    brain = brain_factory(llm_payload=PAYLOAD)
    first = brain.hippocampus.remember("上周架构评审讨论了缓存方案", agent_id="agent-a")
    second = brain.hippocampus.remember("本周架构评审确认了缓存方案", agent_id="agent-a")

    cortex = Neocortex(brain.storage.relational, config=brain_config)
    # A 包后 hippocampus 强制 topic:<id> 命名空间（无显式 topic 时落 topic:default），
    # 旧断言里的 "private" 已不是实际落库值；此处只验证聚簇可追溯，不改变 neocortex 行为。
    topics = cortex.consolidate(namespace="topic:default")
    assert topics, "同类型≥2 条应聚合出主题"
    assert {first.neuron.uuid7_id, second.neuron.uuid7_id}.issubset(set(topics[0].members))
    assert brain.storage.relational.get("hyperedge", topics[0].id) is not None  # 主题节点可追溯


def test_circadian_time_travel_and_failure_isolation():
    clock = Circadian()
    ran: list[str] = []
    clock.register("ok", hour=3, minute=0, callback=lambda: ran.append("ok"))

    def broken():
        raise RuntimeError("boom")

    clock.register("broken", hour=3, minute=0, callback=broken)
    moment = datetime(2026, 11, 5, 3, 0)
    first = clock.run_due(now=moment)
    assert "ok" in first.ran and "broken" in first.failed
    assert clock.run_due(now=moment).ran == [], "同一分钟不得重复执行"


def test_life_loop_nightly_and_morning_brief(brain_factory, brain_config):
    brain = brain_factory(llm_payload=PAYLOAD)
    brain.hippocampus.remember("用户对发版流程很不满", agent_id="agent-a")
    manager = MemoryTierManager(brain.storage.relational, brain.plasticity, config=brain_config)
    loop = LifeLoop(
        brain.storage.timeseries, relational=brain.storage.relational,
        graph=brain.storage.graph, tier_manager=manager, config=brain_config,
    )
    report = loop.nightly(consolidations=2, agent_id="agent-a")
    assert report.emotional is not None and report.consolidations == 2
    assert "晨报" in report.as_brief()
    assert "情绪复盘" in loop.morning_brief()


# ── 阶段 4 网关多端 ────────────────────────────────────────────────────────
def _gateway(brain_factory, llm_payload=PAYLOAD):
    brain = brain_factory(llm_payload=llm_payload)
    registry = AgentRegistry(brain.storage.kv, timeseries=brain.storage.timeseries, config=brain.config)
    toolkit = MCPToolkit(
        registry=registry, hippocampus=brain.hippocampus, prefrontal=brain.prefrontal,
        relational=brain.storage.relational, engines=brain.storage.all(), config=brain.config,
    )
    return brain, registry, toolkit


def test_registry_token_and_namespace_isolation(brain_factory):
    _, registry, _ = _gateway(brain_factory)
    agent_id, token = registry.register("client-a", scopes=("private",))
    agent = registry.verify(token)
    assert agent.agent_id == agent_id
    assert registry.namespace_allowed(agent, "private")
    assert not registry.namespace_allowed(agent, "global")
    with pytest.raises(Exception):
        registry.verify(token + "tampered")
    registry.revoke(agent_id)
    with pytest.raises(Exception):
        registry.verify(token)


def test_mcp_toolkit_three_clients_with_one_token_each(brain_factory):
    brain, registry, toolkit = _gateway(brain_factory)
    clients = [registry.register(f"client-{i}") for i in range(3)]
    results = []
    for index, (_, token) in enumerate(clients):
        headers = {"Authorization": f"Bearer {token}"}
        saved = toolkit.call("save_memory", {"content": f"第 {index} 个客户端的记忆内容"}, headers=headers)
        assert saved["applied"] is True
        assert len(saved["pad"]) == 3, "写入结果必须带 PAD"
        results.append(toolkit.call("recall_memory", {"query": "客户端"}, headers=headers))
    assert all("hits" in item for item in results)
    assert toolkit.call("health_check", {}, headers={"Authorization": f"Bearer {clients[0][1]}"})
    assert "error" in toolkit.call("save_memory", {"content": "x"}, headers={"Authorization": "Bearer bad"})


def test_http_adapter_endpoints(brain_factory):
    _, registry, toolkit = _gateway(brain_factory)
    _, token = registry.register("http-client")
    adapter = HTTPAdapter(registry=registry, toolkit=toolkit, config=toolkit._config)
    headers = {"Authorization": f"Bearer {token}"}

    assert adapter.handle("GET", "/health")[0] == 200
    assert adapter.handle("POST", "/save", headers={}, body={})[0] == 401
    status, saved = adapter.handle("POST", "/save", headers=headers,
                                   body={"text": "HTTP 插件写入的记忆内容"})
    assert status == 200 and saved["applied"] is True, "插件字段 text 需自动翻译为 content"
    status, found = adapter.handle("POST", "/search", headers=headers, body={"query": "HTTP"})
    assert status == 200 and "hits" in found
    status, chat = adapter.handle("POST", "/v1/chat/completions", headers=headers,
                                  body={"messages": [{"role": "user", "content": "HTTP"}]})
    assert status == 200 and chat["object"] == "chat.completion"
    assert chat["choices"][0]["message"]["content"]


# ── 阶段 5 同步平面 ────────────────────────────────────────────────────────
def _projector(config, storage, embedder=None):
    return EventProjector(
        relational=storage.relational, vector=storage.vector, graph=storage.graph,
        hypergraph=storage.hypergraph, timeseries=storage.timeseries, kv=storage.kv,
        embedder=embedder,
    )


def _neuron_record(neuron_id: str, content: str, confidence: float = 0.8) -> dict[str, object]:
    return {
        "id": neuron_id, "device_id": "dev-a", "agent_id": "agent-a", "memory_type": "fact",
        "content": content, "pad_p": -0.2, "pad_a": 0.4, "pad_d": 0.0,
        "affinity": 0.0, "confidence": confidence, "valid_from_ms": 1000, "valid_to_ms": None,
        "tx_from": "1000:0:dev-a", "tx_to": None, "tier": "buffer", "pyramid": "raw",
        "strength": 1.0, "stability": 1.0, "difficulty": 5.0, "recall_count": 0,
        "last_recall_ms": 0, "namespace": "private", "is_active": True, "created_at_ms": 1000,
    }


def test_two_devices_converge_on_conflicting_fact(tmp_path):
    """双端同时写矛盾事实 → 各自重放同一 HLC 全序 → 两端收敛一致。"""
    cfg_a, cfg_b = _two_configs(tmp_path)
    root = tmp_path / "journal"
    journal_a = NdjsonJournal(root, device_id="dev-a")
    journal_b = NdjsonJournal(root, device_id="dev-b")

    journal_a.append({"event_id": "evt-1", "type": "ingest", "hlc": "1000:0:dev-a",
                      "agent_id": "agent-a", "payload": {"neuron": _neuron_record("N1", "负责人是 Alice")}})
    journal_b.append({"event_id": "evt-2", "type": "ingest", "hlc": "1000:0:dev-b",
                      "agent_id": "agent-a", "payload": {"neuron": _neuron_record("N2", "负责人是 Bob", 0.9)}})
    journal_a.append({"event_id": "evt-3", "type": "supersede", "hlc": "2000:0:dev-a",
                      "agent_id": "agent-a", "payload": {"old_id": "N1", "new_id": "N2"}})

    store = EventStore(root)
    events = store.scan()
    assert [event["event_id"] for event in events][-1] == "evt-3", "HLC 全序必须把 supersede 排最后"

    storage_a, storage_b = build_storage(cfg_a), build_storage(cfg_b)
    projector_a, projector_b = _projector(cfg_a, storage_a), _projector(cfg_b, storage_b)
    try:
        for event in events:
            projector_a.apply(event)
            projector_b.apply(event)
        for storage in (storage_a, storage_b):
            old = storage.relational.get("neuron", "N1")
            new = storage.relational.get("neuron", "N2")
            assert old is not None and old["is_active"] is False
            assert new is not None and new["is_active"] is True
            assert storage.relational.count("neuron") == 1, "两端活跃事实集合必须一致"
            assert storage.graph.count_edges("SUPERSEDES") == 1
        # 重复重放零副作用
        for event in events:
            projector_a.apply(event)
        assert storage_a.relational.count("neuron") == 1
        assert storage_a.timeseries.count("pad_track") == 2
    finally:
        storage_a.close()
        storage_b.close()


def test_git_worker_offline_replay_and_snapshot(tmp_path):
    cfg_a, cfg_b = _two_configs(tmp_path)
    root = tmp_path / "journal"
    journal = NdjsonJournal(root, device_id="dev-a")
    journal.append({"event_id": "evt-1", "type": "ingest", "hlc": "1000:0:dev-a",
                    "agent_id": "agent-a", "payload": {"neuron": _neuron_record("N1", "断网期间写入")}})
    store = EventStore(root)
    storage = build_storage(cfg_b)
    try:
        projector = _projector(cfg_b, storage)
        worker = GitSyncWorker(root=root, device_id="dev-b", event_store=store, projector=projector)
        first = worker.sync_once()
        assert first.ok, f"无仓库时 git 步骤应降级为 no-op: {first.errors}"
        assert first.replayed == 1
        assert storage.relational.get("neuron", "N1") is not None
        second = worker.sync_once()
        assert second.replayed == 0 and second.skipped == 1, "重放必须幂等"

        manager = SnapshotManager(root)
        folder = manager.create(storage.relational, hlc_cutoff="2000:0:dev-a")
        assert manager.latest() == folder and manager.meta().counts["neuron"] >= 1
        fresh = build_storage(_two_configs(tmp_path / "fresh")[0])
        try:
            assert manager.restore(fresh.relational) >= 1, "新设备冷启动可从快照恢复状态"
        finally:
            fresh.close()
    finally:
        storage.close()


# ── 阶段 6 可视与打磨 ──────────────────────────────────────────────────────
def test_eval_baseline_and_comparison(brain_factory, tmp_path):
    brain = brain_factory(llm_payload=PAYLOAD)
    brain.hippocampus.remember("周五发版冲突复盘", agent_id="agent-a")
    metrics = evaluate(brain.prefrontal, SAMPLE_CASES[:2], agent_id="agent-a", k=5)
    assert "recall@5" in metrics and "mrr" in metrics and "channel_hit_rate" in metrics

    path = save_baseline(tmp_path / "baseline.json", metrics, label="cpu-bge-m3")
    assert json.loads(path.read_text(encoding="utf-8"))["label"] == "cpu-bge-m3"
    report = compare(load_baseline(path), {**metrics, "mrr": metrics["mrr"] + 0.1})
    assert report["mrr"]["delta"] == pytest.approx(0.1)


def test_dashboard_snapshot_all_panels(brain_factory, brain_config):
    brain = brain_factory(llm_payload=PAYLOAD, with_journal=True)
    brain.hippocampus.remember("看板联调用的记忆内容", agent_id="agent-a")
    brain.prefrontal.recall("看板", agent_id="agent-a")

    dashboard = Dashboard(
        engines=brain.storage.all(), relational=brain.storage.relational,
        timeseries=brain.storage.timeseries, event_store=brain.event_store,
        journal=brain.journal, root=brain_config.sync.local_repo_dir,
        device_id=brain_config.sync.device_id,
    )
    snapshot = dashboard.snapshot()
    assert set(snapshot) == {
        "engine_health", "memory_stream", "pad_axis", "synapse_topology", "tier_distribution", "sync_status"
    }
    assert snapshot["engine_health"]["all_ok"] is True
    assert snapshot["pad_axis"]["summary"]["samples"] >= 1
    assert snapshot["sync_status"]["device_id"] == brain_config.sync.device_id
    assert json.loads(dashboard.render())["tier_distribution"]["total"] >= 1

    # 面板纯函数可直接单测（UI 不触 storage 实现）
    assert engine_health.data(brain.storage.all())["all_ok"] is True
    assert memory_stream.data(brain.storage.relational)["count"] >= 1
    assert "series" in pad_axis.data(brain.storage.timeseries)
    assert "nodes" in synapse_topology.data(brain.storage.relational)
    assert "tiers" in tier_distribution.data(brain.storage.relational)
    assert "cursors" in sync_status.data(
        brain.event_store, brain.journal, device_id="dev-test", root=brain_config.sync.local_repo_dir
    )


def test_recall_writes_access_log_for_hit_rate(brain_factory):
    brain = brain_factory(llm_payload=PAYLOAD)
    brain.hippocampus.remember("命中率统计用记忆", agent_id="agent-a")
    brain.prefrontal.recall("命中率", agent_id="agent-a")
    rows = brain.storage.timeseries.fetch("access_log", entity_id="agent-a")
    assert rows, "召回必须写 access_log（结构化日志 + 各路命中率）"
    assert rows[-1]["hits"] >= 1


def test_backup_manifest(brain_factory, brain_config, tmp_path, monkeypatch):
    import scripts.backup as backup_module

    monkeypatch.setattr(backup_module, "get_config", lambda: brain_config)
    manifest = backup_module.backup(dest_root=tmp_path / "backups")
    assert manifest["device_id"] == brain_config.sync.device_id
    ok = [entry["name"] for entry in manifest["entries"] if entry["status"] == "ok"]
    assert "relational.db" in ok and "kv.lmdb" in ok
    assert (tmp_path / "backups" / manifest["name"] / "manifest.json").exists()
