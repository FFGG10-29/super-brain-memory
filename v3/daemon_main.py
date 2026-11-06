"""v3.daemon_main —— Windows 服务入口（WinSW/NSSM 托管）。

    python -m v3.daemon_main --root "C:\\ProgramData\\super-brain\\data" --worker-mode process

正式形态：Daemon 只做受理/只读/广播，Model Worker 由 ProcessSupervisor 独立进程拉起。
"""
from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path

from config import BrainConfig, ModelConfig, StorageConfig, SyncConfig
from v3.daemon import V3Daemon


def _config(root: Path, device_id: str) -> BrainConfig:
    return BrainConfig(
        storage=StorageConfig(
            sqlite_path=root / "projection.db", lancedb_path=root / "vector.lance",
            kuzu_path=root / "graph.kuzu", lmdb_path=root / "kv.lmdb",
            duckdb_path=root / "timeseries.db",
        ),
        model=ModelConfig(dense_dim=8, use_cuda=False, llm_gguf_dir=root / "models"),
        sync=SyncConfig(device_id=device_id, local_repo_dir=root / "sync"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="v3 daemon (Windows service)")
    parser.add_argument("--root", required=True)
    parser.add_argument("--http-port", type=int, default=8768)
    parser.add_argument("--ws-port", type=int, default=8767)
    parser.add_argument("--worker-mode", choices=("thread", "process"), default="process")
    parser.add_argument("--device-id", default="v3-daemon")
    args = parser.parse_args()

    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    daemon = V3Daemon(
        root, config=_config(root, args.device_id), http_port=args.http_port,
        ws_port=args.ws_port, worker_mode=args.worker_mode, use_llm=False,
    )
    daemon.start()
    stopped = threading.Event()

    def shutdown(*_args: object) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    stopped.wait()
    daemon.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
