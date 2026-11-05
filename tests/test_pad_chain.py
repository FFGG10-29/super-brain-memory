"""情绪记忆全链路验证：PAD 在 8 个环节的贯穿位置。

    ① 生成   router_extractor（LLM/规则）
    ② 建模   models.neuron.PAD（强类型 + 显著性）
    ③ 存储   SQLite pad_p/a/d + DuckDB pad_track
    ④ 检索   prefrontal w_pad·salience（+ 杏仁核冷静期折扣）
    ⑤ 巩固   synaptic_plasticity（高唤醒加成）
    ⑥ 生命循环 workers.life_loop.emotional_review（异常检出）
    ⑦ 同步   sync.journal 事件 payload
    ⑧ 可视化 ui.panels.pad_axis（曲线点位）

一段测试走完 8 段，任何一段丢失 PAD 都会红。
"""
from __future__ import annotations

import pytest

from models.neuron import PAD
from ui.panels.pad_axis import pad_series, pad_summary
from workers.life_loop import LifeLoop

PAYLOAD = {
    "memory_type": "emotion",
    "entities": [{"name": "发布流程", "type": "project"}],
    "relations": [],
    "hyperedge": {"label": "发布冲突", "members": ["发布流程", "Alice"]},
    "pad": {"p": -0.7, "a": 0.8, "d": 0.1},
    "confidence": 0.9,
}


def test_pad_survives_the_whole_chain(brain_factory):
    brain = brain_factory(llm_payload=PAYLOAD, with_embedder=True, with_journal=True)

    # ① 生成：LLM 给出的 PAD 必须原样落到记忆上
    result = brain.hippocampus.remember("周五发版又崩了，Alice 很生气！", agent_id="agent-a")
    neuron = result.neuron
    assert (neuron.pad.p, neuron.pad.a, neuron.pad.d) == pytest.approx((-0.7, 0.8, 0.1))

    # ② 建模：PAD 是强类型值对象，显著性可计算
    assert isinstance(neuron.pad, PAD)
    assert neuron.pad.salience() > 0.5, "高唤醒记忆的显著性必须显著高于中性"

    # ③ 存储：SQLite 三列 + DuckDB 轨迹（同源同值）
    stored = brain.storage.relational.get("neuron", neuron.uuid7_id)
    assert stored is not None
    assert (stored["pad_p"], stored["pad_a"], stored["pad_d"]) == pytest.approx((-0.7, 0.8, 0.1))
    track = brain.storage.timeseries.fetch("pad_track", entity_id=neuron.uuid7_id)
    assert len(track) == 1 and track[0]["a"] == pytest.approx(0.8)

    # ④ 检索：PAD 显著性参与总分，且命中结果带 pad
    hits = brain.prefrontal.recall("发版", agent_id="agent-a", limit=3)
    assert hits and hits[0].neuron.uuid7_id == neuron.uuid7_id
    assert hits[0].neuron.pad.a == pytest.approx(0.8)

    # ⑤ 巩固：强化后稳定度上升（高唤醒加成），衰减快照入时序库
    reinforced = brain.hippocampus.reinforce(neuron.uuid7_id)
    assert reinforced is not None
    assert reinforced.stability > neuron.stability
    assert reinforced.recall_count == 1
    decay = brain.storage.timeseries.fetch("decay_event", entity_id=neuron.uuid7_id)
    assert decay, "强化必须留下衰减快照"

    # ⑥ 生命循环：高负唤醒被复盘识别为异常
    review = LifeLoop(brain.storage.timeseries, config=brain.config).emotional_review(agent_id="agent-a")
    assert review.samples >= 1
    assert review.anomalies, "P≤-0.3 且 A≥0.6 必须触发异常标记"
    assert "情绪复盘" in review.as_brief()

    # ⑦ 同步：事件 payload 里带着同一条 PAD
    events = brain.event_store.scan(include_self=True)
    assert events, "高显著性写入必须产生同步事件"
    synced = events[-1]["payload"]["neuron"]
    assert (synced["pad_p"], synced["pad_a"], synced["pad_d"]) == pytest.approx((-0.7, 0.8, 0.1))

    # ⑧ 可视化：面板数据源能取到同一条轨迹
    series = pad_series(brain.storage.timeseries, agent_id="agent-a")
    assert series and series[0]["a"] == pytest.approx(0.8)
    summary = pad_summary(brain.storage.timeseries, agent_id="agent-a")
    assert summary["samples"] == 1
    assert summary["peak"]["a"] == pytest.approx(0.8)


def test_pad_chain_degrades_to_rule_without_llm(brain_factory):
    """没有 LLM 时，规则端必须补上 PAD —— 链路不能因为模型缺失就断。"""
    brain = brain_factory(llm_payload=None)
    result = brain.hippocampus.remember("这个方案太糟糕了，我很生气！", agent_id="agent-b")
    assert result.neuron.pad.p < 0

    track = brain.storage.timeseries.fetch("pad_track", entity_id=result.neuron.uuid7_id)
    assert track[0]["source"] == "rule", "轨迹必须标注来源，便于 eval 区分质量"


def test_calm_period_affects_recall_but_not_stored_pad(brain_factory):
    """情绪边界只作用于检索/注入，不改写记忆自身的 PAD（否则历史会被"重新染色"）。"""
    brain = brain_factory(llm_payload=PAYLOAD)
    result = brain.hippocampus.remember("周五发版又崩了，Alice 很生气！", agent_id="agent-a")

    assert brain.amygdala.recall_weight("agent-a") < 1.0
    stored = brain.storage.relational.get("neuron", result.neuron.uuid7_id)
    assert stored is not None and stored["pad_p"] == pytest.approx(-0.7)
