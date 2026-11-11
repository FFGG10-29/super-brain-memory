"""storage.graph_db —— Kùzu 图谱库（实体图谱 + 超边二部图编码）。

Schema（架构 §4.3 + §二 裁决 2）：
    Entity(id, name, type)              实体节点
    HyperEvent(id, label, occurred_at_ms)  超边 → 事件节点
    RELATES(FROM Entity TO Entity, predicate, weight, tx_from)
    MEMBER(FROM HyperEvent TO Entity, weight)   超边成员边
    SUPERSEDES(FROM Entity TO Entity, tx_from)  冲突消解演进边

一个进程只能有一个 kuzu.Database（文件锁），因此 HypergraphDB 复用本类的 Database
（见 .memory/DECISIONS.md ADR-003）。Connection 按线程隔离。
"""
from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import kuzu

from core.errors import StorageError
from core.ids import HLCStamp
from core.interfaces import GraphRepository, HealthStatus
from storage import ThreadLocalPool

__all__ = ["KuzuGraphDB"]

_NODE_LABELS = ("Entity", "HyperEvent")
_REL_LABELS = ("RELATES", "MEMBER", "SUPERSEDES")
_MAX_HOPS = 5


class KuzuGraphDB(GraphRepository):
    """Kùzu 实现。节点与关系一律 MERGE，保证重放幂等。"""

    name = "graph:kuzu"

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = kuzu.Database(str(self._path))
        self._pool = ThreadLocalPool(lambda: kuzu.Connection(self._db), name="kuzu")
        self._init_schema()

    # ── 建 schema ─────────────────────────────────────────────────────────
    def _init_schema(self) -> None:
        statements = (
            "CREATE NODE TABLE IF NOT EXISTS Entity("
            "id STRING, name STRING, type STRING, PRIMARY KEY(id))",
            "CREATE NODE TABLE IF NOT EXISTS HyperEvent("
            "id STRING, label STRING, occurred_at_ms INT64, PRIMARY KEY(id))",
            "CREATE REL TABLE IF NOT EXISTS RELATES("
            "FROM Entity TO Entity, predicate STRING, weight DOUBLE, tx_from STRING)",
            "CREATE REL TABLE IF NOT EXISTS MEMBER(FROM HyperEvent TO Entity, weight DOUBLE)",
            "CREATE REL TABLE IF NOT EXISTS SUPERSEDES("
            "FROM Entity TO Entity, tx_from STRING)",
        )
        conn = self._pool.get()
        for statement in statements:
            conn.execute(statement)

    # ── 写入 ──────────────────────────────────────────────────────────────
    def upsert_entity(
        self,
        entity_id: str,
        *,
        name: str,
        entity_type: str = "concept",
        props: Mapping[str, Any] | None = None,
    ) -> None:
        if not entity_id:
            raise StorageError("entity_id 不能为空")
        self._pool.get().execute(
            "MERGE (e:Entity {id:$id}) "
            "ON CREATE SET e.name=$name, e.type=$type "
            "ON MATCH SET e.name=$name, e.type=$type",
            {"id": entity_id, "name": name, "type": entity_type},
        )

    def upsert_hyperedge_node(self, hyperedge_id: str, *, label: str, occurred_at_ms: int) -> None:
        self._pool.get().execute(
            "MERGE (h:HyperEvent {id:$id}) "
            "ON CREATE SET h.label=$label, h.occurred_at_ms=$ts "
            "ON MATCH SET h.label=$label, h.occurred_at_ms=$ts",
            {"id": hyperedge_id, "label": label, "ts": int(occurred_at_ms)},
        )

    def add_relation(
        self,
        subject_id: str,
        predicate: str,
        object_id: str,
        *,
        weight: float = 1.0,
        tx_from: HLCStamp | None = None,
    ) -> None:
        self._ensure_entities(subject_id, object_id)
        self._pool.get().execute(
            "MATCH (a:Entity {id:$s}), (b:Entity {id:$o}) "
            "MERGE (a)-[r:RELATES {predicate:$p}]->(b) "
            "ON CREATE SET r.weight=$w, r.tx_from=$tx "
            "ON MATCH SET r.weight=$w",
            {
                "s": subject_id,
                "o": object_id,
                "p": predicate,
                "w": float(weight),
                "tx": (tx_from or HLCStamp(0, 0, "")).to_token(),
            },
        )

    def add_member(self, hyperedge_id: str, entity_id: str, *, weight: float = 1.0) -> None:
        self._ensure_entities(entity_id)
        self._pool.get().execute(
            "MATCH (h:HyperEvent {id:$h}), (e:Entity {id:$e}) "
            "MERGE (h)-[m:MEMBER]->(e) ON CREATE SET m.weight=$w ON MATCH SET m.weight=$w",
            {"h": hyperedge_id, "e": entity_id, "w": float(weight)},
        )

    def supersede(self, new_id: str, old_id: str, *, tx_from: HLCStamp) -> None:
        """冲突消解：新事实 → 旧事实的演进边（旧记录不物理删除）。"""
        self._ensure_entities(new_id, old_id)
        self._pool.get().execute(
            "MATCH (a:Entity {id:$new}), (b:Entity {id:$old}) "
            "MERGE (a)-[s:SUPERSEDES]->(b) ON CREATE SET s.tx_from=$tx ON MATCH SET s.tx_from=$tx",
            {"new": new_id, "old": old_id, "tx": tx_from.to_token()},
        )

    def supersede_edges_of(self, neuron_id: str, *, limit: int = 20) -> list[dict[str, str]]:
        """E2：以该 neuron 为一端的 SUPERSEDES 边对端（双向匹配）。"""
        result = self._pool.get().execute(
            "MATCH (a:Entity)-[s:SUPERSEDES]-(b:Entity) "
            "WHERE a.id = $id OR b.id = $id "
            "RETURN (CASE WHEN a.id = $id THEN b.id ELSE a.id END) AS counterpart "
            f"LIMIT {int(limit)}",
            {"id": neuron_id},
        )
        return [{"counterpart": str(row["counterpart"])} for row in self._rows(result)]

    def _ensure_entities(self, *entity_ids: str) -> None:
        """关系边要求两端存在；缺失先建占位节点（name=id），后续 upsert 会补全。"""
        conn = self._pool.get()
        for entity_id in entity_ids:
            conn.execute(
                "MERGE (e:Entity {id:$id}) ON CREATE SET e.name=$id, e.type='placeholder'",
                {"id": entity_id},
            )

    # ── 读取 ──────────────────────────────────────────────────────────────
    def neighbors(self, entity_id: str, *, hops: int = 1, limit: int = 20) -> list[Mapping[str, Any]]:
        if not 1 <= int(hops) <= _MAX_HOPS:
            raise StorageError(f"hops 必须在 1..{_MAX_HOPS}")
        conn = self._pool.get()
        result = conn.execute(
            f"MATCH (a:Entity {{id:$id}})-[r:RELATES*1..{int(hops)}]-(b:Entity) "
            f"WHERE b.id <> $id RETURN DISTINCT b.id AS id, b.name AS name, b.type AS type "
            f"LIMIT {int(limit)}",
            {"id": entity_id},
        )
        return self._rows(result)

    def members(self, hyperedge_id: str) -> list[str]:
        result = self._pool.get().execute(
            "MATCH (h:HyperEvent {id:$id})-[m:MEMBER]->(e:Entity) RETURN e.id AS id",
            {"id": hyperedge_id},
        )
        return [str(row["id"]) for row in self._rows(result)]

    def hyperedges_of(self, entity_id: str, *, limit: int = 20) -> list[str]:
        result = self._pool.get().execute(
            f"MATCH (h:HyperEvent)-[m:MEMBER]->(e:Entity {{id:$id}}) "
            f"RETURN DISTINCT h.id AS id LIMIT {int(limit)}",
            {"id": entity_id},
        )
        return [str(row["id"]) for row in self._rows(result)]

    def count_nodes(self, label: str = "Entity") -> int:
        if label not in _NODE_LABELS:
            raise StorageError(f"未知节点标签: {label}")
        result = self._pool.get().execute(f"MATCH (n:{label}) RETURN count(n) AS c")
        rows = self._rows(result)
        return int(rows[0]["c"]) if rows else 0

    def count_edges(self, rel: str = "RELATES") -> int:
        if rel not in _REL_LABELS:
            raise StorageError(f"未知关系标签: {rel}")
        result = self._pool.get().execute(f"MATCH ()-[r:{rel}]->() RETURN count(r) AS c")
        rows = self._rows(result)
        return int(rows[0]["c"]) if rows else 0

    def real_edges(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """★ C 包：读 Kùzu 真实关系边，保留方向 / 谓词 / 权重。

        前端星云旧版用「200 条两两 2-gram 现算」造 semantic 边，丢掉方向与谓词，
        这才是「关联记忆指向看不清」的根因。本方法只读，不改写入链、不碰单写锁。
        """
        conn = self._pool.get()
        cap = max(1, min(int(limit), 2000))
        edges: list[dict[str, Any]] = []
        try:
            rows = self._rows(conn.execute(
                "MATCH (a:Entity)-[r:RELATES]->(b:Entity) "
                "RETURN a.id AS source, a.name AS source_name, b.id AS target, "
                "b.name AS target_name, r.predicate AS predicate, r.weight AS weight "
                f"LIMIT {cap}"))
            for row in rows:
                edges.append({
                    "source": str(row.get("source", "")),
                    "source_name": str(row.get("source_name", "")),
                    "target": str(row.get("target", "")),
                    "target_name": str(row.get("target_name", "")),
                    "predicate": str(row.get("predicate", "")),
                    "weight": float(row.get("weight") or 0.5),
                    "kind": "RELATES",
                })
        except Exception:  # noqa: BLE001 - 关系表为空或 schema 版本差异时优雅降级
            pass
        try:
            rows = self._rows(conn.execute(
                "MATCH (h:HyperEvent)-[m:MEMBER]->(e:Entity) "
                "RETURN h.id AS source, h.label AS label, e.id AS target, "
                "e.name AS target_name, m.weight AS weight "
                f"LIMIT {cap}"))
            for row in rows:
                edges.append({
                    "source": str(row.get("source", "")),
                    "source_name": str(row.get("label", "")),
                    "target": str(row.get("target", "")),
                    "target_name": str(row.get("target_name", "")),
                    "predicate": "成员",
                    "weight": float(row.get("weight") or 0.5),
                    "kind": "MEMBER",
                })
        except Exception:  # noqa: BLE001
            pass
        return edges

    @staticmethod
    def _rows(result: Any) -> list[dict[str, Any]]:
        """kuzu 结果转 dict 列表（不用 get_as_df：它需要 pandas）。"""
        columns = list(result.get_column_names())
        rows: list[dict[str, Any]] = []
        while result.has_next():
            rows.append(dict(zip(columns, result.get_next(), strict=False)))
        return rows

    # ── 供 hypergraph_db 复用同一 Database ────────────────────────────────
    @property
    def database(self) -> kuzu.Database:
        return self._db

    def new_connection(self) -> kuzu.Connection:
        return kuzu.Connection(self._db)

    # ── 健康检查 ──────────────────────────────────────────────────────────
    def health(self) -> HealthStatus:
        started = time.perf_counter()
        try:
            entities = self.count_nodes("Entity")
            hyperevents = self.count_nodes("HyperEvent")
            latency = (time.perf_counter() - started) * 1000
            return HealthStatus(
                engine=self.name,
                ok=True,
                latency_ms=latency,
                version=str(getattr(kuzu, "__version__", "")),
                detail=f"entities={entities}",
                extra={"path": str(self._path), "hyperevents": hyperevents,
                       "relations": self.count_edges("RELATES"),
                       "members": self.count_edges("MEMBER")},
            )
        except Exception as exc:  # noqa: BLE001
            return HealthStatus(
                engine=self.name,
                ok=False,
                latency_ms=(time.perf_counter() - started) * 1000,
                detail=f"{type(exc).__name__}: {exc}",
            )

    def close(self) -> None:
        self._pool.close()
        try:
            self._db.close()
        except Exception:  # noqa: BLE001
            pass
