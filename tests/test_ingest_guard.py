"""一进多出铁律·统一入口判定 + WorkBuddy 侧前置拆分 bundle 契约（2026-11-12）。

与「前置 PAD 注入」同款：对话大模型在 WorkBuddy 侧按信息密度把一条输入拆成
一组，随 save_memory 的 bundle 入参提交；后端跳过 extract_multi 直接落组。
本文件验证：
  1. ingest_guard.classify 四条判定优先级；
  2. 前置 bundle 经 MCP/stdio/RPC/批量导入四个入口都直接落组、不跑拆分模型；
  3. 两个历史漏网口子（worker ingest、批量导入）已堵。
"""
from __future__ import annotations

import pytest

from brain.ingest_guard import classify, is_raw_chat, is_structured
from brain.router_extractor import RouterExtractor
from engine.local_llm import DisabledLLM


def _bundle_items(n: int = 2) -> list[dict]:
    out = []
    for i in range(n):
        out.append({
            "memory_type": "fact" if i % 2 == 0 else "emotion",
            "content": f"前置拆出的第 {i+1} 条记忆",
            "pad": {"p": 0.3, "a": 0.2, "d": 0.1},
            "confidence": 0.8,
        })
    # emotion 需要 scenario 依附，否则被 _bundle_from_payload 丢弃；补一条 scenario
    if n > 1:
        out.insert(0, {"memory_type": "scenario", "content": "用户与阿鱼的一段互动",
                       "pad": {"p": 0.4, "a": 0.3, "d": 0.1}, "confidence": 0.6})
    return out


# ── classify 四分支 ──────────────────────────────────────────────────────
def test_classify_bundle_first() -> None:
    """优先级1：WorkBuddy 前置 bundle 存在 → 直接落组，跳过后端拆分。"""
    plan = classify(bundle={"items": _bundle_items(1)}, cognition=None, content="任意")
    assert plan.mode == "bundle"
    assert plan.bundle and plan.bundle["items"]


def test_classify_structured_single() -> None:
    """优先级2：单条结构化认知契约（已判密度=1）→ 放行。"""
    plan = classify(cognition={"memory_type": "fact", "pad": {"p": 0.2, "a": 0.1, "d": 0.1},
                               "topic": "f844c49c0e58"}, content="用户换 Mac 了")
    assert plan.mode == "single"
    assert plan.cognition["memory_type"] == "fact"


def test_classify_raw_chat_forces_extract() -> None:
    """原始对话即便带单条 cognition 也强制拆分（堵住 OneBot 预构造单条漏拆）。"""
    plan = classify(cognition={"memory_type": "emotion", "pad": {"p": 0.8, "a": 0.5, "d": 0.2},
                               "category": "qq_chat"}, content="阿鱼又喊我想我了")
    assert plan.mode == "extract"
    assert is_raw_chat({"category": "qq_chat"}) is True
    assert is_raw_chat({"source": "qq_bot"}) is True


def test_classify_bare_text_extract() -> None:
    """裸文本无 bundle/cognition → 后端兜底一进多出。"""
    plan = classify(content="今天天气不错")
    assert plan.mode == "extract"


def test_classify_bundle_beats_raw_chat() -> None:
    """原始对话 category 但已带前置 bundle → 尊重 bundle，不重复拆。"""
    plan = classify(bundle={"items": _bundle_items(1)},
                    cognition={"category": "qq_chat"}, content="...")
    assert plan.mode == "bundle"


def test_is_structured() -> None:
    assert is_structured({"pad": {"p": 0.1, "a": 0, "d": 0}}) is True
    assert is_structured({"layer": "persona"}) is True
    assert is_structured({"confidence": 0.9}) is False
    assert is_structured(None) is False


# ── 前置 bundle 契约：白名单校验后直接落组 ─────────────────────────────
def test_bundle_from_payload_public_contract(brain_config) -> None:
    """公开契约 bundle_from_payload 把 WorkBuddy 前置包归一为 ExtractionBundle。"""
    ex = RouterExtractor(DisabledLLM(), brain_config)
    b = ex.bundle_from_payload({"complexity": "light", "items": _bundle_items(3)})
    assert b.items, "前置 bundle 至少落一组"
    assert b.source == "llm"
    assert all(getattr(i.pad, ax, 0) != 0 for i in b.items for ax in ("p", "a", "d"))


def test_bundle_no_extract_model_call(brain_config, monkeypatch) -> None:
    """前置 bundle 路径绝不调用 extract_multi（零拆分开销的核心保证）。"""
    ex = RouterExtractor(DisabledLLM(), brain_config)

    def boom(*a, **kw):
        raise AssertionError("前置 bundle 不应触发 extract_multi")

    monkeypatch.setattr(ex, "extract_multi", boom)
    b = ex.bundle_from_payload({"items": _bundle_items(2)})
    assert b.items


# ── 漏网口子1：批量导入走铁律 ────────────────────────────────────────────
def test_import_memories_uses_bundle(brain_factory, monkeypatch) -> None:
    """批量导入（历史漏网裸 remember）现必须经 _ingest_one → remember_bundle。"""
    from brain.memory_service import MemoryService

    brain = brain_factory()

    def fake_entries(text, suffix):
        return ["阿鱼又喊我想我了迈，想你宝儿 啵啵"]

    monkeypatch.setattr("brain.memory_service.parse_memory_text", fake_entries)
    calls: list[str] = []
    original = brain.hippocampus.remember_bundle

    def spy(bundle, **kw):
        calls.append(getattr(bundle, "complexity", ""))
        return original(bundle, **kw)

    monkeypatch.setattr(brain.hippocampus, "remember_bundle", spy)
    # brain_factory 返回 SimpleNamespace（无 .app），MemoryService 只用到 app.storage / app.hippocampus
    svc = MemoryService(brain, brain.config, write_gate=None)
    res = svc.import_memories("任意", fast=True)
    assert calls, "批量导入必须走 remember_bundle（一进多出），不再裸写单条"
    assert res.get("parsed") == 1
