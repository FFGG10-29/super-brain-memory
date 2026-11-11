"""tests.test_meta_e2e4 —— E2 动态置信度 / knowledge_gap / E4 视角加权验收。

只覆盖 E2 计算层与 E4 注入视角（brain/prefrontal.py 契约）：
- confidence：通道多 + 新鲜 > 单通道 + 陈旧；未消解 supersede 痕迹减分；
- last_knowledge_gap：空结果与全部低置信时 gap=True；
- E4：PAD 正/负时命中顺序按 valence 符号翻转，score 高者组内仍靠前；
- pad_tracker=None / perspective_weight=0 / 冷启动时行为与既有召回一致。
"""
from __future__ import annotations

import time
from typing import Any

import pytest

from brain.amygdala import Amygdala
from brain.current_pad_tracker import CurrentPadTracker
from brain.prefrontal import Prefrontal, RecallHit
from brain.synaptic_plasticity import SynapticPlasticity
from models.neuron import Neuron, PAD
from engine.reranker import IdentityReranker

AGENT = "agent-a"
_DAY_MS = 86_400_000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _write_neuron(
    brain,
    *,
    content: str,
    memory_type: str = "fact",
    pad_p: float = 0.0,
    age_days: float = 0.0,
    last_recall_days: float | None = None,
) -> Neuron:
    """直写主库（绕过 hippocampus，避免抽取器噪声，便于精确控制通道与新鲜度）。"""
    now = _now_ms()
    age_ms = int(age_days * _DAY_MS)
    neuron = Neuron(
        content=content,
        memory_type=memory_type,  # type: ignore[arg-type]
        agent_id=AGENT,
        pad=PAD(p=pad_p),
        created_at_ms=now - age_ms,
        valid_from_ms=now - age_ms,
        last_recall_ms=(now - int(last_recall_days * _DAY_MS)
                        if last_recall_days is not None else 0),
    )
    brain.storage.relational.upsert("neuron", neuron.to_record())
    return neuron


def _deactivate(brain, neuron: Neuron) -> None:
    brain.storage.relational.deactivate(
        "neuron", neuron.uuid7_id, valid_to_ms=_now_ms(), tx_to=brain.clock.tick()
    )


def _recall_hits(brain_or_pre, query: str, limit: int = 10) -> list[RecallHit]:
    return brain_or_pre.recall(query, agent_id=AGENT, limit=limit)


def _hit_by_content(hits: list[RecallHit], needle: str) -> RecallHit:
    matched = [hit for hit in hits if needle in hit.neuron.content]
    assert len(matched) == 1, f"期望唯一命中 {needle!r}，实际 {len(matched)}"
    return matched[0]


class _SupersedeAwareGraph:
    """包装真图库，补一个 duck-typed 的 SUPERSEDES 边读取口（生产接口暂未暴露）。"""

    def __init__(self, inner: Any, edges: dict[str, list[str]]) -> None:
        self._inner = inner
        self._edges = edges

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def supersede_edges_of(self, neuron_id: str) -> list[str]:
        return list(self._edges.get(neuron_id, []))


def _prefrontal(brain, edges: dict[str, list[str]] | None = None, **kwargs: Any) -> Prefrontal:
    storage, config = brain.storage, brain.config
    graph = _SupersedeAwareGraph(storage.graph, edges or {}) if edges is not None else storage.graph
    return Prefrontal(
        relational=storage.relational,
        vector=storage.vector,
        graph=graph,
        kv=storage.kv,
        embedder=None,
        reranker=IdentityReranker(),
        plasticity=SynapticPlasticity(config),
        amygdala=Amygdala(storage.kv, config),
        timeseries=storage.timeseries,
        config=config,
        **kwargs,
    )


# ── E2：confidence ────────────────────────────────────────────────────────

def test_recall_hit_confidence_defaults_to_uncalculated() -> None:
    neuron = Neuron(content="x")
    hit = RecallHit(neuron=neuron, score=1.0, channels=("fts",))
    assert hit.confidence == -1.0


def test_confidence_rewards_channel_diversity_and_freshness(brain_factory) -> None:
    brain = brain_factory()
    # 多通道：profile 类型必进 profile 通道，正文含查询词进 fts，timeline 全量 → 3 通道
    _write_neuron(brain, content="rust analyzer config", memory_type="profile")
    # 单通道 + 陈旧：正文不含查询词（只有 timeline），400 天前写入
    _write_neuron(brain, content="qqq unrelated filler", age_days=400.0)

    hits = _recall_hits(brain.prefrontal, "rust")
    assert hits, "召回不应为空"
    rich = _hit_by_content(hits, "rust analyzer")
    stale = _hit_by_content(hits, "qqq unrelated")

    assert 0.0 <= rich.confidence <= 1.0
    assert 0.0 <= stale.confidence <= 1.0
    assert rich.confidence > stale.confidence


def test_confidence_penalized_by_unresolved_supersede(brain_factory) -> None:
    brain = brain_factory()
    clean = _write_neuron(brain, content="profile beta clean", memory_type="profile", age_days=1.0)
    conflicted = _write_neuron(
        brain, content="profile beta conflicted", memory_type="profile", age_days=1.0
    )
    # 冲突对端仍在召回池（is_active=True）→ 未消解 → 减分
    rival = _write_neuron(brain, content="profile beta rival", memory_type="profile", age_days=1.0)
    pre = _prefrontal(brain, edges={conflicted.uuid7_id: [rival.uuid7_id]})

    hits = pre.recall("beta", agent_id=AGENT, limit=10)
    assert hits
    clean_hit = _hit_by_content(hits, "clean")
    conflicted_hit = _hit_by_content(hits, "conflicted")
    assert clean_hit.confidence > conflicted_hit.confidence
    # 默认权重 0.3/0.3/0.4：无冲突且满通道满新鲜 → 接近满置信；有冲突 → 掉 0.4 档
    assert clean_hit.confidence >= 0.95
    assert conflicted_hit.confidence <= 0.6
    assert clean.uuid7_id and rival.uuid7_id  # 写入即生效，防静默失败


def test_confidence_ignores_resolved_supersede(brain_factory) -> None:
    brain = brain_factory()
    clean = _write_neuron(brain, content="gamma clean", memory_type="profile", age_days=1.0)
    resolved = _write_neuron(brain, content="gamma resolved", memory_type="profile", age_days=1.0)
    old = _write_neuron(brain, content="gamma old version", age_days=1.0)
    _deactivate(brain, old)  # 对端已封存 = 已消解
    pre = _prefrontal(brain, edges={resolved.uuid7_id: [old.uuid7_id]})

    hits = pre.recall("gamma", agent_id=AGENT, limit=10)
    clean_hit = _hit_by_content(hits, "clean")
    resolved_hit = _hit_by_content(hits, "resolved")
    assert resolved_hit.confidence == pytest.approx(clean_hit.confidence, abs=1e-6)


# ── E2：knowledge_gap ─────────────────────────────────────────────────────

def test_knowledge_gap_true_on_empty_result(brain_factory) -> None:
    brain = brain_factory()  # 零记忆 → timeline 通道为空 → rrf 空 → 空结果
    hits = _recall_hits(brain.prefrontal, "anything")
    assert hits == []
    gap = brain.prefrontal.last_knowledge_gap
    assert gap["gap"] is True
    assert gap["query"] == "anything"
    assert gap["note"]


def test_knowledge_gap_true_on_all_low_confidence(brain_factory) -> None:
    brain = brain_factory()
    target = _write_neuron(brain, content="zzz lone stale", age_days=400.0)
    rival = _write_neuron(brain, content="rival shadow", age_days=400.0)
    # 互为未消解冲突（对端都 active）：target ≈ 0.3·1 + 0.3·0.03 ≈ 0.31 < 0.35
    pre = _prefrontal(brain, edges={
        target.uuid7_id: [rival.uuid7_id],
        rival.uuid7_id: [target.uuid7_id],
    })
    hits = pre.recall("zzz", agent_id=AGENT, limit=10)
    assert hits
    assert all(
        hit.confidence < Prefrontal.LOW_CONFIDENCE_THRESHOLD for hit in hits
    ), [hit.confidence for hit in hits]

    gap = pre.last_knowledge_gap
    assert gap["gap"] is True
    assert gap["query"] == "zzz"
    assert gap["note"]


def test_knowledge_gap_false_on_healthy_hits(brain_factory) -> None:
    brain = brain_factory()
    _write_neuron(brain, content="healthy omega signal", memory_type="profile")
    pre = brain.prefrontal
    hits = _recall_hits(pre, "omega")
    assert hits
    assert all(hit.confidence >= Prefrontal.LOW_CONFIDENCE_THRESHOLD for hit in hits)
    gap = pre.last_knowledge_gap
    assert gap["gap"] is False
    assert gap["note"] == ""


# ── E4：注入视角加权 ──────────────────────────────────────────────────────

def _seed_valence_quartet(brain) -> None:
    """四条记忆：预期 score 序（无视角时）pos_high > neg_high > pos_low > neg_low。"""
    _write_neuron(brain, content="meeting notes alpha", pad_p=0.9,
                  age_days=10.0, last_recall_days=0.0)
    _write_neuron(brain, content="meeting notes beta", pad_p=-0.9,
                  age_days=20.0, last_recall_days=10.0)
    _write_neuron(brain, content="meeting notes gamma", pad_p=0.5,
                  age_days=30.0, last_recall_days=20.0)
    _write_neuron(brain, content="meeting notes delta", pad_p=-0.3,
                  age_days=40.0, last_recall_days=30.0)


def _tracker(brain, text: str) -> CurrentPadTracker:
    tracker = CurrentPadTracker(brain.config)
    for _ in range(int(tracker.gating.warmup_turns)):
        tracker.observe(text)
    assert tracker.weight_multiplier() == 1.0, f"tracker 未过门控：P={tracker.current().p}"
    return tracker


def test_perspective_reorders_hits_by_pad_sign(brain_factory) -> None:
    brain = brain_factory()
    _seed_valence_quartet(brain)
    pre = brain.prefrontal

    baseline = _recall_hits(pre, "meeting")
    baseline_ids = [hit.neuron.content for hit in baseline]
    assert baseline_ids == [
        "meeting notes alpha",   # pos_high
        "meeting notes beta",    # neg_high
        "meeting notes gamma",   # pos_low
        "meeting notes delta",   # neg_low
    ]

    # P>0：正 valence 前置（组翻转），组内仍按 score 降序
    pre.pad_tracker = _tracker(brain, "太好了，开心，满意，顺利成功")
    positive = _recall_hits(pre, "meeting")
    assert [hit.neuron.content for hit in positive] == [
        "meeting notes alpha",
        "meeting notes gamma",
        "meeting notes beta",
        "meeting notes delta",
    ]

    # P<0：负 valence 前置（轻度负文本不触发 suppress_negative 门控）
    pre.pad_tracker = _tracker(brain, "难过")
    negative = _recall_hits(pre, "meeting")
    assert [hit.neuron.content for hit in negative] == [
        "meeting notes beta",
        "meeting notes delta",
        "meeting notes alpha",
        "meeting notes gamma",
    ]

    # 只调顺序，不改变内容本身
    assert sorted(hit.neuron.uuid7_id for hit in negative) == \
        sorted(hit.neuron.uuid7_id for hit in baseline)


def test_perspective_disabled_without_tracker_or_weight(brain_factory) -> None:
    brain = brain_factory()
    _seed_valence_quartet(brain)
    pre = brain.prefrontal

    baseline_ids = [hit.neuron.content for hit in _recall_hits(pre, "meeting")]

    # pad_tracker=None：与既有行为一致
    assert pre.pad_tracker is None
    assert [hit.neuron.content for hit in _recall_hits(pre, "meeting")] == baseline_ids

    # perspective_weight=0：显式关闭
    pre_closed = _prefrontal(brain, perspective_weight=0.0)
    pre_closed.pad_tracker = _tracker(brain, "太好了，开心")
    assert [hit.neuron.content for hit in _recall_hits(pre_closed, "meeting")] == baseline_ids

    # 冷启动（weight_multiplier()==0）：不生效
    cold = CurrentPadTracker(brain.config)
    assert cold.weight_multiplier() == 0.0
    pre.pad_tracker = cold
    assert [hit.neuron.content for hit in _recall_hits(pre, "meeting")] == baseline_ids
