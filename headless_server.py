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
Flutter 端只负责 UI 与拉起/探活，后端崩溃或换机时无需任何额外 UI 依赖。
"""
from __future__ import annotations

import argparse
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from brain.current_pad_tracker import CurrentPadTracker
from brain.write_gate import WriteGate
from chat.engine import ChatEngine
from chat.session_registry import SessionRegistry
from config import BrainConfig, get_config
from core.desktop_features import default_secret_store
from core.fast_recall import fast_recall_projected
from gateway.media_service import MediaService
from gateway.model_router import get_model_router
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

__all__ = ["HeadlessServer", "create_server", "ensure_chat_token", "serve"]


def ensure_chat_token(path: str | None = None) -> str:
    """A2 / Q22：chat 专用令牌，持久在文件里，绝不复用 MCP 的 Bridge 令牌。"""
    from core.runtime_paths import user_dir

    token_path = Path(path) if path else user_dir() / "chat_token.txt"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    if token_path.exists():
        existing = token_path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(24)
    token_path.write_text(token, encoding="utf-8")
    return token


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
        # W1（Q17/A1）：先建统一闸门，再装配内核 —— build_app 内部的两条后台写入路径要接线
        self.write_gate = WriteGate()
        self.app = build_app(self.config, write_gate=self.write_gate)

        # 唯一真源：v2 主库路径的兄弟目录放 v3 的 journal/inbox（不另开六引擎）
        sqlite_path = Path(self.config.storage.sqlite_path)
        self.root = Path(root) if root else sqlite_path.parent / "v3"
        self.daemon = V3Daemon(
            self.root, config=self.config, http_port=http_port, ws_port=ws_port,
            worker_mode="thread", use_llm=use_llm, app=self.app,
        )
        self.daemon.write_gate = self.write_gate

        self.registry = AgentRegistry(
            self.app.storage.kv, timeseries=self.app.storage.timeseries, config=self.config
        )
        self.toolkit = MCPToolkit(
            registry=self.registry, hippocampus=self.app.hippocampus,
            prefrontal=self.app.prefrontal, relational=self.app.storage.relational,
            engines=self.app.storage.all(), config=self.config,
            rpc_fallback=self._rpc_via_daemon,
        )
        self.bridge_token = read_local_token() or secrets.token_urlsafe(24)
        if not read_local_token():
            write_local_token(self.bridge_token)
        self.adapter = HTTPAdapter(
            registry=self.registry, toolkit=self.toolkit,
            config=self.config, bridge_token=self.bridge_token,
            write_gate=self.write_gate,
        )
        self.mcp_port = int(mcp_port if mcp_port is not None else self.config.gateway.http_port)
        self._mcp_server: Any = None
        self._mcp_thread: threading.Thread | None = None
        self._circadian = circadian
        # Flutter「应用并重启内核」会走到 v3 的 restart RPC；挂钩子保证 MCP 网关同步换核
        self.daemon.restart_kernel = self.restart_kernel
        # 托盘「完全退出」通过 system.shutdown RPC 触发优雅停机（含 WAL 收缩）
        self.shutdown_event = threading.Event()
        self.daemon.shutdown_callback = self.request_shutdown
        self._wal_thread: threading.Thread | None = None
        # W3 / P2 + P3：chat 专用令牌、专用 Agent、模型路由与 8769 引擎
        self.chat_token = ensure_chat_token(self.config.chat.token_file or None)
        if str(self.config.chat.host) not in ("127.0.0.1", "localhost", ""):
            print(f"[警告] 8769 已暴露到局域网（{self.config.chat.host}），传输为明文；"
                  "仅使用 chat 专用令牌，不复用 Bridge 令牌")
        _, chat_agent_token = self.registry.register("chat-engine")
        self.chat_agent = self.registry.verify(chat_agent_token)
        self.model_router = get_model_router(
            self.config, secret_store=default_secret_store(), local_llm=self.app.llm,
        )
        # W6 / C12：巩固代理复用同一模型路由；叙事重构由昼夜节律窗口触发
        self.app.neocortex._router = self.model_router
        # W5：聊天与 IDE 查询共用同一份「当前会话 PAD」；换核时追踪器不重建
        self.pad_tracker = CurrentPadTracker(self.config)
        self.app.prefrontal.pad_tracker = self.pad_tracker
        # W8 / A4：单 topic 的只读投影走叙事缓存（钩子闭包动态读 self.app，换核后自动生效）
        self.daemon.projection_hook = lambda topic, query, limit: fast_recall_projected(
            topic, query, relational=self.app.storage.relational,
            kv=self.app.storage.kv, limit=limit,
        )
        self.media_service = MediaService(self.config)
        self.chat_engine = ChatEngine(
            config=self.config, toolkit=self.toolkit, router=self.model_router,
            write_gate=self.write_gate, agent=self.chat_agent, token=self.chat_token,
            sessions=SessionRegistry(), pad_tracker=self.pad_tracker,
            media_service=self.media_service,
            event_sink=lambda content, **kw: self.daemon.handle_event(content, **kw),
        )

    def _rpc_via_daemon(self, method: str, args: dict[str, Any]) -> dict[str, Any]:
        """MCP 扩展工具 → v3 RPC。这样 v3 的全部独特能力无需在传输层重复实现。"""
        from v3.rpc import RpcHandler
        reply = RpcHandler(self.daemon).dispatch({"method": method, "args": args})
        if not reply.get("ok"):
            return {"error": "RpcError", "method": method,
                    "message": str(reply.get("error") or "RPC 调用失败")}
        result = reply.get("result")
        return result if isinstance(result, dict) else {"result": result}

    def request_shutdown(self) -> None:
        """延迟 1 秒置位，让 RPC 响应先发回前端，再走 serve() 的优雅清理。"""
        threading.Timer(1.0, self.shutdown_event.set).start()

    def _wal_targets(self) -> tuple[str | Path, ...]:
        """主库 + v3 目录下所有 SQLite 库（inbox / journal / 投影）。"""
        extra: list[str | Path] = []
        try:
            extra.extend(sorted(self.root.glob("*.db")))
            extra.extend(sorted(self.root.glob("*.sqlite3")))
        except OSError:
            pass
        return tuple(extra)

    def checkpoint_wal(self) -> list[dict[str, Any]]:
        """立即收缩 WAL（停机前 / 空闲定时都会调）。"""
        from core.wal_maintenance import checkpoint_data_dir
        return checkpoint_data_dir(self.config.storage.sqlite_path, extra=self._wal_targets())

    def _start_wal_maintenance(self, interval_s: float = 300.0) -> None:
        """每 5 分钟在空闲时 TRUNCATE 一次，防止 -wal 挂机膨胀。"""
        def loop() -> None:
            while not self.shutdown_event.wait(interval_s):
                try:
                    self.checkpoint_wal()
                except Exception:  # noqa: BLE001  维护线程不能拖垮服务
                    pass
        self._wal_thread = threading.Thread(
            target=loop, name="wal-maintenance", daemon=True,
        )
        self._wal_thread.start()

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
        if self.config.chat.enabled:
            self.chat_engine.start()
        if self._circadian:
            self.app.circadian.start()
        self._start_wal_maintenance()
        return {
            "mcp_url": f"http://127.0.0.1:{actual_port}/mcp",
            "http_url": f"http://127.0.0.1:{self.daemon._http_port}/",
            "ws_url": f"ws://127.0.0.1:{self.daemon.hub.port}/ws",
            "root": str(self.root),
            "stdio_launcher": str(launcher) if launcher else "",
            "chat_host": str(self.config.chat.host),
            "chat_port": int(self.config.chat.port),
        }

    def restart_kernel(self) -> dict[str, Any]:
        """按最新配置重建内核；闸门、Worker 线程、MCP toolkit、registry 一起切核。

        W1：顺序为「停 Worker → 排空旧闸门 → 关旧引擎 → 建新闸门 → 装新内核 → 换全部引用」。
        否则残留写入会打到已关闭的旧引擎（§15.11 第 4 条同类坑）。
        """
        old = self.app
        old_gate = self.write_gate
        old_worker = self.daemon.worker
        if old_worker is not None:
            old_worker.stop()
            thread = getattr(self.daemon, "_worker_thread", None)
            if thread is not None:
                thread.join(timeout=2.0)
        if self._circadian:
            old.circadian.stop()
        old_gate.shutdown(timeout=5)
        # Kùzu 同路径只允许一个 Database：必须先关旧内核再开新内核
        old.close()
        # 新闸门必须先于 build_app 建立：组合根内两条后台写入路径要接线
        new_gate = WriteGate()
        try:
            fresh = build_app(self.config, write_gate=new_gate)
        except Exception as exc:  # noqa: BLE001
            new_gate.shutdown(timeout=1)
            return {"ok": False, "message": f"内核重启失败，请重启后端进程：{type(exc).__name__}: {exc}"}
        self.write_gate = new_gate
        self.app = fresh
        self.daemon.app = fresh
        self.daemon.write_gate = new_gate
        self.adapter.rebind_write_gate(new_gate)
        self.chat_engine.rebind(new_gate)  # W3：聊天写入也要切到新闸门
        fresh.prefrontal.pad_tracker = self.pad_tracker  # W5：换核后追踪器继续生效
        fresh.neocortex._router = self.model_router      # W6：巩固代理继续复用
        self.daemon._memory_service = None
        self.registry._kv = fresh.storage.kv
        self.registry._timeseries = fresh.storage.timeseries
        self.toolkit._hippocampus = fresh.hippocampus
        self.toolkit._prefrontal = fresh.prefrontal
        self.toolkit._relational = fresh.storage.relational
        self.toolkit._engines = fresh.storage.all()
        if old_worker is not None:
            from v3.worker import V3Worker

            self.daemon.worker = V3Worker(
                self.daemon.inbox, self.daemon.alarm, fresh,
                hub=self.daemon.hub,
                use_llm=getattr(self.daemon, "_use_llm", True),
                write_gate=new_gate,
            )
            self.daemon._worker_thread = threading.Thread(
                target=self.daemon.worker.run, name="v3-worker", daemon=True,
            )
            self.daemon._worker_thread.start()
        if self._circadian:
            fresh.circadian.start()
        return {"ok": True, "message": "内核已按新配置重启（WriteGate / MCP 网关 / v3 Worker 已同步切核）"}

    def stop(self) -> None:
        if self._circadian:
            self.app.circadian.stop()
        if self._mcp_server is not None:
            self._mcp_server.shutdown()
            self._mcp_server.server_close()
        if self._mcp_thread is not None:
            self._mcp_thread.join(timeout=2.0)
        if self.config.chat.enabled:
            self.chat_engine.stop()
        # W1（停机顺序）：先停 Worker 循环 → 排空 WriteGate → 之后才允许 daemon.stop() 关引擎。
        # 反过来写会让队列里残留的写入打到已关闭的六引擎。
        if self.daemon.worker is not None:
            self.daemon.worker.stop()
        worker_thread = getattr(self.daemon, "_worker_thread", None)
        if worker_thread is not None:
            worker_thread.join(timeout=2.0)
        self.write_gate.shutdown(timeout=5)
        # 停机前收缩 WAL，避免被强杀/重启后留下巨大 -wal 与锁
        try:
            self.checkpoint_wal()
        except Exception:  # noqa: BLE001
            pass
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
    print("Ctrl-C 或 system.shutdown 优雅停止…")
    try:
        while not server.shutdown_event.wait(0.5):
            pass
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
    parser.add_argument("--chat-host", default=None,
                        help="8769 绑定地址（默认 127.0.0.1；0.0.0.0 显式开启局域网）")
    parser.add_argument("--chat-port", type=int, default=None, help="8769 聊天端口")
    parser.add_argument("--chat-token-file", default=None,
                        help="chat 专用令牌文件；暴露到局域网时必须显式指定")
    args = parser.parse_args(argv)
    from config import get_config

    cfg = get_config()
    chat_host = args.chat_host or cfg.chat.host
    if chat_host not in ("127.0.0.1", "localhost") and not args.chat_token_file:
        parser.error("暴露到局域网（--chat-host 0.0.0.0）时必须同时指定 --chat-token-file")
    cfg.chat.host = chat_host
    if args.chat_port is not None:
        cfg.chat.port = args.chat_port
    if args.chat_token_file:
        cfg.chat.token_file = args.chat_token_file
    server = HeadlessServer(
        root=args.root, http_port=args.http_port, ws_port=args.ws_port,
        mcp_port=args.mcp_port, use_llm=not args.fast, circadian=not args.no_circadian,
    )
    serve(server)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
