"""gateway.mcp_server —— MCP 工具面 + 官方 SDK 装配（Streamable HTTP）。

工具实现与传输层解耦：`MCPToolkit.call()` 是纯函数式入口，可被 MCP SDK、
HTTP 适配器、单元测试共用；`serve()` 才按需 import 官方 mcp SDK（缺失时给出
清晰安装提示，不影响离线测试与 HTTP 备通道）。
"""
from __future__ import annotations

import time
from typing import Any

from config import BrainConfig, get_config
from core.errors import GatewayAuthError
from core.interfaces import HealthCheckable, RelationalRepository
from brain.hippocampus import Hippocampus
from brain.prefrontal import Prefrontal
from gateway.registry import Agent, AgentRegistry

__all__ = ["MCPToolkit", "TOOL_SCHEMAS"]

TOOL_SCHEMAS = [
    {"name": "save_memory", "description": "写入一条记忆（含 PAD 情绪坐标）",
     "inputSchema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
    {"name": "recall_memory", "description": "六路召回并收敛注入",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}},
    {"name": "timeline_query", "description": "按事件时间查询记忆",
     "inputSchema": {"type": "object", "properties": {"since_ms": {"type": "integer"}, "until_ms": {"type": "integer"}}}},
    {"name": "profile_read", "description": "读取当前 Agent 的画像记忆",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "conflict_report", "description": "列出被封存/演进的历史事实",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "health_check", "description": "六引擎健康状态",
     "inputSchema": {"type": "object", "properties": {}}},
]


class MCPToolkit:
    """协议无关的工具集合：token 决定 agent_id 与命名空间，客户端不能自报。"""

    def __init__(
        self,
        *,
        registry: AgentRegistry,
        hippocampus: Hippocampus,
        prefrontal: Prefrontal,
        relational: RelationalRepository,
        engines: list[HealthCheckable] | None = None,
        config: BrainConfig | None = None,
    ) -> None:
        self._registry = registry
        self._hippocampus = hippocampus
        self._prefrontal = prefrontal
        self._relational = relational
        self._engines = engines or []
        self._config = config or get_config()

    def authenticate(self, headers: dict) -> Agent:
        authorization = str(headers.get("authorization") or headers.get("Authorization") or "")
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        return self._registry.verify(token)

    def call(self, tool: str, args: dict, *, headers: dict) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            agent = self.authenticate(headers)
            result = self._dispatch(tool, dict(args or {}), agent)
            self._registry.audit(agent.agent_id, tool, ok=True, latency_ms=(time.perf_counter() - started) * 1000)
            return result
        except (GatewayAuthError, ValueError, KeyError) as exc:
            self._registry.audit("anonymous", tool, ok=False, latency_ms=(time.perf_counter() - started) * 1000)
            return {"error": type(exc).__name__, "message": str(exc)}

    def _dispatch(self, tool: str, args: dict, agent: Agent) -> dict[str, Any]:
        namespace = str(args.get("namespace") or agent.scopes[0])
        if not self._registry.namespace_allowed(agent, namespace):
            raise GatewayAuthError(f"Agent 无权访问命名空间 {namespace}")

        if tool == "save_memory":
            if not self._registry.permission_allowed(agent, "write"):
                raise GatewayAuthError("Agent 无 write 权限")
            content = str(args.get("content", "")).strip()
            if not content:
                raise ValueError("content 不能为空")
            result = self._hippocampus.remember(content, agent_id=agent.agent_id, namespace=namespace)
            return {
                "applied": result.applied,
                "neuron_id": result.neuron.uuid7_id,
                "pad": result.neuron.pad.as_tuple(),
                "reason": result.admission.reason,
                "duplicate_of": result.admission.duplicate_of,
                "superseded": result.superseded,
            }

        if tool == "recall_memory":
            limit = int(args.get("limit") or self._config.recall.top_k)
            hits = self._prefrontal.recall(
                str(args.get("query", "")), agent_id=agent.agent_id,
                namespaces=tuple(agent.scopes), limit=limit,
            )
            return {
                "hits": [
                    {
                        "id": hit.neuron.uuid7_id,
                        "content": hit.neuron.content,
                        "score": round(hit.score, 4),
                        "pad": hit.neuron.pad.as_tuple(),
                        "channels": list(hit.channels),
                    }
                    for hit in hits
                ],
                "injection": self._prefrontal.inject(hits, agent_id=agent.agent_id),
            }

        if tool == "timeline_query":
            neurons = self._prefrontal.timeline_query(
                since_ms=args.get("since_ms"), until_ms=args.get("until_ms"),
                agent_id=agent.agent_id, limit=int(args.get("limit") or 20),
            )
            return {
                "items": [
                    {"id": n.uuid7_id, "content": n.content, "valid_from_ms": n.valid_from_ms,
                     "valid_to_ms": n.valid_to_ms}
                    for n in neurons
                ]
            }

        if tool == "profile_read":
            rows = self._relational.query(
                "neuron", namespaces=tuple(agent.scopes), memory_types=("profile",), limit=50
            )
            return {"profiles": [dict(row) for row in rows]}

        if tool == "conflict_report":
            rows = self._relational.query("neuron", active_only=False, limit=200)
            sealed = [dict(row) for row in rows if not row.get("is_active", True)]
            return {"sealed": sealed, "count": len(sealed)}

        if tool == "health_check":
            return {"engines": [status.as_dict() for status in (e.health() for e in self._engines)]}

        raise ValueError(f"未知工具: {tool}")

    def tool_list(self) -> list[dict]:
        return list(TOOL_SCHEMAS)

    def serve(self, *, host: str | None = None, port: int | None = None) -> None:
        """装配官方 MCP SDK（Streamable HTTP）。未安装时给出安装提示而非静默降级。"""
        try:
            import mcp  # noqa: F401  # pyright: ignore[reportMissingImports]
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "MCP SDK 未安装：执行 .venv\\Scripts\\python.exe -m pip install -r requirements-optional.txt "
                "（阶段 4 依赖）后重试；HTTP 备通道无需此依赖即可用。"
            ) from exc
        # 传输层装配由 SDK 版本决定，工具面已在 MCPToolkit.call 中就绪
        raise NotImplementedError(
            f"Streamable HTTP 传输装配待 SDK 接入（host={host or self._config.gateway.mcp_host}, "
            f"port={port or self._config.gateway.mcp_port}）；工具面 {len(TOOL_SCHEMAS)} 个已就绪"
        )
