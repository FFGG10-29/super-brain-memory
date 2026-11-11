"""v3.journal —— NDJSON 唯一真源：group commit + fsync 先于回执 + 崩溃补齐。

- 顺序 append，单文件句柄；多条并发请求合并为一次 fsync（Leader 刷盘、Follower 等同一批）；
- 每条事件带全局 event_id（UUID7/HLC 字符串），inbox 与投影只认它做幂等；
- `reconcile()` 从 journal 把「journal 有、inbox 没有」的缺口补进队列，覆盖双写崩溃窗口。
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.ids import new_uuid7

__all__ = ["V3Journal"]


@dataclass
class _Ticket:
    event: dict[str, Any]
    done: threading.Event = field(default_factory=threading.Event)
    error: Exception | None = None


class V3Journal:
    def __init__(self, root: Path, *, device_id: str = "v3", flush_interval_s: float = 0.005) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "journal.ndjson"
        self.device_id = device_id
        self._flush_interval = flush_interval_s
        self._handle = open(self.path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        self._queue: list[_Ticket] = []
        self._cond = threading.Condition()
        self._running = True
        self._thread = threading.Thread(target=self._committer, name="v3-journal", daemon=True)
        self._thread.start()

    def append(self, content: str, *, agent_id: str, namespace: str = "private",
               event_type: str = "ingest", extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """阻塞直到本批 fsync 完成；返回事件（含 event_id）。

        W2：`event_type="overwrite"` 时，`extra` 应带 `entity_id / seq / cognition`，
        且 payload 只含新内容（不带旧 payload）——状态 = 同一 entity 最后一条 overwrite。
        """
        event = {
            "event_id": new_uuid7(),
            "type": event_type,
            "hlc": f"{int(time.time() * 1000)}:0:{self.device_id}",
            "agent_id": agent_id,
            "namespace": namespace,
            "content": content,
            "ts_ms": int(time.time() * 1000),
            **(extra or {}),
        }
        ticket = _Ticket(event)
        with self._cond:
            self._queue.append(ticket)
            self._cond.notify()
        ticket.done.wait()
        if ticket.error is not None:
            raise ticket.error
        return event

    def append_raw(self, event: dict[str, Any]) -> dict[str, Any]:
        """迁移用：原样追加已带 event_id/HLC 的事件（同样 group commit + fsync）。"""
        ticket = _Ticket(event)
        with self._cond:
            self._queue.append(ticket)
            self._cond.notify()
        ticket.done.wait()
        if ticket.error is not None:
            raise ticket.error
        return event

    def _committer(self) -> None:
        while self._running or self._queue:
            with self._cond:
                self._cond.wait_for(lambda: self._queue or not self._running, timeout=self._flush_interval)
                batch, self._queue = self._queue, []
            if not batch:
                continue
            try:
                for ticket in batch:
                    self._handle.write(json.dumps(ticket.event, ensure_ascii=False) + "\n")
                self._handle.flush()
                os.fsync(self._handle.fileno())
            except Exception as exc:  # noqa: BLE001
                for ticket in batch:
                    ticket.error = exc
            finally:
                for ticket in batch:
                    ticket.done.set()

    def iter_events(self, *, after: str | None = None) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if not self.path.exists():
            return events
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if after is not None and str(event.get("event_id", "")) <= after:
                continue
            events.append(event)
        return events

    def last_event_id(self) -> str | None:
        events = self.iter_events()
        return str(events[-1]["event_id"]) if events else None

    def reconcile(self, inbox: Any) -> int:
        """崩溃补齐：把 journal 里有、inbox 没有的事件重新入队（幂等）。

        W2 / A3：同一 `entity_id` 的多条 `overwrite` 只保留 **seq 最大** 的那条，
        重放时"最后一个 overwrite 决定当前状态"，不依赖 HLC 跨设备全序；
        事务性内容不跨设备同步，中间版本无需重放。
        """
        events = self.iter_events()
        latest_seq: dict[str, int] = {}
        for event in events:
            if event.get("type") == "overwrite" and event.get("entity_id"):
                key = str(event["entity_id"])
                try:
                    seq = int(event.get("seq") or 0)
                except (TypeError, ValueError):
                    seq = 0
                latest_seq[key] = max(latest_seq.get(key, -1), seq)
        restored = 0
        for event in events:
            if event.get("type") == "overwrite" and event.get("entity_id"):
                key = str(event["entity_id"])
                try:
                    seq = int(event.get("seq") or 0)
                except (TypeError, ValueError):
                    seq = 0
                if seq < latest_seq[key]:
                    continue  # 中间版本，跳过
            if inbox.enqueue(event):
                restored += 1
        return restored

    def close(self) -> None:
        self._running = False
        with self._cond:
            self._cond.notify()
        self._thread.join(timeout=2.0)
        try:
            self._handle.flush()
            os.fsync(self._handle.fileno())
        finally:
            self._handle.close()
