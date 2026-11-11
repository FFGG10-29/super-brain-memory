"""v3.worker —— 无状态后台消费者：认知全链路搬到这里。

- drain：醒来后一直 claim 到取空，再回去等闹钟（避免"一通知一任务"漏消费）；
- 复用现有 Hippocampus 的抽取/PAD/丘脑/语义合并/六库投影，仅关闭 journal 再入队（emit_event=False）；
- CAS 完成、租约心跳、幂等 event_id；结果通过 hub 广播给前端。
"""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from brain.write_gate import WriteGate, WriteRequest
from brain.write_router import normalized_signature

__all__ = ["V3Worker"]


class V3Worker:
    def __init__(self, inbox, alarm, app, *, hub: Any | None = None,
                 worker_id: str | None = None, lease_s: float = 60.0,
                 use_llm: bool = True, write_gate: WriteGate | None = None) -> None:
        self._inbox = inbox
        self._alarm = alarm
        self._app = app
        self._hub = hub
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self._lease_s = lease_s
        self._use_llm = use_llm
        # W1（Q17 / W1-补3）：inbox 只负责受理/持久化/幂等，执行串行化交给 WriteGate。
        self._write_gate = write_gate or WriteGate()
        self._stop = threading.Event()
        self.processed = 0
        self.failed = 0

    def _execute(self, fn) -> Any:
        """所有主库写都经闸门；30s 超时交给 lease 回收，不在这里 mark failed。"""
        return self._write_gate.submit(WriteRequest(fn=fn)).result(timeout=30)

    # ── 单轮 ──────────────────────────────────────────────────────────────
    def run_once(self, *, limit: int = 20) -> int:
        rows = self._inbox.claim(self.worker_id, limit=limit, lease_s=self._lease_s)
        for row in rows:
            self._process(row)
        return len(rows)

    def _process(self, row: dict[str, Any]) -> None:
        event_id = str(row["event_id"])
        try:
            payload = json.loads(str(row.get("payload") or "{}"))
            kind = str(payload.get("type") or "ingest")
            # R15：媒体交互只记 meta、直接完成，绝不产生 neuron
            if kind == "media_interaction":
                self._inbox.finish(event_id, self.worker_id, ok=True)
                return
            # R19：覆盖式事件必须走专用分支，不能落到通用 remember() 的合并裁决
            if kind == "overwrite":
                return self._handle_overwrite(payload, row, event_id)
            legacy = payload.get("legacy_neuron")
            if isinstance(legacy, dict) and legacy.get("id"):
                # v2 迁移事件：原样落投影并保留双时态/PAD，再让 outbox 重建派生库
                def apply_legacy() -> None:
                    self._app.storage.relational.upsert("neuron", legacy)
                    for target in ("vector", "graph", "timeseries", "hypergraph"):
                        self._app.storage.relational.outbox_enqueue(
                            event_id=str(legacy["id"]), target=target,
                            payload={"neuron": legacy, "extraction": {}},
                        )
                    self._app.hippocampus.dispatch_outbox()

                self._execute(apply_legacy)
                ok = self._inbox.finish(event_id, self.worker_id, ok=True)
                if ok and self._hub is not None:
                    self._hub.broadcast({
                        "type": "node_created", "event_id": event_id, "id": legacy["id"],
                        "content": payload.get("content", ""),
                        "memory_type": legacy.get("memory_type", "fact"),
                        "tier": legacy.get("tier", "buffer"),
                        "pad": (legacy.get("pad_p", 0), legacy.get("pad_a", 0), legacy.get("pad_d", 0)),
                        "merge": "add",
                    })
                self.processed += 1
                return
            # ★ A 包：把 journal 里的 namespace 一路带到落库，
            #   否则 handle_event 收到的 topic:<id> 会在 worker 丢失、全落 private。
            _ns = str(payload.get("namespace") or row.get("namespace") or "topic:default")
            result = self._execute(lambda: self._app.hippocampus.remember(
                str(row["content"]), agent_id=str(row["agent_id"]),
                namespace=_ns, emit_event=False, use_llm=self._use_llm,
            ))
            ok = self._inbox.finish(event_id, self.worker_id, ok=True)
            if not ok:
                return  # 租约被抢，丢弃结果，防双花
            self.processed += 1
            if self._hub is not None:
                payload = {
                    "type": "node_created" if result.applied else "node_rejected",
                    "event_id": event_id,
                    "id": result.neuron.uuid7_id,
                    "content": result.neuron.content,
                    "memory_type": result.neuron.memory_type,
                    "tier": result.neuron.tier,
                    "pad": result.neuron.pad.as_tuple(),
                    "reason": result.admission.reason,
                    "superseded": result.superseded,
                    "merge": (result.merge.action if result.merge else "add"),
                }
                self._hub.broadcast(payload)
        except FutureTimeout:
            # Q26：WriteGate 30s 超时后**不调 finish**，让 lease 到期自动回 pending，
            # 由下一个 drain 重领（inbox 现有机制，零改动）。finish(failed) 会永久丢事件。
            return
        except Exception as exc:  # noqa: BLE001  单条失败不拖垮 Worker
            self.failed += 1
            self._inbox.finish(event_id, self.worker_id, ok=False, error=f"{type(exc).__name__}: {exc}")
            if self._hub is not None:
                self._hub.broadcast({"type": "node_failed", "event_id": event_id, "error": str(exc)[:200]})

    def _handle_overwrite(self, event: dict[str, Any], row: dict[str, Any],
                          event_id: str) -> None:
        """W2 / A8+R19：覆盖式事件重放 —— 直接调 overwrite_remember，跳过通用路径。"""
        try:
            inner = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            content = str(inner.get("content") or row.get("content") or "")
            cognition = inner.get("cognition") if isinstance(inner.get("cognition"), dict) else {}
            routing = event.get("routing") if isinstance(event.get("routing"), dict) else None
            if not isinstance(routing, dict) or routing.get("path") != "overwrite":
                # 旧事件 / 崩溃窗口里缺 routing：按 entity_id 或签名补一个最小决策，
                # 保证「同一 entity 最后一条 overwrite 决定当前状态」仍成立。
                signature, fuzzy = normalized_signature(content)
                entity_id = str(event.get("entity_id") or "") or hashlib.sha1(
                    f"{row.get('namespace')}|{signature}".encode("utf-8", "replace")
                ).hexdigest()
                routing = {
                    "path": "overwrite", "entity_id": entity_id, "confidence": 0.9,
                    "agent_confidence": 0.9, "category": "", "signature": signature,
                    "entity_fuzzy": fuzzy, "routing_fallback": False,
                }
            result = self._execute(lambda: self._app.hippocampus.overwrite_remember(
                content,
                agent_id=str(row.get("agent_id") or "default"),
                namespace=str(row.get("namespace") or "topic:default"),
                cognition=cognition, routing=routing, skip_consolidator=True,
            ))
            ok = self._inbox.finish(event_id, self.worker_id, ok=True)
            if not ok:
                return  # 租约被抢，防双花
            self.processed += 1
            if self._hub is not None:
                self._hub.broadcast({
                    "type": "node_created" if result.applied else "node_rejected",
                    "event_id": event_id,
                    "event_type": "overwrite",
                    "id": result.neuron.uuid7_id,
                    "content": result.neuron.content,
                    "memory_type": result.neuron.memory_type,
                    "tier": result.neuron.tier,
                    "pad": result.neuron.pad.as_tuple(),
                    "entity_id": routing.get("entity_id", ""),
                    "seq": event.get("seq", 0),
                    "superseded": result.superseded,
                    "merge": "overwrite",
                })
        except FutureTimeout:
            return  # Q26：靠 lease 回收，不 mark failed
        except Exception as exc:  # noqa: BLE001
            self.failed += 1
            self._inbox.finish(event_id, self.worker_id, ok=False,
                               error=f"{type(exc).__name__}: {exc}")
            if self._hub is not None:
                self._hub.broadcast({"type": "node_failed", "event_id": event_id,
                                     "error": str(exc)[:200]})

    # ── 常驻循环 ──────────────────────────────────────────────────────────
    def run(self) -> None:
        while not self._stop.is_set():
            try:
                drained = 0
                while not self._stop.is_set():
                    count = self.run_once()
                    drained += count
                    if count == 0:
                        break
                if drained == 0:
                    self._alarm.wait(timeout=1.0)  # 1s 兜底轮询：通知丢了也扫 pending
            except Exception:  # noqa: BLE001  守护循环不因单轮异常退出
                self._stop.wait(0.2)

    def stop(self) -> None:
        self._stop.set()
        self._alarm.ring("stop")
