"""C0 缓存基础设施验收：精确失效、开关回退、双介质、指标落盘、WriteGate 发布点。"""
from __future__ import annotations

import time
from types import SimpleNamespace

from brain.write_gate import WriteGate, WriteRequest
from config import BrainConfig, CacheConfig
from core.cache_invalidation import CacheInvalidationBus, write_result_to_event
from core.cache_manager import CacheManager


def _manager(storage, config: BrainConfig | None = None,
             bus: CacheInvalidationBus | None = None) -> CacheManager:
    return CacheManager(
        config=config or BrainConfig(_env_file=None),
        kv=storage.kv, timeseries=storage.timeseries, bus=bus,
    )


# ── 内存 LRU：命中 / TTL / 容量淘汰 ─────────────────────────────────────
def test_memory_lru_hit_miss_and_expiry(storage) -> None:
    manager = _manager(storage)
    manager.set("recall", "k1", "v1", ttl_sec=0.05)
    assert manager.get("recall", "k1") == "v1"
    time.sleep(0.06)
    assert manager.get("recall", "k1") is None  # TTL 过期等价 miss


def test_memory_capacity_evicts_lru(storage) -> None:
    config = BrainConfig(_env_file=None, cache=CacheConfig(
        namespaces={"recall": {"enabled": True, "capacity": 2, "ttl_sec": 60,
                               "media": "memory"}}))
    manager = _manager(storage, config)
    for index in range(3):
        manager.set("recall", f"k{index}", index)
    assert manager.get("recall", "k0") is None   # 最早条目被 LRU 淘汰
    assert manager.get("recall", "k2") == 2
    manager.flush_metrics()
    rows = storage.timeseries.fetch("cache_metrics", limit=20)
    assert any(int(row["evictions"]) >= 1 and row["namespace"] == "recall"
               for row in rows)


# ── Q34：命名空间/总开关关闭后回退无缓存路径 ────────────────────────────
def test_disabled_namespace_falls_back_to_no_cache(storage) -> None:
    config = BrainConfig(_env_file=None, cache=CacheConfig(
        namespaces={"output": {"enabled": False, "capacity": 100,
                               "ttl_sec": 60, "media": "memory"}}))
    manager = _manager(storage, config)
    manager.set("output", "k", "v")
    assert manager.get("output", "k") is None


def test_master_switch_disables_everything(storage) -> None:
    manager = _manager(storage, BrainConfig(_env_file=None, cache=CacheConfig(enabled=False)))
    manager.set("recall", "k", "v")
    manager.set("embedding", "e", "vec")
    assert manager.get("recall", "k") is None
    assert manager.get("embedding", "e") is None


# ── LMDB 介质：跨实例读取（模拟重启重建）───────────────────────────────
def test_lmdb_media_survives_manager_rebuild(storage) -> None:
    config = BrainConfig(_env_file=None, cache=CacheConfig(
        namespaces={"embedding": {"enabled": True, "capacity": 100,
                                  "ttl_sec": 3600, "media": "lmdb"}}))
    _manager(storage, config).set("embedding", "hash-1", [0.1, 0.2])
    second = _manager(storage, config)
    assert second.get("embedding", "hash-1") == [0.1, 0.2]


# ── C0.2：精确失效，不清空命名空间 ──────────────────────────────────────
def test_invalidate_is_precise(storage) -> None:
    manager = _manager(storage)
    manager.set("recall", "a", {"ids": ["n1"]})
    manager.set("recall", "b", {"ids": ["n2"]})
    manager.set("route", "c", {"unrelated": True})
    assert manager.invalidate("recall", "a") == 1
    assert manager.get("recall", "a") is None
    assert manager.get("recall", "b") is not None
    assert manager.get("route", "c") is not None  # 其他命名空间不动


def test_memory_updated_event_invalidates_only_affected(storage) -> None:
    bus = CacheInvalidationBus()
    manager = _manager(storage, bus=bus)
    manager.set("recall", "q1", {"memory_ids": ["n1", "n2"]})
    manager.set("recall", "q2", {"memory_ids": ["n9"]})
    assert bus.publish({"type": "memory_updated", "event_type": "supersede",
                        "affected_ids": ["n1"], "ts_ms": 0}) == 1
    assert manager.get("recall", "q1") is None      # 值里含 n1 → 精确失效
    assert manager.get("recall", "q2") is not None  # 无关条目保留


def test_other_events_and_empty_ids_do_nothing(storage) -> None:
    bus = CacheInvalidationBus()
    manager = _manager(storage, bus=bus)
    manager.set("recall", "q1", {"memory_ids": ["n1"]})
    assert bus.publish({"type": "persona.baseline.write", "affected_ids": ["n1"]}) == 1
    assert bus.publish({"type": "memory_updated", "affected_ids": []}) == 1
    assert manager.get("recall", "q1") is not None  # 非目标事件 / 无 ids → 不动


# ── Q33：WriteGate 成功写入即发布，失败不发布 ───────────────────────────
def test_write_result_to_event_extracts_ids() -> None:
    request = WriteRequest(event_type="supersede")
    result = SimpleNamespace(neuron=SimpleNamespace(uuid7_id="n-new"), superseded=["n-old"])
    event = write_result_to_event(request, result)
    assert event is not None and event["type"] == "memory_updated"
    assert event["event_type"] == "supersede"
    assert event["affected_ids"] == ["n-new", "n-old"]
    assert write_result_to_event(request, {"ok": True}) is None  # 提取不到 id → 不发


def test_write_gate_publishes_memory_updated(storage) -> None:
    bus = CacheInvalidationBus()
    seen: list[dict] = []
    bus.subscribe(seen.append)
    manager = _manager(storage, bus=bus)
    manager.set("recall", "q1", {"memory_ids": ["n1"]})

    def write_ok() -> SimpleNamespace:
        return SimpleNamespace(neuron=SimpleNamespace(uuid7_id="n1"), superseded=[])

    gate = WriteGate(on_completed=lambda request, result: bus.publish(
        write_result_to_event(request, result) or {"type": "_skip"}))
    try:
        gate.submit(WriteRequest(fn=write_ok, event_type="ingest")).result(timeout=5)
    finally:
        gate.shutdown(timeout=5)
    event = next(item for item in seen if item.get("type") == "memory_updated")
    assert event["event_type"] == "ingest" and "n1" in event["affected_ids"]
    assert manager.get("recall", "q1") is None  # 闸门写入触发了精确失效


def test_write_gate_failure_publishes_nothing(storage) -> None:
    bus = CacheInvalidationBus()
    seen: list[dict] = []
    bus.subscribe(seen.append)
    gate = WriteGate(on_completed=lambda request, result: bus.publish(
        write_result_to_event(request, result) or {"type": "_skip"}))
    try:
        future = gate.submit(WriteRequest(fn=_raise))
        try:
            future.result(timeout=5)
        except RuntimeError:
            pass
    finally:
        gate.shutdown(timeout=5)
    assert seen == []  # 失败写入不改状态，不发布失效事件


def _raise() -> None:
    raise RuntimeError("boom")


# ── C0.3：指标聚合并落 DuckDB cache_metrics ────────────────────────────
def test_metrics_flush_to_duckdb(storage) -> None:
    manager = _manager(storage)
    manager.set("recall", "k1", "v1")
    assert manager.get("recall", "k1") == "v1"   # hit
    assert manager.get("recall", "missing") is None  # miss
    assert manager.flush_metrics() >= 1
    rows = storage.timeseries.fetch("cache_metrics", limit=10)
    row = next(item for item in rows if item["namespace"] == "recall")
    assert int(row["hits"]) >= 1 and int(row["misses"]) >= 1
    assert 0.0 <= float(row["hit_rate"]) <= 1.0
