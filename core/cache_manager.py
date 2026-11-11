"""core.cache_manager —— 统一缓存管理器（v4.1-C / C0.1）。

按命名空间隔离容量 / TTL / 介质；本期实现 memory（LRU）与 lmdb（复用 KVRepository，
桶名 `cache`，键 `{ns}:{key}`，值 JSON 带 expires_at）两种介质；SQLite 介质为
C01 冲突裁决预留枚举位。指标按命名空间聚合，攒批写 DuckDB `cache_metrics`。

纪律（Q28-Q36 裁决）：
- 关闭任一命名空间后行为回退无缓存路径（get 恒 miss / set 无操作）；
- 失效必须精确（键或值命中 affected_id），禁止清空整个命名空间；
- 冷启动不依赖缓存：miss 时调用方走完整路径。
"""
from __future__ import annotations

import fnmatch
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from enum import Enum
from typing import Any

from config import BrainConfig, get_config
from core.cache_invalidation import CacheInvalidationBus

__all__ = ["CacheManager", "CacheNamespace"]

_FLUSH_BATCH = 500
_LMDB_BUCKET = "cache"


class CacheNamespace(str, Enum):
    """全部命名空间（含预留位）。本期有消费者的见 `config.cache.namespaces` 默认值。"""

    THALAMUS = "thalamus"
    SALIENCE = "salience"
    EPISODIC = "episodic"
    PROMPT_PREFIX = "prompt_prefix"
    PATH = "path"
    RECALL = "recall"
    EMBEDDING = "embedding"
    NARRATIVE = "narrative"          # 由 W8 的 fast_recall 投影缓存承担（Q28）
    PERSONA = "persona"
    ROUTE = "route"
    SAFETY = "safety"
    OUTPUT = "output"
    TOOL = "tool"
    DOC_DISTILL = "doc_distill"
    CONFLICT = "conflict"


class _MemoryLRU:
    """单命名空间的内存 LRU + TTL。capacity<=0 视为不设上限。"""

    def __init__(self, capacity: int) -> None:
        self._capacity = max(0, int(capacity))
        self._items: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self.evictions = 0

    def get(self, key: str, now: float) -> Any | None:
        item = self._items.pop(key, None)
        if item is None:
            return None
        value, expires_at = item
        if expires_at and now >= expires_at:
            return None  # 已过期：不回填，等价 miss
        self._items[key] = item
        return value

    def set(self, key: str, value: Any, expires_at: float) -> None:
        if self._capacity <= 0:
            return
        self._items.pop(key, None)
        self._items[key] = (value, expires_at)
        while self._capacity and len(self._items) > self._capacity:
            self._items.popitem(last=False)
            self.evictions += 1

    def delete(self, key: str) -> bool:
        return self._items.pop(key, None) is not None

    def delete_matching(self, predicate: Callable[[str, Any], bool]) -> int:
        victims = [key for key, (value, _expires) in self._items.items()
                   if predicate(key, value)]
        for key in victims:
            self._items.pop(key, None)
        return len(victims)

    def size(self) -> int:
        return len(self._items)


class CacheManager:
    def __init__(
        self,
        *,
        config: BrainConfig | None = None,
        kv: Any | None = None,
        timeseries: Any | None = None,
        bus: CacheInvalidationBus | None = None,
    ) -> None:
        self._config = config or get_config()
        self._kv = kv
        self._timeseries = timeseries
        self._lock = threading.RLock()
        self._stores: dict[str, _MemoryLRU] = {}
        # ns -> {hits, misses, evictions, invalidations, latency_us}
        self._counters: dict[str, dict[str, float]] = {}
        self._evict_synced: dict[str, int] = {}
        self._ops_since_flush = 0
        if bus is not None:
            bus.subscribe(self.invalidate_by_event)

    # ── 配置 ──────────────────────────────────────────────────────────────
    def _settings(self, ns: str) -> dict[str, Any]:
        block = getattr(self._config, "cache", None)
        defaults: dict[str, Any] = {"enabled": True, "capacity": 512,
                                    "ttl_sec": 600, "media": "memory"}
        if block is None:
            return dict(defaults)
        namespaces = getattr(block, "namespaces", None) or {}
        return {**defaults, **dict(namespaces.get(ns) or {})}

    def _enabled(self, ns: str) -> bool:
        block = getattr(self._config, "cache", None)
        if block is not None and not getattr(block, "enabled", True):
            return False
        return bool(self._settings(ns).get("enabled", True))

    def _store(self, ns: str) -> _MemoryLRU:
        with self._lock:
            store = self._stores.get(ns)
            if store is None:
                store = _MemoryLRU(int(self._settings(ns).get("capacity", 512)))
                self._stores[ns] = store
            return store

    # ── 读写 ──────────────────────────────────────────────────────────────
    def get(self, ns: str, key: str) -> Any | None:
        if not self._enabled(ns):
            return None
        started = time.perf_counter()
        value = self._get_value(ns, str(key))
        self._record(ns, hit=value is not None,
                     latency_us=(time.perf_counter() - started) * 1_000_000)
        return value

    def _get_value(self, ns: str, key: str) -> Any | None:
        settings = self._settings(ns)
        if settings.get("media") == "lmdb":
            return self._lmdb_get(ns, key)
        return self._store(ns).get(key, time.time())

    def _lmdb_get(self, ns: str, key: str) -> Any | None:
        if self._kv is None:
            return None
        raw = self._kv.get(_LMDB_BUCKET, f"{ns}:{key}")
        if not raw:
            return None
        try:
            payload = json.loads(bytes(raw).decode("utf-8"))
        except (ValueError, TypeError):
            self._kv.delete(_LMDB_BUCKET, f"{ns}:{key}")
            return None
        if payload.get("expires_at") and time.time() >= float(payload["expires_at"]):
            self._kv.delete(_LMDB_BUCKET, f"{ns}:{key}")  # 惰性过期
            return None
        return payload.get("value")

    def set(self, ns: str, key: str, value: Any, *, ttl_sec: float | None = None) -> None:
        if not self._enabled(ns):
            return
        settings = self._settings(ns)
        ttl = settings.get("ttl_sec", 600) if ttl_sec is None else float(ttl_sec)
        expires_at = (time.time() + float(ttl)) if ttl and float(ttl) > 0 else 0.0
        if settings.get("media") == "lmdb":
            if self._kv is not None:
                self._kv.put(_LMDB_BUCKET, f"{ns}:{key}", json.dumps(
                    {"expires_at": expires_at, "value": value},
                    ensure_ascii=False, default=str).encode("utf-8"))
        else:
            self._store(ns).set(str(key), value, expires_at)
        self._ops_since_flush += 1
        self._maybe_flush()

    def delete(self, ns: str, key: str) -> None:
        settings = self._settings(ns)
        if settings.get("media") == "lmdb" and self._kv is not None:
            self._kv.delete(_LMDB_BUCKET, f"{ns}:{key}")
        else:
            self._store(ns).delete(str(key))

    # ── 精确失效 ──────────────────────────────────────────────────────────
    def invalidate(self, ns: str, key_pattern: str) -> int:
        """通配失效（fnmatch，键形如 `{ns}:{key}`）；返回删除条数。"""
        if not self._enabled(ns):
            return 0
        count = 0
        settings = self._settings(ns)
        with self._lock:
            if settings.get("media") == "lmdb":
                count = self._invalidate_lmdb_pattern(ns, key_pattern)
            else:
                count = self._store(ns).delete_matching(
                    lambda key, _value: fnmatch.fnmatch(key, key_pattern))
        self._count(ns)["invalidations"] += count
        return count

    def _invalidate_lmdb_pattern(self, ns: str, key_pattern: str) -> int:
        if self._kv is None:
            return 0
        count = 0
        for stored_key, _raw in self._kv.scan_prefix(_LMDB_BUCKET, f"{ns}:", limit=5000):
            if fnmatch.fnmatch(str(stored_key), f"{ns}:{key_pattern}"):
                self._kv.delete(_LMDB_BUCKET, str(stored_key))
                count += 1
        return count

    def invalidate_by_event(self, event: dict[str, Any]) -> int:
        """`memory_updated` 驱动的精确失效：键或值命中 affected_id 的条目被删除，
        其余条目（含其他命名空间）一律不动（C0.2 纪律）。"""
        if not isinstance(event, dict) or event.get("type") != "memory_updated":
            return 0
        ids = [str(item) for item in (event.get("affected_ids") or []) if item]
        if not ids:
            return 0
        namespaces = [str(item) for item in
                      (event.get("affected_namespaces") or ("recall", "episodic"))]
        total = 0
        for ns in namespaces:
            if not self._enabled(ns):
                continue
            if self._settings(ns).get("media") == "lmdb":
                total += self._invalidate_ids_lmdb(ns, ids)
            else:
                total += self._store(ns).delete_matching(
                    lambda key, value: self._matches_ids(key, value, ids))
        if total:
            bucket = namespaces[0] if len(namespaces) == 1 else "recall"
            self._count(bucket)["invalidations"] += total
        return total

    @staticmethod
    def _matches_ids(key: str, value: Any, ids: list[str]) -> bool:
        if any(item and item in key for item in ids):
            return True
        try:
            blob = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return False
        return any(item and item in blob for item in ids)

    def _invalidate_ids_lmdb(self, ns: str, ids: list[str]) -> int:
        if self._kv is None:
            return 0
        count = 0
        for stored_key, raw in self._kv.scan_prefix(_LMDB_BUCKET, f"{ns}:", limit=5000):
            try:
                payload = json.loads(bytes(raw).decode("utf-8"))
            except (ValueError, TypeError):
                continue
            if self._matches_ids(str(stored_key), payload.get("value"), ids):
                self._kv.delete(_LMDB_BUCKET, str(stored_key))
                count += 1
        return count

    def size(self, ns: str) -> int:
        return self._store(ns).size()

    # ── 指标（C0.3：命中率/延迟进 DuckDB cache_metrics）──────────────────
    def _count(self, ns: str) -> dict[str, float]:
        with self._lock:
            row = self._counters.get(ns)
            if row is None:
                row = {"hits": 0, "misses": 0, "evictions": 0,
                       "invalidations": 0, "latency_us": 0.0}
                self._counters[ns] = row
            return row

    def _record(self, ns: str, *, hit: bool, latency_us: float) -> None:
        row = self._count(ns)
        if hit:
            row["hits"] += 1
        else:
            row["misses"] += 1
        row["latency_us"] += latency_us
        self._ops_since_flush += 1
        self._maybe_flush()

    def _maybe_flush(self) -> None:
        try:
            every = int(getattr(self._config.cache, "flush_every_ops", _FLUSH_BATCH))
        except (AttributeError, TypeError, ValueError):
            every = _FLUSH_BATCH
        if self._ops_since_flush >= max(1, every):
            self.flush_metrics()

    def flush_metrics(self) -> int:
        """把聚合指标写 DuckDB `cache_metrics`（一行/命名空间），返回写出的行数。"""
        if self._timeseries is None:
            return 0
        with self._lock:
            # LRU 淘汰计数记在 store 对象上，flush 时把增量同步进指标
            for ns, store in self._stores.items():
                delta = store.evictions - self._evict_synced.get(ns, 0)
                if delta:
                    self._count(ns)["evictions"] += delta
                    self._evict_synced[ns] = store.evictions
            snapshot = dict(self._counters)
            self._counters.clear()
            self._ops_since_flush = 0
        rows = []
        now = int(time.time() * 1000)
        for ns, row in sorted(snapshot.items()):
            lookups = row["hits"] + row["misses"]
            rows.append({
                "ts_ms": now, "namespace": ns,
                "hits": int(row["hits"]), "misses": int(row["misses"]),
                "evictions": int(row["evictions"]),
                "invalidations": int(row["invalidations"]),
                "hit_rate": round(row["hits"] / lookups, 4) if lookups else 0.0,
                "avg_latency_us": round(row["latency_us"] / lookups, 1) if lookups else 0.0,
            })
        if not rows:
            return 0
        try:
            self._timeseries.append("cache_metrics", rows)
        except Exception:  # noqa: BLE001  指标写失败不影响缓存功能
            return 0
        return len(rows)
