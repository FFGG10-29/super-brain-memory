"""gateway.mcp_stdio —— 标准 MCP stdio 服务器：外部 Agent（WorkBuddy / CodeBuddy / DSH 等）的接入点。

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
import sys
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from config import get_config
from main import build_app

__all__ = ["build_server", "main"]

_APP: Any = None


def _get_app():
    global _APP
    if _APP is None:
        _APP = build_app(get_config())
    return _APP


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def build_server() -> Server:
    server: Server = Server("super-brain-memory")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="save_memory",
                description="写入一条记忆：自动分类（事实/偏好/情绪/画像）、SimHash 去重、情绪标注、六引擎落库",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "记忆正文"},
                        "agent_id": {"type": "string", "description": "调用方标识，如 workbuddy / codebuddy"},
                    },
                    "required": ["content"],
                },
            ),
            Tool(
                name="recall_memory",
                description="召回记忆：全文+语义+图谱+时间多通道融合，返回带分数的命中与注入上下文",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "查询语句"},
                        "agent_id": {"type": "string"},
                        "top_k": {"type": "integer", "description": "返回条数，默认 5"},
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="timeline_query",
                description="按时间窗口查询记忆（事件时间轴）",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "since_ms": {"type": "integer"},
                        "until_ms": {"type": "integer"},
                        "limit": {"type": "integer"},
                    },
                },
            ),
            Tool(
                name="profile_read",
                description="读取用户画像槽位（偏好、习惯等 profile 类型记忆）",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="conflict_report",
                description="查看被推翻 / 冲突的记忆清单",
                inputSchema={"type": "object", "properties": {"limit": {"type": "integer"}}},
            ),
            Tool(
                name="health_check",
                description="六引擎健康检查",
                inputSchema={"type": "object", "properties": {}},
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        app = _get_app()
        agent_id = str(arguments.get("agent_id", "mcp-client"))
        if name == "save_memory":
            result = app.hippocampus.remember(str(arguments["content"]), agent_id=agent_id)
            payload = {
                "applied": result.applied,
                "id": result.neuron.uuid7_id,
                "reason": result.admission.reason,
                "memory_type": result.neuron.memory_type,
                "pad": result.neuron.pad.as_tuple(),
            }
        elif name == "recall_memory":
            hits = app.prefrontal.recall(
                str(arguments["query"]), agent_id=agent_id, limit=int(arguments.get("top_k", 5))
            )
            payload = {
                "hits": [
                    {
                        "id": hit.neuron.uuid7_id,
                        "content": hit.neuron.content,
                        "score": round(hit.score, 4),
                        "channels": list(hit.channels),
                        "memory_type": hit.neuron.memory_type,
                    }
                    for hit in hits
                ]
            }
        elif name == "timeline_query":
            rows = app.storage.relational.query(
                "neuron", active_only=True, limit=int(arguments.get("limit", 20))
            )
            since = arguments.get("since_ms")
            until = arguments.get("until_ms")
            payload = {
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
        elif name == "profile_read":
            rows = app.storage.relational.query(
                "neuron", memory_types=["profile"], active_only=True, limit=50
            )
            payload = {"profiles": [{"id": row["id"], "content": row["content"]} for row in rows]}
        elif name == "conflict_report":
            rows = app.storage.relational.query("neuron", active_only=False, limit=int(arguments.get("limit", 50)))
            payload = {
                "superseded": [
                    {"id": row["id"], "content": row["content"], "tx_to": row.get("tx_to")}
                    for row in rows
                    if not row.get("is_active", True)
                ]
            }
        elif name == "health_check":
            payload = {"engines": [status.as_dict() for status in app.health_report()]}
        else:
            payload = {"error": f"未知工具：{name}"}
        return [TextContent(type="text", text=_json(payload))]

    return server


async def _serve() -> None:
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> int:
    _get_app()  # 冷启动先完成，MCP 客户端 initialize 时引擎已就绪
    import anyio

    anyio.run(_serve)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(0)
