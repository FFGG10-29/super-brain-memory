"""端到端验收：无头后端单进程共享六引擎（MCP 8766 + v3 HTTP/WS）。

用临时目录隔离全部数据/配置/令牌，随机高位端口，验证：
1. /v1/health、/mcp initialize + tools/list（X-Super-Brain-Bridge 鉴权）
2. POST /v1/events 异步受理 → Worker 消费 → memory.list 可见
3. WS RPC 全量能力：memory.list/inspect/correct、timeline、import_memories、
   config.get/presets、gateway.status、真实 recall、engines、model_status
运行：.venv\\Scripts\\python.exe scripts/verify_headless.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="sb_headless_"))
os.environ.update({
    "SUPER_BRAIN_DATA_DIR": str(TMP / "data"),
    "SUPER_BRAIN_HOME": str(TMP / "home"),
    "SUPER_BRAIN_CONFIG_DIR": str(TMP / "config"),
    "SUPER_BRAIN_RESOURCE_DIR": str(TMP / "resources"),  # 空模型目录 → 快速降级，不加载 3GB 权重
    "APPDATA": str(TMP / "appdata"),
    "LOCALAPPDATA": str(TMP / "localappdata"),
})

from config import BrainConfig  # noqa: E402
from headless_server import HeadlessServer  # noqa: E402

MCP_PORT, HTTP_PORT, WS_PORT = 18766, 18768, 18767


def _request(url: str, payload: dict | None = None, headers: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        return exc.code, {"error": exc.read().decode("utf-8", "replace")[:200]}


async def _rpc(ws, method: str, args: dict | None = None) -> dict:
    await ws.send(json.dumps({"type": "rpc", "id": method, "method": method, "args": args or {}},
                             ensure_ascii=False))
    while True:
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=45))
        if frame.get("type") == "rpc_result":
            return frame


def main() -> int:
    config = BrainConfig()
    config.gateway.http_port = MCP_PORT
    server = HeadlessServer(
        config, http_port=HTTP_PORT, ws_port=WS_PORT, mcp_port=MCP_PORT,
        use_llm=False, circadian=False,
    )
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}{' — ' + detail if detail else ''}")
        if not ok:
            failures.append(name)

    try:
        info = server.start()
        print(f"临时目录：{TMP}")
        print(f"端口：{info['mcp_url']} / {info['http_url']} / {info['ws_url']}\n")
        time.sleep(0.5)

        status, health = _request(f"http://127.0.0.1:{HTTP_PORT}/v1/health")
        check("v3 /v1/health", status == 200 and health.get("ok") is True, str(health)[:120])

        headers = {"X-Super-Brain-Bridge": server.bridge_token,
                   "Accept": "application/json, text/event-stream"}
        status, initialized = _request(
            f"http://127.0.0.1:{MCP_PORT}/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "verify", "version": "1"}}},
            headers,
        )
        check("MCP initialize + 桥接头鉴权", status == 200 and "result" in initialized,
              str(initialized)[:120])
        status, tools = _request(
            f"http://127.0.0.1:{MCP_PORT}/mcp",
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, headers,
        )
        tool_names = [item.get("name") for item in (tools.get("result") or {}).get("tools", [])]
        check("MCP tools/list 六工具", status == 200 and len(tool_names) == 6, str(tool_names))

        status, accepted = _request(
            f"http://127.0.0.1:{HTTP_PORT}/v1/events",
            {"content": "周五发版需要双人复核，这是端到端验收写入的记忆",
             "agent_id": "verify", "namespace": "private"},
        )
        check("POST /v1/events 异步受理", status == 202 and accepted.get("accepted") is True,
              str(accepted)[:120])

        # 等 Worker 消费（最多 15s）
        rows: list[dict] = []
        for _ in range(30):
            _, data = _request(f"http://127.0.0.1:{HTTP_PORT}/v1/memories?limit=20")
            rows = data.get("memories", [])
            if rows:
                break
            time.sleep(0.5)
        check("Worker 消费后主库投影可见", bool(rows), f"{len(rows)} 条")

        async def ws_checks() -> None:
            import websockets

            async with websockets.connect(f"ws://127.0.0.1:{WS_PORT}/ws") as ws:
                checks: list[tuple[str, str, dict | None]] = [
                    ("memory.list", "memory.list", {"limit": 20}),
                    ("timeline", "timeline", {"limit": 20}),
                    ("config.get", "config.get", None),
                    ("config.presets", "config.presets", None),
                    ("gateway.status", "gateway.status", None),
                    ("engines", "engines", None),
                    ("model_status", "model_status", None),
                    ("recall 真实六通道", "recall", {"q": "发版", "limit": 5}),
                    ("import_memories", "import_memories",
                     {"text": "第一条导入记忆：项目使用 Flutter 前端\n第二条导入记忆：后端是无头 Python 进程",
                      "suffix": ".txt", "fast": True}),
                    ("memories", "memories", {"limit": 20}),
                    ("graph", "graph", None),
                    ("detail", "detail", None),
                    ("health", "health", None),
                    ("core_memory.get", "core_memory.get", None),
                    ("core_memory.set", "core_memory.set", {"blocks": {"human": "验收用户"}}),
                    ("tasks", "tasks", None),
                    ("snapshots", "snapshots", None),
                    ("export", "export", None),
                    ("create_snapshot", "create_snapshot", None),
                    ("sync_once", "sync_once", None),
                    ("config.set", "config.set", {"patches": {"recall": {"top_k": 6}}}),
                    ("config.apply_preset", "config.apply_preset", {"name": "balanced"}),
                ]
                for label, method, args in checks:
                    frame = await _rpc(ws, method, args)
                    check(f"WS RPC {label}", frame.get("ok") is True,
                          str(frame.get("result", frame.get("error")))[:140])

                listed = await _rpc(ws, "memory.list", {"limit": 20})
                rows_after = listed.get("result") or []
                check("导入后记忆可查", isinstance(rows_after, list) and len(rows_after) >= 2,
                      f"{len(rows_after) if isinstance(rows_after, list) else '?'} 条")
                if isinstance(rows_after, list) and rows_after:
                    memory_id = str(rows_after[0].get("id") or "")
                    inspected = await _rpc(ws, "memory.inspect", {"memory_id": memory_id})
                    check("memory.inspect 检查器", inspected.get("ok") is True
                          and "confidence_breakdown" in (inspected.get("result") or {}),
                          str(inspected.get("error", ""))[:100])
                    corrected = await _rpc(ws, "memory.correct", {
                        "memory_id": memory_id,
                        "changes": {"content": "纠正后的记忆：周五发版必须双人复核且留审计"},
                        "reason": "验收纠正",
                    })
                    check("memory.correct 用户控制", corrected.get("ok") is True,
                          str(corrected.get("result", corrected.get("error")))[:100])
                    sensitive = await _rpc(ws, "memory.sensitive",
                                           {"memory_id": memory_id, "flag": True})
                    check("memory.sensitive 敏感标记", sensitive.get("ok") is True)
                    audited = await _rpc(ws, "memory.audit", {"memory_id": memory_id, "limit": 10})
                    check("memory.audit 审计轨迹", audited.get("ok") is True)

                # 手动执行昼夜任务（此前 process 模式必失败）
                tasks_frame = await _rpc(ws, "tasks")
                names = [str(item.get("name")) for item in (tasks_frame.get("result") or [])
                         if isinstance(item, dict) and item.get("name")]
                if names:
                    run = await _rpc(ws, "run_task", {"name": names[0]})
                    check("run_task 手动执行任务", run.get("ok") is True,
                          str(run.get("result", run.get("error")))[:100])

                # 热重启：MCP 网关、v3 Worker、业务服务必须一起切到新内核
                restarted = await _rpc(ws, "restart")
                check("restart 统一换核", restarted.get("ok") is True
                      and (restarted.get("result") or {}).get("ok") is True,
                      str(restarted.get("result", restarted.get("error")))[:140])
                after = await _rpc(ws, "memory.list", {"limit": 10})
                check("重启后主库仍可读", after.get("ok") is True,
                      str(after.get("error", ""))[:100])
                mcp_after = _request(
                    f"http://127.0.0.1:{MCP_PORT}/mcp",
                    {"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}},
                    {"X-Super-Brain-Bridge": server.bridge_token,
                     "Accept": "application/json, text/event-stream"},
                )
                check("重启后 MCP 网关仍可用", mcp_after[0] == 200
                      and "result" in mcp_after[1], str(mcp_after[1])[:100])

        asyncio.run(ws_checks())
    finally:
        server.stop()

    print(f"\n结果：{'全部通过' if not failures else '失败 ' + str(len(failures)) + ' 项：' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
