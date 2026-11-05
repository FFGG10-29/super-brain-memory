"""storage —— 六引擎（全部嵌入式，均为事件日志的投影视图）。

并发纪律：每引擎 thread-local 连接；SQLite WAL + busy_timeout；
连接对象禁止跨线程共享；跨层只走 asyncio 队列。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, Generic, TypeVar

__all__ = ["ThreadLocalPool"]

T = TypeVar("T")


class ThreadLocalPool(Generic[T]):
    """thread-local 资源池：每线程首次访问时创建，close() 统一释放全部线程的资源。

    六引擎的连接对象（sqlite3 / lmdb txn-less env / kuzu Connection / duckdb cursor）
    都禁止跨线程共享，统一走这里，避免各自手写一遍且漏掉 close。
    """

    def __init__(self, factory: Callable[[], T], *, name: str = "resource") -> None:
        self._factory = factory
        self._name = name
        self._local = threading.local()
        self._registry: list[T] = []
        self._lock = threading.Lock()
        self._closed = False

    def get(self) -> T:
        if self._closed:
            raise RuntimeError(f"{self._name} 已关闭，不可再取连接")
        resource = getattr(self._local, "resource", None)
        if resource is None:
            resource = self._factory()
            self._local.resource = resource
            with self._lock:
                self._registry.append(resource)
        return resource

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            registry, self._registry = self._registry, []
        for resource in registry:
            closer: Any = getattr(resource, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001  关闭失败不应阻断其他引擎
                    pass
        self._local = threading.local()
