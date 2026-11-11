"""brain.write_gate —— 统一写入闸门（v4.1 W1）。

所有写入路径（MCP 网关 / v3 Worker / stdio 独占模式 / 文档蒸馏 / 夜间巩固 /
用户纠正与遗忘）都必须经 `WriteGate.submit()` 投递，由**唯一 daemon 线程** FIFO
串行执行。这样 Kùzu 单写者、丘脑准入、语义合并裁决不再依赖"恰好在同一进程"兜底，
而是结构性保证。

设计要点（Q17 / R20 / W1-补1）：
- `submit()` 返回 `concurrent.futures.Future`；调用方 **必须 `.result(timeout=...)`**，
  等待的是"闸门线程执行完"，不是自己执行，串行化语义不丢。
- 执行体用无参闭包 `WriteRequest.fn` 传入（闸门本身不持有 app/hippocampus），
  因此换核时只需重建闸门并替换各调用方引用，不会出现"写到已关闭旧引擎"。
- `shutdown(timeout)`：停止接受新提交 → 等队列排空 → 关线程 → 返回是否排空。
  必须在 `app.close()` 之前调用。
"""
from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = ["WriteGate", "WriteRequest"]

_SENTINEL = object()


@dataclass
class WriteRequest:
    """一次写入请求。字段一次定全，W2 的 overwrite 不需要重构数据结构。"""

    fn: Callable[[], Any] | None = None          # 通用执行体；6 条收口路径都用它
    args: dict[str, Any] = field(default_factory=dict)  # 预留给按 mode 分派的 remember 参数
    event_type: str = "ingest"                   # W2 起可为 overwrite / media_interaction
    entity_id: str = ""                          # A3：同一事务实体的稳定标识
    seq: int = 0                                 # A3：该 entity 的单调递增版本号
    mode: str = "ingest"                         # ingest | overwrite
    skip_consolidator: bool = False              # R19：覆盖式写入跳过语义合并裁决


class WriteGate:
    """单线程 FIFO 写入执行器。"""

    def __init__(
        self,
        *,
        executor: Callable[[WriteRequest], Any] | None = None,
        on_completed: Callable[[WriteRequest, Any], None] | None = None,
    ) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.RLock()
        self._closed = False
        self._executor = executor
        # C0：写入成功后的领域事件发布点（失效总线挂这里；失败写入不改状态，不发布）
        self._on_completed = on_completed
        self._thread = threading.Thread(
            target=self._consume, name="brain-write-gate", daemon=True
        )
        self._thread.start()

    # ── 提交 ──────────────────────────────────────────────────────────────
    def submit(
        self,
        request: WriteRequest | None = None,
        *,
        fn: Callable[[], Any] | None = None,
        **kwargs: Any,
    ) -> Future:
        """入队并返回 Future。闸门关闭后返回已带异常的 Future，不静默丢写入。"""
        if request is None:
            request = WriteRequest(fn=fn, **kwargs)
        future: Future = Future()
        with self._lock:
            if self._closed:
                future.set_exception(RuntimeError("WriteGate 已关闭，拒绝新写入"))
                return future
            self._queue.put((request, future))
        return future

    def pending(self) -> int:
        """排队中的请求数（HTTPAdapter.queue_depth 复用）。"""
        return self._queue.qsize()

    # ── 唯一执行线程 ──────────────────────────────────────────────────────
    def _run(self, request: WriteRequest) -> Any:
        if request.fn is not None:
            return request.fn()
        if self._executor is not None:
            return self._executor(request)
        raise RuntimeError("WriteRequest 既没有 fn 也没有 executor，无法执行")

    def _consume(self) -> None:
        while True:
            item = self._queue.get()
            if item is _SENTINEL:
                self._queue.task_done()
                return
            request, future = item
            try:
                result = self._run(request)
                future.set_result(result)
                # C0 / Q33：只对成功写入发布领域事件；发布失败绝不影响写入本身
                if self._on_completed is not None:
                    try:
                        self._on_completed(request, result)
                    except Exception:  # noqa: BLE001
                        pass
            except BaseException as exc:  # noqa: BLE001  异常必须回传，不能让 Future 悬空
                future.set_exception(exc)
            finally:
                self._queue.task_done()

    # ── 优雅停机（必须在 app.close() 之前）────────────────────────────────
    def shutdown(self, timeout: float = 5.0) -> bool:
        """停止接受新提交 → 排空队列（带超时）→ 关线程。返回是否完整排空。"""
        with self._lock:
            if self._closed:
                return True
            self._closed = True
        deadline = time.monotonic() + max(0.0, float(timeout))
        while self._queue.unfinished_tasks > 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        drained = self._queue.unfinished_tasks == 0
        self._queue.put(_SENTINEL)
        self._thread.join(timeout=max(0.5, deadline - time.monotonic() + 0.5))
        return drained
