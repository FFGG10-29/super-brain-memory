"""无头后端（Flutter 单前端改造的统一入口）。

一个进程内只装配**一份** `build_app()` 六引擎，同时提供：

- `127.0.0.1:8766` v2 HTTP 网关：`POST /mcp`（标准 MCP，Agent 接入）、`/mcp/call`、`/health` …
- `127.0.0.1:8768` v3 HTTP：`/v1/health`、`/v1/events`、只读投影、WebGL 星云静态页
- `127.0.0.1:8767` v3 WebSocket：Flutter 全部 RPC + node_created/pulse 广播

为什么必须单进程：Kùzu 一个库只允许一个写者，MCP 网关与 v3 Worker 若各自
`build_app()` 会抢锁；而 `V3Daemon._projection_db` 取的是 `config.storage.sqlite_path`，
共用同一 BrainConfig 后 v3 只读投影与 v2 主库天然是同一份数据。

启动（开发态，Flutter 会自动拉起这条命令）：
    .venv\\Scripts\\python.exe -m headless_server
Flutter 端只负责 UI 与拉起/探活，后端崩溃或换机时不带任何 Qt 依赖。
"""
from __future__ import annotations

import argparse
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from config import BrainConfig, get_config
from core.runtime_paths import (
    clear_local_bridge,
    read_local_token,
    write_local_bridge,
    write_local_token,
)
from core.user_config import apply_overrides, load_overrides
from gateway.bridge_launcher import ensure_launcher
from gateway.http_adapter import HTTPAdapter
from gateway.mcp_server import MCPToolkit
from gateway.registry import AgentRegistry
from main import build_app
from v3.daemon import V3Daemon

__all__ = ["HeadlessServer", "create_server", "serve"]


class HeadlessServer:
    def __init__(
        self,
        config: BrainConfig | None = None,
        *,
        root: str | Path | None = None,
        http_port: int = 8768,
        ws_port: int = 8767,
        mcp_port: int | None = None,
        use_llm: bool = True,
        circadian: bool = True,
    ) -> None:
        self.config = config or get_config()
        apply_overrides(self.config, load_overrides())
        self.app = build_app(self.config)

        # 唯一真源：v2 主库路径的兄弟目录放 v3 的 journal/inbox（不另开六引擎）
        sqlite_path = Path(self.config.storage.sqlite_path)
        self.root = Path(root) if root else sqlite_path.parent / "v3"
        self.daemon = V3Daemon(
            self.root, config=self.config, http_port=http_port, ws_port=ws_port,
            worker_mode="thread", use_llm=use_llm, app=self.app,
        )

        self.registry = AgentRegistry(
            self.app.storage.kv, timeseries=self.app.storage.timeseries, config=self.config
        )
        self.toolkit = MCPToolkit(
            registry=self.registry, hippocampus=self.app.hippocampus,
            prefrontal=self.app.prefrontal, relational=self.app.storage.relational,
            engines=self.app.storage.all(), config=self.config,
        )
        self.bridge_token = read_local_token() or secrets.token_urlsafe(24)
        if not read_local_token():
            write_local_token(self.bridge_token)
        self.adapter = HTTPAdapter(
            registry=self.registry, toolkit=self.toolkit,
            config=self.config, bridge_token=self.bridge_token,
        )
        self.mcp_port = int(mcp_port if mcp_port is not None else self.config.gateway.http_port)
        self._mcp_server: Any = None
        self._mcp_thread: threading.Thread | None = None
        self._circadian = circadian
        # Flutter「应用并重启内核」会走到 v3 的 restart RPC；挂钩子保证 MCP 网关同步换核
        self.daemon.restart_kernel = self.restart_kernel

    def start(self) -> dict[str, Any]:
        # v3 先起（WS/HTTP/worker 用注入的 app），再起 MCP 网关
        self.daemon.start()
        self._mcp_server = self.adapter.make_server(host="127.0.0.1", port=self.mcp_port)
        self._mcp_thread = threading.Thread(
            target=self._mcp_server.serve_forever,
            kwargs={"poll_interval": 0.2}, name="mcp-http", daemon=True,
        )
        self._mcp_thread.start()
        actual_port = int(self._mcp_server.server_address[1])
        agent_id, _ = self.registry.register("headless-mcp")
        write_local_bridge(
            host="127.0.0.1", port=actual_port,
            token=str(self.bridge_token), agent_id=agent_id,
        )
        launcher = ensure_launcher()
        if self._circadian:
            self.app.circadian.start()
        return {
            "mcp_url": f"http://127.0.0.1:{actual_port}/mcp",
            "http_url": f"http://127.0.0.1:{self.daemon._http_port}/",
            "ws_url": f"ws://127.0.0.1:{self.daemon.hub.port}/ws",
            "root": str(self.root),
            "stdio_launcher": str(launcher) if launcher else "",
        }

    def restart_kernel(self) -> dict[str, Any]:
        """按最新配置重建内核，并让 v3 Worker、MCP toolkit、registry 全部切到新对象。"""
        old = self.app
        if self._circadian:
            old.circadian.stop()
        # Kùzu 同路径只允许一个 Database：必须先关旧内核再开新内核
        old.close()
        try:
            fresh = build_app(self.config)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"内核重启失败，请重启后端进程：{type(exc).__name__}: {exc}"}
        self.app = fresh
        self.daemon.app = fresh
        if self.daemon.worker is not None:
            self.daemon.worker._app = fresh
        self.daemon._memory_service = None
        self.registry._kv = fresh.storage.kv
        self.registry._timeseries = fresh.storage.timeseries
        self.toolkit._hippocampus = fresh.hippocampus
        self.toolkit._prefrontal = fresh.prefrontal
        self.toolkit._relational = fresh.storage.relational
        self.toolkit._engines = fresh.storage.all()
        if self._circadian:
            fresh.circadian.start()
        return {"ok": True, "message": "内核已按新配置重启（MCP 网关与 v3 Worker 已同步切核）"}

    def stop(self) -> None:
        if self._circadian:
            self.app.circadian.stop()
        if self._mcp_server is not None:
            self._mcp_server.shutdown()
            self._mcp_server.server_close()
        if self._mcp_thread is not None:
            self._mcp_thread.join(timeout=2.0)
        clear_local_bridge()
        self.app.journal.flush()
        self.daemon.stop()  # 内部会 app.close()（唯一持有者）


def create_server(**kwargs: Any) -> HeadlessServer:
    return HeadlessServer(**kwargs)


def serve(server: HeadlessServer) -> None:
    info = server.start()
    print("无头后端已就绪：")
    for key, value in info.items():
        print(f"  {key}: {value}")
    print("Ctrl-C 优雅停止…")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="超级大脑无头后端（MCP + v3 WS/HTTP，单进程共享六引擎）")
    parser.add_argument("--root", default=None, help="v3 journal/inbox 目录，默认 <sqlite_path>/../v3")
    parser.add_argument("--http-port", type=int, default=8768)
    parser.add_argument("--ws-port", type=int, default=8767)
    parser.add_argument("--mcp-port", type=int, default=None, help="默认取 config.gateway.http_port（8766）")
    parser.add_argument("--fast", action="store_true", help="v3 Worker 跳过 LLM 抽取（批量导入更快）")
    parser.add_argument("--no-circadian", action="store_true", help="不启动昼夜巩固调度")
    args = parser.parse_args(argv)
    server = HeadlessServer(
        root=args.root, http_port=args.http_port, ws_port=args.ws_port,
        mcp_port=args.mcp_port, use_llm=not args.fast, circadian=not args.no_circadian,
    )
    serve(server)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
