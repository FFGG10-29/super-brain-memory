"""scripts/run_v3.py —— 一键启动 v3（Daemon + Worker + HTTP + WebSocket + 星云页）。

    .venv\\Scripts\\python.exe scripts/run_v3.py
    .venv\\Scripts\\python.exe scripts/run_v3.py --root data/v3 --no-browser

默认同进程线程模式（便于本机验证）；--separate 用 ProcessSupervisor 拉起独立 Worker 进程。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import BrainConfig, ModelConfig, StorageConfig, SyncConfig  # noqa: E402
from v3.daemon import V3Daemon  # noqa: E402


def _config(root: Path) -> BrainConfig:
    return BrainConfig(
        storage=StorageConfig(
            sqlite_path=root / "projection.db", lancedb_path=root / "vector.lance",
            kuzu_path=root / "graph.kuzu", lmdb_path=root / "kv.lmdb",
            duckdb_path=root / "timeseries.db",
        ),
        model=ModelConfig(dense_dim=8, use_cuda=False, llm_gguf_dir=root / "models"),
        sync=SyncConfig(device_id="v3-daemon", local_repo_dir=root / "sync"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="启动 v3 记忆内核")
    parser.add_argument("--root", default=str(ROOT / "data" / "v3"))
    parser.add_argument("--http-port", type=int, default=8768)
    parser.add_argument("--ws-port", type=int, default=8767)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--separate", action="store_true", help="Worker 独立进程（正式形态）")
    args = parser.parse_args()

    root = Path(args.root)
    if not root.is_absolute():
        root = (ROOT / root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    daemon = V3Daemon(
        root, config=_config(root), http_port=args.http_port,
        ws_port=args.ws_port, use_llm=False,
        worker_mode="process" if args.separate else "thread",
    )
    daemon.start()
    url = f"http://127.0.0.1:{args.http_port}/"
    print(f"v3 daemon ready: {url}  (ws://127.0.0.1:{args.ws_port}/ws)")
    print(f"journal: {daemon.journal.path}")
    print("Ctrl-C to stop")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        daemon.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
