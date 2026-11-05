"""brain.memory_editor —— 用户主导的记忆纠正 / 封存 / 遗忘。

与业界「可信记忆」模式对齐：记忆必须可见、可纠正、可遗忘，且纠正要保留原值。
实现遵循项目「事件日志是唯一真源」纪律，并**复用现有 7 类 EventType 与
EventProjector handler**，不新增事件契约，因此跨设备重放天然可用：

- 纠正 = 新版本 `ingest` 事件 + 旧版本 `supersede` 事件。
  `supersede.payload` 带 before / after / actor / reason，是就地可查的审计原值。
  绝不原地覆盖 payload —— 主库 upsert 与 vector.upsert 都是整行替换。
- 遗忘 = `tombstone` 事件：从所有推理路径移除（deactivate + vector.delete），
  主库保留封存行、journal 保留事件。对应 Simms 模式 07
  「hard delete from inference, soft delete from audit」，不做物理删除，
  否则跨设备重放会把已删记忆复活。

写入顺序固定为：journal.append(fsync) → 主库 → 派生库 / outbox → dispatch。
"""
from __future__ import annotations

import time
from typing import Any

from core.errors import StorageError
from core.ids import new_uuid7
from models.neuron import Neuron

__all__ = ["MemoryEditor"]

_EDITABLE = frozenset({
    "content", "memory_type", "tier", "pyramid", "namespace",
    "confidence", "affinity", "pad_p", "pad_a", "pad_d",
    "strength", "stability", "difficulty",
})


def _now() -> int:
    return int(time.time() * 1000)


class MemoryEditor:
    def __init__(self, *, app: Any) -> None:
        """app 是 BrainApp；显式持有，避免模块级单例。"""
        self._app = app
        self._relational = app.storage.relational
        self._vector = app.storage.vector
        self._graph = app.storage.graph
        self._journal = app.journal
        self._clock = app.clock
        self._hippocampus = app.hippocampus

    # ── 事件 ──────────────────────────────────────────────────────────────
    def _emit(self, kind: str, payload: dict[str, Any], *, agent_id: str = "user") -> str:
        event_id = new_uuid7()
        self._journal.append({
            "event_id": event_id,
            "agent_id": agent_id,
            "type": kind,
            "hlc": self._clock.tick().to_token(),
            "payload": payload,
        })
        return event_id

    # ── 纠正 ──────────────────────────────────────────────────────────────
    def correct(
        self,
        memory_id: str,
        *,
        changes: dict[str, Any],
        actor: str = "user",
        reason: str = "",
    ) -> dict[str, Any]:
        """新建版本 + 封存旧版本；原值写入 supersede 事件供审计。"""
        old = self._relational.get("neuron", memory_id)
        if old is None:
            return {"ok": False, "message": "记忆不存在"}
        if not old.get("is_active", True):
            return {"ok": False, "message": "该记忆已封存，不能再编辑"}
        patch = {key: value for key, value in changes.items() if key in _EDITABLE}
        if not patch:
            return {"ok": False, "message": "没有可修改的字段"}
        if "content" in patch and not str(patch["content"]).strip():
            return {"ok": False, "message": "正文不能为空"}

        now = _now()
        record = dict(old)
        record.update(patch)
        try:
            neuron = Neuron.from_record(record).model_copy(update={
                "uuid7_id": new_uuid7(now),
                "transaction_from": self._clock.tick().to_token(),
                "created_at_ms": now,
                "valid_from_ms": now,
                "valid_to_ms": None,
                "transaction_to": None,
                "is_active": True,
                "recall_count": 0,
                "last_recall_ms": 0,
            })
        except (ValueError, TypeError) as exc:
            return {"ok": False, "message": f"字段校验失败: {exc}"}
        new_record = neuron.to_record()

        # 1) 事件先落盘（fsync 后才有回执）
        ingest_id = self._emit("ingest", {
            "neuron": new_record,
            "extraction": {"source": "user_correction", "entities": [], "relations": []},
        }, agent_id=actor)
        supersede_id = self._emit("supersede", {
            "old_id": memory_id,
            "new_id": neuron.uuid7_id,
            "before": dict(old),
            "after": new_record,
            "changed": sorted(patch),
            "actor": actor,
            "reason": reason,
            "ts_ms": now,
        }, agent_id=actor)

        # 2) 主库：封存旧版本（双时态打点），写入新版本
        stamp = self._clock.tick()
        self._relational.deactivate("neuron", memory_id, valid_to_ms=now, tx_to=stamp)
        self._relational.upsert("neuron", new_record)

        # 3) 派生库：旧向量立即移出召回，新向量走 outbox 投影
        try:
            self._vector.delete([memory_id])
        except Exception:  # noqa: BLE001  向量库不可用时由 outbox 重建兜底
            pass
        for target in ("vector", "timeseries"):
            self._relational.outbox_enqueue(
                event_id=neuron.uuid7_id,
                target=target,
                payload={"neuron": new_record, "extraction": {}},
            )
        self._hippocampus.dispatch_outbox()
        try:
            self._graph.supersede(neuron.uuid7_id, memory_id, tx_from=stamp)
        except Exception:  # noqa: BLE001  图谱不可用不阻断纠正
            pass
        return {
            "ok": True,
            "id": neuron.uuid7_id,
            "superseded_id": memory_id,
            "event_ids": [ingest_id, supersede_id],
            "changed": sorted(patch),
            "message": f"已纠正并封存原值（{len(patch)} 个字段）",
        }

    # ── 封存 / 遗忘 ───────────────────────────────────────────────────────
    def forget(
        self,
        memory_id: str,
        *,
        actor: str = "user",
        reason: str = "",
        action: str = "forget",
    ) -> dict[str, Any]:
        """从推理路径硬移除、在审计路径软保留（不物理删除，保证重放幂等）。"""
        old = self._relational.get("neuron", memory_id)
        if old is None:
            return {"ok": False, "message": "记忆不存在"}
        if not old.get("is_active", True):
            return {"ok": True, "message": "该记忆已封存", "id": memory_id}

        now = _now()
        stamp = self._clock.tick()
        event_id = self._emit("tombstone", {
            "neuron_id": memory_id,
            "before": dict(old),
            "actor": actor,
            "reason": reason,
            "action": action,
            "ts_ms": now,
        }, agent_id=actor)
        self._relational.deactivate("neuron", memory_id, valid_to_ms=now, tx_to=stamp)
        try:
            self._vector.delete([memory_id])
        except Exception:  # noqa: BLE001
            pass
        return {
            "ok": True,
            "id": memory_id,
            "event_id": event_id,
            "message": "已遗忘：退出所有召回通道，主库保留封存记录用于审计",
        }

    # ── 审计 ──────────────────────────────────────────────────────────────
    def history(self, memory_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """从 journal 就地取出该记忆的创建 / 纠正 / 封存轨迹。"""
        rows: list[dict[str, Any]] = []
        try:
            events = list(self._journal.iter_events(include_self=True))
        except Exception:  # noqa: BLE001
            return []
        for event in events:
            payload = dict(event.get("payload") or {})
            refs = {
                payload.get("neuron_id"), payload.get("old_id"), payload.get("new_id"),
                (payload.get("neuron") or {}).get("id"),
            }
            if memory_id not in refs:
                continue
            rows.append({
                "event_id": str(event.get("event_id") or ""),
                "type": str(event.get("type") or ""),
                "hlc": str(event.get("hlc") or ""),
                "actor": str(payload.get("actor") or event.get("agent_id") or ""),
                "reason": str(payload.get("reason") or ""),
                "changed": list(payload.get("changed") or []),
                "before": payload.get("before") or {},
                "after": payload.get("after") or {},
            })
        return rows[-int(limit):][::-1]
