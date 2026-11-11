"""chat.event_bus —— 进程内事件总线：8769 与 8767 完全解耦（C9 / P2.2）。

8767 是 Flutter 管理端专用 WS，聊天客户端只连 8769；异步事件
（deep_recall_ready / media_ready / typing）由 ChatEngine 从 8769 直接推给
对应 session 的所有在线连接，不依赖 8767 的 pulse 广播。
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

__all__ = ["EventBus"]


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscribers: dict[str, list[Callable[[dict[str, Any]], None]]] = {}

    def subscribe(self, session_id: str, callback: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            self._subscribers.setdefault(session_id, []).append(callback)

    def unsubscribe(self, session_id: str, callback: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            items = self._subscribers.get(session_id, [])
            if callback in items:
                items.remove(callback)
            if not items:
                self._subscribers.pop(session_id, None)

    def publish(self, session_id: str, event: dict[str, Any]) -> int:
        """同步通知订阅者；返回收到事件的订阅者数。"""
        with self._lock:
            callbacks = list(self._subscribers.get(session_id, []))
        for callback in callbacks:
            try:
                callback(event)
            except Exception:  # noqa: BLE001  单个订阅者失败不影响其他端点
                pass
        return len(callbacks)
