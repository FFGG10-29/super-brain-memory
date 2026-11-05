"""storage.time_series_db —— DuckDB 时序 OLAP（只写不改）。

三张表（架构 §四 + 情绪全链路 §存储）：
    pad_track(ts_ms, neuron_id, device_id, agent_id, p, a, d, source)  PAD 波动轨迹
    access_log(ts_ms, agent_id, channel, query, hits, latency_ms, ok)   召回/写入审计
    decay_event(ts_ms, neuron_id, strength, stability, retrievability, tier) 衰减快照

并发模型：DuckDB 单写多读，同进程多连接写同一文件会冲突 → 单连接 + 每线程 cursor
（见 .memory/DECISIONS.md ADR-004）。写路径再加一把锁，保证 append 原子。
"""
from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import duckdb

from core.errors import StorageError
from core.interfaces import HealthStatus, Record, TimeSeriesRepository, TimeSeriesTable

__all__ = ["DuckDBTimeSeries", "TABLE_COLUMNS"]

# 表 → (列定义, 实体过滤列)
TABLE_COLUMNS: dict[TimeSeriesTable, tuple[dict[str, str], str]] = {
    "pad_track": (
        {
            "ts_ms": "BIGINT",
            "neuron_id": "VARCHAR",
            "device_id": "VARCHAR",
            "agent_id": "VARCHAR",
            "p": "DOUBLE",
            "a": "DOUBLE",
            "d": "DOUBLE",
            "source": "VARCHAR",
        },
        "neuron_id",
    ),
    "access_log": (
        {
            "ts_ms": "BIGINT",
            "agent_id": "VARCHAR",
            "channel": "VARCHAR",
            "query": "VARCHAR",
            "hits": "INTEGER",
            "latency_ms": "DOUBLE",
            "ok": "BOOLEAN",
        },
        "agent_id",
    ),
    "decay_event": (
        {
            "ts_ms": "BIGINT",
            "neuron_id": "VARCHAR",
            "strength": "DOUBLE",
            "stability": "DOUBLE",
            "retrievability": "DOUBLE",
            "tier": "VARCHAR",
        },
        "neuron_id",
    ),
}

_FORBIDDEN_KEYWORDS = ("insert", "update", "delete", "drop", "alter", "create", "copy", "attach", "vacuum")


class DuckDBTimeSeries(TimeSeriesRepository):
    """DuckDB 实现。只允许 append + 只读分析，禁 UPDATE/DELETE。"""

    name = "timeseries:duckdb"

    def __init__(self, path: str | Path, *, threads: int = 2) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(str(self._path))
        self._conn.execute(f"PRAGMA threads={int(threads)}")
        self._write_lock = threading.Lock()
        self._cursors: dict[int, Any] = {}
        self._cursor_lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        for table, (columns, _) in TABLE_COLUMNS.items():
            cols = ", ".join(f"{name} {dtype}" for name, dtype in columns.items())
            self._conn.execute(f"CREATE TABLE IF NOT EXISTS {table}({cols})")

    def _cursor(self) -> Any:
        """每线程一个 cursor（DuckDB 官方线程隔离手段）。"""
        key = threading.get_ident()
        with self._cursor_lock:
            cursor = self._cursors.get(key)
            if cursor is None:
                cursor = self._conn.cursor()
                self._cursors[key] = cursor
            return cursor

    def _spec(self, table: TimeSeriesTable) -> tuple[dict[str, str], str]:
        try:
            return TABLE_COLUMNS[table]
        except KeyError as exc:
            raise StorageError(f"未知时序表: {table}") from exc

    # ── 写入（只 append）────────────────────────────────────────────────
    def append(self, table: TimeSeriesTable, rows: Sequence[Record]) -> None:
        if not rows:
            return
        columns, _ = self._spec(table)
        names = [name for name in columns if any(name in row for row in rows)]
        if not names:
            raise StorageError(f"{table} 的写入行不含任何已知列")
        placeholders = ", ".join("?" for _ in names)
        sql = f"INSERT INTO {table}({', '.join(names)}) VALUES ({placeholders})"
        values = [tuple(row.get(name) for name in names) for row in rows]
        with self._write_lock:
            self._conn.executemany(sql, values)

    # ── 读取 ──────────────────────────────────────────────────────────────
    def fetch(
        self,
        table: TimeSeriesTable,
        *,
        since_ms: int | None = None,
        until_ms: int | None = None,
        entity_id: str | None = None,
        limit: int = 1000,
    ) -> list[Record]:
        columns, entity_column = self._spec(table)
        clauses: list[str] = []
        params: list[Any] = []
        if since_ms is not None:
            clauses.append("ts_ms >= ?")
            params.append(int(since_ms))
        if until_ms is not None:
            clauses.append("ts_ms <= ?")
            params.append(int(until_ms))
        if entity_id is not None:
            clauses.append(f"{entity_column} = ?")
            params.append(entity_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT {', '.join(columns)} FROM {table}{where} ORDER BY ts_ms LIMIT ?"
        result = self._cursor().execute(sql, [*params, int(limit)])
        names = list(columns)
        return [dict(zip(names, row, strict=False)) for row in result.fetchall()]

    def query(self, sql: str, params: Sequence[Any] | None = None) -> list[Record]:
        head = sql.lstrip().lower()
        if not head.startswith("select") and not head.startswith("with"):
            raise StorageError("时序分析入口只读：仅允许 SELECT / WITH 语句")
        for keyword in _FORBIDDEN_KEYWORDS:
            if f" {keyword} " in f" {head} ":
                raise StorageError(f"只读入口拒绝包含 {keyword.upper()} 的语句")
        result = self._cursor().execute(sql, list(params or []))
        names = [desc[0] for desc in result.description]
        return [dict(zip(names, row, strict=False)) for row in result.fetchall()]

    def count(self, table: TimeSeriesTable) -> int:
        _ = self._spec(table)  # 校验表名合法，防止 f-string 注入
        row = self._cursor().execute(f"SELECT count(*) FROM {table}").fetchone()
        return int(row[0]) if row else 0

    # ── 健康检查 ──────────────────────────────────────────────────────────
    def health(self) -> HealthStatus:
        started = time.perf_counter()
        try:
            counts = {table: self.count(table) for table in TABLE_COLUMNS}
            latency = (time.perf_counter() - started) * 1000
            return HealthStatus(
                engine=self.name,
                ok=True,
                latency_ms=latency,
                version=getattr(duckdb, "__version__", ""),
                detail="append-only",
                extra={"path": str(self._path), "counts": counts},
            )
        except Exception as exc:  # noqa: BLE001
            return HealthStatus(
                engine=self.name,
                ok=False,
                latency_ms=(time.perf_counter() - started) * 1000,
                detail=f"{type(exc).__name__}: {exc}",
            )

    def close(self) -> None:
        with self._cursor_lock:
            self._cursors.clear()
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass
