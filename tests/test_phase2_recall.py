"""阶段 2 验收：召回流（六路 → RRF → 精排 → 分层配额 → 收敛注入）。

重点验证 PAD 显著性（w4）真的参与排序，且归档不再被召回。
"""
from __future__ import annotations

import pytest

def _payload(*, pad: dict, confidence: float, mt: str = "scenario") -> dict:
    return {
        "memory_type": mt,
        "entities": [],
        "relations": [],
        "hyperedge": None,
        "pad": pad,
        "confidence": confidence,
    }


def test_recall_returns_pad_annotated_hits(brain_factory):
    brain = brain_factory(
        llm_payload=_payload(pad={"p": -0.7, "a": 0.9, "d": 0.1}, confidence=0.9),
        with_embedder=True,
    )
    written = brain.hippocampus.remember("周五发版现场冲突", agent_id="agent-a")

    hits = brain.prefrontal.recall("发版冲突", agent_id="agent-a", limit=5)
    assert hits, "召回不能为空"
    top = hits[0]
    assert top.neuron.uuid7_id == written.neuron.uuid7_id
    assert top.neuron.pad.p == pytest.approx(written.neuron.pad.p)
    assert "dense" in top.channels or "fts" in top.channels


def test_arousal_wins_when_everything_else_equal(brain_factory):
    """同一时刻、同置信度下，高唤醒记忆必须排前（w4·PAD 显著性）。"""
    brain = brain_factory(
        llm_payload=_payload(pad={"p": -0.6, "a": 0.95, "d": 0.1}, confidence=0.8)
    )
    hot = brain.hippocampus.remember("发版当场炸了，全员加班到凌晨", agent_id="agent-a")

    calm_brain = brain_factory(
        llm_payload=_payload(pad={"p": -0.6, "a": 0.05, "d": 0.1}, confidence=0.8)
    )
    cold = calm_brain.hippocampus.remember("发版流程有个小笔误需要记录在案", agent_id="agent-a")

    hits = brain.prefrontal.recall("发版", agent_id="agent-a", limit=5)
    order = [hit.neuron.uuid7_id for hit in hits]
    assert hot.neuron.uuid7_id in order and cold.neuron.uuid7_id in order
    assert order.index(hot.neuron.uuid7_id) < order.index(cold.neuron.uuid7_id)


def test_archived_memory_is_not_recalled(brain_factory):
    brain = brain_factory(llm_payload=_payload(pad={"p": 0.2, "a": 0.1, "d": 0.2}, confidence=0.7))
    written = brain.hippocampus.remember("临时记录：发布窗口改到周四", agent_id="agent-a")
    assert brain.prefrontal.recall("发布窗口", agent_id="agent-a")

    brain.storage.relational.set_tier("neuron", written.neuron.uuid7_id, "archive")
    assert brain.prefrontal.recall("发布窗口", agent_id="agent-a") == [], "归档记录不得再被召回"


def test_calm_period_suppresses_recall_weight(brain_factory):
    brain = brain_factory(
        llm_payload=_payload(pad={"p": -0.9, "a": 0.95, "d": -0.2}, confidence=0.9)
    )
    brain.hippocampus.remember("和用户大吵一架，情绪很糟", agent_id="agent-a")
    weight = brain.amygdala.recall_weight("agent-a")
    assert weight == pytest.approx(brain.config.amygdala.calm_recall_penalty)


def test_injection_text_carries_tone_and_pad(brain_factory):
    brain = brain_factory(llm_payload=_payload(pad={"p": -0.5, "a": 0.7, "d": 0.0}, confidence=0.8))
    brain.hippocampus.remember("用户上次对发版流程很不满", agent_id="agent-a")
    hits = brain.prefrontal.recall("发版", agent_id="agent-a", limit=3)

    text = brain.prefrontal.inject(hits, agent_id="agent-a")
    assert "【记忆注入】" in text
    assert "P-" in text and "A+" in text, "注入文本必须带 PAD 标注"
    assert "收敛措辞" in text, "冷静期必须影响注入语气"


def test_timeline_query_filters_by_time(brain_factory):
    brain = brain_factory(llm_payload=_payload(pad={"p": 0.0, "a": 0.0, "d": 0.0}, confidence=0.7))
    first = brain.hippocampus.remember("上个月做了架构评审", agent_id="agent-a", now_ms=1_000_000)
    second = brain.hippocampus.remember("今天确认了同步方案细节", agent_id="agent-a", now_ms=2_000_000)

    recent = brain.prefrontal.timeline_query(since_ms=1_500_000)
    ids = [neuron.uuid7_id for neuron in recent]
    assert second.neuron.uuid7_id in ids
    assert first.neuron.uuid7_id not in ids
