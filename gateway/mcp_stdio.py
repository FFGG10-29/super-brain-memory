"""gateway.mcp_stdio —— 标准 MCP stdio 服务器：外部 Agent（WorkBuddy / CodeBuddy / DSH 等）的接入点。

两种模式（自动选择，无需配置）：
- **桥接模式**（推荐，桌面开着时）：读到 `%APPDATA%/super-brain/mcp_bridge.json` 就把工具调用
  转发给正在运行的桌面内核——多个 Agent 同时接入也只操作**同一份记忆、同一个数据库**；
- **独占模式**（桌面没开）：自己 `build_app()` 打开六引擎。Kùzu 一个库只允许一个进程，
  所以多个 Agent 不能各自独占，桥接模式就是为此存在。

无论哪种模式，记忆都是**全局共享**的：agent_id 只写审计，不做隔离。

启动：`python -m gateway.mcp_stdio`（阻塞在 stdio，由 MCP 客户端拉起）
配置到客户端（例如 CodeBuddy / Claude Desktop 的 mcp.json）：
    {
      "mcpServers": {
        "super-brain-memory": {
          "command": "B:\\...\\my_brain_backend\\.venv\\Scripts\\python.exe",
          "args": ["-m", "gateway.mcp_stdio"],
          "env": {"BRAIN_NAMESPACE_POLICY": "private"}
        }
      }
    }

工具与 HTTP / MCPToolkit 完全同源：save_memory / recall_memory / timeline_query /
profile_read / conflict_report / health_check。
stdio 是本机信任通道：agent_id 由客户端传入（用于区分 WorkBuddy / CodeBuddy 等），
写入仍走完整丘脑流程（自动分类 + 去重 + PAD + 六引擎 + 事件日志）。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from config import get_config
from core.runtime_paths import local_bridge_path
from core.structured_log import configure_logging
from main import build_app

__all__ = ["build_server", "main"]

_APP: Any = None


def _get_app():
    global _APP
    if _APP is None:
        _APP = build_app(get_config())
    return _APP


# ── 本机桥接：多 Agent 同时接入的唯一可行姿势 ───────────────────────────────
# Kùzu 一个数据库文件只允许一个进程打开。三个 Agent 各自 spawn 一个 stdio 服务器时，
# 只有第一个能开库，其余会卡在写锁上。因此：桌面内核（或 `--serve`）持有引擎并开 HTTP，
# stdio 进程只做协议翻译 + HTTP 转发（/mcp/call）。地址与令牌从用户目录的 mcp_bridge.json 读。
def bridge_config() -> dict[str, str] | None:
    url = os.environ.get("SUPER_BRAIN_HTTP") or os.environ.get("SUPER_BRAIN_MCP_BRIDGE")
    token = os.environ.get("SUPER_BRAIN_TOKEN", "")
    if url:
        return {"url": url.rstrip("/"), "token": token}
    path = local_bridge_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    bridge_url = str(payload.get("url") or "").rstrip("/")
    if not bridge_url:
        return None
    if not _endpoint_alive(bridge_url):
        # 陈旧文件（上次内核被强杀）不能把 Agent 卡死：探测不通就当没有桥接，退回直连模式
        return None
    return {"url": bridge_url, "token": str(payload.get("token") or "")}


def _endpoint_alive(url: str) -> bool:
    from urllib.parse import urlparse
    import socket

    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not host:
        return False
    # 0.0.0.0 / :: 是"监听所有网卡"的写法，不能用来连：本机必须换成回环地址
    if host in ("0.0.0.0", "::", "::0"):
        host = "127.0.0.1"
    try:
        with socket.create_connection((host, parsed.port or 80), timeout=0.4):
            return True
    except OSError:
        return False


def _remote_call(bridge: dict[str, str], tool: str, args: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{bridge['url']}/mcp/call",
        data=json.dumps({"tool": tool, "arguments": args}, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            # 本机桥接密钥：由桌面内核写进 mcp_bridge.json，仅回环可用
            "X-Super-Brain-Bridge": bridge["token"],
            "Authorization": f"Bearer {bridge['token']}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        return {"error": f"bridge_http_{exc.code}", "message": detail[:400]}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {
            "error": "bridge_unreachable",
            "message": f"{type(exc).__name__}: {exc}；"
                       "请确认桌面端「接口与端口」页的 HTTP 服务在运行（或已开自动启动）",
        }


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _dispatch_local(app: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """直连模式（独占数据库）下的工具实现：桌面没开、也没有可用的 HTTP 桥接时用。"""
    agent_id = str(arguments.get("agent_id", "mcp-client"))
    if name == "save_memory":
        result = app.hippocampus.remember(str(arguments["content"]), agent_id=agent_id)
        return {
            "applied": result.applied,
            "id": result.neuron.uuid7_id,
            "reason": result.admission.reason,
            "memory_type": result.neuron.memory_type,
            "pad": result.neuron.pad.as_tuple(),
        }
    if name == "recall_memory":
        query = str(arguments.get("query", ""))
        limit = int(arguments.get("limit") or arguments.get("top_k") or 3)
        deep = bool(arguments.get("deep", False))
        from core.context_layers import get_context_layers
        from core.fast_recall import convergence_injection, fast_recall

        layers = get_context_layers()
        if not deep:
            # 独占模式默认也走轻量投影（<0.05s），与 HTTP 桥接路径对齐。
            rows = app.storage.relational.query("neuron", active_only=True, limit=500)
            result = fast_recall([dict(row) for row in rows], query, limit)
            hits = result["hits"]
            injection = convergence_injection(hits)
        else:
            hits_obj = app.prefrontal.recall(
                query, agent_id=agent_id, limit=limit,
            )
            hits = [
                {
                    "id": hit.neuron.uuid7_id,
                    "content": hit.neuron.content,
                    "score": round(hit.score, 4),
                    "channels": list(hit.channels),
                    "memory_type": hit.neuron.memory_type,
                }
                for hit in hits_obj
            ]
            injection = app.prefrontal.inject(hits_obj, agent_id=agent_id)
        projection = layers.compose(query)
        if projection:
            injection = (injection + "\n\n" + projection).strip()
        return {
            "hits": hits,
            "injection": injection,
            "context_layers": layers.status()["layers"],
            "recall_mode": "six-channel" if deep else "projection",
        }
    if name == "timeline_query":
        rows = app.storage.relational.query(
            "neuron", active_only=True, limit=int(arguments.get("limit", 20))
        )
        since = arguments.get("since_ms")
        until = arguments.get("until_ms")
        return {
            "memories": [
                {
                    "id": row["id"],
                    "content": row["content"],
                    "valid_from_ms": row.get("valid_from_ms"),
                    "memory_type": row.get("memory_type"),
                }
                for row in rows
                if (since is None or int(row.get("valid_from_ms") or 0) >= int(since))
                and (until is None or int(row.get("valid_from_ms") or 0) <= int(until))
            ]
        }
    if name == "profile_read":
        rows = app.storage.relational.query(
            "neuron", memory_types=["profile"], active_only=True, limit=50
        )
        return {"profiles": [{"id": row["id"], "content": row["content"]} for row in rows]}
    if name == "conflict_report":
        rows = app.storage.relational.query("neuron", active_only=False, limit=int(arguments.get("limit", 50)))
        return {
            "superseded": [
                {"id": row["id"], "content": row["content"], "tx_to": row.get("tx_to")}
                for row in rows
                if not row.get("is_active", True)
            ]
        }
    if name == "health_check":
        return {"engines": [status.as_dict() for status in app.health_report()]}
    # 分层上下文 / 硬件仪表等本地层能力：桌面没开、独占数据库时也能用
    from gateway.tool_catalog import dispatch_standalone
    local = dispatch_standalone(name, arguments)
    if local is not None:
        return local
    return {"error": f"未知工具：{name}（该工具需要桌面后端在线，请先启动超级大脑）"}


MCP_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "save_memory",
        "description": "写入一条记忆：自动分类（事实/偏好/情绪/画像）、SimHash 去重、情绪标注、六引擎落库",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "记忆正文"},
                "agent_id": {"type": "string", "description": "调用方标识，如 workbuddy / codebuddy"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "recall_memory",
        "description": "召回记忆：全文+语义+图谱+时间多通道融合，返回带分数的命中与注入上下文",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "查询语句"},
                "agent_id": {"type": "string"},
                "top_k": {"type": "integer", "description": "返回条数，默认 5"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "timeline_query",
        "description": "按时间窗口查询记忆（事件时间轴）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "since_ms": {"type": "integer"},
                "until_ms": {"type": "integer"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "profile_read",
        "description": "读取用户画像槽位（偏好、习惯等 profile 类型记忆）",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "conflict_report",
        "description": "查看被推翻 / 冲突的记忆清单",
        "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}},
    },
    {
        "name": "health_check",
        "description": "六引擎健康检查",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# 单一真源：stdio 的 list_tools() 与 HTTP 的 tools/list 共用同一张表。
# 下面保留的旧列表仅作迁移参考，实际暴露给 Agent 的是 catalog 版本。
from gateway.tool_catalog import MCP_TOOL_DEFS as _CATALOG_TOOL_DEFS  # noqa: E402
MCP_TOOL_DEFS = _CATALOG_TOOL_DEFS


def build_server() -> Server:
    server: Server = Server("super-brain-memory")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [Tool(**spec) for spec in MCP_TOOL_DEFS]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        args = dict(arguments or {})
        bridge = bridge_config()
        if bridge is not None:
            payload = _remote_call(bridge, name, args)
        else:
            payload = _dispatch_local(_get_app(), name, args)
        return [TextContent(type="text", text=_json(payload))]

    return server


async def _serve() -> None:
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> int:
    # stdout 是 JSON-RPC 通道：日志必须走 stderr，且要抢在 build_app 的首次 log_event 之前配置
    configure_logging(get_config().log_level, stream=sys.stderr)
    bridge = bridge_config()
    if bridge is None:
        try:
            _get_app()  # 冷启动先完成，MCP 客户端 initialize 时引擎已就绪
        except RuntimeError as exc:  # Kùzu 单写锁：桌面内核正在运行时会撞上
            print(
                f"super-brain MCP 启动失败：{exc}\n"
                "提示：Kùzu 一个库文件只能被一个进程打开。桌面端开着时请用桥接模式——"
                "先启动桌面应用（「接口与端口」页会自动开 HTTP），本进程会自动读取 "
                f"{local_bridge_path()} 走转发；或设环境变量 SUPER_BRAIN_HTTP 指向该地址。",
                file=sys.stderr,
            )
            return 2
    else:
        print(f"super-brain MCP 桥接模式 → {bridge['url']}（本进程不独占数据库）", file=sys.stderr)
    import anyio

    anyio.run(_serve)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(0)
