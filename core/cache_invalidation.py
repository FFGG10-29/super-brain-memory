"""core.cache_invalidation —— 缓存失效总线（v4.1-C / C0.2）。

双驱动失效的领域事件侧：`memory_updated`（ingest / supersede / overwrite /
tombstone / reconsolidate）+ persona / documents / session / tool_catalog。

发布点（Q33 裁决）：**`WriteGate` 的成功完成回调**——W1 之后所有主库写都经闸门，
它是唯一不需要逐条路径埋点的发布位置。`chat/event_bus.py` 保持 session 语义，不混用。

纪律（C0.2）：订阅者只做**精确 key 失效**，禁止清空整个命名空间。
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

__all__ = ["CacheInvalidationBus", "write_result_to_event"]

MEMORY_EVENT = "memory_updated"
# 记忆更新默认波及的缓存命名空间（消费者可按事件扩展）
DEFAULT_AFFECTED = ("recall", "episodic", "salience", "doc_distill", "semantic")


class CacheInvalidationBus:
    """进程内失效事件分发器：subscribe / publish，订阅者异常互不影响。"""

    def __init__(self) -> None:
        self._subscribers: list[Callable[[dict[str, Any]], None]] = []
        self.published = 0

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> None:
        if callable(callback) and callback not in self._subscribers:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[dict[str, Any]], None]) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def publish(self, event: dict[str, Any]) -> int:
        """同步分发；返回收到事件的订阅者数。"""
        if not isinstance(event, dict) or not event.get("type"):
            return 0
        self.published += 1
        delivered = 0
        for callback in list(self._subscribers):
            try:
                callback(event)
                delivered += 1
            except Exception:  # noqa: BLE001  单个订阅者失败不影响其他缓存
                pass
        return delivered


def _collect_ids_from_result(result: Any, ids: list[str]) -> None:
    """尽力从执行结果里提取受影响记忆 id（不抛异常，宁缺勿错）。"""
    if result is None:
        return
    # WriteResult 风格：result.neuron.uuid7_id + result.superseded
    neuron = getattr(result, "neuron", None)
    if neuron is not None:
        neuron_id = getattr(neuron, "uuid7_id", None) or getattr(neuron, "id", None)
        if neuron_id:
            ids.append(str(neuron_id))
    for extra in (getattr(result, "superseded", None) or []):
        if extra:
            ids.append(str(extra))
    # dict 风格：MemoryEditor.correct/forget 返回 {"ok", "id", "event_ids"}；
    # MCPToolkit 风格可能包一层 {"result": {...}}
    if isinstance(result, dict):
        for key in ("id", "memory_id", "neuron_id"):
            if result.get(key):
                ids.append(str(result[key]))
        for item in (result.get("event_ids") or []):
            if item:
                ids.append(str(item))
        inner = result.get("result")
        if isinstance(inner, dict):
            _collect_ids_from_result(inner, ids)
    if isinstance(result, str) and len(result) <= 64:
        ids.append(result)


def write_result_to_event(request: Any, result: Any) -> dict[str, Any] | None:
    """把一次成功的闸门写入转成 `memory_updated` 事件。

    提取不到任何受影响 id 时返回 None（调用方跳过发布）——宁缺勿滥，
    绝不发"清空命名空间"级别的粗粒度事件。
    """
    ids: list[str] = []
    _collect_ids_from_result(result, ids)
    deduped = list(dict.fromkeys(item for item in ids if item))
    if not deduped:
        return None
    event_type = str(getattr(request, "event_type", "") or "ingest")
    return {
        "type": MEMORY_EVENT,
        "event_type": event_type,
        "affected_ids": deduped,
        "affected_namespaces": list(DEFAULT_AFFECTED),
        "ts_ms": int(time.time() * 1000),
    }
