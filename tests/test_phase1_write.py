"""阶段 1 验收：写入流。

- 重复写入零冗余（丘脑去重）
- 矛盾事实触发封存且图谱现 supersedes 边
- PAD 从写入起就落库（建模 + 存储环节）
"""
from __future__ import annotations

import pytest

from models.neuron import PAD, Neuron

LLM_PAYLOAD = {
    "memory_type": "emotion",
    "entities": [{"name": "发布流程", "type": "project"}],
    "relations": [{"subject": "发布流程", "predicate": "owner", "object": "Alice"}],
    "hyperedge": {"label": "发布冲突", "members": ["Alice", "发布流程"]},
    "pad": {"p": -0.7, "a": 0.8, "d": 0.1},
    "confidence": 0.9,
}


def test_llm_extraction_drives_write(brain_factory):
    brain = brain_factory(llm_payload=LLM_PAYLOAD, with_embedder=True)
    result = brain.hippocampus.remember("周五发版又崩了，Alice 很生气！", agent_id="agent-a")

    assert result.applied is True
    assert result.neuron.memory_type == "emotion"
    assert result.neuron.pad.a > 0.5, "PAD 唤醒度必须来自抽取结果"
    assert result.admission.accepted is True

    stored = brain.storage.relational.get("neuron", result.neuron.uuid7_id)
    assert stored is not None
    assert stored["pad_p"] == pytest.approx(result.neuron.pad.p)
    assert stored["pad_a"] == pytest.approx(result.neuron.pad.a)

    # 图谱：实体 + 关系 + 超边成员
    assert brain.storage.graph.count_edges("RELATES") == 1
    assert set(brain.storage.hypergraph.members(result.neuron.uuid7_id)) == {"Alice", "发布流程"}

    # 向量投影：embedder 存在 → 必须写入
    assert brain.storage.vector.count() == 1


def test_rule_fallback_still_produces_pad(brain_factory):
    """LLM 不可用时走规则抽取，但 PAD 不能缺失（全链路不变量）。"""
    brain = brain_factory(llm_payload=None)
    result = brain.hippocampus.remember("这个方案太糟糕了，我很生气！", agent_id="agent-a")

    assert result.applied is True
    assert result.neuron.pad.p < 0, "负面词典必须给出负愉悦度"
    assert result.neuron.pad.a > 0, "感叹号/负面词必须抬高唤醒度"
    assert any("embedder" in warning for warning in result.warnings)


def test_duplicate_write_is_rejected(brain_factory):
    brain = brain_factory(llm_payload=LLM_PAYLOAD)
    content = "周五发版又崩了，Alice 很生气！"
    first = brain.hippocampus.remember(content, agent_id="agent-a")
    second = brain.hippocampus.remember(content, agent_id="agent-a")

    assert first.applied is True
    assert second.applied is False
    assert second.admission.duplicate_of == first.neuron.uuid7_id
    assert brain.storage.relational.count("neuron") == 1, "重复写入必须零冗余"


def test_conflict_supersedes_and_creates_graph_edge(brain_factory):
    first_payload = {
        **LLM_PAYLOAD,
        "relations": [{"subject": "发布流程", "predicate": "owner", "object": "Alice"}],
        "confidence": 0.6,
    }
    brain = brain_factory(llm_payload=first_payload)
    old = brain.hippocampus.remember("发布流程的负责人是 Alice", agent_id="agent-a")
    assert old.applied is True

    # 换一个置信度更高的矛盾事实（同 subject+predicate，object 不同）
    brain2 = brain_factory(
        llm_payload={
            **LLM_PAYLOAD,
            "relations": [{"subject": "发布流程", "predicate": "owner", "object": "Bob"}],
            "confidence": 0.9,
        }
    )
    # 内容刻意与旧记录拉开距离，避免被 SimHash 近邻去重拦下（去重是另一条用例）
    new = brain2.hippocampus.remember("Bob 现在接管了发布流程的 owner 角色", agent_id="agent-a")

    assert new.applied is True
    assert old.neuron.uuid7_id in new.superseded

    sealed = brain.storage.relational.get("neuron", old.neuron.uuid7_id)
    assert sealed is not None and sealed["is_active"] is False, "旧记录必须封存而非删除"
    assert sealed["valid_to_ms"] is not None
    assert brain.storage.graph.count_edges("SUPERSEDES") == 1, "图谱必须留演进边"


def test_amygdala_affinity_accumulates_over_events(brain_factory):
    brain = brain_factory(llm_payload=LLM_PAYLOAD)
    brain.hippocampus.remember("周五发版又崩了，Alice 很生气！", agent_id="agent-a")
    brain.hippocampus.remember("又一次发布事故，太难了！", agent_id="agent-a")

    state = brain.amygdala.state("agent-a")
    assert state.affinity < 0, "连续负面事件必须拉低好感度"
    assert state.pad.p < 0

    # 高负唤醒触发冷静期 → 召回权重被压制
    assert brain.amygdala.recall_weight("agent-a") < 1.0


def test_pad_track_written_to_timeseries(brain_factory):
    brain = brain_factory(llm_payload=LLM_PAYLOAD)
    result = brain.hippocampus.remember("周五发版又崩了，Alice 很生气！", agent_id="agent-a")

    rows = brain.storage.timeseries.fetch("pad_track", entity_id=result.neuron.uuid7_id)
    assert len(rows) == 1
    assert rows[0]["p"] == pytest.approx(result.neuron.pad.p)
    assert rows[0]["source"] == "llm"


def test_neuron_record_roundtrip_preserves_pad():
    neuron = Neuron(content="hello", pad=PAD(p=-0.2, a=0.6, d=0.3), device_id="dev-test")
    restored = Neuron.from_record(neuron.to_record())
    assert restored.pad == neuron.pad
    assert restored.uuid7_id == neuron.uuid7_id
