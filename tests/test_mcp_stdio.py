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


def test_multi_agent_bridge_forwards_to_single_kernel(desktop_config, tmp_path, monkeypatch):
    """三个 Agent 同时接入：桌面内核独占 Kùzu，stdio 进程走 /mcp/call 转发。"""
    monkeypatch.setenv("SUPER_BRAIN_HOME", str(tmp_path / "home"))
    from core.runtime_paths import local_bridge_path

    import gateway.mcp_stdio as mcp_stdio
    from ui.desktop.backend import DesktopBackend

    backend = DesktopBackend(desktop_config)
    try:
        assert backend.start_http("127.0.0.1", 0)["ok"] is True
        payload = json.loads(local_bridge_path().read_text(encoding="utf-8"))
        assert payload["token"] and payload["url"].startswith("http://127.0.0.1:")

        bridge = mcp_stdio.bridge_config()
        assert bridge is not None, "stdio 进程必须能自动发现桌面内核"
        assert bridge["url"] == payload["url"]

        saved = mcp_stdio._remote_call(
            bridge, "save_memory", {"content": "三个 Agent 共用同一内核", "agent_id": "codebuddy"}
        )
        assert saved["applied"] is True, saved
        recalled = mcp_stdio._remote_call(
            bridge, "recall_memory", {"query": "共用同一内核", "agent_id": "dsh", "top_k": 3}
        )
        assert recalled["hits"], recalled
        assert mcp_stdio._remote_call(bridge, "health_check", {})["engines"]
    finally:
        backend.stop_http()
        backend.close()
    assert not local_bridge_path().exists(), "停服务要清桥接文件，避免 Agent 连到死端口"


def test_memories_are_shared_across_all_agents(desktop_config):
    """核心承诺：超级大脑是外置共享大脑——A 写的记忆，B 立刻能召回，不需任何额外参数。"""
    from gateway.mcp_server import MCPToolkit
    from gateway.registry import AgentRegistry

    app = build_app(desktop_config)
    try:
        registry = AgentRegistry(app.storage.kv, timeseries=app.storage.timeseries, config=desktop_config)
        toolkit = MCPToolkit(
            registry=registry,
            hippocampus=app.hippocampus,
            prefrontal=app.prefrontal,
            relational=app.storage.relational,
            engines=app.storage.all(),
            config=desktop_config,
        )
        codebuddy = {"Authorization": f"Bearer {registry.register('codebuddy')[1]}"}
        dsh = {"Authorization": f"Bearer {registry.register('dsh')[1]}"}

        saved = toolkit.call(
            "save_memory", {"content": "共享实验：周五发版流程改为双人复核", "agent_id": "codebuddy"},
            headers=codebuddy,
        )
        assert saved["applied"] is True, saved

        # 换一个 Agent、不带 namespace、不做任何额外配置
        recalled = toolkit.call("recall_memory", {"query": "双人复核"}, headers=dsh)
        contents = [hit["content"] for hit in recalled["hits"]]
        assert any("双人复核" in text for text in contents), contents

        # 反向：dsh 写的，codebuddy 也能读到
        toolkit.call("save_memory", {"content": "共享实验：DSH 记录的排期是 11 月 20 日"}, headers=dsh)
        back = toolkit.call("recall_memory", {"query": "11 月 20 日"}, headers=codebuddy)
        assert any("11 月 20 日" in hit["content"] for hit in back["hits"]), back["hits"]

        # 两条记忆落在同一个命名空间（中枢唯一库），不是每个 Agent 一份
        rows = app.storage.relational.query("neuron", limit=50)
        assert len({str(row["namespace"]) for row in rows}) == 1
    finally:
        app.close()


def test_structured_log_can_target_stderr_for_stdio(monkeypatch):
    """stdio 模式下 stdout 是 JSON-RPC 通道：日志插进去会让客户端解析失败。"""
    import io

    import structlog

    from core import structured_log as structured

    buffer = io.StringIO()
    monkeypatch.setattr(structured, "_CONFIGURED", False)
    structlog.reset_defaults()
    try:
        structured.configure_logging("INFO", stream=buffer)
        structured.log_event("stdio_channel_probe", agent_id="smoke")
        assert "stdio_channel_probe" in buffer.getvalue(), "日志必须写进指定流"
    finally:
        # 复原进程级日志配置，避免影响后续用例
        monkeypatch.setattr(structured, "_CONFIGURED", False)
        structlog.reset_defaults()
        structured.configure_logging("INFO")
