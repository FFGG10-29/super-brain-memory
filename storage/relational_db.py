"""storage.relational_db —— SQLite(WAL) 元数据主库。

角色（架构 §四）：唯一事实源，写入先落此库；六引擎分发靠 outbox 表。
- 三张主表：neurons / synapses / hyperedges，双时态列 + 晋升列 + payload(JSON 全量)
- payload 才是权威记录，promoted 列只服务于过滤/排序/索引（避免频繁 ALTER）
- FTS5 覆盖神经元正文与超边标签；CJK 靠 LIKE 兜底（trigram 需 ≥3 字，故不启用）
- applied_events 表给"重放幂等"提供硬去重（架构 §8.4 验收③）
- schema_version 自建版本号表 + 启动校验（架构 §十三 风险 1）

并发：thread-local 连接（ThreadLocalPool），WAL + busy_timeout，事务用 `with conn:`。
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.errors import SchemaVersionMismatch, StorageError
from core.ids import HLCStamp
from core.interfaces import (
    EntityKind,
    HealthStatus,
    OutboxItem,
    Record,
    RelationalRepository,
    Tier,
)
from storage import ThreadLocalPool

__all__ = ["SqliteRelationalDB", "KIND_SPEC"]

_COMPONENT = "relational"
_OUTBOX_MAX_ATTEMPTS_DEFAULT = 3


@dataclass(frozen=True)
class _KindSpec:
    table: str
    columns: Mapping[str, str]   # promoted 列 → sqlite 类型
    fts_field: str | None        # payload 中参与全文索引的字段


KIND_SPEC: Mapping[EntityKind, _KindSpec] = {
    "neuron": _KindSpec(
        table="neurons",
        columns={
            "device_id": "TEXT",
            "agent_id": "TEXT",
            "memory_type": "TEXT",
            "namespace": "TEXT",
            "tier": "TEXT",
            "pyramid": "TEXT",
            "confidence": "REAL",
            "affinity": "REAL",
            "pad_p": "REAL",
            "pad_a": "REAL",
            "pad_d": "REAL",
            "strength": "REAL",
            "stability": "REAL",
            "valid_from_ms": "INTEGER",
            "valid_to_ms": "INTEGER",
            "tx_from": "TEXT",
            "tx_to": "TEXT",
            "is_active": "INTEGER",
        },
        fts_field="content",
    ),
    "synapse": _KindSpec(
        table="synapses",
        columns={
            "device_id": "TEXT",
            "agent_id": "TEXT",
            "subject_id": "TEXT",
            "predicate": "TEXT",
            "object_id": "TEXT",
            "namespace": "TEXT",
            "tier": "TEXT",
            "weight": "REAL",
            "polarity": "REAL",
            "valid_from_ms": "INTEGER",
            "valid_to_ms": "INTEGER",
            "tx_from": "TEXT",
            "tx_to": "TEXT",
            "is_active": "INTEGER",
        },
        fts_field=None,
    ),
    "hyperedge": _KindSpec(
        table="hyperedges",
        columns={
            "device_id": "TEXT",
            "agent_id": "TEXT",
            "label": "TEXT",
            "namespace": "TEXT",
            "tier": "TEXT",
            "pad_p": "REAL",
            "pad_a": "REAL",
            "pad_d": "REAL",
            "occurred_at_ms": "INTEGER",
            "valid_from_ms": "INTEGER",
            "valid_to_ms": "INTEGER",
            "tx_from": "TEXT",
            "tx_to": "TEXT",
            "is_active": "INTEGER",
        },
        fts_field="label",
    ),
}


def _json_default(value: Any) -> Any:
    if isinstance(value, HLCStamp):
        return value.to_token()
    raise TypeError(f"无法序列化 {type(value)!r}")


def _now_ms() -> int:
    return int(time.time() * 1000)


class SqliteRelationalDB(RelationalRepository):
    """SQLite 实现。记录以 payload JSON 为权威，promoted 列仅用于查询。"""

    name = "relational:sqlite"

    def __init__(
        self,
        path: str | Path,
        *,
        schema_version: int = 1,
        busy_timeout_ms: int = 5000,
        wal_autocheckpoint: int = 1000,
    ) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_version = int(schema_version)
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._wal_autocheckpoint = int(wal_autocheckpoint)
        self._pool = ThreadLocalPool(self._connect, name="sqlite")
        self._init_schema()

    # ── 连接与建表 ────────────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), timeout=self._busy_timeout_ms / 1000.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        conn.execute(f"PRAGMA wal_autocheckpoint={self._wal_autocheckpoint}")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_version("
                    "component TEXT PRIMARY KEY, version INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL)"
                )
                for spec in KIND_SPEC.values():
                    cols = ", ".join(f"{name} {dtype}" for name, dtype in spec.columns.items())
                    conn.execute(
                        f"CREATE TABLE IF NOT EXISTS {spec.table}("
                        f"id TEXT PRIMARY KEY, {cols}, payload TEXT NOT NULL, updated_at_ms INTEGER NOT NULL)"
                    )
                    conn.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_{spec.table}_active "
                        f"ON {spec.table}(is_active, namespace, tier)"
                    )
                    # query() 恒按 updated_at_ms DESC 排序取最近若干条；没有这个索引时
                    # 10 万条会全表排序，星云构图的 SQL 部分从 O(log n) 退化成 O(n log n)。
                    conn.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_{spec.table}_updated "
                        f"ON {spec.table}(updated_at_ms DESC)"
                    )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS outbox("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "event_id TEXT NOT NULL, target TEXT NOT NULL, payload TEXT NOT NULL,"
                    "state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,"
                    "last_error TEXT, created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL)"
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_state ON outbox(state, id)")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS applied_events("
                    "event_id TEXT PRIMARY KEY, applied_at_ms INTEGER NOT NULL)"
                )
                self._init_fts(conn)
            self._check_schema_version(conn)
        finally:
            conn.close()

    def _init_fts(self, conn: sqlite3.Connection) -> None:
        for kind, spec in KIND_SPEC.items():
            if not spec.fts_field:
                continue
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {spec.table}_fts "
                f"USING fts5(id UNINDEXED, body, tokenize='unicode61')"
            )

    def _check_schema_version(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT version FROM schema_version WHERE component=?", (_COMPONENT,)
        ).fetchone()
        if row is None:
            with conn:
                conn.execute(
                    "INSERT INTO schema_version(component, version, updated_at_ms) VALUES (?,?,?)",
                    (_COMPONENT, self._schema_version, _now_ms()),
                )
            return
        if int(row["version"]) != self._schema_version:
            raise SchemaVersionMismatch(
                f"SQLite schema 版本不一致：库内 {row['version']} / 代码 {self._schema_version}；"
                "需迁移或走快照重建"
            )

    # ── 实体读写 ──────────────────────────────────────────────────────────
    def _spec(self, kind: EntityKind) -> _KindSpec:
        try:
            return KIND_SPEC[kind]
        except KeyError as exc:  # pragma: no cover - 类型已限定，防御式
            raise StorageError(f"未知实体类型: {kind}") from exc

    def upsert(self, kind: EntityKind, record: Record) -> None:
        spec = self._spec(kind)
        entity_id = str(record.get("id") or record.get("uuid7_id") or "")
        if not entity_id:
            raise StorageError(f"{kind} 记录缺少 id")
        data = dict(record)
        data["id"] = entity_id
        payload = json.dumps(data, ensure_ascii=False, default=_json_default)

        promoted = [name for name in spec.columns if name in data]
        columns = ["id", *promoted, "payload", "updated_at_ms"]
        values: list[Any] = [entity_id, *[self._coerce(data[name]) for name in promoted], payload, _now_ms()]
        placeholders = ", ".join("?" for _ in columns)
        assignments = ", ".join(f"{name}=excluded.{name}" for name in columns if name != "id")
        sql = (
            f"INSERT INTO {spec.table}({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {assignments}"
        )

        conn = self._pool.get()
        with conn:
            conn.execute(sql, values)
            self._sync_fts(conn, spec, entity_id, data)

    @staticmethod
    def _coerce(value: Any) -> Any:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, HLCStamp):
            return value.to_token()
        return value

    def _sync_fts(self, conn: sqlite3.Connection, spec: _KindSpec, entity_id: str, data: Mapping[str, Any]) -> None:
        if not spec.fts_field:
            return
        table = f"{spec.table}_fts"
        conn.execute(f"DELETE FROM {table} WHERE id=?", (entity_id,))
        body = data.get(spec.fts_field)
        if body:
            conn.execute(f"INSERT INTO {table}(id, body) VALUES (?,?)", (entity_id, str(body)))

    def get(self, kind: EntityKind, entity_id: str) -> Record | None:
        spec = self._spec(kind)
        row = self._pool.get().execute(
            f"SELECT payload FROM {spec.table} WHERE id=?", (entity_id,)
        ).fetchone()
        return None if row is None else json.loads(row["payload"])

    def deactivate(
        self,
        kind: EntityKind,
        entity_id: str,
        *,
        valid_to_ms: int,
        tx_to: HLCStamp,
    ) -> None:
        spec = self._spec(kind)
        conn = self._pool.get()
        with conn:
            row = conn.execute(f"SELECT payload FROM {spec.table} WHERE id=?", (entity_id,)).fetchone()
            if row is None:
                raise StorageError(f"{kind} 不存在，无法封存: {entity_id}")
            data = json.loads(row["payload"])
            data["valid_to_ms"] = int(valid_to_ms)
            data["tx_to"] = tx_to.to_token()
            data["is_active"] = False
            conn.execute(
                f"UPDATE {spec.table} SET valid_to_ms=?, tx_to=?, is_active=0, payload=?, updated_at_ms=? "
                f"WHERE id=?",
                (int(valid_to_ms), tx_to.to_token(), json.dumps(data, ensure_ascii=False, default=_json_default),
                 _now_ms(), entity_id),
            )
            self._sync_fts(conn, spec, entity_id, data)

    def set_tier(self, kind: EntityKind, entity_id: str, tier: Tier) -> None:
        spec = self._spec(kind)
        conn = self._pool.get()
        with conn:
            row = conn.execute(f"SELECT payload FROM {spec.table} WHERE id=?", (entity_id,)).fetchone()
            if row is None:
                raise StorageError(f"{kind} 不存在: {entity_id}")
            data = json.loads(row["payload"])
            data["tier"] = tier
            conn.execute(
                f"UPDATE {spec.table} SET tier=?, payload=?, updated_at_ms=? WHERE id=?",
                (tier, json.dumps(data, ensure_ascii=False, default=_json_default), _now_ms(), entity_id),
            )

    def query(
        self,
        kind: EntityKind,
        *,
        namespaces: Sequence[str] = (),
        tiers: Sequence[str] = (),
        memory_types: Sequence[str] = (),
        active_only: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Record]:
        spec = self._spec(kind)
        clauses: list[str] = []
        params: list[Any] = []
        if active_only:
            clauses.append("is_active=1")
        if namespaces:
            clauses.append(f"namespace IN ({', '.join('?' for _ in namespaces)})")
            params.extend(namespaces)
        if tiers:
            clauses.append(f"tier IN ({', '.join('?' for _ in tiers)})")
            params.extend(tiers)
        if memory_types and "memory_type" in spec.columns:
            clauses.append(f"memory_type IN ({', '.join('?' for _ in memory_types)})")
            params.extend(memory_types)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT payload FROM {spec.table}{where} ORDER BY updated_at_ms DESC LIMIT ? OFFSET ?"
        rows = self._pool.get().execute(sql, [*params, int(limit), int(offset)]).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def full_text_search(
        self,
        kind: EntityKind,
        query: str,
        *,
        limit: int = 20,
        namespaces: Sequence[str] = (),
    ) -> list[Record]:
        spec = self._spec(kind)
        conn = self._pool.get()
        scope = ""
        params: list[object] = []
        if namespaces:
            scope = f" AND namespace IN ({', '.join('?' for _ in namespaces)})"
            params.extend(str(item) for item in namespaces)
        if not spec.fts_field:
            # 无全文列（突触）：退化为 payload 子串匹配，保证接口语义一致
            rows = conn.execute(
                f"SELECT payload FROM {spec.table} WHERE payload LIKE ? AND is_active=1{scope} LIMIT ?",
                [f"%{query}%", *params, int(limit)],
            ).fetchall()
            return [json.loads(row["payload"]) for row in rows]

        try:
            rows = conn.execute(
                f"SELECT t.payload FROM {spec.table}_fts f "
                f"JOIN {spec.table} t ON t.id = f.id "
                f"WHERE {spec.table}_fts MATCH ? AND t.is_active=1{scope} LIMIT ?",
                [query, *params, int(limit)],
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        if rows:
            return [json.loads(row["payload"]) for row in rows]

        # CJK 兜底：unicode61 对中文按整段切词，两字查询常常命中 0 条 → 对索引列做 LIKE 补搜
        rows = conn.execute(
            f"SELECT t.payload FROM {spec.table}_fts f JOIN {spec.table} t ON t.id = f.id "
            f"WHERE f.body LIKE ? AND t.is_active=1{scope} LIMIT ?",
            [f"%{query}%", *params, int(limit)],
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def count(self, kind: EntityKind, *, active_only: bool = True) -> int:
        spec = self._spec(kind)
        where = " WHERE is_active=1" if active_only else ""
        row = self._pool.get().execute(f"SELECT count(*) AS c FROM {spec.table}{where}").fetchone()
        return int(row["c"])

    # ── outbox ────────────────────────────────────────────────────────────
    def outbox_enqueue(self, event_id: str, target: str, payload: Record) -> None:
        now = _now_ms()
        conn = self._pool.get()
        with conn:
            conn.execute(
                "INSERT INTO outbox(event_id, target, payload, state, attempts, created_at_ms, updated_at_ms) "
                "VALUES (?,?,?,'pending',0,?,?)",
                (event_id, target, json.dumps(payload, ensure_ascii=False, default=_json_default), now, now),
            )

    def outbox_claim(self, limit: int = 100) -> list[OutboxItem]:
        conn = self._pool.get()
        with conn:
            rows = conn.execute(
                "SELECT id, event_id, target, payload, attempts FROM outbox "
                "WHERE state='pending' ORDER BY id LIMIT ?",
                (int(limit),),
            ).fetchall()
            ids = [row["id"] for row in rows]
            if ids:
                conn.execute(
                    f"UPDATE outbox SET state='dispatched', updated_at_ms=? "
                    f"WHERE id IN ({', '.join('?' for _ in ids)})",
                    [_now_ms(), *ids],
                )
        return [
            OutboxItem(
                outbox_id=int(row["id"]),
                event_id=row["event_id"],
                target=row["target"],
                payload=json.loads(row["payload"]),
                attempts=int(row["attempts"]),
            )
            for row in rows
        ]

    def outbox_ack(self, outbox_ids: Sequence[int]) -> None:
        if not outbox_ids:
            return
        conn = self._pool.get()
        with conn:
            conn.execute(
                f"UPDATE outbox SET state='acked', updated_at_ms=? "
                f"WHERE id IN ({', '.join('?' for _ in outbox_ids)})",
                [_now_ms(), *[int(i) for i in outbox_ids]],
            )

    def outbox_fail(self, outbox_id: int, error: str, *, max_attempts: int = _OUTBOX_MAX_ATTEMPTS_DEFAULT) -> None:
        conn = self._pool.get()
        with conn:
            row = conn.execute("SELECT attempts FROM outbox WHERE id=?", (int(outbox_id),)).fetchone()
            if row is None:
                return
            attempts = int(row["attempts"]) + 1
            state = "pending" if attempts < max_attempts else "dead"
            conn.execute(
                "UPDATE outbox SET state=?, attempts=?, last_error=?, updated_at_ms=? WHERE id=?",
                (state, attempts, error[:500], _now_ms(), int(outbox_id)),
            )

    def outbox_pending_count(self) -> int:
        row = self._pool.get().execute("SELECT count(*) AS c FROM outbox WHERE state='pending'").fetchone()
        return int(row["c"])

    # ── 重放幂等 ──────────────────────────────────────────────────────────
    def mark_event_applied(self, event_id: str) -> bool:
        conn = self._pool.get()
        with conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO applied_events(event_id, applied_at_ms) VALUES (?,?)",
                (event_id, _now_ms()),
            )
            return cursor.rowcount == 1

    # ── 健康检查 ──────────────────────────────────────────────────────────
    def health(self) -> HealthStatus:
        started = time.perf_counter()
        try:
            conn = self._pool.get()
            conn.execute("SELECT 1").fetchone()
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            version = conn.execute(
                "SELECT version FROM schema_version WHERE component=?", (_COMPONENT,)
            ).fetchone()
            counts = {
                kind: conn.execute(f"SELECT count(*) FROM {spec.table}").fetchone()[0]
                for kind, spec in KIND_SPEC.items()
            }
            latency = (time.perf_counter() - started) * 1000
            return HealthStatus(
                engine=self.name,
                ok=True,
                latency_ms=latency,
                version=str(version["version"]) if version else "?",
                detail=f"journal={journal_mode}",
                extra={
                    "path": str(self._path),
                    "counts": counts,
                    "outbox_pending": self.outbox_pending_count(),
                },
            )
        except Exception as exc:  # noqa: BLE001  健康检查不抛异常
            return HealthStatus(
                engine=self.name,
                ok=False,
                latency_ms=(time.perf_counter() - started) * 1000,
                detail=f"{type(exc).__name__}: {exc}",
            )

    def close(self) -> None:
        self._pool.close()
