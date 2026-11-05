"""MCP stdio 服务器验收：外部 Agent（WorkBuddy/CodeBuddy/DSH）经 MCP 协议读写记忆。

stdio 传输层由 mcp SDK 承担；本测试直接调用 server 注册的工具处理器，
与 stdio 走完全相同的代码路径（list_tools / call_tool handler）。
"""
from __future__ import annotations

import json

import mcp.types as types_module
import pytest

from config import BrainConfig, ModelConfig, StorageConfig, SyncConfig
from main import build_app


@pytest.fixture()
def desktop_config(tmp_path):
    return BrainConfig(
        storage=StorageConfig(
            sqlite_path=tmp_path / "r.db", lancedb_path=tmp_path / "v.lance",
            kuzu_path=tmp_path / "g.kuzu", lmdb_path=tmp_path / "kv.lmdb", duckdb_path=tmp_path / "t.db",
        ),
        model=ModelConfig(dense_dim=8, use_cuda=False, llm_gguf_dir=tmp_path / "models"),
        sync=SyncConfig(device_id="dev-mcp", local_repo_dir=tmp_path / "sync"),
    )


def test_mcp_tools_cover_six_tools(desktop_config):
    import anyio

    import gateway.mcp_stdio as mcp_stdio
    from gateway.mcp_stdio import build_server

    app = build_app(desktop_config)
    try:
        mcp_stdio._APP = app
        server = build_server()

        async def list_tools() -> list[str]:
            handler = server.request_handlers[types_module.ListToolsRequest]
            request = types_module.ListToolsRequest(method="tools/list")
            result = await handler(request)
            return [tool.name for tool in result.root.tools]

        names = anyio.run(list_tools)
        assert {"save_memory", "recall_memory", "timeline_query",
                "profile_read", "conflict_report", "health_check"} <= set(names)
    finally:
        app.close()


def test_mcp_save_recall_health_via_handlers(desktop_config):
    import anyio

    import gateway.mcp_stdio as mcp_stdio
    from gateway.mcp_stdio import build_server

    app = build_app(desktop_config)
    try:
        mcp_stdio._APP = app
        server = build_server()

        async def call(name: str, args: dict) -> dict:
            handler = server.request_handlers[types_module.CallToolRequest]
            request = types_module.CallToolRequest(
                method="tools/call",
                params=types_module.CallToolRequestParams(name=name, arguments=args),
            )
            result = await handler(request)
            return json.loads(result.root.content[0].text)

        save = anyio.run(call, "save_memory", {"content": "WorkBuddy 通过 MCP 写入的记忆", "agent_id": "workbuddy"})
        assert save["applied"] is True
        assert save["memory_type"] in ("fact", "preference", "scenario", "emotion", "profile")

        recall = anyio.run(call, "recall_memory", {"query": "WorkBuddy 写入", "agent_id": "codebuddy"})
        assert recall["hits"], "跨 agent 召回：同命名空间下同库可见"

        health = anyio.run(call, "health_check", {})
        assert health["engines"]
    finally:
        app.close()
