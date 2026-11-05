"""sync.journal —— 事件日志写入器（追加式 NDJSON）。

契约（架构 §8.1）：fsync 完成先于向调用方确认写入成功 —— "先落盘再回执"。
目录布局：{root}/devices/{device_id}/{yyyymmdd}.{seq}.ndjson，每设备只写自己目录
→ Git 合并退化为纯新增，结构性免冲突。

事件字段：event_id / device_id / agent_id / hlc / type / schema_version / payload。
PAD 就在 payload.neuron 里随事件跨设备流动（情绪全链路·同步环节）。
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

from core.errors import SyncError
from core.ids import HLCStamp
from core.interfaces import EventLog, Record

__all__ = ["NdjsonJournal"]

_DEVICES_DIR = "devices"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _day_stamp(now_ms: int | None = None) -> str:
    moment = _now_ms() if now_ms is None else int(now_ms)
    return time.strftime("%Y%m%d", time.localtime(moment / 1000))


class NdjsonJournal(EventLog):
    """本地事件段写入器。单写者：只写本设备目录。"""

    def __init__(
        self,
        root: str | Path,
        *,
        device_id: str,
        segment_max_bytes: int = 4 * 1024 * 1024,
        schema_version: int = 1,
    ) -> None:
        if not device_id:
            raise SyncError("journal 需要 device_id")
        self._root = Path(root)
        self._device_id = device_id
        self._segment_max_bytes = int(segment_max_bytes)
        self._schema_version = int(schema_version)
        self._device_dir = self._root / _DEVICES_DIR / device_id
        self._device_dir.mkdir(parents=True, exist_ok=True)
        self._current: Path | None = None
        self._last_stamp: HLCStamp | None = self._scan_last_stamp()

    # ── 写入 ──────────────────────────────────────────────────────────────
    def append(self, event: Record) -> None:
        payload = dict(event)
        payload.setdefault("event_id", payload.get("id", ""))
        if not payload["event_id"]:
            raise SyncError("事件缺少 event_id")
        payload["device_id"] = self._device_id
        payload.setdefault("schema_version", self._schema_version)
        stamp = self._stamp_of(payload)
        payload["hlc"] = stamp.to_token()

        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        segment = self._segment_for(len(line.encode("utf-8")))
        with open(segment, "ab") as handle:
            handle.write(line.encode("utf-8") + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._last_stamp = stamp

    def _stamp_of(self, event: Record) -> HLCStamp:
        raw = event.get("hlc")
        if isinstance(raw, HLCStamp):
            return raw
        if isinstance(raw, str) and raw:
            return HLCStamp.from_token(raw)
        # 调用方没带戳时按本地墙钟兜底（组合根里应总是携带 HLC）
        return HLCStamp(_now_ms(), 0, self._device_id)

    def _segment_for(self, incoming_bytes: int) -> Path:
        if self._current is not None and self._current.exists():
            if self._current.stat().st_size + incoming_bytes <= self._segment_max_bytes:
                return self._current
        day = _day_stamp()
        index = 0
        while True:
            candidate = self._device_dir / f"{day}.{index:04d}.ndjson"
            if not candidate.exists() or candidate.stat().st_size + incoming_bytes <= self._segment_max_bytes:
                self._current = candidate
                return candidate
            index += 1

    # ── 读取 ──────────────────────────────────────────────────────────────
    def iter_events(self, *, after: HLCStamp | None = None, include_self: bool = False) -> Iterator[Record]:
        """按 HLC 全序产出事件；默认跳过本设备（本地已投影）。"""
        events: list[Record] = []
        for path in sorted(self._root.glob(f"{_DEVICES_DIR}/*/*.ndjson")):
            if not include_self and path.parent.name == self._device_id:
                continue
            events.extend(self._read_segment(path))
        events.sort(key=lambda event: HLCStamp.from_token(str(event["hlc"])))
        for event in events:
            if after is not None and HLCStamp.from_token(str(event["hlc"])) <= after:
                continue
            yield event

    def _read_segment(self, path: Path) -> list[Record]:
        records: list[Record] = []
        with open(path, "rb") as handle:
            for raw in handle:
                if not raw.strip():
                    continue
                try:
                    records.append(json.loads(raw.decode("utf-8")))
                except json.JSONDecodeError as exc:
                    raise SyncError(f"事件段损坏 {path.name}: {exc}") from exc
        return records

    def _scan_last_stamp(self) -> HLCStamp | None:
        stamps: list[HLCStamp] = []
        for path in sorted(self._device_dir.glob("*.ndjson")):
            for event in self._read_segment(path):
                raw = event.get("hlc")
                if isinstance(raw, str) and raw:
                    stamps.append(HLCStamp.from_token(raw))
        return max(stamps) if stamps else None

    def last_stamp(self) -> HLCStamp | None:
        return self._last_stamp

    @property
    def device_dir(self) -> Path:
        return self._device_dir
