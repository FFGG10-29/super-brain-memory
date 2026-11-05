"""storage.disabled —— 引擎开关关闭后的空实现占位。

语义（与架构一致：事件日志是唯一真源）：
- 写操作 no-op：不抛异常，写入流照常走，事件仍进 journal；
  重新打开该引擎后可用「事件重放 / 快照重建」把数据补回来。
- 读操作返回空：召回少一条通道（RRF 自动降权），画像/KV 读不到默认值。
- health 返回 ok=True + detail 标注「已关闭」，自检不因此失败。
- relational（唯一事实源）不提供关闭开关，组合根强制启用。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from core.interfaces import (
    GraphRepository,
    HealthCheckable,
    HealthStatus,
    HLCStamp,
    HypergraphRepository,
    KVRepository,
    OutboxItem,
    RecallFilter,
    Record,
    ScoredId,
    TimeSeriesRepository,
    VectorRecord,
    VectorRepository,
)

__all__ = [
    "DisabledGraph",
    "DisabledHypergraph",
    "DisabledKV",
    "DisabledTimeSeries",
    "DisabledVector",
]

_DETAIL = "已关闭（engines_enabled=False；事件日志可重放重建）"


def _health(name: str) -> HealthStatus:
    return HealthStatus(engine=name, ok=True, latency_ms=0.0, detail=_DETAIL)


class DisabledVector(VectorRepository):  # type: ignore[misc]
    @property
    def dense_dim(self) -> int:
        return 0

    def health(self) -> HealthStatus:
        return _health("vector:lancedb")

    def upsert(self, records: Sequence[VectorRecord]) -> None: ...

    def delete(self, ids: Sequence[str]) -> None: ...

    def search_dense(self, vector, *, limit, where=None) -> list[ScoredId]:
        return []

    def search_sparse(self, weights, *, limit, where=None) -> list[ScoredId]:
        return []

    def search_text(self, query, *, limit, where=None) -> list[ScoredId]:
        return []

    def count(self) -> int:
        return 0


class DisabledGraph(GraphRepository):  # type: ignore[misc]
    def health(self) -> HealthStatus:
        return _health("graph:kuzu")

    def upsert_entity(self, entity_id, *, name, entity_type="concept", props=None) -> None: ...

    def upsert_hyperedge_node(self, hyperedge_id, *, label, occurred_at_ms) -> None: ...

    def add_relation(self, subject_id, predicate, object_id, *, weight=1.0, tx_from=None) -> None: ...

    def add_member(self, hyperedge_id, entity_id, *, weight=1.0) -> None: ...

    def supersede(self, new_id, old_id, *, tx_from: HLCStamp) -> None: ...

    def neighbors(self, entity_id, *, hops=1, limit=20) -> list[Record]:
        return []

    def members(self, hyperedge_id) -> list[str]:
        return []

    def hyperedges_of(self, entity_id, *, limit=20) -> list[str]:
        return []

    def count_nodes(self, label="Entity") -> int:
        return 0

    def count_edges(self, rel="RELATES") -> int:
        return 0


class DisabledKV(KVRepository):  # type: ignore[misc]
    def health(self) -> HealthStatus:
        return _health("kv:lmdb")

    def put(self, bucket: str, key: str, value: bytes) -> None: ...

    def get(self, bucket: str, key: str) -> bytes | None:
        return None

    def delete(self, bucket: str, key: str) -> None: ...

    def scan_prefix(self, bucket: str, prefix: str, *, limit: int = 100) -> list[tuple[str, bytes]]:
        return []

    def incr(self, bucket: str, key: str, *, ttl_ms: int | None = None) -> int:
        return 0


class DisabledTimeSeries(TimeSeriesRepository):  # type: ignore[misc]
    def health(self) -> HealthStatus:
        return _health("timeseries:duckdb")

    def append(self, table, rows) -> None: ...

    def fetch(self, table, *, since_ms=None, until_ms=None, entity_id=None, limit=1000) -> list[Record]:
        return []

    def query(self, sql: str, params=None) -> list[Record]:
        return []

    def count(self, table) -> int:
        return 0


class DisabledHypergraph(HypergraphRepository):  # type: ignore[misc]
    def health(self) -> HealthStatus:
        return _health("hypergraph:logic")

    def add(self, hyperedge: Record) -> None: ...

    def members(self, hyperedge_id: str) -> list[str]:
        return []

    def hyperedges_of(self, entity_id: str, *, limit: int = 20) -> list[str]:
        return []

    def incidence(self, hyperedge_ids=None) -> Mapping[str, list[str]]:
        return {}
