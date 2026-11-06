"""scripts/verify_v3_rpc.py —— 真 WebSocket RPC 端到端验收（Flutter 的实际通道）。

    .venv\\Scripts\\python.exe scripts/verify_v3_rpc.py
"""
from __future__ import annotations

import asyncio
import json

import websockets

URL = "ws://127.0.0.1:8767/ws"
METHODS = (
    "health", "graph", "snapshot", "tasks", "engines", "model_status",
    "config.get", "spaces", "gateway", "snapshots", "memories",
)


async def main() -> int:
    async with websockets.connect(URL) as socket:
        for method in METHODS:
            await socket.send(json.dumps({"type": "rpc", "id": method, "method": method, "args": {}}))
            reply = json.loads(await asyncio.wait_for(socket.recv(), timeout=8))
            assert reply["type"] == "rpc_result" and reply["ok"] is True, f"{method}: {reply}"
            print(f"rpc {method:16s} OK")
        await socket.send(json.dumps({
            "type": "rpc", "id": "recall", "method": "recall", "args": {"q": "缓存", "limit": 5},
        }))
        reply = json.loads(await asyncio.wait_for(socket.recv(), timeout=8))
        hits = reply["result"]["hits"]
        print(f"rpc recall           OK  ({len(hits)} hits)")
        await socket.send(json.dumps({
            "type": "rpc", "id": "save", "method": "save_memory",
            "args": {"content": "WebSocket RPC 验收记忆", "agent_id": "flutter"},
        }))
        reply = json.loads(await asyncio.wait_for(socket.recv(), timeout=8))
        print("rpc save_memory      OK ", reply["result"]["event_id"][:12])
    print("v3 WS RPC OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
