"""一进多出铁律 + 三梯队分层拆解验收（2026-11-12 用户裁决）。

铁律：任何途径写记忆必须先过拆分判定（按信息密度决定拆几类），不符合的不准写入，
原文留项目档案待重试。拆解数量由信息密度决定，不是所有输入都拆成 5 类。
"""
from __future__ import annotations

import pytest

from brain.router_extractor import RouterExtractor
from engine.local_llm import DisabledLLM
from models.neuron import PAD


@pytest.fixture()
def extractor(brain_config):
    return RouterExtractor(DisabledLLM(), brain_config)


# ── 三梯队分流 ──────────────────────────────────────────────────────────
def test_tier1_transactional_no_cognitive(extractor) -> None:
    """第一梯队：代码/配置走事务性，不产出认知 item、不建超边。"""
    b = extractor.extract_multi("帮我把这段 Python 改成异步的：\ndef foo(): return 1\nimport asyncio")
    assert b.transactional is True
    assert b.items == ()
    assert b.event_label is None
    assert b.summary, "事务性要留一句摘要"


def test_tier1_greeting_is_ephemeral(extractor) -> None:
    """第一梯队：寒暄/系统指令不进六引擎。"""
    for text in ("早上好", "在吗", "清空上下文"):
        b = extractor.extract_multi(text)
        assert b.ephemeral is True and b.items == (), f"{text} 应判为寒暄丢弃"


def test_tier2_knowledge_single_fact(extractor) -> None:
    """第二梯队·客观知识：只存 1 条 fact，不建超边。"""
    b = extractor.extract_multi("FastAPI 怎么开启 CORS？")
    assert b.complexity == "light"
    assert [i.memory_type for i in b.items] == ["fact"]
    assert b.event_label is None


def test_tier2_one_way_emotion(extractor) -> None:
    """第二梯队·单向倾诉：scenario+emotion，单向不建超边。"""
    b = extractor.extract_multi("气死我了，今天那个客户又改需求")
    types = {i.memory_type for i in b.items}
    assert {"scenario", "emotion"} <= types
    assert b.event_label is None, "单向倾诉（系统未参与交互）不建超边"


def test_tier2_fact_plus_profile(extractor) -> None:
    """第二梯队·客观信息+偏好：fact+profile 两个抽象层各留一条。"""
    b = extractor.extract_multi("我最近换 Mac 了，这台 M3 芯片的屏幕看着真舒服")
    types = {i.memory_type for i in b.items}
    assert "profile" in types, "偏好应抽成 profile"
    assert b.event_label is None, "无双向交互/情绪波动不建超边"
    assert b.complexity in ("light", "heavy")


def test_tier3_heavy_intimacy_full_split(extractor) -> None:
    """第三梯队·暧昧交互：scenario+emotion+fact/profile + 超边（能撑起叙事）。"""
    b = extractor.extract_multi("阿鱼又喊我想我了迈，我说是的呀你在想我吗，想你宝儿 啵啵")
    assert b.complexity == "heavy"
    types = {i.memory_type for i in b.items}
    assert {"scenario", "emotion"} <= types, "重度必须有情景骨架+情绪"
    assert b.event_label, "双向交互必须建超边"
    assert len(b.event_members) >= 2, "超边至少两个主体"
    assert any(i.requires_hyperedge for i in b.items), "至少一个交互 scenario 标 requires_hyperedge"


# ── 边界规则（互斥/无根情绪/画像置信度）────────────────────────────────
def test_profile_fact_mutual_exclusion(extractor) -> None:
    """互斥：偏好类同义 fact+profile 只留 profile。"""
    b = extractor._bundle_from_payload({"items": [
        {"memory_type": "fact", "content": "阿鱼喜欢啵啵", "pad": {"p": 0.8, "a": 0.5, "d": 0.2}},
        {"memory_type": "profile", "content": "阿鱼喜欢啵啵", "pad": {"p": 0.8, "a": 0.5, "d": 0.2}},
    ]})
    assert [i.memory_type for i in b.items] == ["profile"]


def test_rootless_emotion_dropped(extractor) -> None:
    """无根情绪：没有 scenario 依附的 emotion 直接丢弃。"""
    b = extractor._bundle_from_payload({"items": [
        {"memory_type": "emotion", "content": "莫名开心", "pad": {"p": 0.8, "a": 0.5, "d": 0.2}},
    ]})
    assert all(i.memory_type != "emotion" for i in b.items)


def test_profile_confidence_capped(extractor) -> None:
    """画像单次置信度封顶 0.7，多次出现才由后处理升维固化。"""
    b = extractor._bundle_from_payload({"items": [
        {"memory_type": "profile", "content": "用户偏好X", "pad": {"p": 0.3, "a": 0.1, "d": 0.2},
         "confidence": 0.98},
    ]})
    assert b.items[0].confidence <= 0.7


def test_hyperedge_requires_two_members(extractor) -> None:
    """超边补全失败（<2 主体 / 无交互 scenario）→ 降级为散落记忆。"""
    b = extractor._bundle_from_payload({
        "items": [{"memory_type": "scenario", "content": "我发了条消息", "pad": {"p": 0.1, "a": 0.1, "d": 0.1},
                   "requires_hyperedge": True}],
        "event": {"event_summary": "单节点", "members": ["我"]},
    })
    assert b.event_label is None


def test_zero_pad_fallback(extractor) -> None:
    """每条记忆 PAD 必须非零（全 0 兜底中性基线）。"""
    b = extractor._bundle_from_payload({"items": [
        {"memory_type": "fact", "content": "X", "pad": {"p": 0, "a": 0, "d": 0}},
    ]})
    assert any(getattr(b.items[0].pad, ax, 0) != 0 for ax in ("p", "a", "d"))


# ── 铁律：所有写入通道不符合一进多出 → 拒收 + 留项目档案 ─────────────────
def test_iron_rule_entry_forces_bundle(brain_factory, monkeypatch) -> None:
    """铁律执行在入口层：MCP save_memory 裸文本必须走 extract_multi→bundle，不是单条 remember。"""
    from gateway.mcp_server import MCPToolkit

    brain = brain_factory()
    # 用 spy 替换 remember_bundle，确认入口确实走 bundle 通道
    calls: list[str] = []
    original = brain.hippocampus.remember_bundle

    def spy(bundle, **kw):
        calls.append(getattr(bundle, "complexity", ""))
        return original(bundle, **kw)

    monkeypatch.setattr(brain.hippocampus, "remember_bundle", spy)
    from tests.test_meta_e2e6 import FakePrefrontal, FakeRegistry, FakeRelational

    tk = MCPToolkit(registry=FakeRegistry(), hippocampus=brain.hippocampus,
                    prefrontal=FakePrefrontal(), relational=brain.storage.relational,
                    config=brain.config)
    res = tk.call("save_memory", {"content": "阿鱼又喊我想我了迈，想你宝儿 啵啵"},
                  headers={"Authorization": "Bearer fake"})
    assert calls, "裸文本写入必须触发 remember_bundle（一进多出）"
    assert res.get("applied") is True
    assert res.get("written", 0) >= 1


def test_iron_rule_allows_bundle_members(brain_factory) -> None:
    """enforce=True：来自合规 bundle 的逐条落库（_from_bundle=True）放行。"""
    brain = brain_factory()
    brain.config.one_to_many.enforce = True
    from brain.router_extractor import ExtractionBundle, ExtractionItem
    from models.neuron import PAD

    bundle = ExtractionBundle(items=(
        ExtractionItem(memory_type="scenario", content="阿鱼喊我", pad=PAD(p=0.2, a=0.1, d=0.1), confidence=0.5),
        ExtractionItem(memory_type="emotion", content="暧昧感", pad=PAD(p=0.8, a=0.6, d=0.2), confidence=0.45),
    ))
    res = brain.hippocampus.remember_bundle(bundle, agent_id="agent-a", namespace="topic:f844c49c0e58")
    assert res["applied"] >= 1
    assert res.get("handled") is True


def test_transactional_bundle_goes_overwrite_not_cognitive(brain_factory, monkeypatch) -> None:
    """事务性 bundle 走覆盖式，不产认知 item、不建超边。"""
    brain = brain_factory()
    brain.config.one_to_many.enforce = True
    from brain.router_extractor import ExtractionBundle

    overwrite_calls: list[str] = []
    monkeypatch.setattr(brain.hippocampus, "overwrite_remember",
                        lambda content, **kw: overwrite_calls.append(content))
    b = ExtractionBundle(items=(), transactional=True, summary="重构用户模块",
                         raw_content="def foo(): return 1", source="rule")
    res = brain.hippocampus.remember_bundle(b, agent_id="agent-a", namespace="topic:x")
    assert res.get("transactional") is True
    assert res.get("handled") is True
