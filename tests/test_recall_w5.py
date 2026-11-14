"""W5 / P4.1-4.3 验收：情感词典、分段 PAD 门控、激活扩散、事件骨架整体注入。"""
from __future__ import annotations

import pytest

from brain.current_pad_tracker import CurrentPadTracker
from brain.hyperedge_backfill import HyperedgeBackfill
from brain.pad_lexicon import estimate_pad, lexicon_size
from gateway.mcp_server import MCPToolkit
from gateway.registry import AgentRegistry


def _seed(brain, count: int = 4) -> list[str]:
    ids: list[str] = []
    for index in range(count):
        result = brain.hippocampus.remember(
            f"项目发版需要双人复核并确认缓存方案 第{index}轮记录",
            agent_id="agent-a",
            cognition={"topic": "proj", "memory_type": "fact", "source": "agent"},
        )
        ids.append(result.neuron.uuid7_id)
    return ids


def _prepared(brain_factory):
    brain = brain_factory()
    brain.config.hyperedge_backfill.similarity_threshold = 0.1
    ids = _seed(brain)
    backfill = HyperedgeBackfill(relational=brain.storage.relational,
                                 graph=brain.storage.graph, config=brain.config)
    result = backfill.run(limit=100)
    assert result["hyperedges"], "补边必须先成功"
    return brain, ids, result["hyperedges"][0]


# ── C7 / R10：词典与追踪器 ────────────────────────────────────────────────
def test_pad_lexicon_is_fast_and_nonzero() -> None:
    assert lexicon_size() > 30
    assert estimate_pad("今天很开心，太好了！")[0] > 0
    assert estimate_pad("我很生气，又吵架了，太崩溃了！！")[0] < 0
    p, a, d = estimate_pad("这是一段普通说明文字")
    assert p == 0.0 and a > 0 and d > 0  # 中性但不全零


def test_tracker_cold_start_then_recovers(brain_config) -> None:
    tracker = CurrentPadTracker(brain_config)
    assert tracker.weight_multiplier() == 0.0  # 冷启动 w6=0
    for _ in range(4):
        tracker.observe("今天天气不错")
    assert tracker.warmup_done is False
    tracker.observe("今天天气不错")
    assert tracker.warmup_done is True
    assert tracker.weight_multiplier() == 1.0  # A 低，正常生效


def test_tracker_suppresses_negative_when_high_arousal(brain_config) -> None:
    tracker = CurrentPadTracker(brain_config)
    for _ in range(6):
        tracker.observe("我很生气，又吵架了，太崩溃了，非常失望！！")
    state = tracker.current()
    assert state is not None and state.p < -0.15
    assert state.a >= brain_config.pad_gating.arousal_threshold
    assert tracker.suppress_negative is True
    assert tracker.weight_multiplier() == 0.0  # 分段：不是调小权重，是置零 + 配额压制


# ── R4：分段门控进入总分 ─────────────────────────────────────────────────
def test_pad_bonus_is_segmented_not_additive(brain_factory, brain_config) -> None:
    brain = brain_factory()
    tracker = CurrentPadTracker(brain_config)
    brain.prefrontal.pad_tracker = tracker
    neutral = brain.hippocampus.remember(
        "项目发版需要双人复核", agent_id="agent-a",
        cognition={"topic": "proj", "memory_type": "fact"},
    ).neuron
    assert brain.prefrontal._pad_bonus(neutral) == 0.0  # 冷启动
    for _ in range(6):
        tracker.observe("我很生气，又吵架了，太崩溃了，非常失望！！")
    assert brain.prefrontal._pad_bonus(neutral) == 0.0  # 高负唤醒、中性记忆不加权
    emotion = brain.hippocampus.remember(
        "吵架时我很委屈", agent_id="agent-a",
        cognition={"topic": "proj", "memory_type": "emotion"},
    ).neuron
    assert brain.prefrontal._pad_bonus(emotion) < 0   # 负面/情绪记忆被压制


# ── P4.1：激活扩散 ───────────────────────────────────────────────────────
def test_spreading_activation_brings_event_members(brain_factory) -> None:
    brain, ids, _ = _prepared(brain_factory)
    channels = {"dense": [ids[0]], "fts": ids[:2]}
    spread, degraded, note = brain.prefrontal._spreading_channel(channels)
    assert not degraded
    assert any(item in spread for item in ids[1:]), "扩散应带回同事件成员"
    assert "图密度不足" in note or note == ""  # 稀疏图会注明降 1 跳


def test_spreading_respects_timeout(brain_factory) -> None:
    brain, ids, _ = _prepared(brain_factory)
    brain.config.spreading_activation.timeout_ms = 0
    spread, degraded, note = brain.prefrontal._spreading_channel({"dense": ids[:1]})
    assert degraded is True and "超时" in note
    assert spread == []


# ── P4.2 / Q44：事件骨架整体注入，成员不单独占名次 ───────────────────────
def test_hyperedge_skeleton_absorbs_members(brain_factory) -> None:
    brain, ids, hyperedge_id = _prepared(brain_factory)
    channels = {"fts": list(ids), "dense": [ids[0]]}
    narratives = brain.prefrontal._absorb_hyperedges(channels)
    assert narratives and narratives[0]["id"] == hyperedge_id
    assert "【事件】" in narratives[0]["text"]
    remaining = {item for values in channels.values() for item in values}
    assert not (set(ids) & remaining), "内部成员必须从各通道移除"


# ── 与 MCP 工具面集成 ────────────────────────────────────────────────────
def test_recall_for_agent_exposes_narratives_and_degraded(brain_factory) -> None:
    brain, ids, _ = _prepared(brain_factory)
    registry = AgentRegistry(brain.storage.kv, timeseries=brain.storage.timeseries,
                             config=brain.config)
    _, token = registry.register("w5-test")
    agent = registry.verify(token)
    toolkit = MCPToolkit(
        registry=registry, hippocampus=brain.hippocampus,
        prefrontal=brain.prefrontal, relational=brain.storage.relational,
        config=brain.config,
    )
    result = toolkit.recall_for_agent(agent, {
        "query": "项目发版 缓存方案", "deep": True, "channel": "bot", "limit": 5,
    })
    assert "hits" in result and "narratives" in result and "degraded" in result
    assert isinstance(result["narratives"], list)


def test_recall_defaults_to_six_channel_without_explicit_deep(brain_factory) -> None:
    """v4.1：全入口默认六通道。不传 deep 也必须走 six-channel（快速投影退出主路径）。"""
    brain, ids, _ = _prepared(brain_factory)
    registry = AgentRegistry(brain.storage.kv, timeseries=brain.storage.timeseries,
                             config=brain.config)
    _, token = registry.register("default-deep")
    agent = registry.verify(token)
    toolkit = MCPToolkit(
        registry=registry, hippocampus=brain.hippocampus,
        prefrontal=brain.prefrontal, relational=brain.storage.relational,
        config=brain.config,
    )
    # 关键：不传 deep 键
    result = toolkit.recall_for_agent(agent, {"query": "项目发版 缓存方案", "channel": "workbuddy"})
    assert result.get("recall_mode") == "six-channel", (
        f"默认应走六通道，实际={result.get('recall_mode')}")
    assert "narratives" in result and "knowledge_gap" in result
    # 显式 deep=false 且装配了 rpc_fallback 时才降级快速投影（兼容路径仍在）。
    # 注意：无 fallback 时即使 deep=false 也会强制六通道（见 mcp_server 的
    # `if deep or self._rpc_fallback is None`），这是"快速投影退出主路径"的兜底。
    fast_toolkit = MCPToolkit(
        registry=registry, hippocampus=brain.hippocampus,
        prefrontal=brain.prefrontal, relational=brain.storage.relational,
        config=brain.config,
        rpc_fallback=lambda method, args: {"hits": [{"id": "n", "content": "投影", "score": 0.5}]},
    )
    fast = fast_toolkit.recall_for_agent(agent, {"query": "项目发版 缓存方案", "deep": False})
    assert fast.get("recall_mode") == "projection"


# ── C3：精排预算 ─────────────────────────────────────────────────────────
def test_rerank_budget_matches_15s_target(brain_config) -> None:
    assert brain_config.recall.rerank_top_n == 20
    assert brain_config.recall.rerank_timeout_ms == 8000
