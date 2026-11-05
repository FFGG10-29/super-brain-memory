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

import json
import time
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

    def handle(self, method: str, path: str, *, headers: dict | None = None, body: dict | None = None) -> tuple[int, dict]:
        headers = dict(headers or {})
        body = dict(body or {})
        if method.upper() == "GET" and path.rstrip("/") in ("/health", "/v1/health"):
            return 200, {"status": "ok", "app": self._config.app_name, "ts_ms": int(time.time() * 1000)}

        try:
            self._registry.verify(self._bearer(headers))
        except Exception as exc:  # noqa: BLE001
            return 401, {"error": "unauthorized", "message": str(exc)}

        if method.upper() == "POST" and path.rstrip("/") == "/save":
            # 记忆插件常用字段：text / content / memory / query
            content = body.get("content") or body.get("text") or body.get("memory") or ""
            return 200, self._toolkit.call(
                "save_memory", {"content": content, "namespace": body.get("namespace")}, headers=headers
            )
        if method.upper() == "POST" and path.rstrip("/") == "/search":
            query = body.get("query") or body.get("text") or body.get("content") or ""
            return 200, self._toolkit.call(
                "recall_memory", {"query": query, "limit": body.get("limit")}, headers=headers
            )
        if method.upper() == "GET" and path.rstrip("/") == "/profile":
            return 200, self._toolkit.call("profile_read", {}, headers=headers)
        if method.upper() == "POST" and path.rstrip("/") == "/v1/chat/completions":
            return self._chat_completions(body, headers)
        return 404, {"error": "not_found", "path": path}

    def _chat_completions(self, body: dict, headers: dict) -> tuple[int, dict]:
        messages = body.get("messages") or []
        last_user = ""
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                last_user = str(message.get("content", ""))
                break
        recalled = self._toolkit.call("recall_memory", {"query": last_user}, headers=headers)
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
        port = int(port or self._config.gateway.http_port)

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
