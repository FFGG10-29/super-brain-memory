"""storage.vector_db —— LanceDB 向量库。

三通道（架构 §四）：稠密 1024 / 稀疏 lexical weights / 原生 FTS。
关键规则：**标量预过滤先于向量检索**（架构 §二 裁决 5），因此每次检索都带 where。

实现取舍（见 .memory/DECISIONS.md ADR-006）：
- LanceDB 0.41 无原生稀疏索引 → 稀疏权重以 JSON 文本列存储，在预过滤后的候选集上线性打分。
- FTS 索引在建表时创建（`create_index(config=FTS())`，`create_fts_index` 已废弃）。
- payload 只存投影需要的标量列，正文存 text 列供 FTS；完整记录仍以 SQLite 为权威。
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import lancedb
import pyarrow as pa

from core.errors import SchemaVersionMismatch, StorageError
from core.interfaces import (
    HealthStatus,
    RecallFilter,
    ScoredId,
    VectorRecord,
    VectorRepository,
)

__all__ = ["LanceVectorDB"]

_TABLE = "neuron_vectors"
_SPARSE_FIELD = "sparse"


def _where_clause(where: RecallFilter | None) -> str:
    """把 RecallFilter 译成 LanceDB SQL 片段；空条件返回空串（不加 where）。"""
    if where is None:
        return ""
    clauses: list[str] = []
    if where.active_only:
        clauses.append("is_active = true")
    for column, values in (
        ("namespace", where.namespaces),
        ("tier", where.tiers),
        ("memory_type", where.memory_types),
    ):
        if values:
            quoted = ", ".join("'" + str(v).replace("'", "''") + "'" for v in values)
            clauses.append(f"{column} IN ({quoted})")
    return " AND ".join(clauses)


def _in_clause(ids: Sequence[str]) -> str:
    """生成 SQL IN 列表（转义单引号，id 虽为 UUID 也不能假设无特殊字符）。"""
    return ", ".join("'" + str(entity_id).replace("'", "''") + "'" for entity_id in ids)


def _sparse_dot(query: Mapping[str, float], doc: Mapping[str, float]) -> float:
    """稀疏向量打分：只在 query 的非零项上累加，避免全量遍历。"""
    if not query or not doc:
        return 0.0
    total = 0.0
    for token, q_weight in query.items():
        d_weight = doc.get(token)
        if d_weight:
            total += q_weight * d_weight
    return total


class LanceVectorDB(VectorRepository):
    """LanceDB 实现。写操作串行化（Lance 追加写不保证并发安全）。"""

    name = "vector:lancedb"

    def __init__(self, path: str | Path, *, dense_dim: int) -> None:
        if dense_dim <= 0:
            raise ValueError("dense_dim 必须为正数（读 config.model.dense_dim）")
        self._path = Path(path)
        self._path.mkdir(parents=True, exist_ok=True)
        self._dense_dim = int(dense_dim)
        self._write_lock = threading.Lock()
        self._db = lancedb.connect(str(self._path))
        self._schema = pa.schema(
            [
                pa.field("id", pa.string()),
                pa.field("dense", pa.list_(pa.float32(), self._dense_dim)),
                pa.field("text", pa.string()),
                pa.field(_SPARSE_FIELD, pa.string()),
                pa.field("namespace", pa.string()),
                pa.field("tier", pa.string()),
                pa.field("memory_type", pa.string()),
                pa.field("agent_id", pa.string()),
                pa.field("device_id", pa.string()),
                pa.field("is_active", pa.bool_()),
            ]
        )
        self._table = self._open_or_create()

    # ── 建表 / 维度校验 ───────────────────────────────────────────────────
    def _open_or_create(self):
        listing = self._db.list_tables()
        # lancedb 0.41 返回 TableListing（0.5x 返回 list[str]），两种都兼容
        names = getattr(listing, "tables", listing)
        existing = {str(name) for name in names}
        if _TABLE in existing:
            table = self._db.open_table(_TABLE)
            self._verify_dim(table)
            self._ensure_fts(table)
            return table
        table = self._db.create_table(_TABLE, schema=self._schema, exist_ok=True)
        self._ensure_fts(table)
        return table

    def _verify_dim(self, table) -> None:
        field = table.schema.field("dense")
        list_size = getattr(field.type, "list_size", None)
        if list_size is not None and int(list_size) != self._dense_dim:
            raise SchemaVersionMismatch(
                f"向量维度不一致：库内 {list_size} / config {self._dense_dim}；"
                "改维度必须走快照重建，不能原地迁移"
            )

    def _ensure_fts(self, table) -> None:
        try:
            from lancedb.index import FTS

            table.create_index("text", config=FTS(), replace=True)
        except Exception:  # noqa: BLE001  索引创建失败不应阻断启动，检索时再报
            pass

    @property
    def dense_dim(self) -> int:
        return self._dense_dim

    # ── 写入 ──────────────────────────────────────────────────────────────
    def upsert(self, records: Sequence[VectorRecord]) -> None:
        if not records:
            return
        rows: list[dict[str, Any]] = []
        ids: list[str] = []
        for record in records:
            if len(record.dense) != self._dense_dim:
                raise StorageError(
                    f"向量维度不符：{record.id} 是 {len(record.dense)}，期望 {self._dense_dim}"
                )
            meta = record.metadata
            ids.append(record.id)
            rows.append(
                {
                    "id": record.id,
                    "dense": [float(x) for x in record.dense],
                    "text": record.text,
                    _SPARSE_FIELD: json.dumps(dict(record.sparse), ensure_ascii=False),
                    "namespace": str(meta.get("namespace", "private")),
                    "tier": str(meta.get("tier", "buffer")),
                    "memory_type": str(meta.get("memory_type", "fact")),
                    "agent_id": str(meta.get("agent_id", "")),
                    "device_id": str(meta.get("device_id", "")),
                    "is_active": bool(meta.get("is_active", True)),
                }
            )
        with self._write_lock:
            self._table.delete(f"id IN ({_in_clause(ids)})")
            self._table.add(rows)

    def delete(self, ids: Sequence[str]) -> None:
        if not ids:
            return
        with self._write_lock:
            self._table.delete(f"id IN ({_in_clause(ids)})")

    # ── 检索 ──────────────────────────────────────────────────────────────
    def search_dense(
        self,
        vector: Sequence[float],
        *,
        limit: int,
        where: RecallFilter | None = None,
    ) -> list[ScoredId]:
        if len(vector) != self._dense_dim:
            raise StorageError(f"查询向量维度 {len(vector)} != {self._dense_dim}")
        query = self._table.search([float(x) for x in vector]).metric("cosine")  # pyright: ignore[reportAttributeAccessIssue]
        clause = _where_clause(where)
        if clause:
            query = query.where(clause, prefilter=True)
        hits = query.limit(int(limit)).to_list()
        # cosine 距离 ∈ [0,2] → 相似度 1-distance，便于与其他通道同量纲融合
        return [ScoredId(id=str(hit["id"]), score=1.0 - float(hit.get("_distance", 0.0)), channel="dense")
                for hit in hits]

    def search_sparse(
        self,
        weights: Mapping[str, float],
        *,
        limit: int,
        where: RecallFilter | None = None,
    ) -> list[ScoredId]:
        if not weights:
            return []
        scored: list[ScoredId] = []
        for row in self._scan(where):
            try:
                doc = json.loads(row.get(_SPARSE_FIELD) or "{}")
            except json.JSONDecodeError:
                continue
            score = _sparse_dot(weights, doc)
            if score > 0:
                scored.append(ScoredId(id=str(row["id"]), score=score, channel="sparse"))
        scored.sort(key=lambda hit: hit.score, reverse=True)
        return scored[: int(limit)]

    def search_text(
        self,
        query: str,
        *,
        limit: int,
        where: RecallFilter | None = None,
    ) -> list[ScoredId]:
        if not query.strip():
            return []
        clause = _where_clause(where)
        try:
            search = self._table.search(query, query_type="fts")
            if clause:
                search = search.where(clause, prefilter=True)
            hits = search.limit(int(limit)).to_list()
            return [ScoredId(id=str(hit["id"]), score=float(hit.get("_score", 0.0)), channel="fts")
                    for hit in hits]
        except Exception:  # noqa: BLE001  FTS 索引缺失时退化为子串匹配
            needle = query.lower()
            scored = [
                ScoredId(id=str(row["id"]), score=1.0, channel="fts")
                for row in self._scan(where)
                if needle in str(row.get("text", "")).lower()
            ]
            return scored[: int(limit)]

    def _scan(self, where: RecallFilter | None) -> list[dict[str, Any]]:
        """预过滤全表扫描（稀疏通道与 FTS 退化路径用）。"""
        table = self._table
        clause = _where_clause(where)
        try:
            if clause:
                table = table.search().where(clause, prefilter=True)
        except Exception:  # noqa: BLE001  search() 不接受空查询时直接用 to_arrow
            table = self._table
        try:
            rows = table.to_arrow().to_pylist()
        except Exception:  # noqa: BLE001
            rows = self._table.to_arrow().to_pylist()
        if not clause:
            return rows
        # 保底：SQL 片段万一没生效，再在内存里过滤一次（正确性优先于速度）
        active_only = where.active_only if where else True
        namespaces = set(where.namespaces) if where else set()
        tiers = set(where.tiers) if where else set()
        memory_types = set(where.memory_types) if where else set()
        filtered = []
        for row in rows:
            if active_only and not row.get("is_active", True):
                continue
            if namespaces and row.get("namespace") not in namespaces:
                continue
            if tiers and row.get("tier") not in tiers:
                continue
            if memory_types and row.get("memory_type") not in memory_types:
                continue
            filtered.append(row)
        return filtered

    def count(self) -> int:
        return int(self._table.count_rows())

    # ── 健康检查 ──────────────────────────────────────────────────────────
    def health(self) -> HealthStatus:
        started = time.perf_counter()
        try:
            rows = int(self._table.count_rows())
            latency = (time.perf_counter() - started) * 1000
            return HealthStatus(
                engine=self.name,
                ok=True,
                latency_ms=latency,
                version=str(self._table.version),
                detail=f"dim={self._dense_dim}",
                extra={"path": str(self._path), "rows": rows},
            )
        except Exception as exc:  # noqa: BLE001
            return HealthStatus(
                engine=self.name,
                ok=False,
                latency_ms=(time.perf_counter() - started) * 1000,
                detail=f"{type(exc).__name__}: {exc}",
            )

    def close(self) -> None:
        """LanceDB 无显式关闭接口；连接是进程级缓存，由解释器退出时回收。

        这里刻意不把 _table 置 None：置 None 会让"已关闭"状态与"未初始化"混淆，
        且 close() 之后 health()/count() 仍应可用（只读）。
        """
