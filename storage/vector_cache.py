"""storage.vector_cache —— C18 向量索引段缓存（装饰器模式，零接口变更）。

包装任意 VectorRepository（LanceVectorDB 等）：三个召回检索通道
search_dense / search_sparse / search_text 结果缓存于 cache_manager 的
`vector_index` 命名空间（memory 介质；CacheManager 对未配置 ns 有默认
settings 兜底，无需改 config.py）；写方法（upsert/delete）成功后按
affected_ids 精确失效；count / dense_dim / health / close 全部直通。

设计依据（.memory/架构设计总章.md §16.12/16.13，C0 交付）：
- 失效走 CacheManager.invalidate_by_event（_matches_ids 按缓存值内
  的 id 精确匹配，整条删除——保守但正确，实现最简）；
- 键 = sha256(json({channel, limit, where, query}))：除 (where, top_k)
  外必须含查询本身，否则不同 query 的检索会互相污染（同构性约束）；
- cache_manager=None（或后注入前）全直通，回退无缓存路径。
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any

from core.cache_manager import CacheManager
from core.interfaces import (
    HealthStatus,
    RecallFilter,
    ScoredId,
    VectorRecord,
    VectorRepository,
)

__all__ = ["CachedVectorStore"]

_NS = "vector_index"


class CachedVectorStore(VectorRepository):
    """VectorRepository 装饰器：只读检索走缓存，写后精确失效，其余全直通。"""

    name = "vector:cached"

    def __init__(
        self,
        delegate: VectorRepository,
        *,
        cache_manager: CacheManager | None = None,
    ) -> None:
        self._delegate = delegate
        self._cache_manager = cache_manager
        self._lock = threading.RLock()

    # ── 装配（主线程后注入）───────────────────────────────────────────────
    def set_cache_manager(self, manager: CacheManager | None) -> None:
        with self._lock:
            self._cache_manager = manager

    @property
    def delegate(self) -> VectorRepository:
        return self._delegate

    def _manager(self) -> CacheManager | None:
        with self._lock:
            return self._cache_manager

    # ── 缓存键（channel + where + top_k + query 本身，见模块 docstring）───
    @staticmethod
    def _cache_key(
        channel: str,
        query: Any,
        *,
        limit: int,
        where: RecallFilter | None,
    ) -> str:
        payload = {
            "channel": channel,
            "limit": int(limit),
            "where": asdict(where) if where is not None else None,
            "query": query,
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    # ── 读路径（近邻/召回检索类，走缓存）─────────────────────────────────
    def _cached(
        self,
        channel: str,
        query: Any,
        *,
        limit: int,
        where: RecallFilter | None,
        call,  # () -> list[ScoredId]
    ) -> list[ScoredId]:
        manager = self._manager()
        if manager is None:
            return call()  # 回退无缓存路径：全直通
        key = self._cache_key(channel, query, limit=limit, where=where)
        try:
            cached = manager.get(_NS, key)
        except Exception:  # noqa: BLE001  缓存读故障不阻断检索
            cached = None
        if cached is not None:
            # 同构重建：与 delegate 返回一致的 list[ScoredId]
            return [
                ScoredId(id=str(item_id), score=float(score), channel=str(chan))
                for item_id, score, chan in zip(
                    cached.get("ids") or [],
                    cached.get("scores") or [],
                    cached.get("channels") or [],
                )
            ]
        result = call()
        try:
            # 值形如 {"ids":[...], "scores":[...]}（+channels 供无损重建）；
            # _matches_ids 按值内的 id 精确匹配，upsert/delete 后按值失效自动生效
            manager.set(_NS, key, {
                "ids": [str(hit.id) for hit in result],
                "scores": [float(hit.score) for hit in result],
                "channels": [str(hit.channel) for hit in result],
            })
        except Exception:  # noqa: BLE001  缓存写故障不影响本次检索结果
            pass
        return result

    def search_dense(
        self,
        vector: Sequence[float],
        *,
        limit: int,
        where: RecallFilter | None = None,
    ) -> list[ScoredId]:
        return self._cached(
            "dense", [float(x) for x in vector], limit=limit, where=where,
            call=lambda: self._delegate.search_dense(vector, limit=limit, where=where),
        )

    def search_sparse(
        self,
        weights: Mapping[str, float],
        *,
        limit: int,
        where: RecallFilter | None = None,
    ) -> list[ScoredId]:
        return self._cached(
            "sparse", dict(weights), limit=limit, where=where,
            call=lambda: self._delegate.search_sparse(weights, limit=limit, where=where),
        )

    def search_text(
        self,
        query: str,
        *,
        limit: int,
        where: RecallFilter | None = None,
    ) -> list[ScoredId]:
        return self._cached(
            "text", str(query), limit=limit, where=where,
            call=lambda: self._delegate.search_text(query, limit=limit, where=where),
        )

    # ── 写路径（成功后精确失效）───────────────────────────────────────────
    def _invalidate(self, ids: Sequence[str]) -> None:
        manager = self._manager()
        if manager is None:
            return
        affected = [str(item) for item in ids if item]
        if not affected:
            return
        try:
            # 注意必须带 affected_namespaces=("vector_index",)：invalidate_by_event
            # 默认只扫 ("recall","episodic")，不带则碰不到本命名空间
            manager.invalidate_by_event({
                "type": "memory_updated",
                "affected_ids": affected,
                "affected_namespaces": (_NS,),
            })
        except Exception:  # noqa: BLE001  失效失败只影响新鲜度，不破坏写入
            pass

    def upsert(self, records: Sequence[VectorRecord]) -> None:
        self._delegate.upsert(records)  # 失败则不失效（写入未发生，状态未变）
        self._invalidate([record.id for record in records])

    def delete(self, ids: Sequence[str]) -> None:
        self._delegate.delete(ids)
        self._invalidate(ids)

    # ── 直通（size 类 / 元信息 / 生命周期）────────────────────────────────
    @property
    def dense_dim(self) -> int:
        return self._delegate.dense_dim

    def count(self) -> int:
        return self._delegate.count()

    def health(self) -> HealthStatus:
        return self._delegate.health()

    def close(self) -> None:
        self._delegate.close()
