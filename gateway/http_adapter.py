"""gateway.http_adapter —— HTTP / OpenAI 兼容适配器（零额外依赖即可跑）。

端点：
    GET  /health                  无需鉴权
    POST /save                    记忆插件友好（字段按插件规格映射）
    POST /search                  同上
    GET  /profile                 画像槽位
    POST /v1/chat/completions     OpenAI 兼容：把召回结果作为记忆上下文注入

插件侧零改动：字段翻译在适配器里完成，不要求客户端改协议。
`serve()` 用标准库 ThreadingHTTPServer，FastAPI/uvicorn 只是可选增强。
"""
from __future__ import annotations

import hmac
import json
import queue
import secrets
import threading
import time
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from config import BrainConfig, get_config
from gateway.mcp_server import MCPToolkit
from gateway.registry import AgentRegistry

__all__ = ["HTTPAdapter"]


class HTTPAdapter:
    def __init__(self, *, registry: AgentRegistry, toolkit: MCPToolkit, config: BrainConfig | None = None) -> None:
        self._registry = registry
        self._toolkit = toolkit
        self._config = config or get_config()
        # 本机桥接专用：/mcp/call 用小范围共享密钥认证，再映射到一个服务端注册的 agent。
        # 不用 registry 签发的令牌——未配 GW_MCP_TOKEN_SECRET 时签名密钥是进程内临时值，
        # 桌面重启后旧令牌立刻失效，多 Agent 的 stdio 进程会莫名 401。
        self._bridge_token = secrets.token_urlsafe(24)
        self._bridge_agent_token: str | None = None
        # 入口即闸门：所有 Agent 的工具调用先入队，再由**唯一的工作线程**逐个执行。
        # 六引擎与 ONNX 会话都不是并发安全的；更重要是语义——记忆必须由中枢统一裁决
        # （丘脑准入、去重、冲突消解），不能被多个请求穿插着直写存储。
        self._jobs: queue.Queue = queue.Queue()
        self._worker = threading.Thread(
            target=self._consume, name="brain-tool-queue", daemon=True
        )
        self._worker.start()

    # ── 入口队列（统一由中枢串行处理）──────────────────────────────────────
    @property
    def queue_depth(self) -> int:
        """排队中的请求数（桌面状态栏可显示，便于观察多 Agent 争用）。"""
        return self._jobs.qsize()

    def _consume(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                self._jobs.task_done()
                return
            tool, args, headers, future = job
            try:
                future.set_result(self._toolkit.call(tool, args, headers=headers))
            except BaseException as exc:  # noqa: BLE001  异常必须回传给调用方，不能让请求悬空
                future.set_exception(exc)
            finally:
                self._jobs.task_done()

    # ── 本机桥接 ──────────────────────────────────────────────────────────
    @property
    def bridge_token(self) -> str:
        return self._bridge_token

    def _bridge_credentials(self) -> dict:
        """桥接调用统一用服务端自己注册的 agent，权限固定 read/write + private 命名空间。"""
        if self._bridge_agent_token is None:
            _, token = self._registry.register("mcp-bridge")
            self._bridge_agent_token = token
        return {"Authorization": f"Bearer {self._bridge_agent_token}"}

    def handle(self, method: str, path: str, *, headers: dict | None = None, body: dict | None = None) -> tuple[int, dict]:
        headers = dict(headers or {})
        body = dict(body or {})
        if method.upper() == "GET" and path.rstrip("/") in ("/health", "/v1/health"):
            return 200, {"status": "ok", "app": self._config.app_name, "ts_ms": int(time.time() * 1000)}

        # 桥接通道先于令牌校验：只认 mcp_bridge.json 里那把密钥（本机文件 + 本机回环）
        if method.upper() == "POST" and path.rstrip("/") == "/mcp/call":
            supplied = str(
                headers.get("X-Super-Brain-Bridge") or headers.get("x-super-brain-bridge") or ""
            )
            if not supplied or not hmac.compare_digest(supplied, self._bridge_token):
                return 401, {"error": "unauthorized", "message": "本机桥接密钥无效"}
            tool = str(body.get("tool") or "").strip()
            if not tool:
                return 400, {"error": "invalid_request", "message": "缺少 tool"}
            args = body.get("arguments") or {}
            if not isinstance(args, dict):
                return 400, {"error": "invalid_request", "message": "arguments 必须是对象"}
            return 200, self._call_tool(tool, args, headers=self._bridge_credentials())

        try:
            self._registry.verify(self._bearer(headers))
        except Exception as exc:  # noqa: BLE001
            return 401, {"error": "unauthorized", "message": str(exc)}

        if method.upper() == "POST" and path.rstrip("/") == "/save":
            # 记忆插件常用字段：text / content / memory / query
            content = body.get("content") or body.get("text") or body.get("memory") or ""
            return 200, self._call_tool(
                "save_memory", {"content": content, "namespace": body.get("namespace")}, headers=headers
            )
        if method.upper() == "POST" and path.rstrip("/") == "/search":
            query = body.get("query") or body.get("text") or body.get("content") or ""
            return 200, self._call_tool(
                "recall_memory", {"query": query, "limit": body.get("limit")}, headers=headers
            )
        if method.upper() == "GET" and path.rstrip("/") == "/profile":
            return 200, self._call_tool("profile_read", {}, headers=headers)
        if method.upper() == "POST" and path.rstrip("/") == "/v1/chat/completions":
            return self._chat_completions(body, headers)
        return 404, {"error": "not_found", "path": path}

    def _call_tool(self, tool: str, args: dict, *, headers: dict) -> dict:
        """入队并等待结果：并发请求自动排队，中枢按 FIFO 逐条处理。"""
        future: Future = Future()
        self._jobs.put((tool, args, headers, future))
        return future.result(timeout=300)

    def _chat_completions(self, body: dict, headers: dict) -> tuple[int, dict]:
        messages = body.get("messages") or []
        last_user = ""
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                last_user = str(message.get("content", ""))
                break
        recalled = self._call_tool("recall_memory", {"query": last_user}, headers=headers)
        injection = recalled.get("injection", "")
        # 本地无 LLM 时不伪造模型回答：把记忆上下文作为 assistant 内容返回并标注 degraded
        content = injection or "（无相关记忆）"
        return 200, {
            "id": f"chatcmpl-{int(time.time() * 1000)}",
            "object": "chat.completion",
            "model": body.get("model") or "super-brain-memory",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "memory": recalled.get("hits", []),
            "degraded": True,
        }

    @staticmethod
    def _bearer(headers: dict) -> str:
        authorization = str(headers.get("Authorization") or headers.get("authorization") or "")
        return authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""

    def make_server(self, *, host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
        """构造服务实例（暴露给测试/一键启停脚本，便于 shutdown）。"""
        adapter = self
        host = host or self._config.gateway.http_host
        # port=0 是合法语义（内核分配随机空闲端口），不能被 `or` 吞掉
        port = int(self._config.gateway.http_port if port is None else port)

        class Handler(BaseHTTPRequestHandler):
            def _respond(self, status: int, payload: dict) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _dispatch(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body: dict[str, Any] = {}
                if length:
                    try:
                        body = json.loads(self.rfile.read(length).decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        self._respond(400, {"error": "invalid_json"})
                        return
                status, payload = adapter.handle(method, self.path.split("?")[0], headers=dict(self.headers), body=body)
                self._respond(status, payload)

            def do_GET(self) -> None:  # noqa: N802
                self._dispatch("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._dispatch("POST")

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        return ThreadingHTTPServer((host, port), Handler)

    def serve(self, *, host: str | None = None, port: int | None = None) -> None:
        """标准库 HTTP 服务（阻塞）。生产可换 FastAPI/uvicorn，接口不变。"""
        server = self.make_server(host=host, port=port)
        try:
            server.serve_forever()
        finally:
            server.server_close()


