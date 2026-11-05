"""core.ids —— UUIDv7 + HLC 混合逻辑时钟。

- UUIDv7：48bit 毫秒时间戳 + 74bit 随机，字符串天然按时间有序，用作 event_id 幂等键。
- HLC：物理毫秒 + 计数器 + device_id，跨设备全序；物理时钟回拨只影响墙钟，不影响单调性。
- transaction_time 一律取 HLC（架构 §十三 风险清单：时钟回拨/跨设备乱序）。
"""
from __future__ import annotations

import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable

__all__ = [
    "COUNTER_MAX",
    "HLCStamp",
    "HybridLogicalClock",
    "new_uuid7",
    "uuid7_time_ms",
]

COUNTER_MAX = 0xFFFF
_TS_MASK = (1 << 48) - 1
_RAND_A_BITS = 12
_RAND_B_BITS = 62


def _wall_ms() -> int:
    return int(time.time() * 1000)


def new_uuid7(now_ms: int | None = None) -> str:
    """生成 RFC 9562 UUIDv7 字符串（时间段在前，字典序≈时间序）。"""
    ts = _wall_ms() if now_ms is None else int(now_ms)
    if ts < 0:
        raise ValueError("now_ms 不能为负")
    value = (ts & _TS_MASK) << 80
    value |= 0x7 << 76                     # version = 7
    value |= secrets.randbits(_RAND_A_BITS) << 64
    value |= 0b10 << 62                    # variant = RFC 4122 (10xx)
    value |= secrets.randbits(_RAND_B_BITS)
    return str(uuid.UUID(int=value))


def uuid7_time_ms(value: str | uuid.UUID) -> int:
    """取回 UUIDv7 内嵌的毫秒时间戳；非 v7 直接报错，避免静默错序。"""
    uid = value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    if uid.version != 7:
        raise ValueError(f"不是 UUIDv7: {value}")
    return uid.int >> 80


@dataclass(frozen=True, order=True)
class HLCStamp:
    """混合逻辑时钟戳。

    排序即全序：先物理毫秒，再计数器，最后 device_id 打破平局，
    保证两台设备在同一毫秒产生的事件也有确定顺序（重放结果一致）。
    """

    physical_ms: int
    counter: int = 0
    device_id: str = ""

    def to_token(self) -> str:
        """单字段序列化（NDJSON 里存这个或直接存三段字段均可）。"""
        return f"{self.physical_ms}:{self.counter}:{self.device_id}"

    @classmethod
    def from_token(cls, token: str) -> "HLCStamp":
        physical, counter, device_id = token.split(":", 2)
        return cls(int(physical), int(counter), device_id)

    def with_device(self, device_id: str) -> "HLCStamp":
        return HLCStamp(self.physical_ms, self.counter, device_id)


class HybridLogicalClock:
    """线程安全的 HLC 发生器。

    tick()      本地事件（写入前取戳）
    observe(ts) 收到远端事件（git pull 重放前推进本地时钟，保证后续戳大于远端）
    """

    def __init__(
        self,
        device_id: str,
        *,
        now_ms: Callable[[], int] | None = None,
        counter_max: int = COUNTER_MAX,
    ) -> None:
        if not device_id:
            raise ValueError("device_id 不能为空：HLC 用它做平局裁决")
        self.device_id = device_id
        self._now = now_ms or _wall_ms
        self._counter_max = counter_max
        self._last = HLCStamp(int(self._now()), 0, device_id)
        self._lock = threading.Lock()

    @property
    def last(self) -> HLCStamp:
        with self._lock:
            return self._last

    def tick(self) -> HLCStamp:
        with self._lock:
            self._last = self._bump(self._last, int(self._now()))
            return self._last

    def observe(self, remote: HLCStamp) -> HLCStamp:
        """合并远端戳：本地时钟永不落后于已知的最大戳。"""
        with self._lock:
            wall = int(self._now())
            last = self._last
            peak = max(wall, last.physical_ms, remote.physical_ms)
            if peak == last.physical_ms and peak == remote.physical_ms:
                counter = max(last.counter, remote.counter) + 1
            elif peak == last.physical_ms:
                counter = last.counter + 1
            elif peak == remote.physical_ms:
                counter = remote.counter + 1
            else:
                counter = 0
            self._last = self._normalize(peak, counter)
            return self._last

    def _bump(self, last: HLCStamp, wall: int) -> HLCStamp:
        if wall > last.physical_ms:
            return HLCStamp(wall, 0, self.device_id)
        return self._normalize(last.physical_ms, last.counter + 1)

    def _normalize(self, physical_ms: int, counter: int) -> HLCStamp:
        # 计数器溢出：借 1ms，保持单调且不越界
        while counter > self._counter_max:
            physical_ms += 1
            counter -= self._counter_max + 1
        return HLCStamp(physical_ms, counter, self.device_id)


def stamps_from(events: list[dict]) -> list[HLCStamp]:
    """从事件字典批量提取 HLC 戳（重放排序用）。"""
    out: list[HLCStamp] = []
    for event in events:
        raw = event.get("hlc")
        if isinstance(raw, HLCStamp):
            out.append(raw)
        elif isinstance(raw, str):
            out.append(HLCStamp.from_token(raw))
        elif isinstance(raw, dict):
            out.append(HLCStamp(int(raw["physical_ms"]), int(raw.get("counter", 0)), str(raw.get("device_id", ""))))
        else:
            raise ValueError(f"事件缺少可解析的 hlc 字段: {raw!r}")
    return out
