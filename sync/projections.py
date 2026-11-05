"""sync.projections —— 投影器：事件 → 六引擎（全量重建入口）。

幂等靠两道闸：event_id 去重表（relational.mark_event_applied）+ 所有写操作都是
upsert / MERGE / append-only 同键覆盖，重复应用不产生副作用。
可塑性变量在投影时确定性计算，绝不读随机态（架构 §8.2）。
"""
from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass

from core.errors import EngineUnavailable, StorageError
from core.interfaces import (
    Embedder,
    GraphRepository,
    HypergraphRepository,
    KVRepository,
    Projector,
    RelationalRepository,
    TimeSeriesRepository,
    VectorRecord,
    VectorRepository,
)

__all__ = ["EventProjector", "ProjectionReport"]


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class ProjectionReport:
    applied: int = 0
    skipped: int = 0
    failed: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.failed is None:
            self.failed = {}


class EventProjector(Projector):
    def __init__(
        self,
        *,
        relational: RelationalRepository,
        vector: VectorRepository,
        graph: GraphRepository,
        hypergraph: HypergraphRepository,
        timeseries: TimeSeriesRepository,
        kv: KVRepository,
        embedder: Embedder | None = None,
    ) -> None:
        self._relational = relational
        self._vector = vector
        self._graph = graph
        self._hypergraph = hypergraph
        self._timeseries = timeseries
        self._kv = kv
        self._embedder = embedder
        self.last_applied = False

    def apply(self, event) -> None:  # type: ignore[override]
        event_id = str(event.get("event_id", ""))
        if not event_id:
            raise StorageError("事件缺少 event_id")
        self.last_applied = self._relational.mark_event_applied(event_id)
        if not self.last_applied:
            return  # 幂等：重复事件直接跳过
        payload = dict(event.get("payload") or {})
        kind = str(event.get("type", ""))
        handler = getattr(self, f"_on_{kind}", None)
        if handler is None:
            raise StorageError(f"未知事件类型: {kind}")
        handler(payload, event)

    def rebuild_all(self, events: Iterable) -> ProjectionReport:  # type: ignore[override]
        """全量重建：调用方需指向全新空库；事件按 HLC 升序由 EventStore 保证。"""
        report = ProjectionReport()
        for event in events:
            try:
                self.apply(event)
                if self.last_applied:
                    report.applied += 1
                else:
                    report.skipped += 1
            except (StorageError, EngineUnavailable) as exc:
                assert report.failed is not None
                report.failed[str(event.get("event_id", "?"))] = f"{type(exc).__name__}: {exc}"
        return report

    # ── 各事件类型的投影 ──────────────────────────────────────────────────
    def _on_ingest(self, payload: dict, event: dict) -> None:
        neuron = dict(payload.get("neuron") or {})
        if not neuron.get("id"):
            raise StorageError("ingest 事件缺少 neuron")
        self._relational.upsert("neuron", neuron)
        self._timeseries.append(
            "pad_track",
            [
                {
                    "ts_ms": int(neuron.get("valid_from_ms") or _now_ms()),
                    "neuron_id": neuron["id"],
                    "device_id": str(neuron.get("device_id", "")),
                    "agent_id": str(neuron.get("agent_id", "")),
                    "p": float(neuron.get("pad_p", 0.0)),
                    "a": float(neuron.get("pad_a", 0.0)),
                    "d": float(neuron.get("pad_d", 0.0)),
                    "source": str((payload.get("extraction") or {}).get("source", "replay")),
                }
            ],
        )
        self._project_vector(neuron)
        extraction = payload.get("extraction") or {}
        for entity in extraction.get("entities") or []:
            name = str(entity.get("name", ""))
            if name:
                self._graph.upsert_entity(name, name=name, entity_type=str(entity.get("type", "concept")))
        if extraction.get("hyperedge_label"):
            self._hypergraph.add(
                {
                    "id": neuron["id"],
                    "label": extraction["hyperedge_label"],
                    "members": extraction.get("hyperedge_members") or [],
                    "occurred_at_ms": int(neuron.get("valid_from_ms") or 0),
                }
            )

    def _project_vector(self, neuron: dict) -> None:
        if self._embedder is None:
            return  # 可从主库重建，缺 embedder 不算失败
        try:
            embedding = self._embedder.embed_query(str(neuron.get("content", "")))
        except EngineUnavailable:
            return
        self._vector.upsert(
            [
                VectorRecord(
                    id=str(neuron["id"]),
                    dense=embedding.dense,
                    text=str(neuron.get("content", "")),
                    sparse=embedding.sparse,
                    metadata={
                        "is_active": bool(neuron.get("is_active", True)),
                        "namespace": neuron.get("namespace", "private"),
                        "tier": neuron.get("tier", "buffer"),
                        "memory_type": neuron.get("memory_type", "fact"),
                    },
                )
            ]
        )

    def _on_supersede(self, payload: dict, event: dict) -> None:
        from core.ids import HLCStamp

        old_id = str(payload.get("old_id", ""))
        new_id = str(payload.get("new_id", ""))
        if not old_id:
            return
        stamp = HLCStamp.from_token(str(event.get("hlc", f"{_now_ms()}:0:replay")))
        self._relational.deactivate("neuron", old_id, valid_to_ms=_now_ms(), tx_to=stamp)
        try:
            self._vector.delete([old_id])
        except Exception:  # noqa: BLE001
            pass
        if new_id:
            self._graph.upsert_entity(new_id, name=new_id, entity_type="fact")
            self._graph.upsert_entity(old_id, name=old_id, entity_type="fact")
            self._graph.supersede(new_id, old_id, tx_from=stamp)

    def _on_reinforce(self, payload: dict, event: dict) -> None:
        neuron_id = str(payload.get("neuron_id", ""))
        record = self._relational.get("neuron", neuron_id)
        if record is None:
            return
        updated = dict(record)
        for key in ("stability", "strength", "difficulty", "recall_count", "last_recall_ms"):
            if key in payload:
                updated[key] = payload[key]
        self._relational.upsert("neuron", updated)
        self._timeseries.append(
            "decay_event",
            [
                {
                    "ts_ms": int(payload.get("ts_ms") or _now_ms()),
                    "neuron_id": neuron_id,
                    "strength": float(updated.get("strength", 0.0)),
                    "stability": float(updated.get("stability", 0.0)),
                    "retrievability": float(payload.get("retrievability", 0.0)),
                    "tier": updated.get("tier", "buffer"),
                }
            ],
        )

    def _on_tier_change(self, payload: dict, event: dict) -> None:
        neuron_id = str(payload.get("neuron_id", ""))
        target = str(payload.get("to", ""))
        if neuron_id and target in ("buffer", "core", "recall", "archive"):
            self._relational.set_tier("neuron", neuron_id, target)  # type: ignore[arg-type]

    def _on_consolidate(self, payload: dict, event: dict) -> None:
        topic_id = str(payload.get("topic_id", ""))
        if not topic_id:
            return
        self._relational.upsert(
            "hyperedge",
            {
                "id": topic_id,
                "label": str(payload.get("label", "")),
                "members": list(payload.get("members") or []),
                "namespace": payload.get("namespace", "private"),
                "pyramid": payload.get("level", "topic"),
                "is_active": True,
                "valid_from_ms": _now_ms(),
                "tx_from": str(event.get("hlc", "")),
                "device_id": str(event.get("device_id", "")),
                "agent_id": "system",
            },
        )

    def _on_policy(self, payload: dict, event: dict) -> None:
        key = str(payload.get("key", ""))
        if not key:
            return
        bucket = str(payload.get("bucket") or "policy")
        # 核心记忆块是用户可编辑配置，重放时必须写回同一 bucket / 同一结构
        if bucket == "core_memory":
            self._kv.put_json(bucket, key, payload.get("blocks") or {})
        else:
            self._kv.put_json(bucket, key, payload)

    def _on_tombstone(self, payload: dict, event: dict) -> None:
        from core.ids import HLCStamp

        neuron_id = str(payload.get("neuron_id", ""))
        if neuron_id:
            self._relational.deactivate(
                "neuron", neuron_id, valid_to_ms=_now_ms(),
                tx_to=HLCStamp.from_token(str(event.get("hlc", f"{_now_ms()}:0:replay"))),
            )
            self._vector.delete([neuron_id])
