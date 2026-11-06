"""v3 WebSocket RPC 验收：Flutter 十页所需方法全部真实可用。"""
from __future__ import annotations

import json
import time

import pytest

from config import BrainConfig, ModelConfig, StorageConfig, SyncConfig
from v3.daemon import V3Daemon


@pytest.fixture()
def v3_config(tmp_path):
    return BrainConfig(
        storage=StorageConfig(
            sqlite_path=tmp_path / "projection.db", lancedb_path=tmp_path / "vector.lance",
            kuzu_path=tmp_path / "graph.kuzu", lmdb_path=tmp_path / "kv.lmdb",
            duckdb_path=tmp_path / "timeseries.db",
        ),
        model=ModelConfig(dense_dim=8, use_cuda=False, llm_gguf_dir=tmp_path / "models"),
        sync=SyncConfig(device_id="v3-rpc-test", local_repo_dir=tmp_path / "sync"),
    )


def _rpc(daemon: V3Daemon, method: str, **args):
    return daemon.rpc.dispatch({"type": "rpc", "id": "1", "method": method, "args": args})


def test_rpc_covers_all_console_pages(v3_config, tmp_path):
    daemon = V3Daemon(tmp_path / "v3", config=v3_config, http_port=18878, ws_port=18877,
                      use_llm=False)
    daemon.start(serve_http=False, serve_ws=False)
    try:
        daemon.handle_event("RPC 验收记忆：缓存服务与向量召回联动", agent_id="tester")
        deadline = time.time() + 15
        while time.time() < deadline and daemon.inbox.counts().get("done", 0) < 1:
            time.sleep(0.1)

        methods = ("graph", "memories", "recall", "health", "spaces",
                   "snapshot", "engines", "tasks", "model_status", "config.get",
                   "snapshots", "gateway", "export", "sync_once", "create_snapshot")
        for method in methods:
            result = _rpc(daemon, method, q="缓存", limit=10)
            assert result["ok"] is True, f"{method} 失败：{result}"
            payload = result.get("result")
            assert not (isinstance(payload, dict) and "error" in payload), f"{method} 返回错误：{result}"

        graph = _rpc(daemon, "graph")["result"]
        assert graph["nodes"], "写入后 graph 必须有节点"
        assert _rpc(daemon, "recall", q="缓存")["result"]["hits"], "召回必须有命中"
        first_id = str(graph["nodes"][0]["id"])
        detail = _rpc(daemon, "detail", memory_id=first_id)["result"]
        assert detail["content"], "真 ID 必须返回记忆详情"
        assert _rpc(daemon, "detail", memory_id="nope")["result"]["error"], "假 ID 应返回不存在"

        config = _rpc(daemon, "config.get")["result"]
        assert "model" in config["sections"] and "recall" in config["sections"]
        saved = _rpc(daemon, "config.set", patches={"ui": {"max_labels": 7}})
        assert saved["ok"] is True and daemon._config.ui.max_labels == 7

        unknown = _rpc(daemon, "no_such_method")
        assert unknown["ok"] is False
    finally:
        daemon.stop()


def test_rpc_over_websocket_roundtrip(v3_config, tmp_path):
    """真 WS：发 rpc 帧 → 收 rpc_result（Flutter 的实际通道）。"""
    import asyncio

    import websockets

    daemon = V3Daemon(tmp_path / "v3ws", config=v3_config, http_port=18880, ws_port=18881,
                      use_llm=False)
    daemon.start(serve_http=False, serve_ws=True)
    try:
        async def drive():
            async with websockets.connect("ws://127.0.0.1:18881/ws") as socket:
                await socket.send(json.dumps({
                    "type": "rpc", "id": "abc", "method": "health", "args": {},
                }))
                return json.loads(await asyncio.wait_for(socket.recv(), timeout=5))

        result = asyncio.run(drive())
        assert result["type"] == "rpc_result"
        assert result["id"] == "abc"
        assert result["ok"] is True
        assert result["result"]["ok"] is True
    finally:
        daemon.stop()
