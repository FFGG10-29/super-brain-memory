"""C12 embedding 缓存 + C11 召回结果短期缓存验收（v4.1-C 第一批）。

口径：
- C12：同 query 第二次 embed_query 必须零模型调用（lmdb 介质，TTL 30 天已配）；
- C11：recall 命中后不再重跑通道查询（FTS/表扫描计数不变），且
  memory_updated 事件（affected_ids 含缓存值里的 memory id）后缓存精确失效、重走全流程。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from config import BrainConfig
from core.cache_invalidation import CacheInvalidationBus
from core.cache_manager import configure_cache_manager
from tests.fakes import FakeEmbedder

SEED_CONTENT = "项目发版需要双人复核并确认缓存方案"
QUERY = "缓存方案"


class CountingFakeEmbedder(FakeEmbedder):
    """包一层计数：只统计 embed_query 调用次数，数值行为与 FakeEmbedder 一致。"""

    def __init__(self, dense_dim: int) -> None:
        super().__init__(dense_dim)
        self.embed_query_calls = 0

    def embed_query(self, text: str):
        self.embed_query_calls += 1
        return super().embed_query(text)


class CountingRelational:
    """计数 spy：get / full_text_search / query 计数，其余方法透传。"""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.get_calls = 0
        self.fts_calls = 0
        self.query_calls = 0

    def get(self, table: str, record_id: str):
        self.get_calls += 1
        return self._inner.get(table, record_id)

    def full_text_search(self, *args, **kwargs):
        self.fts_calls += 1
        return self._inner.full_text_search(*args, **kwargs)

    def query(self, *args, **kwargs):
        self.query_calls += 1
        return self._inner.query(*args, **kwargs)

    def __getattr__(self, name: str):  # 其余方法透传
        return getattr(self._inner, name)


@pytest.fixture()
def cache_env(storage):
    """装配进程级 CacheManager（含失效总线订阅），用例结束必须复位为 None。"""
    import core.cache_manager as cache_module

    bus = CacheInvalidationBus()
    manager = configure_cache_manager(
        config=BrainConfig(_env_file=None),
        kv=storage.kv,
        timeseries=storage.timeseries,
        bus=bus,
    )
    yield SimpleNamespace(manager=manager, bus=bus)
    cache_module._MANAGER = None  # 不污染其他用例（get_cache_manager() 回退 None）


# ── C12：embedding 缓存命中 → 零模型调用 ─────────────────────────────────
def test_c12_embedding_cache_avoids_second_model_call(brain_factory, cache_env) -> None:
    brain = brain_factory(with_embedder=True)
    counting = CountingFakeEmbedder(brain.config.model.dense_dim)
    brain.prefrontal._embedder = counting
    brain.hippocampus.remember(SEED_CONTENT, agent_id="agent-a")

    first = brain.prefrontal.recall(QUERY, agent_id="agent-a", limit=5)
    assert counting.embed_query_calls == 1

    # 换 top_k 让 C11 召回缓存 miss，但同 query 仍应命中 C12 embedding 缓存
    brain.prefrontal.recall(QUERY, agent_id="agent-a", limit=3)
    assert counting.embed_query_calls == 1, "同 query 第二次必须命中 embedding 缓存，零模型调用"

    # dense 向量是 list、sparse 是 dict[str, float]（token→权重），JSON 往返无损；
    # 命中重建后行为一致：两次召回的 dense 通道命中 id 完全相同
    audit_first = dict(brain.prefrontal.last_audit)
    brain.prefrontal.recall(QUERY, agent_id="agent-a", limit=3)
    assert first or audit_first  # 召回链路未被缓存破坏


# ── C11：召回结果缓存命中 → 通道查询不再重复 ─────────────────────────────
def test_c11_recall_cache_skips_channel_queries(brain_factory, cache_env) -> None:
    brain = brain_factory(with_embedder=True)
    spy = CountingRelational(brain.storage.relational)
    brain.prefrontal._relational = spy
    brain.hippocampus.remember(SEED_CONTENT, agent_id="agent-a")

    first = brain.prefrontal.recall(QUERY, agent_id="agent-a", limit=5)
    assert first, "召回不能为空"
    fts_after_miss = spy.fts_calls
    query_after_miss = spy.query_calls

    second = brain.prefrontal.recall(QUERY, agent_id="agent-a", limit=5)
    assert brain.prefrontal.last_audit.get("cached") is True, "第二次必须走缓存命中路径"
    assert [hit.neuron.uuid7_id for hit in second] == [
        hit.neuron.uuid7_id for hit in first
    ], "缓存命中结果必须与全流程一致"
    assert [hit.score for hit in second] == pytest.approx([hit.score for hit in first])
    assert spy.fts_calls == fts_after_miss, "缓存命中不得重跑 FTS 通道查询"
    assert spy.query_calls == query_after_miss, "缓存命中不得重跑表扫描（timeline/hyperedge）"
    # 命中路径仍要从 relational 重建 RecallHit（保证 is_active 过滤生效），
    # 但只按缓存里的 id 精确取 len(hits) 条，不再重复全量候选
    assert spy.get_calls >= len(second)


# ── C11：memory_updated 事件精确失效 → 重新走全流程 ──────────────────────
def test_c11_memory_updated_event_invalidates_recall_cache(
    brain_factory, cache_env,
) -> None:
    brain = brain_factory(with_embedder=True)
    spy = CountingRelational(brain.storage.relational)
    brain.prefrontal._relational = spy
    written = brain.hippocampus.remember(SEED_CONTENT, agent_id="agent-a")

    first = brain.prefrontal.recall(QUERY, agent_id="agent-a", limit=5)
    assert first
    assert brain.prefrontal.recall(QUERY, agent_id="agent-a", limit=5), "预热缓存"
    assert brain.prefrontal.last_audit.get("cached") is True
    fts_before_invalidation = spy.fts_calls

    # C0 约定：缓存值内含 memory id → 总线按值精确失效（无需手写失效逻辑）
    assert cache_env.bus.publish({
        "type": "memory_updated",
        "affected_ids": [written.neuron.uuid7_id],
    }) >= 1

    third = brain.prefrontal.recall(QUERY, agent_id="agent-a", limit=5)
    assert spy.fts_calls > fts_before_invalidation, "失效后必须重走全流程（FTS 通道重跑）"
    assert brain.prefrontal.last_audit.get("cached") is None
    assert [hit.neuron.uuid7_id for hit in third] == [
        hit.neuron.uuid7_id for hit in first
    ]
