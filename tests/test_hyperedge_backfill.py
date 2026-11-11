"""W4 / P4.0 补边验收：聚簇、骨架 payload、幂等建边、图密度、RPC 触发。"""
from __future__ import annotations

from types import SimpleNamespace

from brain.hyperedge_backfill import GraphDensity, HyperedgeBackfill, graph_density
from storage.hypergraph_db import GraphHypergraph
from v3.rpc import RpcHandler


def _seed(brain, count: int = 5, topic: str = "proj") -> None:
    for index in range(count):
        brain.hippocampus.remember(
            f"项目发版需要双人复核并确认缓存方案 第{index}轮记录",
            agent_id="agent-a",
            cognition={"topic": topic, "memory_type": "fact", "source": "agent"},
        )


def _backfill(brain) -> HyperedgeBackfill:
    return HyperedgeBackfill(
        relational=brain.storage.relational, graph=brain.storage.graph,
        config=brain.config,
    )


def test_backfill_creates_hyperedges_and_member_edges(brain_factory) -> None:
    brain = brain_factory()
    brain.config.hyperedge_backfill.similarity_threshold = 0.1
    _seed(brain)
    service = _backfill(brain)
    before = graph_density(brain.storage.graph, brain.config)
    result = service.run(limit=100)
    assert result["ok"] is True
    assert result["clusters"] >= 1 and result["hyperedges"]
    after = graph_density(brain.storage.graph, brain.config)
    assert after.member_edges > before.member_edges  # MEMBER 边确实补上了
    assert "density_before" in result and "density_after" in result


def test_skeleton_lives_in_payload_and_has_four_blocks(brain_factory) -> None:
    brain = brain_factory()
    brain.config.hyperedge_backfill.similarity_threshold = 0.1
    _seed(brain)
    service = _backfill(brain)
    result = service.run(limit=100)
    hyperedge_id = result["hyperedges"][0]
    row = brain.storage.relational.get("hyperedge", hyperedge_id)
    assert row is not None and isinstance(row.get("skeleton"), dict)
    text = service.skeleton_text(hyperedge_id)
    for marker in ("【事件】", "【成员】", "【情绪】", "【后续】"):
        assert marker in text, f"骨架缺少 {marker}：{text}"
    # Hyperedge 模型不动：Kùzu 只加了 MEMBER 边，没有新列
    assert brain.storage.graph.members(hyperedge_id)


def test_backfill_is_idempotent(brain_factory) -> None:
    brain = brain_factory()
    brain.config.hyperedge_backfill.similarity_threshold = 0.1
    _seed(brain)
    service = _backfill(brain)
    first = service.run(limit=100)
    edges_after_first = graph_density(brain.storage.graph, brain.config).member_edges
    second = service.run(limit=100)
    edges_after_second = graph_density(brain.storage.graph, brain.config).member_edges
    assert first["hyperedges"] == second["hyperedges"]  # 稳定 id
    assert edges_after_first == edges_after_second      # MERGE 幂等，不重复建边


def test_high_threshold_produces_no_cluster(brain_factory) -> None:
    brain = brain_factory()
    brain.config.hyperedge_backfill.similarity_threshold = 0.999
    _seed(brain, count=2)
    result = _backfill(brain).run(limit=100)
    assert result["ok"] is True and result["clusters"] == 0
    assert result["density_after"]["advice"].startswith("先跑")


def test_disabled_backfill_is_a_noop(brain_factory) -> None:
    brain = brain_factory()
    brain.config.hyperedge_backfill.enabled = False
    result = _backfill(brain).run(limit=10)
    assert result["ok"] is False and "enabled=false" in result["message"]


def test_graph_density_sparse_flag() -> None:
    sparse = GraphDensity(nodes=10, relates_edges=2, member_edges=0, avg_degree=0.4)
    dense = GraphDensity(nodes=10, relates_edges=20, member_edges=5, avg_degree=4.5)
    assert sparse.sparse is True and dense.sparse is False
    assert "降 1 跳" in sparse.as_dict()["advice"]


def test_rpc_handlers_expose_density_backfill_and_skeleton(brain_factory) -> None:
    brain = brain_factory()
    brain.config.hyperedge_backfill.similarity_threshold = 0.1
    _seed(brain)
    daemon = SimpleNamespace(app=brain, worker_mode="thread")
    handler = RpcHandler(daemon)
    density = handler.dispatch({"method": "graph.density", "args": {}})
    assert density["ok"] is True and "avg_degree" in density["result"]
    backfill = handler.dispatch({"method": "graph.backfill", "args": {"limit": 100}})
    assert backfill["ok"] is True and backfill["result"]["ok"] is True
    hyperedge_id = backfill["result"]["hyperedges"][0]
    skeleton = handler.dispatch({"method": "graph.event_skeleton",
                                 "args": {"hyperedge_id": hyperedge_id}})
    assert skeleton["ok"] is True and "【事件】" in skeleton["result"]["text"]


def test_missing_skeleton_falls_back_to_members(brain_factory) -> None:
    brain = brain_factory()
    graph = brain.storage.graph
    graph.upsert_hyperedge_node("legacy-event", label="旧事件", occurred_at_ms=1)
    graph.upsert_entity("e1", name="e1")
    graph.add_member("legacy-event", "e1")
    view = GraphHypergraph(graph, relational=brain.storage.relational)
    text = view.get_event_skeleton("legacy-event")
    assert "【事件】" in text and "历史数据" in text and "e1" in text
