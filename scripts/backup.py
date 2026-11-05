"""scripts/backup.py —— 一键备份 / 迁移（阶段 6 验收）。

用法：
    .venv\\Scripts\\python.exe scripts/backup.py backup
    .venv\\Scripts\\python.exe scripts/backup.py restore --from backups/<name>/manifest.json

复制五个物理基座 + 事件日志目录，生成 manifest.json（含文件大小与设备标识）。
恢复前不删除现有数据，由调用方确认目标目录（避免误覆盖）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import get_config  # noqa: E402

__all__ = ["backup", "restore"]


def _sources(config) -> list[tuple[str, Path]]:
    storage = config.storage
    return [
        ("relational.db", Path(storage.sqlite_path)),
        ("vector.lance", Path(storage.lancedb_path)),
        ("graph.kuzu", Path(storage.kuzu_path)),
        ("kv.lmdb", Path(storage.lmdb_path)),
        ("timeseries.db", Path(storage.duckdb_path)),
        ("journal", Path(config.sync.local_repo_dir)),
    ]


def backup(*, dest_root: Path | None = None) -> dict:
    config = get_config()
    dest_root = dest_root or ROOT / "backups"
    name = time.strftime("backup-%Y%m%d-%H%M%S")
    target = dest_root / name
    target.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []
    for label, source in _sources(config):
        if not source.exists():
            entries.append({"name": label, "source": str(source), "status": "missing"})
            continue
        copied = target / label
        try:
            if source.is_dir():
                shutil.copytree(source, copied, dirs_exist_ok=True)
                size = sum(item.stat().st_size for item in copied.rglob("*") if item.is_file())
            else:
                # Windows 上嵌入式库可能独占句柄：退化为流式共享读复制
                try:
                    shutil.copy2(source, copied)
                except PermissionError:
                    with open(source, "rb", buffering=0) as reader, open(copied, "wb") as writer:
                        shutil.copyfileobj(reader, writer, length=1024 * 1024)
                size = copied.stat().st_size
            entries.append({"name": label, "source": str(source), "status": "ok", "bytes": size})
        except (OSError, PermissionError) as exc:
            # 单个基座被占用不拖垮整次备份；状态写进 manifest 供告警
            entries.append({"name": label, "source": str(source), "status": "locked", "error": str(exc)})
    manifest = {
        "name": name,
        "created_at_ms": int(time.time() * 1000),
        "device_id": config.sync.device_id,
        "schema_version": config.sync.schema_version,
        "entries": entries,
    }
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest


def restore(manifest_path: Path) -> list[str]:
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    source_root = Path(manifest_path).parent
    config = get_config()
    destination = {label: path for label, path in _sources(config)}
    restored: list[str] = []
    for entry in manifest["entries"]:
        if entry["status"] != "ok" or entry["name"] not in destination:
            continue
        source = source_root / entry["name"]
        target = destination[entry["name"]]
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)
        restored.append(entry["name"])
    return restored


def main() -> int:
    parser = argparse.ArgumentParser(description="超级大脑备份/迁移")
    parser.add_argument("action", choices=("backup", "restore"))
    parser.add_argument("--from", dest="manifest", default="")
    args = parser.parse_args()
    if args.action == "backup":
        print(json.dumps(backup(), ensure_ascii=False, indent=2))
        return 0
    if not args.manifest:
        print("restore 需要 --from <manifest.json>")
        return 1
    print("已恢复:", ", ".join(restore(Path(args.manifest))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
