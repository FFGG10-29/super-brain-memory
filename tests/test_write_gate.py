"""W1 验收：WriteGate 并发串行、Future 语义、shutdown 排空。

刻意不碰真实六引擎（fake executor 记录执行顺序），避免与 conftest 的
brain_factory / Kùzu 单进程锁互相干扰。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor

from brain.write_gate import WriteGate, WriteRequest


def test_submit_serializes_concurrent_writes() -> None:
    gate = WriteGate()
    guard = threading.Lock()
    order: list[int] = []
    running = {"now": 0, "peak": 0}

    def make_fn(index: int):
        def fn() -> int:
            with guard:
                running["now"] += 1
                running["peak"] = max(running["peak"], running["now"])
                order.append(("enter", index))
            time.sleep(0.005)  # 拉长临界区，若有并发必定被 peak 抓到
            with guard:
                running["now"] -= 1
                order.append(("leave", index))
            return index * 2
        return fn

    try:
        futures: list[Future] = [gate.submit(WriteRequest(fn=make_fn(i))) for i in range(9)]
        with ThreadPoolExecutor(max_workers=3) as pool:
            waiters = [pool.submit(future.result, timeout=5) for future in futures]
            results = [item.result() for item in waiters]
        assert sorted(results) == [i * 2 for i in range(9)]
        assert running["peak"] == 1, "任何时刻只能有一个写入执行体在跑"
        # 严格 FIFO：enter(i) 与 leave(i) 必须成对相邻，不能穿插
        for offset in range(0, len(order), 2):
            assert order[offset][0] == "enter" and order[offset + 1][0] == "leave"
            assert order[offset][1] == order[offset + 1][1]
    finally:
        gate.shutdown(timeout=5)


def test_pending_reflects_queue_depth() -> None:
    gate = WriteGate()
    release = threading.Event()
    try:
        first = gate.submit(WriteRequest(fn=lambda: release.wait(timeout=3)))
        # 等第一个真正进入执行体，确保后续请求都堆在队列里
        for _ in range(100):
            if gate.pending() >= 0 and first.running():
                break
            time.sleep(0.01)
        gate.submit(WriteRequest(fn=lambda: "b"))
        gate.submit(WriteRequest(fn=lambda: "c"))
        assert gate.pending() >= 2
    finally:
        release.set()
        assert gate.shutdown(timeout=5) is True


def test_shutdown_drains_and_rejects_new() -> None:
    gate = WriteGate()
    done: list[int] = []
    for index in range(5):
        gate.submit(WriteRequest(fn=lambda index=index: (
            time.sleep(0.01), done.append(index)
        )[-1]))
    assert gate.shutdown(timeout=5) is True, "shutdown 必须排空已在队列里的写入"
    assert sorted(done) == list(range(5))
    rejected = gate.submit(WriteRequest(fn=lambda: 1))
    assert rejected.done() and rejected.exception() is not None
    assert "关闭" in str(rejected.exception())


def test_exception_is_propagated_to_future() -> None:
    gate = WriteGate()
    try:
        def boom() -> None:
            raise ValueError("写入失败样例")
        future = gate.submit(WriteRequest(fn=boom))
        assert isinstance(future.exception(timeout=2), ValueError)
        assert gate.submit(WriteRequest(fn=lambda: "ok")).result(timeout=2) == "ok"
    finally:
        gate.shutdown(timeout=5)


def test_request_fields_are_ready_for_w2() -> None:
    request = WriteRequest(
        fn=lambda: None, event_type="overwrite", entity_id="sha1:abc",
        seq=7, mode="overwrite", skip_consolidator=True,
    )
    assert request.event_type == "overwrite" and request.seq == 7
    assert request.mode == "overwrite" and request.skip_consolidator is True
