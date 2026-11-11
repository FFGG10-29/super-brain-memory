"""workers.consolidation_retry —— 巩固任务专用重试队列（W6 / R40 / C12）。

**不复用 v3 inbox**：inbox 是事件受理队列，巩固任务是「叙事重构任务」，语义不同。
内存队列 + DuckDB `consolidation_failures` 表持久化，进程重启后从表中恢复；
最多 3 次指数退避，3 次后标 `narrative_pending`，下个巩固窗口再试，不静默丢弃。
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from config import BrainConfig, get_config
from core.ids import new_uuid7

__all__ = ["ConsolidationRetry", "RetryTask"]

_TABLE = "consolidation_failures"


def _now() -> int:
    return int(time.time() * 1000)


@dataclass
class RetryTask:
    task_id: str
    topic: str
    hyperedge_ids: list[str] = field(default_factory=list)
    attempts: int = 0
    next_at_ms: int = 0
    status: str = "pending"        # pending | narrative_pending | done
    error: str = ""


class ConsolidationRetry:
    def __init__(
        self,
        *,
        timeseries: Any | None = None,
        config: BrainConfig | None = None,
    ) -> None:
        self._timeseries = timeseries
        self._config = config or get_config()
        self._tasks: dict[str, RetryTask] = {}
        self._recover()

    @property
    def max_retries(self) -> int:
        return max(1, int(self._config.narrative_reconstruct.max_retries))

    @property
    def backoff_sec(self) -> float:
        return max(0.1, float(self._config.narrative_reconstruct.retry_backoff_sec))

    # ── DuckDB 持久化 / 恢复 ──────────────────────────────────────────────
    def _persist(self, task: RetryTask) -> None:
        if self._timeseries is None:
            return
        try:
            self._timeseries.append(_TABLE, [{
                "ts_ms": _now(), "task_id": task.task_id,
                "hyperedge_id": ",".join(task.hyperedge_ids), "topic": task.topic,
                "attempts": task.attempts, "error": task.error, "status": task.status,
            }])
        except Exception:  # noqa: BLE001  持久化失败不能拖垮内存重试
            pass

    def _recover(self) -> None:
        if self._timeseries is None:
            return
        try:
            rows = self._timeseries.fetch(_TABLE, limit=500)
        except Exception:  # noqa: BLE001  旧库没有该表时忽略
            return
        for row in rows:
            if str(row.get("status")) in ("done", ""):
                continue
            task_id = str(row.get("task_id") or "")
            if not task_id:
                continue
            task = RetryTask(
                task_id=task_id,
                topic=str(row.get("topic") or "topic:default"),
                hyperedge_ids=[item for item in str(row.get("hyperedge_id") or "").split(",") if item],
                attempts=int(row.get("attempts") or 0),
                status=str(row.get("status") or "pending"),
                error=str(row.get("error") or ""),
            )
            existing = self._tasks.get(task_id)
            if existing is None or task.attempts >= existing.attempts:
                self._tasks[task_id] = task

    # ── 队列 ──────────────────────────────────────────────────────────────
    def enqueue(self, topic: str, hyperedge_ids: list[str], error: str = "") -> RetryTask:
        task = RetryTask(task_id=new_uuid7(), topic=topic,
                         hyperedge_ids=list(hyperedge_ids), error=error)
        self._tasks[task.task_id] = task
        self._persist(task)
        return task

    def pending(self) -> list[dict[str, Any]]:
        return [asdict(task) for task in self._tasks.values()
                if task.status != "done"]

    def drain(self, handler: Callable[[RetryTask], bool]) -> dict[str, Any]:
        """处理到期任务。成功则标 done；失败按指数退避重排，3 次后 narrative_pending。"""
        now = _now()
        ran = succeeded = failed = parked = 0
        for task in list(self._tasks.values()):
            if task.status == "done" or task.next_at_ms > now:
                continue
            ran += 1
            try:
                ok = bool(handler(task))
            except Exception as exc:  # noqa: BLE001  单个任务失败不影响其它任务
                ok = False
                task.error = f"{type(exc).__name__}: {exc}"
            if ok:
                task.status = "done"
                succeeded += 1
            else:
                task.attempts += 1
                if task.attempts >= self.max_retries:
                    task.status = "narrative_pending"
                    parked += 1
                else:
                    delay = self.backoff_sec * (2 ** (task.attempts - 1))
                    task.next_at_ms = _now() + int(delay * 1000)
                    failed += 1
            self._persist(task)
        return {"ran": ran, "succeeded": succeeded, "retrying": failed,
                "parked": parked, "pending": len(self.pending())}
