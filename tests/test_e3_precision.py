"""E3 精确归因验收：access_log 带 neuron_ids、旧格式行兼容、sweep precise_hits 生效。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import duckdb

from brain.prefrontal import Prefrontal, RecallHit
from brain.memory_tier_manager import MemoryTierManager
from brain.synaptic_plasticity import SynapticPlasticity
from config import BrainConfig, ForgetConfig
from models.neuron import Neuron
from storage.time_series_db import DuckDBTimeSeries
from workers.life_loop import LifeLoop

_MS_PER_DAY = 86_400_000
_NOW = 1_700_000_000_000


def _db(tmp_path) -> DuckDBTimeSeries:
    return DuckDBTimeSeries(tmp_path / "ts.duckdb")


# ── 1. access_log 写入带 neuron_ids 且 fetch 读回 ─────────────────────────
def test_access_log_neuron_ids_roundtrip(tmp_path) -> None:
    ts = _db(tmp_path)
    ts.append("access_log", [{
        "ts_ms": _NOW, "agent_id": "a1", "channel": "dense,sparse",
        "query": "q", "hits": 2, "latency_ms": 1.5, "ok": True,
        "neuron_ids": json.dumps(["n-1", "n-2"]),
    }])
    rows = ts.fetch("access_log")
    assert len(rows) == 1
    assert json.loads(rows[0]["neuron_ids"]) == ["n-1", "n-2"]


# ── 2. prefrontal._audit 写入 neuron_ids（保持七列 + 限 20 个）────────────
def test_prefrontal_audit_writes_neuron_ids(tmp_path) -> None:
    ts = _db(tmp_path)
    pre = Prefrontal(
        relational=SimpleNamespace(), vector=SimpleNamespace(),
        graph=SimpleNamespace(), kv=SimpleNamespace(),
        embedder=None, reranker=SimpleNamespace(),
        plasticity=SimpleNamespace(), amygdala=SimpleNamespace(),
        timeseries=ts, config=BrainConfig(_env_file=None),
    )
    hits = [
        RecallHit(Neuron(content=f"m{i}", memory_type="fact"), 1.0, ("dense",))
        for i in range(25)                       # 25 个命中 → 只留前 20 个
    ]
    pre._audit("q", {"dense": ["x"]}, hits, "a1", started_ms=_NOW)
    rows = ts.fetch("access_log")
    assert len(rows) == 1
    ids = json.loads(rows[0]["neuron_ids"])
    assert len(ids) == 20 and ids[0] == hits[0].neuron.uuid7_id
    # 既有七列不变
    for col in ("ts_ms", "agent_id", "channel", "query", "hits", "latency_ms", "ok"):
        assert col in rows[0]


# ── 3. 旧格式行（无 neuron_ids 列）不炸：迁移补列 + 读回 None ──────────────
def test_old_format_access_log_migrates_and_reads(tmp_path) -> None:
    db = tmp_path / "old.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute(
        "CREATE TABLE access_log(ts_ms BIGINT, agent_id VARCHAR, channel VARCHAR,"
        " query VARCHAR, hits INTEGER, latency_ms DOUBLE, ok BOOLEAN)"
    )
    conn.execute(
        "INSERT INTO access_log VALUES (?, 'a1', 'dense', 'q', 1, 0.5, true)",
        [_NOW],
    )
    conn.close()
    ts = DuckDBTimeSeries(db)                    # 打开即迁移
    rows = ts.fetch("access_log")
    assert rows[0]["neuron_ids"] is None         # 旧行读回 None，不炸
    ts.append("access_log", [{                   # 迁移后新格式行可写
        "ts_ms": _NOW + 1, "agent_id": "a1", "channel": "dense",
        "query": "q2", "hits": 1, "latency_ms": 0.1, "ok": True,
        "neuron_ids": json.dumps(["n-9"]),
    }])
    assert json.loads(ts.fetch("access_log")[1]["neuron_ids"]) == ["n-9"]


# ── 4. sweep 的 precise_hits 生效 ─────────────────────────────────────────
class FakeRelational:
    def __init__(self, records):
        self.records = records
        self.tier_changes = []

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


class FakeTimeseries:
    def __init__(self, rows):
        self.rows = rows

    def fetch(self, table, **_):
        return self.rows


def _neuron_record(content, *, recall_count, last_recall_ms, stability):
    return Neuron(
        content=content, memory_type="fact", tier="recall",
        recall_count=recall_count, last_recall_ms=last_recall_ms,
        stability=stability,
    ).to_record() | {"is_active": True}


def _loop(records, rows):
    config = BrainConfig(_env_file=None)
    # 已修文档代码：默认 w_trust=0.2 使「零纠正」记忆的价值下限恒为 0.2（archive
    # 阈值恰为 0.2），叠加 retention>0 后 value 恒 >0.2，文档断言在默认权重下
    # 数学上不可能成立。此处置 w_trust=0（只测 retention×usage 两个信号，
    # 与文档「5 hits → usage=0.5 → value≈0.4」的推演口径一致），生产默认权重不变。
    config.forget.w_trust = 0.0
    relational = FakeRelational(records)
    plasticity = SynapticPlasticity(config)
    tier_manager = MemoryTierManager(relational, plasticity, config=config)
    return LifeLoop(
        FakeTimeseries(rows), relational=relational, tier_manager=tier_manager,
        plasticity=plasticity, config=config,
    ), relational


def test_sweep_precise_hits_prevent_archive(tmp_path) -> None:
    stale_ms = _NOW - 200 * _MS_PER_DAY          # 衰减到位，retention≈0
    hot = _neuron_record("30天5次精确召回", recall_count=0, last_recall_ms=stale_ms, stability=1.0)
    cold = _neuron_record("30天零召回", recall_count=0, last_recall_ms=stale_ms, stability=1.0)
    rows = [{
        "ts_ms": _NOW - _MS_PER_DAY, "agent_id": "a1", "channel": "dense",
        "query": "q", "hits": 1, "latency_ms": 1.0, "ok": True,
        "neuron_ids": json.dumps([hot["id"]]),
    }] * 5                                       # hot：30 天 5 次精确召回
    loop, relational = _loop([hot, cold], rows)
    result = loop.predictive_value_sweep(now_ms=_NOW)
    # 5 hits → usage=0.5 → value≈0.4 > 0.2：不进 archive
    assert hot["id"] not in result["archived"]
    assert result["recall_hits_30d"][hot["id"]] == 5
    # 0 hits（recall_count 回退=0）→ value=0.2 ≤ 0.2：进 archive
    assert cold["id"] in result["archived"]
    assert ("archive" == dict(relational.tier_changes).get(cold["id"]))


def test_sweep_old_format_rows_fall_back_to_recall_count(tmp_path) -> None:
    stale_ms = _NOW - 200 * _MS_PER_DAY
    only_old = _neuron_record("只出现在旧格式行", recall_count=0, last_recall_ms=stale_ms, stability=1.0)
    rows = [{                                    # 旧格式：无 neuron_ids 键
        "ts_ms": _NOW - _MS_PER_DAY, "agent_id": "a1", "channel": "dense",
        "query": "q", "hits": 1, "latency_ms": 1.0, "ok": True,
    }] * 5
    loop, _rel = _loop([only_old], rows)
    result = loop.predictive_value_sweep(now_ms=_NOW)
    assert result["recall_hits_30d"] == {}       # 旧行不产生精确计数
    assert only_old["id"] in result["archived"]  # 回退 recall_count 平滑项，行为不变
