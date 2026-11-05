"""sync.snapshot —— 快照与压缩（分段归档）。

冷启动路径：clone → 载入最新快照（records.ndjson 直接 upsert 回主库）
            → 重放快照切点之后的事件（EventProjector 幂等去重）→ 分钟级重建。
快照只存"状态"，事件日志仍是唯一真源；`covered_segments()` 给出可裁剪的旧段
清单，但不在本模块物理删除（避免删掉尚未推送到远端的段）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from core.errors import SyncError
from core.interfaces import EntityKind, RelationalRepository

__all__ = ["SnapshotManager", "SnapshotMeta"]

_KINDS: tuple[EntityKind, ...] = ("neuron", "synapse", "hyperedge")


@dataclass(frozen=True)
class SnapshotMeta:
    hlc_cutoff: str
    created_at_ms: int
    counts: dict[str, int]
    schema_version: int


class SnapshotManager:
    def __init__(self, root: str | Path, *, schema_version: int = 1) -> None:
        self._root = Path(root)
        self._snapshots = self._root / "snapshots"
        self._snapshots.mkdir(parents=True, exist_ok=True)
        self._schema_version = int(schema_version)

    def create(self, relational: RelationalRepository, *, hlc_cutoff: str, label: str | None = None) -> Path:
        name = label or time.strftime("snapshot-%Y%m%d-%H%M%S")
        folder = self._snapshots / name
        folder.mkdir(parents=True, exist_ok=True)
        counts: dict[str, int] = {}
        records_path = folder / "records.ndjson"
        with open(records_path, "wb") as handle:
            for kind in _KINDS:
                rows = relational.query(kind, active_only=False, limit=1_000_000)
                counts[kind] = len(rows)
                for row in rows:
                    line = json.dumps({"kind": kind, "record": dict(row)}, ensure_ascii=False, sort_keys=True)
                    handle.write(line.encode("utf-8") + b"\n")
            handle.flush()
        meta = SnapshotMeta(hlc_cutoff, int(time.time() * 1000), counts, self._schema_version)
        (folder / "manifest.json").write_text(
            json.dumps(
                {
                    "hlc_cutoff": meta.hlc_cutoff,
                    "created_at_ms": meta.created_at_ms,
                    "counts": meta.counts,
                    "schema_version": meta.schema_version,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return folder

    def latest(self) -> Path | None:
        candidates = sorted(
            (path for path in self._snapshots.iterdir() if (path / "manifest.json").exists()),
            key=lambda path: path.name,
        )
        return candidates[-1] if candidates else None

    def meta(self, folder: Path | None = None) -> SnapshotMeta:
        path = folder or self.latest()
        if path is None:
            raise SyncError("没有可用快照")
        data = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        if int(data.get("schema_version", 0)) != self._schema_version:
            raise SyncError("快照 schema_version 与本地不兼容，禁止静默导入")
        return SnapshotMeta(
            hlc_cutoff=str(data["hlc_cutoff"]),
            created_at_ms=int(data["created_at_ms"]),
            counts=dict(data.get("counts") or {}),
            schema_version=int(data["schema_version"]),
        )

    def load(self, folder: Path | None = None) -> list[dict]:
        path = folder or self.latest()
        if path is None:
            raise SyncError("没有可用快照")
        records: list[dict] = []
        with open(path / "records.ndjson", "rb") as handle:
            for raw in handle:
                if raw.strip():
                    records.append(json.loads(raw.decode("utf-8")))
        return records

    def restore(self, relational: RelationalRepository, *, folder: Path | None = None) -> int:
        """把快照状态 upsert 回主库（新设备冷启动第一步）。"""
        restored = 0
        for entry in self.load(folder):
            kind = str(entry.get("kind", ""))
            record = entry.get("record") or {}
            if kind in _KINDS and record.get("id"):
                relational.upsert(kind, record)  # type: ignore[arg-type]
                restored += 1
        return restored

    def covered_segments(self, folder: Path | None = None) -> list[Path]:
        """列出 hlc_cutoff 之前、理论上可裁剪的事件段（不执行删除）。"""
        from core.ids import HLCStamp

        cutoff = HLCStamp.from_token(self.meta(folder).hlc_cutoff)
        covered: list[Path] = []
        for path in sorted((self._root / "devices").glob("*/*.ndjson")):
            # 段名 {yyyymmdd}.{seq}.ndjson 与 HLC 物理毫秒非同序，用段内最大戳判断
            with open(path, "rb") as handle:
                stamps = [
                    HLCStamp.from_token(str(json.loads(line.decode("utf-8"))["hlc"]))
                    for line in handle
                    if line.strip()
                ]
            if stamps and max(stamps) <= cutoff:
                covered.append(path)
        return covered
