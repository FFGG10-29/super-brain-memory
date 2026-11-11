"""C18 向量索引段缓存验收（装饰器 CachedVectorStore，零接口变更）。

口径：
- 同 (where, top_k, query) 二次检索零 delegate 调用，且结果与首次一致（同构）；
- upsert/delete 成功后缓存精确失效，重查重新走 delegate；
- cache_manager=None（或未注入）全直通；
- delegate 异常透传，且不污染缓存。
"""
from __future__ import annotations

import pytest

from config import BrainConfig
from core.cache_manager import CacheManager
from core.interfaces import (
    HealthStatus,
    RecallFilter,
    ScoredId,
    VectorRecord,
    VectorRepository,
)
from storage.vector_cache import CachedVectorStore

WHERE = RecallFilter()
QUERY = [0.1, 0.2, 0.3, 0.4]


class CountingVectorDB(VectorRepository):
    """带调用计数的假向量库：固定脚本结果，行为可预测。"""

    name = "vector:fake"

    def __init__(self, dense_dim: int = 4) -> None:
        self._dense_dim = dense_dim
        self.search_dense_calls = 0
        self.search_sparse_calls = 0
        self.search_text_calls = 0
        self.upsert_calls = 0
        self.delete_calls = 0
        self.count_calls = 0
        self.dense_result = [ScoredId(id="n1", score=0.9, channel="dense"),
                             ScoredId(id="n2", score=0.5, channel="dense")]
        self.sparse_result = [ScoredId(id="n3", score=0.7, channel="sparse")]
        self.text_result = [ScoredId(id="n4", score=1.0, channel="fts")]

    @property
    def dense_dim(self) -> int:
        return self._dense_dim

    def upsert(self, records) -> None:
        self.upsert_calls += 1

    def delete(self, ids) -> None:
        self.delete_calls += 1

    def search_dense(self, vector, *, limit, where=None):
        self.search_dense_calls += 1
        return list(self.dense_result)

    def search_sparse(self, weights, *, limit, where=None):
        self.search_sparse_calls += 1
        return list(self.sparse_result)

    def search_text(self, query, *, limit, where=None):
        self.search_text_calls += 1
        return list(self.text_result)

    def count(self) -> int:
        self.count_calls += 1
        return 42

    def health(self) -> HealthStatus:
        return HealthStatus(engine="vector:fake", ok=True)


@pytest.fixture()
def env():
    fake = CountingVectorDB()
    manager = CacheManager(config=BrainConfig(_env_file=None))  # vector_index 未配置 → 默认 memory 开
    store = CachedVectorStore(fake, cache_manager=manager)
    return fake, manager, store


# ── 命中：同 (where, top_k, query) 二次查询零 delegate 调用且结果一致 ────
def test_c18_hit_no_second_delegate_call(env) -> None:
    fake, _manager, store = env
    first = store.search_dense(QUERY, limit=5, where=WHERE)
    assert fake.search_dense_calls == 1
    second = store.search_dense(QUERY, limit=5, where=WHERE)
    assert fake.search_dense_calls == 1, "命中必须零 delegate 调用"
    assert second == first, "命中结果必须与原实现同构"
    # 换 top_k → miss；换 where → miss（键隔离正确）
    store.search_dense(QUERY, limit=3, where=WHERE)
    assert fake.search_dense_calls == 2
    store.search_dense(QUERY, limit=5, where=RecallFilter(active_only=False))
    assert fake.search_dense_calls == 3
    # 换 query → miss（键含查询本身，不互相污染）
    store.search_dense([0.9, 0.8, 0.7, 0.6], limit=5, where=WHERE)
    assert fake.search_dense_calls == 4


def test_c18_sparse_and_text_channels_cached(env) -> None:
    fake, _manager, store = env
    assert store.search_sparse({"缓存": 1.0}, limit=5, where=WHERE)
    assert store.search_sparse({"缓存": 1.0}, limit=5, where=WHERE)
    assert store.search_text("缓存方案", limit=5, where=WHERE)
    assert store.search_text("缓存方案", limit=5, where=WHERE)
    assert fake.search_sparse_calls == 1 and fake.search_text_calls == 1


def test_c18_count_and_passthrough_not_cached(env) -> None:
    fake, _manager, store = env
    assert store.count() == store.count() == 42
    assert fake.count_calls == 2, "count（size 类）必须直通"
    assert store.dense_dim == 4


# ── 写后精确失效：重查重走 delegate，拿到新结果 ──────────────────────────
def test_c18_upsert_invalidates_and_requeries(env) -> None:
    fake, _manager, store = env
    assert store.search_dense(QUERY, limit=5, where=WHERE)
    assert store.search_dense(QUERY, limit=5, where=WHERE)
    assert fake.search_dense_calls == 1, "预热命中"

    fake.dense_result = [ScoredId(id="n9", score=0.99, channel="dense")]
    store.upsert([VectorRecord(id="n1", dense=[0.0] * 4, text="更新")])
    assert fake.upsert_calls == 1

    third = store.search_dense(QUERY, limit=5, where=WHERE)
    assert fake.search_dense_calls == 2, "写后必须失效重查"
    assert [hit.id for hit in third] == ["n9"], "重查必须拿到新结果"


def test_c18_delete_invalidates(env) -> None:
    fake, _manager, store = env
    assert store.search_text("缓存方案", limit=5, where=WHERE)
    fake.text_result = []
    store.delete(["n4"])
    assert fake.delete_calls == 1
    assert store.search_text("缓存方案", limit=5, where=WHERE) == []
    assert fake.search_text_calls == 2


# ── 无 cache_manager：全直通 ─────────────────────────────────────────────
def test_c18_no_cache_manager_full_passthrough() -> None:
    fake = CountingVectorDB()
    store = CachedVectorStore(fake)  # 未注入
    first = store.search_dense(QUERY, limit=5, where=WHERE)
    second = store.search_dense(QUERY, limit=5, where=WHERE)
    assert fake.search_dense_calls == 2, "无 cache_manager 必须全直通"
    assert second == first
    store.upsert([])
    store.delete([])
    assert store.count() == 42
    assert fake.upsert_calls == 1 and fake.delete_calls == 1


def test_c18_cache_manager_late_injection() -> None:
    fake = CountingVectorDB()
    store = CachedVectorStore(fake)  # 先不注入
    store.search_dense(QUERY, limit=5, where=WHERE)
    store.search_dense(QUERY, limit=5, where=WHERE)
    assert fake.search_dense_calls == 2

    manager = CacheManager(config=BrainConfig(_env_file=None))
    store.set_cache_manager(manager)  # 主线程后注入
    store.search_dense(QUERY, limit=5, where=WHERE)   # miss（写入缓存）
    store.search_dense(QUERY, limit=5, where=WHERE)   # 命中
    assert fake.search_dense_calls == 3, "注入后第三次应 miss、第四次应命中"


# ── 委托异常透传，且不污染缓存 ───────────────────────────────────────────
def test_c18_delegate_exception_propagates(env) -> None:
    fake, _manager, store = env

    class Boom(Exception):
        pass

    original = fake.search_dense

    def boom(vector, *, limit, where=None):
        raise Boom("lance down")

    fake.search_dense = boom  # type: ignore[method-assign]
    with pytest.raises(Boom):
        store.search_dense(QUERY, limit=5, where=WHERE)
    with pytest.raises(Boom):
        store.search_dense(QUERY, limit=5, where=WHERE)

    fake.search_dense = original  # type: ignore[method-assign]  还原：异常未污染缓存
    assert fake.search_dense_calls == 0
    assert store.search_dense(QUERY, limit=5, where=WHERE)
    assert fake.search_dense_calls == 1


def test_c18_upsert_failure_does_not_invalidate(env) -> None:
    fake, _manager, store = env
    assert store.search_dense(QUERY, limit=5, where=WHERE)
    calls_before = fake.search_dense_calls

    def _boom(records):
        raise RuntimeError("write failed")

    fake.upsert = _boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        store.upsert([VectorRecord(id="n1", dense=[0.0] * 4, text="x")])
    # 写失败未走失效逻辑，但缓存也未收到无关事件；命中路径不受影响
    assert store.search_dense(QUERY, limit=5, where=WHERE)
    assert fake.search_dense_calls == calls_before, "写失败后缓存仍应可用"
