"""阶段 0：UUIDv7 与 HLC 全序。"""
from __future__ import annotations

import uuid

import pytest

from core.ids import HLCStamp, HybridLogicalClock, new_uuid7, uuid7_time_ms


def test_uuid7_is_time_ordered_and_versioned():
    ids = [new_uuid7(now_ms=1_700_000_000_000 + i) for i in range(5)]
    assert ids == sorted(ids), "UUIDv7 字典序必须等于时间序"
    assert all(uuid.UUID(value).version == 7 for value in ids)
    assert uuid7_time_ms(ids[2]) == 1_700_000_000_002


def test_uuid7_rejects_non_v7():
    with pytest.raises(ValueError):
        uuid7_time_ms(str(uuid.uuid4()))


def test_hlc_stamp_token_roundtrip():
    stamp = HLCStamp(1_700_000_000_123, 7, "dev-a")
    assert HLCStamp.from_token(stamp.to_token()) == stamp


def test_hlc_monotonic_when_wall_clock_rolls_back():
    """时钟回拨不能破坏单调性（架构 §十三 风险：时钟回拨/跨设备乱序）。"""
    ticks = iter([1_000, 999, 998, 997, 996])  # 首个值被 __init__ 消费
    clock = HybridLogicalClock("dev-a", now_ms=lambda: next(ticks))
    stamps = [clock.tick() for _ in range(4)]
    assert stamps == sorted(stamps)
    assert len({stamp.to_token() for stamp in stamps}) == 4, "同一物理毫秒内必须靠计数器区分"


def test_hlc_observe_remote_ahead():
    clock = HybridLogicalClock("dev-a", now_ms=lambda: 500)
    remote = HLCStamp(5000, 3, "dev-b")
    observed = clock.observe(remote)
    assert observed > remote, "合并远端戳后本地戳必须严格大于远端"
    assert clock.tick() > observed


def test_hlc_counter_overflow_borrows_a_millisecond():
    ticks = iter([1000, 1000, 1000, 1000])  # 首个值被 __init__ 消费
    clock = HybridLogicalClock("dev-a", now_ms=lambda: next(ticks), counter_max=2)
    first = clock.tick()
    second = clock.tick()
    third = clock.tick()
    # __init__ 已占用 (1000, 0)，因此同毫秒内从 counter=1 起算
    assert (first.physical_ms, first.counter) == (1000, 1)
    assert (second.physical_ms, second.counter) == (1000, 2)
    assert third.physical_ms == 1001 and third.counter == 0, "计数器溢出必须借 1ms"


def test_hlc_requires_device_id():
    with pytest.raises(ValueError):
        HybridLogicalClock("")
