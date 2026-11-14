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
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from config import BrainConfig, get_config
from core.cache_invalidation import CacheInvalidationBus

__all__ = ["CacheManager", "CacheNamespace"]

_FLUSH_BATCH = 500
_LMDB_BUCKET = "cache"

# 每个命名空间生产消费者 → **中文人话说明**（单一真源；缓存页据此展示）。
# 2026-11-12 用户裁决：卡片必须用中文写明白"这是什么缓存、存了什么、谁在读写"。
_CONSUMERS: dict[str, str] = {
    "recall": "召回缓存——把「搜记忆」的检索结果存下来，相同问题不重新搜（六路召回）",
    "embedding": "向量缓存——同一段文字算过的语义向量存下来，不再重复算",
    "vector_index": "向量索引缓存——按查询缓存检索用的索引段，写入记忆后自动失效",
    "doc_distill": "文档蒸馏缓存——记录哪些项目档案已提炼过，防止重复提炼（72 小时过期；内容变更立即失效）",
    "persona": "人格缓存——大肥鱼人格参数快照（容量小，24 小时过期）",
    "prompt_prefix": "提示词前缀缓存——人格注入等固定开头不重复拼接",
    "tool_read": "读工具缓存——MCP 只读工具同样的调用直接返回上次结果（60 秒内）",
    "tool": "工具清单缓存——tools/list 的工具定义列表复用",
    "episodic": "情节快照缓存——聊天每轮记录召回了哪些记忆，记忆更新后精确失效",
    "semantic": "语义输出缓存——意思相近的问题直接复用上次回复（阈值 0.95）",
    "output": "输出缓存——一模一样的提问直接复用整段回复（敏感内容不缓存）",
    "thalamus": "意图缓存——「这句话是想找记忆还是闲聊」的分类结果复用",
    "salience": "显著性缓存——记忆重要度打分结果复用",
    "safety": "敏感判定缓存——聊天敏感词判断结果复用",
    "narrative": "叙事投影缓存——召回结果的叙事骨架投影（写入后精确失效）",
    "path": "路径缓存——数据目录/模型目录的解析结果复用",
    "route": "路由镜像缓存——「这轮聊天用了哪个模型」的选路留痕（供诊断）",
    "conflict": "冲突缓存——新旧记忆覆盖关系的检测结果（写入后按 id 失效）",
    "agent_brief": "协作简报缓存——与各 Agent 协作要点/主题摘要（5 分钟刷新）",
}


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
    AGENT_BRIEF = "agent_brief"      # E6 Agent 心智模型 brief（TTL 300s）


@dataclass
class _Entry:
    """内存缓存条目（E1：主值 + 访问统计 + 干扰暂存区）。"""

    value: Any
    expires_at: float                       # 0 = 永不过期（ttl<=0 语义）
    set_at: float = 0.0                     # 写入时刻：衰减放大有效 TTL 的基点
    access_count: int = 0                   # get 命中次数（LTP 类比：越常用越久活）
    last_access: float = 0.0
    superseded: list[tuple[Any, float]] = field(default_factory=list)  # (旧值, 被覆盖时刻)

    def effective_ttl_base(self) -> float:
        """本次写入时的固定 TTL（base_ttl）。"""
        return max(0.0, self.expires_at - self.set_at)


class _MemoryLRU:
    """单命名空间的内存 LRU + 衰减 TTL + 认知性干扰暂存。capacity<=0 视为不设上限。

    衰减（E1）：过期判定不再用固定 expires_at，而是
        effective_ttl = base_ttl * (1 + log1p(access_count))
    高频命中的条目活得更久（长时程增强 LTP 的类比）；decay_enabled=False 时
    退回固定 TTL 语义。expires_at<=0 的条目仍永久。
    干扰（E1）：仅认知性命名空间——同 key 覆写时旧值进 superseded 暂存区，
    grace 期内可经 recall_superseded 取回，过 grace 惰性清除；get 永远返回新值。
    """

    def __init__(self, capacity: int, *, decay_enabled: bool = True,
                 cognitive: bool = False, grace_sec: float = 300.0) -> None:
        self._capacity = max(0, int(capacity))
        self._items: OrderedDict[str, _Entry] = OrderedDict()
        self.evictions = 0
        self._decay = bool(decay_enabled)
        self._cognitive = bool(cognitive)
        self._grace = max(0.0, float(grace_sec))

    # ── 过期判定 ──────────────────────────────────────────────────────────
    def _expired(self, entry: _Entry, now: float) -> bool:
        if entry.expires_at <= 0:
            return False  # ttl<=0 永久
        if not self._decay:
            return now >= entry.expires_at
        effective = entry.effective_ttl_base() * (1.0 + math.log1p(entry.access_count))
        return now >= entry.set_at + effective

    def _purge_superseded(self, entry: _Entry, now: float) -> None:
        if entry.superseded:
            entry.superseded = [(value, at) for value, at in entry.superseded
                                if now - at <= self._grace]

    # ── 读写 ──────────────────────────────────────────────────────────────
    def get(self, key: str, now: float) -> Any | None:
        entry = self._items.get(key)
        if entry is None:
            return None
        if self._expired(entry, now):
            self._items.pop(key, None)
            return None  # 已过期：等价 miss
        entry.access_count += 1
        entry.last_access = now
        self._items.move_to_end(key)
        return entry.value

    def set(self, key: str, value: Any, expires_at: float, now: float) -> None:
        if self._capacity <= 0:
            return
        previous = self._items.pop(key, None)
        entry = _Entry(value=value, expires_at=expires_at, set_at=now, last_access=now)
        if previous is not None:
            entry.access_count = previous.access_count  # 覆写延续访问热度
            if self._cognitive:
                # 认知性命名空间：旧值进暂存区而非立即消失（干扰效应）
                self._purge_superseded(previous, now)
                if not self._expired(previous, now):
                    entry.superseded = previous.superseded + [(previous.value, now)]
        self._items[key] = entry
        while self._capacity and len(self._items) > self._capacity:
            self._items.popitem(last=False)
            self.evictions += 1

    def recall_superseded(self, key: str, now: float) -> list[Any]:
        """grace 期内被覆盖的旧值（新到旧，最近被覆盖的先看到）。"""
        entry = self._items.get(key)
        if entry is None:
            return []
        self._purge_superseded(entry, now)  # 过 grace 期惰性清除
        return [value for value, _at in reversed(entry.superseded)]

    def delete(self, key: str) -> bool:
        return self._items.pop(key, None) is not None

    def delete_matching(self, predicate: Callable[[str, Any], bool]) -> int:
        victims = [key for key, entry in self._items.items()
                   if predicate(key, entry.value)]
        for key in victims:
            self._items.pop(key, None)
        return len(victims)

    def size(self) -> int:
        return len(self._items)

    def hot_entries(self, min_access: int) -> list[dict[str, Any]]:
        """晋升候选：access_count ≥ 阈值的键（按热度降序）。"""
        rows = [{"key": key, "access_count": entry.access_count}
                for key, entry in self._items.items()
                if entry.access_count >= min_access]
        rows.sort(key=lambda row: (-int(row["access_count"]), str(row["key"])))
        return rows


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
        # 运行时开关覆盖（前端缓存页 RPC）：不落盘，重启回到 config
        self._runtime_enabled: dict[str, bool] = {}
        # ns -> {hits, misses, evictions, invalidations, latency_us}
        self._counters: dict[str, dict[str, float]] = {}
        self._evict_synced: dict[str, int] = {}
        self._ops_since_flush = 0
        if bus is not None:
            bus.subscribe(self.invalidate_by_event)

    # ── 配置 ──────────────────────────────────────────────────────────────
    def _cache_block(self) -> Any:
        return getattr(self._config, "cache", None)

    def _decay_enabled(self) -> bool:
        block = self._cache_block()
        if block is None:
            return True
        return bool(getattr(block, "decay_enabled", True))

    def _cognitive_namespaces(self) -> list[str]:
        block = self._cache_block()
        if block is None:
            return ["output"]
        return list(getattr(block, "cognitive_namespaces", None) or ["output"])

    def _interference_grace(self) -> float:
        block = self._cache_block()
        if block is None:
            return 300.0
        try:
            return max(0.0, float(getattr(block, "interference_grace_sec", 300)))
        except (TypeError, ValueError):
            return 300.0

    def _is_cognitive(self, ns: str) -> bool:
        """认知性命名空间才做干扰暂存；其余（事务性）ns 直接覆盖。"""
        return ns in self._cognitive_namespaces()

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
        override = self._runtime_enabled.get(ns)
        if override is not None:
            return override
        return bool(self._settings(ns).get("enabled", True))

    def set_enabled(self, ns: str, enabled: bool) -> None:
        """运行时 per-ns 开关（cache.toggle RPC）：覆盖配置但不落盘。"""
        with self._lock:
            self._runtime_enabled[ns] = bool(enabled)

    def _store(self, ns: str) -> _MemoryLRU:
        with self._lock:
            store = self._stores.get(ns)
            if store is None:
                store = _MemoryLRU(
                    int(self._settings(ns).get("capacity", 512)),
                    decay_enabled=self._decay_enabled(),
                    cognitive=self._is_cognitive(ns),
                    grace_sec=self._interference_grace(),
                )
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
        stored = f"{ns}:{key}"
        raw = self._kv.get(_LMDB_BUCKET, stored)
        if not raw:
            return None
        try:
            payload = json.loads(bytes(raw).decode("utf-8"))
        except (ValueError, TypeError):
            self._kv.delete(_LMDB_BUCKET, stored)
            return None
        expires_at = float(payload.get("expires_at") or 0.0)
        now = time.time()
        access_count = int(payload.get("access_count") or 0)
        if expires_at > 0:
            if self._decay_enabled():
                # 惰性过期同样应用衰减：高频条目有效 TTL 按 log1p 放大
                base = max(0.0, expires_at - float(payload.get("set_at") or expires_at))
                effective = base * (1.0 + math.log1p(access_count))
                expired = now >= float(payload.get("set_at") or expires_at) + effective
            else:
                expired = now >= expires_at
            if expired:
                self._kv.delete(_LMDB_BUCKET, stored)  # 惰性过期
                return None
        # 命中：access_count +1 写回（E1：LMDB 介质的访问计数）
        payload["access_count"] = access_count + 1
        try:
            self._kv.put(_LMDB_BUCKET, stored, json.dumps(
                payload, ensure_ascii=False, default=str).encode("utf-8"))
        except Exception:  # noqa: BLE001  计数写回失败不影响读路径
            pass
        return payload.get("value")

    def set(self, ns: str, key: str, value: Any, *, ttl_sec: float | None = None) -> None:
        if not self._enabled(ns):
            return
        settings = self._settings(ns)
        ttl = settings.get("ttl_sec", 600) if ttl_sec is None else float(ttl_sec)
        now = time.time()
        expires_at = (now + float(ttl)) if ttl and float(ttl) > 0 else 0.0
        if settings.get("media") == "lmdb":
            if self._kv is not None:
                # E1：LMDB 介质本期不做干扰暂存（superseded 不落盘），
                # 只记录 set_at / access_count 供惰性衰减过期使用。
                self._kv.put(_LMDB_BUCKET, f"{ns}:{key}", json.dumps(
                    {"expires_at": expires_at, "set_at": now,
                     "access_count": 0, "value": value},
                    ensure_ascii=False, default=str).encode("utf-8"))
        else:
            self._store(ns).set(str(key), value, expires_at, now)
        self._ops_since_flush += 1
        self._maybe_flush()

    def delete(self, ns: str, key: str) -> None:
        settings = self._settings(ns)
        if settings.get("media") == "lmdb" and self._kv is not None:
            self._kv.delete(_LMDB_BUCKET, f"{ns}:{key}")
        else:
            self._store(ns).delete(str(key))

    def recall_superseded(self, ns: str, key: str) -> list[Any]:
        """干扰效应纠错口：grace 期内被同 key 覆盖的旧值（新到旧）。

        仅对认知性命名空间（config.cache.cognitive_namespaces）生效；
        其余 ns 覆写即丢（返回 []）。get 永远只返回新值，旧值只从这里取。
        LMDB 介质本期不做干扰暂存（恒返回 []）。
        """
        if not self._enabled(ns) or not self._is_cognitive(ns):
            return []
        settings = self._settings(ns)
        if settings.get("media") == "lmdb":
            return []  # LMDB 介质不做干扰（注释占位，本期不实现）
        return self._store(ns).recall_superseded(str(key), time.time())

    def hot_entries(self, ns: str, *, min_access: int) -> list[dict[str, Any]]:
        """E1 晋升：access_count ≥ min_access 的键与计数（热度降序）。

        内存介质从条目 access_count 读取；LMDB 介质惰性扫描 JSON 里的
        access_count。命名空间级聚合指标（hits）没有 per-key 粒度，故
        不混入；调用方如需 ns 总命中量走 cache_metrics 表。
        """
        if not self._enabled(ns) or min_access <= 0:
            return []
        settings = self._settings(ns)
        rows: list[dict[str, Any]] = []
        if settings.get("media") == "lmdb":
            if self._kv is None:
                return []
            for stored_key, raw in self._kv.scan_prefix(_LMDB_BUCKET, f"{ns}:", limit=5000):
                try:
                    payload = json.loads(bytes(raw).decode("utf-8"))
                except (ValueError, TypeError):
                    continue
                count = int(payload.get("access_count") or 0)
                if count >= min_access:
                    rows.append({"key": str(stored_key).split(":", 1)[-1],
                                 "access_count": count})
        else:
            rows = self._store(ns).hot_entries(min_access)
        rows.sort(key=lambda row: (-int(row["access_count"]), str(row["key"])))
        return rows

    def iter_entries(self, ns: str) -> list[tuple[str, Any]]:
        """C17 语义输出缓存专用：遍历命名空间全部未过期条目（键, 值）。

        memory 介质遍历 LRU store（跳过已过期）；lmdb 介质走 scan_prefix
        惰性解析 JSON。ns 关闭时返回 []（与 get 的回退语义一致）。
        只读：不改访问计数/LRU 顺序，不写指标，不删除过期条目。
        """
        if not self._enabled(ns):
            return []
        settings = self._settings(ns)
        now = time.time()
        items: list[tuple[str, Any]] = []
        if settings.get("media") == "lmdb":
            if self._kv is None:
                return []
            for stored_key, raw in self._kv.scan_prefix(_LMDB_BUCKET, f"{ns}:", limit=5000):
                try:
                    payload = json.loads(bytes(raw).decode("utf-8"))
                except (ValueError, TypeError):
                    continue
                expires_at = float(payload.get("expires_at") or 0.0)
                if expires_at > 0 and now >= expires_at:
                    continue  # 惰性过期条目不算数（保持只读，不删除）
                items.append((str(stored_key).split(":", 1)[-1], payload.get("value")))
        else:
            store = self._store(ns)
            with self._lock:
                for key, entry in list(store._items.items()):
                    if not store._expired(entry, now):
                        items.append((key, entry.value))
        return items

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
        # v4.1-C 全开：conflict（冲突痕迹）、semantic（语义输出）、output（精确输出）
        # 加入默认波及集——三者值内均含 memory id / affected_ids 可精确命中；
        # vector_index 由 CachedVectorStore 写入时显式带 affected_namespaces。
        namespaces = [str(item) for item in
                      (event.get("affected_namespaces")
                       or ("recall", "episodic", "conflict", "semantic", "output"))]
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
        """条目数：memory 介质读 LRU，lmdb 介质按前缀扫桶（修复 lmdb 恒显 0 的 bug）。"""
        settings = self._settings(ns)
        if settings.get("media") == "lmdb":
            if self._kv is None:
                return 0
            try:
                count = 0
                for _key, _raw in self._kv.scan_prefix(_LMDB_BUCKET, f"{ns}:", limit=5000):
                    count += 1
                return count
            except Exception:  # noqa: BLE001  扫描失败按 0 计，不影响快照
                return 0
        return self._store(ns).size()

    def snapshot_metrics(self) -> dict[str, Any]:
        """前端缓存监控页：不清空地读当前 per-ns 聚合 + 条目数 + 配置开关 + 消费者。"""
        with self._lock:
            counters = {ns: dict(row) for ns, row in self._counters.items()}
        block = getattr(self._config, "cache", None)
        namespaces_cfg = dict(getattr(block, "namespaces", None) or {})
        result: dict[str, Any] = {}
        # 候选集含 _CONSUMERS：确保每个已接线 ns 即使本轮零访问也出现在快照里
        seen = set(counters) | set(namespaces_cfg) | set(self._stores) | set(_CONSUMERS)
        for ns in sorted(seen):
            cfg_ns = namespaces_cfg.get(ns) or {}
            row = counters.get(ns, {"hits": 0, "misses": 0, "evictions": 0,
                                    "invalidations": 0, "latency_us": 0.0})
            lookups = row["hits"] + row["misses"]
            result[ns] = {
                "enabled": bool(self._enabled(ns)),
                "media": cfg_ns.get("media", "memory"),
                "capacity": int(cfg_ns.get("capacity", 512)),
                "entries": self.size(ns) if self._enabled(ns) else 0,
                "hits": int(row["hits"]),
                "misses": int(row["misses"]),
                "hit_rate": round(row["hits"] / lookups, 4) if lookups else 0.0,
                "evictions": int(row["evictions"]),
                "invalidations": int(row["invalidations"]),
                "avg_latency_us": round(row["latency_us"] / lookups, 1) if lookups else 0.0,
                "consumer": _CONSUMERS.get(ns, "（无生产消费者）"),
            }
        return {"enabled": bool(getattr(block, "enabled", True)), "namespaces": result}

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


# ── 进程级单例（v4.1-C 第一批消费者的统一获取入口）─────────────────────
_MANAGER: CacheManager | None = None


def configure_cache_manager(*, config: BrainConfig | None = None, kv: Any | None = None,
                            timeseries: Any | None = None,
                            bus: CacheInvalidationBus | None = None) -> CacheManager:
    """headless_server 装配/换核时调用；重建实例并重新订阅失效总线。"""
    global _MANAGER
    _MANAGER = CacheManager(config=config, kv=kv, timeseries=timeseries, bus=bus)
    return _MANAGER


def get_cache_manager() -> CacheManager | None:
    """消费者统一入口；未装配（测试/独立脚本）返回 None，调用方回退无缓存路径。"""
    return _MANAGER
