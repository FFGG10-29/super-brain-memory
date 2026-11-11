"""E1 缓存类脑化 + E3 主动遗忘验收（全部 fake/夹具，零网络）。

E1：衰减曲线（高频条目比低频活得久）、干扰效应（grace 期内可取旧值）、晋升（hot_entries）。
E3：预测价值扫描（低价值降 archive 且产生 tier_change 事件、高价值低访问列 review）。
"""
from __future__ import annotations

from types import SimpleNamespace

from brain.memory_tier_manager import MemoryTierManager
from brain.synaptic_plasticity import SynapticPlasticity
from config import BrainConfig, CacheConfig, ForgetConfig
from core.cache_manager import CacheManager
from models.neuron import Neuron
from workers.life_loop import LifeLoop

_MS_PER_DAY = 86_400_000


# ── 时间夹具：可控时钟（替换 core.cache_manager 的 time 模块引用）─────────
class _FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = float(start)

    def time(self) -> float:
        return self.now

    def perf_counter(self) -> float:
        return 0.0

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def _cache_manager(clock: _FakeClock, **cache_kwargs: object) -> CacheManager:
    config = BrainConfig(_env_file=None, cache=CacheConfig(**cache_kwargs))
    manager = CacheManager(config=config)
    # 只替换本模块引用，不污染全局 time
    import core.cache_manager as module
    module.time = clock  # type: ignore[assignment]
    return manager


# ══ E1.1 衰减曲线 ════════════════════════════════════════════════════════
def test_decay_high_frequency_entry_lives_longer() -> None:
    clock = _FakeClock()
    manager = _cache_manager(
        clock,
        decay_enabled=True,
        namespaces={"recall": {"enabled": True, "capacity": 16,
                               "ttl_sec": 100, "media": "memory"}},
    )
    manager.set("recall", "hot", "H", ttl_sec=100)
    manager.set("recall", "cold", "C", ttl_sec=100)
    for _ in range(30):
        assert manager.get("recall", "hot") == "H"   # 高频命中：access_count → 30

    clock.advance(50)  # base_ttl 内：高频条目仍活着
    assert manager.get("recall", "hot") == "H"

    clock.advance(60)  # t=+110 > base_ttl=100：低频已死，高频有效 TTL≈100*(1+log1p(30))≈443
    assert manager.get("recall", "cold") is None     # 低频条目按固定 TTL 过期
    assert manager.get("recall", "hot") == "H"       # 高频条目继续存活（LTP 类比）


def test_decay_disabled_falls_back_to_fixed_ttl() -> None:
    clock = _FakeClock()
    manager = _cache_manager(
        clock,
        decay_enabled=False,
        namespaces={"recall": {"enabled": True, "capacity": 16,
                               "ttl_sec": 100, "media": "memory"}},
    )
    manager.set("recall", "k", "v", ttl_sec=100)
    for _ in range(30):
        assert manager.get("recall", "k") == "v"
    clock.advance(110)  # 衰减关闭：无论多高频，固定 TTL 到点即失效
    assert manager.get("recall", "k") is None


def test_decay_zero_ttl_stays_permanent() -> None:
    clock = _FakeClock()
    manager = _cache_manager(
        clock,
        namespaces={"recall": {"enabled": True, "capacity": 16,
                               "ttl_sec": 0, "media": "memory"}},
    )
    manager.set("recall", "k", "v", ttl_sec=0)
    clock.advance(10_000)
    assert manager.get("recall", "k") == "v"  # ttl<=0 永久，不受衰减影响


# ══ E1.2 干扰效应 ════════════════════════════════════════════════════════
def _interference_manager(clock: _FakeClock) -> CacheManager:
    return _cache_manager(
        clock,
        cognitive_namespaces=["output"],
        interference_grace_sec=300,
        namespaces={
            "output": {"enabled": True, "capacity": 16, "ttl_sec": 3600, "media": "memory"},
            "recall": {"enabled": True, "capacity": 16, "ttl_sec": 3600, "media": "memory"},
        },
    )


def test_interference_superseded_recallable_within_grace() -> None:
    clock = _FakeClock()
    manager = _interference_manager(clock)
    manager.set("output", "k", "v1")
    manager.set("output", "k", "v2")                 # 同 key 覆写：旧值进暂存区
    assert manager.get("output", "k") == "v2"        # get 永远返回新值
    assert manager.recall_superseded("output", "k") == ["v1"]  # grace 期内可纠错
    manager.set("output", "k", "v3")
    assert manager.recall_superseded("output", "k") == ["v2", "v1"]  # 新到旧


def test_interference_expired_after_grace_and_cognitive_only() -> None:
    clock = _FakeClock()
    manager = _interference_manager(clock)
    manager.set("output", "k", "v1")
    manager.set("output", "k", "v2")
    # 非认知性命名空间：直接覆盖，无干扰暂存（事务语义优先）
    manager.set("recall", "k", "a1")
    manager.set("recall", "k", "a2")
    assert manager.recall_superseded("recall", "k") == []

    clock.advance(301)  # 超过 grace=300s：旧值被清除
    assert manager.recall_superseded("output", "k") == []
    assert manager.get("output", "k") == "v2"        # 主值不受影响


def test_interference_lmdb_media_not_supported() -> None:
    clock = _FakeClock()
    manager = _cache_manager(
        clock,
        cognitive_namespaces=["output"],
        namespaces={"output": {"enabled": True, "capacity": 16,
                               "ttl_sec": 3600, "media": "lmdb"}},
    )
    # 本期 LMDB 不做干扰：恒返回空（get 仍正常）
    assert manager.recall_superseded("output", "k") == []


# ══ E1.3 晋升 hot_entries ════════════════════════════════════════════════
def test_hot_entries_returns_access_counts() -> None:
    clock = _FakeClock()
    manager = _cache_manager(
        clock,
        namespaces={"recall": {"enabled": True, "capacity": 16,
                               "ttl_sec": 600, "media": "memory"}},
    )
    manager.set("recall", "k1", 1)
    manager.set("recall", "k2", 2)
    manager.set("recall", "k3", 3)
    for _ in range(5):
        _ = manager.get("recall", "k1")
    _ = manager.get("recall", "k2")
    _ = manager.get("recall", "k2")                  # k2 命中 2 次，达阈值

    hot = manager.hot_entries("recall", min_access=2)
    assert [row["key"] for row in hot] == ["k1", "k2"]   # 热度降序，k3 不足阈值
    assert hot[0]["access_count"] == 5
    assert manager.hot_entries("recall", min_access=99) == []


# ══ E3 主动遗忘 ══════════════════════════════════════════════════════════
class FakeRelational:
    def __init__(self, records: list[dict]) -> None:
        self.records = records
        self.tier_changes: list[tuple[str, str]] = []

    def query(self, kind, *, memory_types=(), active_only=True, limit=50, **_):
        rows = [r for r in self.records if r["memory_type"] in memory_types]
        if active_only:
            rows = [r for r in rows if r["is_active"]]
        return rows[:limit]

    def set_tier(self, kind, entity_id, tier):
        self.tier_changes.append((entity_id, tier))
        for record in self.records:
            if record["id"] == entity_id:
                record["tier"] = tier


class FakeEventLog:
    def __init__(self, events: list[dict] | None = None) -> None:
        self.events = events or []
        self.appended: list[dict] = []

    def iter_events(self, **_):
        return iter(self.events)

    def append(self, event):
        self.appended.append(event)


class FakeTimeseries:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []

    def fetch(self, table, **_):
        return self.rows


def _forget_config(**overrides) -> ForgetConfig:
    params: dict = {
        "w_retention": 0.6, "w_usage": 0.3, "w_trust": 0.1,
        "archive_value_threshold": 0.25, "max_recall_count_archive": 1,
        "review_value_threshold": 0.65, "review_max_recall_count": 1,
    }
    params.update(overrides)
    return ForgetConfig(**params)


def _neuron_record(content: str, *, recall_count: int, last_recall_ms: int,
                   stability: float, memory_type: str = "fact",
                   tier: str = "recall", is_active: bool = True) -> dict:
    return Neuron(
        content=content, memory_type=memory_type, tier=tier,
        recall_count=recall_count, last_recall_ms=last_recall_ms,
        stability=stability,
    ).to_record() | {"is_active": is_active}


def _build_loop(records, *, events=None, forget: ForgetConfig | None = None):
    config = BrainConfig(_env_file=None, forget=forget or _forget_config())
    relational = FakeRelational(records)
    event_log = FakeEventLog(events)
    plasticity = SynapticPlasticity(config)
    tier_manager = MemoryTierManager(
        relational, plasticity, event_log=event_log, config=config,
    )
    loop = LifeLoop(
        FakeTimeseries([{"ts_ms": 1}]),   # access_log 总量趋势：1 行
        relational=relational,
        tier_manager=tier_manager,
        event_log=event_log,
        plasticity=plasticity,
        config=config,
    )
    return loop, relational, event_log


def test_forget_sweep_archives_low_value_and_emits_event() -> None:
    now = 1_700_000_000_000
    stale = _neuron_record(
        "很久没被想起的旧事实", recall_count=0,
        last_recall_ms=now - 200 * _MS_PER_DAY, stability=1.0,
    )
    loop, relational, _log = _build_loop([stale])
    result = loop.predictive_value_sweep(now_ms=now)
    assert result["scanned"] == 1
    assert result["archived"] == [stale["id"]]
    assert result["events_emitted"] == 1
    assert relational.tier_changes == [(stale["id"], "archive")]
    assert stale["tier"] == "archive"
    assert result["access_log_rows_30d"] == 1


def test_forget_sweep_keeps_high_value_low_access_for_review() -> None:
    now = 1_700_000_000_000
    fresh = _neuron_record(
        "刚被巩固的高价值记忆", recall_count=0,
        last_recall_ms=now - 1_000, stability=10.0,
    )
    loop, relational, _log = _build_loop([fresh])
    result = loop.predictive_value_sweep(now_ms=now)
    assert result["archived"] == []                       # 不降级
    assert relational.tier_changes == []
    assert any(row["id"] == fresh["id"] for row in result["review"])  # 列入人审


def test_forget_sweep_corrections_reduce_value() -> None:
    now = 1_700_000_000_000
    target = _neuron_record(
        "被反复纠正的记忆", recall_count=0,
        last_recall_ms=now - 1_000, stability=10.0,
    )
    events = [{"type": "supersede",
               "payload": {"old_id": target["id"], "new_id": f"n-other-{i}"}}
              for i in range(3)]                 # 纠正度 3/3 → 满额折损
    # 无纠正：retention≈1 → 价值≈0.7 → 进 review
    loop_clean, _rel, _log = _build_loop([target])
    assert any(row["id"] == target["id"] for row in
               loop_clean.predictive_value_sweep(now_ms=now)["review"])
    # 有纠正：价值被压到阈值之下 → 不再进 review
    loop_corrected, _rel, _log = _build_loop([target], events=events)
    assert all(row["id"] != target["id"] for row in
               loop_corrected.predictive_value_sweep(now_ms=now)["review"])


def test_forget_sweep_skips_core_tier_and_respects_switch() -> None:
    now = 1_700_000_000_000
    core = _neuron_record(
        "核心层记忆", recall_count=0, last_recall_ms=now - 400 * _MS_PER_DAY,
        stability=1.0, tier="core",
    )
    loop, relational, _log = _build_loop([core])
    result = loop.predictive_value_sweep(now_ms=now)
    assert result["scanned"] == 0                     # core 不自动降级（分层不变量）
    assert relational.tier_changes == []

    disabled_loop, _rel, _log = _build_loop(
        [], forget=_forget_config(enabled=False))
    assert disabled_loop.predictive_value_sweep()["skipped"]
