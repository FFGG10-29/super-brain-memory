"""W9 / P7.3：v4.1 全链路契约汇总——对外能力面不膨胀、关键默认值不漂移。"""
from __future__ import annotations

from core.interfaces import EventType
from gateway import tool_catalog
from v3.rpc import RpcHandler


def test_mcp_manifest_stays_66_59_6() -> None:
    """P7.3 #11：C4 只允许加 v3 RPC，不许新增 MCP 工具。"""
    manifest = tool_catalog.manifest()
    assert manifest["toolCount"] == len(tool_catalog.MCP_TOOL_DEFS) == 66
    assert manifest["rpcBacked"] == 59
    assert len(manifest["resources"]) == 6


def test_event_type_has_nine_members() -> None:
    assert set(EventType.__args__) == {
        "ingest", "supersede", "reinforce", "tier_change", "consolidate",
        "policy", "tombstone", "overwrite", "media_interaction",
    }


def test_new_rpc_handlers_exist_without_mcp_tools() -> None:
    handler = RpcHandler(object())
    for method in (
        "graph.backfill", "graph.density", "graph.event_skeleton",
        "narrative.reconstruct", "narrative.list", "narrative.retry_status",
        "pad.current",
    ):
        assert hasattr(handler, "_" + method.replace(".", "_")), method
        assert method not in {tool["name"] for tool in tool_catalog.MCP_TOOL_DEFS}


def test_cloud_models_are_off_by_default() -> None:
    from config import BrainConfig

    config = BrainConfig(_env_file=None)
    assert config.model_router.enabled is False
    assert config.chat.host == "127.0.0.1" and config.chat.port == 8769
    assert config.recall.rerank_top_n == 20
    assert config.recall.rerank_timeout_ms == 8000
    assert config.memory_type_routing.confidence_threshold == 0.8
    assert config.narrative_reconstruct.max_retries == 3
    assert config.media_service.stub_mode is True
