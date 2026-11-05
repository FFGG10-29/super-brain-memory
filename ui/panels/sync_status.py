"""ui.panels.sync_status —— 同步状态（各设备游标 / 待重放事件数 / 快照）。"""
from __future__ import annotations

from pathlib import Path

from sync.event_store import EventStore
from sync.journal import NdjsonJournal
from sync.snapshot import SnapshotManager

__all__ = ["data"]


def data(event_store: EventStore, journal: NdjsonJournal, *, device_id: str, root: str | Path) -> dict:
    cursors: dict[str, str] = {}
    for path in sorted((Path(root) / "devices").glob("*")):
        if not path.is_dir():
            continue
        stamp = event_store.load_cursor(path.name)
        cursors[path.name] = stamp.to_token() if stamp else "-"
    pending = len(event_store.scan(include_self=False, device_id=device_id))
    latest = SnapshotManager(root).latest()
    return {
        "device_id": device_id,
        "local_last_hlc": journal.last_stamp().to_token() if journal.last_stamp() else None,
        "cursors": cursors,
        "pending_events": pending,
        "latest_snapshot": latest.name if latest else None,
    }
