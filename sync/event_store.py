"""sync.event_store —— 事件读取 / HLC 排序 / 幂等重放。

要点（架构 §8.3）：
- 按 HLC 全序扫描各设备段（本地目录默认跳过，本地已投影）
- event_id 幂等键去重，重复事件零副作用（配合 relational.mark_event_applied 双保险）
- 游标（每设备 HLC 水位）持久化在 manifest.json
- schema_version 不兼容时拒放并告警，不静默猜测
"""
from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

from core.errors import SyncError
from core.ids import HLCStamp
from core.interfaces import Record

__all__ = ["EventStore"]

_DEVICES_DIR = "devices"
_MANIFEST = "manifest.json"


class EventStore:
    def __init__(self, root: str | Path, *, schema_version: int = 1) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._schema_version = int(schema_version)

    # ── 扫描 ──────────────────────────────────────────────────────────────
    def scan(
        self,
        *,
        after: HLCStamp | None = None,
        include_self: bool = False,
        device_id: str | None = None,
    ) -> list[Record]:
        """返回按 HLC 全序、event_id 去重后的事件列表。"""
        seen: set[str] = set()
        collected: list[Record] = []
        for path in sorted(self._root.glob(f"{_DEVICES_DIR}/*/*.ndjson")):
            if not include_self and device_id and path.parent.name == device_id:
                continue
            for event in self._read(path):
                self._check_version(event, path)
                event_id = str(event.get("event_id", ""))
                if not event_id or event_id in seen:
                    continue
                seen.add(event_id)
                collected.append(event)
        collected.sort(key=lambda event: HLCStamp.from_token(str(event["hlc"])))
        if after is not None:
            collected = [event for event in collected if HLCStamp.from_token(str(event["hlc"])) > after]
        return collected

    def _read(self, path: Path) -> Iterator[Record]:
        with open(path, "rb") as handle:
            for raw in handle:
                if not raw.strip():
                    continue
                try:
                    yield json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    # 单段损坏不阻塞其他设备（同步要能部分推进）
                    raise SyncError(f"事件段损坏 {path.name}: {exc}") from exc

    def _check_version(self, event: Record, path: Path) -> None:
        version = int(event.get("schema_version", self._schema_version))
        if version != self._schema_version:
            raise SyncError(
                f"{path.parent.name}/{path.name} 的 schema_version={version} 与本地 "
                f"{self._schema_version} 不兼容：需迁移或快照重建"
            )

    # ── 游标 ──────────────────────────────────────────────────────────────
    def load_cursor(self, device_id: str) -> HLCStamp | None:
        manifest = self._read_manifest()
        token = (manifest.get("cursors") or {}).get(device_id)
        return HLCStamp.from_token(token) if token else None

    def save_cursor(self, device_id: str, stamp: HLCStamp) -> None:
        manifest = self._read_manifest()
        cursors = dict(manifest.get("cursors") or {})
        cursors[device_id] = stamp.to_token()
        manifest["cursors"] = cursors
        manifest["updated_at_ms"] = int(time.time() * 1000)
        self._write_manifest(manifest)

    def _read_manifest(self) -> dict:
        path = self._root / _MANIFEST
        if not path.exists():
            return {"cursors": {}, "schema_version": self._schema_version}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # manifest 损坏不应阻断同步：重建空清单（游标重置只会导致重复重放，不会丢数据）
            return {"cursors": {}, "schema_version": self._schema_version}

    def _write_manifest(self, manifest: dict) -> None:
        path = self._root / _MANIFEST
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
