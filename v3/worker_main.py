"""v3.worker_main —— 独立 Model Worker 进程入口（进程分离模式）。

由 ProcessSupervisor 拉起；与 Daemon 共享 v3.db / journal.ndjson / UDP 闹钟端口。
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from config import BrainConfig, ModelConfig, StorageConfig, SyncConfig
from main import build_app
from v3.inbox import Inbox
from v3.notify import V3Alarm
from v3.worker import V3Worker


def _config_for(root: Path) -> BrainConfig:
    return BrainConfig(
        storage=StorageConfig(
            sqlite_path=root / "projection.db", lancedb_path=root / "vector.lance",
            kuzu_path=root / "graph.kuzu", lmdb_path=root / "kv.lmdb",
            duckdb_path=root / "timeseries.db",
        ),
        model=ModelConfig(dense_dim=8, use_cuda=False, llm_gguf_dir=root / "models"),
        sync=SyncConfig(device_id=f"worker-{os.getpid()}", local_repo_dir=root / "sync"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="v3 Model Worker")
    parser.add_argument("--root", required=True)
    parser.add_argument("--alarm-port", type=int, required=True)
    args = parser.parse_args()

    root = Path(args.root)
    inbox = Inbox(root / "v3.db")
    alarm = V3Alarm()  # 独立随机端口，避免与 Daemon 抢端口
    (root / "worker.port").write_text(str(alarm.port), encoding="utf-8")
    app = build_app(_config_for(root))
    worker = V3Worker(inbox, alarm, app, use_llm=False)
    try:
        worker.run()
    finally:
        inbox.close()
        alarm.close()
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
