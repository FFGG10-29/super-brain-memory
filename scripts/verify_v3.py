"""scripts/verify_v3.py —— v3 实机端到端验收（Daemon 需已在 8768/8767 运行）。

    .venv\\Scripts\\python.exe scripts/verify_v3.py

检查：HTTP 202 受理延迟、WS 增量广播、Worker 消费、投影可读。
"""
from __future__ import annotations

import json
import time
import urllib.request

from websockets.sync.client import connect

BASE = "http://127.0.0.1:8768"


def post(content: str, agent_id: str = "workbuddy") -> dict:
    body = json.dumps({"content": content, "agent_id": agent_id}).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE}/v1/events", data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 本地回环
        return {"status": response.status, **json.loads(response.read().decode("utf-8"))}


def health() -> dict:
    with urllib.request.urlopen(f"{BASE}/v1/health", timeout=5) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    events = []
    with connect("ws://127.0.0.1:8767/ws", open_timeout=5) as ws:
        acks = [post(f"v3 实机验收第 {i} 条：缓存服务与向量召回联动") for i in range(3)]
        for ack in acks:
            print("ack:", ack["status"], ack.get("event_id", "")[:12], f"{ack.get('ack_ms')}ms")
        assert all(ack["status"] == 202 and ack["accepted"] for ack in acks)
        assert max(ack["ack_ms"] for ack in acks) < 500, "受理不得等待模型/投影"
        deadline = time.time() + 15
        while time.time() < deadline and len(events) < 3:
            try:
                events.append(json.loads(ws.recv(timeout=0.8)))
            except TimeoutError:
                pass
        print("ws events:", [event["type"] for event in events])
        assert sum(1 for event in events if event["type"] == "node_created") >= 3

    deadline = time.time() + 15
    while time.time() < deadline and health()["inbox"].get("done", 0) < 3:
        time.sleep(0.2)
    final = health()
    with urllib.request.urlopen(f"{BASE}/v1/memories?limit=20", timeout=5) as response:  # noqa: S310
        memories = json.loads(response.read().decode("utf-8"))["memories"]
    print("inbox:", final["inbox"], "worker:", final["worker"], "memories:", len(memories))
    assert final["inbox"].get("done", 0) >= 3
    assert memories, "投影表必须能读到新记忆"
    print("v3 end-to-end OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
