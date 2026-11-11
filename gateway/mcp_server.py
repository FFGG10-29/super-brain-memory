"""gateway.mcp_server —— MCP 工具面 + 官方 SDK 装配（Streamable HTTP）。

工具实现与传输层解耦：`MCPToolkit.call()` 是纯函数式入口，可被 MCP SDK、
HTTP 适配器、单元测试共用；`serve()` 才按需 import 官方 mcp SDK（缺失时给出
清晰安装提示，不影响离线测试与 HTTP 备通道）。
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

from config import BrainConfig, get_config
from core.errors import GatewayAuthError
from core.interfaces import HealthCheckable, RelationalRepository
from brain.hippocampus import Hippocampus
from brain.prefrontal import Prefrontal
from gateway.registry import Agent, AgentRegistry
from gateway.tool_catalog import RPC_MAP, TOOL_SCHEMAS, manifest as context_manifest

__all__ = ["MCPToolkit", "TOOL_SCHEMAS"]


class MCPToolkit:
    """协议无关的工具集合（所有 Agent 共用同一份记忆）。

    token 只决定 **agent_id**（审计 / 限流 / 显式命名空间时的权限），
    读写默认落在同一个中枢命名空间 `config.namespace_policy`；
    这是「外置超级大脑」的前提：任何 Agent 写的记忆，其它 Agent 立刻能召回。
    """

    def __init__(
        self,
        *,
        registry: AgentRegistry,
        hippocampus: Hippocampus,
        prefrontal: Prefrontal,
        relational: RelationalRepository,
        engines: list[HealthCheckable] | None = None,
        config: BrainConfig | None = None,
        rpc_fallback: Any | None = None,
    ) -> None:
        self._registry = registry
        self._hippocampus = hippocampus
        self._prefrontal = prefrontal
        self._relational = relational
        self._engines = engines or []
        self._config = config or get_config()
        # 未命中内置工具时转发给 v3 RPC：所有扩展能力自动成为 MCP 工具
        self._rpc_fallback = rpc_fallback

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

    def recall_for_agent(self, agent: Agent, args: dict) -> dict[str, Any]:
        """Q19 / A5：ChatEngine 等进程内模块复用「带人格注入的召回」。

        不走 HTTP 鉴权（调用方已是服务端注册的 Agent），但保留参数校验、分派与审计；
        `channel="bot"` 会由 `_dispatch` 注入 L0 人格层，与 MCP 客户端语义完全一致。
        """
        started = time.perf_counter()
        try:
            result = self._dispatch("recall_memory", dict(args or {}), agent)
            self._registry.audit(agent.agent_id, "recall_memory", ok=True,
                                 latency_ms=(time.perf_counter() - started) * 1000)
            return result
        except (GatewayAuthError, ValueError, KeyError) as exc:
            return {"error": type(exc).__name__, "message": str(exc)}

    def save_for_agent(self, agent: Agent, args: dict) -> dict[str, Any]:
        """Q19：ChatEngine 写入长期记忆的进程内入口（外层由 WriteGate 串行化）。"""
        started = time.perf_counter()
        try:
            result = self._dispatch("save_memory", dict(args or {}), agent)
            self._registry.audit(agent.agent_id, "save_memory", ok=True,
                                 latency_ms=(time.perf_counter() - started) * 1000)
            return result
        except (GatewayAuthError, ValueError, KeyError) as exc:
            return {"error": type(exc).__name__, "message": str(exc)}

    # ── C06：prompt_prefix 稳定前缀缓存 ──────────────────────────────────
    @staticmethod
    def _context_fingerprint(layers: Any) -> str:
        """人格 / 上下文层版本指纹：任一层版本变化 → 前缀缓存键变化（旧键自然失效）。

        ttl=0 永久缓存的「失效」即靠换键实现：runtime 层 revision、人格参数
        versions/params_count 任一变化都会改变指纹。
        """
        parts: list[str] = []
        try:
            status = layers.status() or {}
            parts.append(f"layers:{status.get('revision', 0)}")
        except Exception:  # noqa: BLE001 - 层状态不可用不阻断召回
            parts.append("layers:-")
        try:
            from core.persona import get_persona_layer
            persona = get_persona_layer().status()
            parts.append(
                f"persona:{persona.get('versions', 0)}:{persona.get('params_count', 0)}")
        except Exception:  # noqa: BLE001 - 人格层不可用不阻断召回
            parts.append("persona:-")
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

    def _compose_with_prefix_cache(self, layers: Any, query: str, channel: str) -> str:
        """compose 产出的缓存版组装：缓存稳定前缀 + 现算查询相关档案块。

        稳定前缀 = compose("", channel)（空 query 不含档案块，其余部分与
        compose(query) 完全一致——runtime 投影 / 分区标签 / bot 人格层均与
        query 无关）；档案块按 query 现算并镜像 compose 的文档块格式。
        未装配缓存管理器时整体回退 compose(query) 原路径。
        """
        from core.cache_manager import get_cache_manager
        manager = get_cache_manager()
        if manager is None:
            return layers.compose(query, channel=channel)  # 回退无缓存路径
        key = hashlib.sha256(
            f"{channel}|{self._context_fingerprint(layers)}".encode("utf-8")).hexdigest()
        prefix = manager.get("prompt_prefix", key)
        if prefix is None:
            prefix = layers.compose("", channel=channel)
            manager.set("prompt_prefix", key, prefix, ttl_sec=0)  # ttl=0 永久，靠换键失效
        parts = [prefix]
        if query.strip():
            docs = layers.documents_search(query, limit=3)
            if docs:
                # 与 core/context_layers.compose 的档案块逐字一致（镜像，勿单方改动）
                lines = ["【项目档案摘要】仅标题与描述，需要细节请单独读取全文"]
                for doc in docs:
                    lines.append(
                        f"- #{doc['id']} {doc.get('title', '')} — "
                        f"{doc.get('description', '')[:120]}")
                parts.append("\n".join(lines))
        return "\n\n".join(part for part in parts if part)

    def _dispatch(self, tool: str, args: dict, agent: Agent) -> dict[str, Any]:
        # 超级大脑是**统一记忆中枢**：所有 Agent 默认写进同一个共享库、读同一份记忆。
        # agent_id 只用于审计与限流，绝不参与隔离——否则每个 Agent 会看到不同的「自己」。
        # 只有客户端**显式指定**别的命名空间时才做权限校验（显式越权仍要拒绝）。
        from core.context_layers import get_context_layers
        layers = get_context_layers()
        requested = args.get("namespace")
        cognition_raw = args.get("cognition")
        cognition = cognition_raw if isinstance(cognition_raw, dict) else None
        # ★ A 包：topic 分区。复用现有 namespace 列（值 topic:<空间id>），
        #   id 必须来自后端 spaces.json，避免任意字符串污染索引。
        topic = str((cognition or {}).get("topic") or args.get("topic") or "").strip()
        if topic:
            valid_ids = {str(s.get("id")) for s in layers.spaces_list().get("spaces", [])}
            valid_ids.add("default")
            if topic not in valid_ids:
                raise GatewayAuthError(f"未知主题分区 {topic}（先调 spaces_list 取合法 id）")
            namespace = f"topic:{topic}"
        else:
            namespace = str(requested or self._config.namespace_policy or "")
            if requested and not self._registry.namespace_allowed(agent, str(requested)):
                raise GatewayAuthError(f"Agent 无权访问命名空间 {requested}")
            # ★ 无 cognition.topic 时也强制落空间，杜绝裸 private/document 写入
            if not namespace.startswith("topic:"):
                namespace = "topic:default"
        # ★ L0 人格层写入门槛：全量可读 ≠ 可写人格，只有提案流程能显式授权
        if tool == "save_memory" and cognition and str(cognition.get("layer") or "") == "persona":
            if args.get("persona_write") is not True:
                raise GatewayAuthError("L0 人格层写入需显式授权 persona_write=true")

        if tool == "save_memory":
            if not self._registry.permission_allowed(agent, "write"):
                raise GatewayAuthError("Agent 无 write 权限")
            content = str(args.get("content", "")).strip()
            if not content:
                raise ValueError("content 不能为空")
            # ★ 认知契约：外部 Agent（WorkBuddy 等）直接给出分类 / PAD / 实体 / 置信度时，
            #   后端跳过 Qwen3-0.6B 抽取，但仍做语义合并裁决（同事项 update/merge 收敛，
            #   merge_cognition_enabled 可关）。不传 cognition 时行为与旧版完全一致。
            if cognition_raw is not None and cognition is None:
                raise ValueError("cognition 必须是对象")
            result = self._hippocampus.remember(
                content, agent_id=agent.agent_id, namespace=namespace,
                cognition=cognition,
            )
            return {
                "applied": result.applied,
                "neuron_id": result.neuron.uuid7_id,
                "pad": result.neuron.pad.as_tuple(),
                "source": "agent" if cognition else "local",
                "reason": result.admission.reason,
                "duplicate_of": result.admission.duplicate_of,
                "superseded": result.superseded,
                "merge": (result.merge.action if result.merge is not None else None),
                "namespace": namespace,
                "topic": topic,
                "layer": str((cognition or {}).get("layer") or "fact"),
            }

        if tool == "recall_memory":
            # 默认收敛到 Top 3：给 Agent 的注入越少越不容易发散。
            # 顺手兼容历史上被忽略的 top_k 参数；显式传 limit 时完全尊重调用方。
            limit = int(args.get("limit") or args.get("top_k") or min(self._config.recall.top_k, 3))
            query = str(args.get("query", ""))
            # 默认走只读投影的字符 2-gram 召回（毫秒级，与 Flutter 前端一致）。
            # 六通道 embedding/RRF/精排实测 30-45s，只有 deep=true 才走，
            # 避免 Agent 每轮提问都卡在检索上（DSH 档案里的 known recall-hang）。
            deep = bool(args.get("deep", False))
            # ★ A 包：按类召回下推。topics/scope=active/namespace 三选一；
            #   Bot 通道（channel=bot）不传即 ALL 全量可读，不做隔离。
            channel = str(args.get("channel") or "workbuddy")
            topics = args.get("topics")
            ns_filter: tuple[str, ...] = ()
            if isinstance(topics, (list, tuple)) and topics:
                ns_filter = tuple(
                    str(t) if str(t).startswith("topic:") else f"topic:{t}" for t in topics)
            elif str(args.get("scope") or "") == "active":
                ns_filter = tuple(
                    f"topic:{s.get('id')}" for s in layers.spaces_list().get("spaces", [])
                    if s.get("active"))
            elif requested and not topic:
                ns_filter = (str(requested),)
            mode = "projection"
            rows: list[dict[str, Any]] = []
            injection = ""
            if deep or self._rpc_fallback is None:
                mode = "six-channel"
                # namespaces=() = 全量可读；非空则 RecallFilter 下推到 dense/sparse/fts 三路
                hits = self._prefrontal.recall(
                    query, agent_id=agent.agent_id, namespaces=ns_filter, limit=limit,
                )
                rows = [
                    {
                        "id": hit.neuron.uuid7_id,
                        "content": hit.neuron.content,
                        "score": round(hit.score, 4),
                        "pad": hit.neuron.pad.as_tuple(),
                        "channels": list(hit.channels),
                    }
                    for hit in hits
                ]
                injection = self._prefrontal.inject(hits, agent_id=agent.agent_id)
            else:
                fast = self._rpc_fallback("recall", {
                    "q": query, "limit": limit, "deep": False,
                    "namespaces": list(ns_filter),
                })
                raw_hits = fast.get("hits", []) if isinstance(fast, dict) else []
                rows = [
                    {
                        "id": str(row.get("id", "")),
                        "content": str(row.get("content", "")),
                        "score": float(row.get("score", 0.0)),
                        "pad": list(row.get("pad") or []),
                        "channels": list(row.get("channels") or []),
                    }
                    for row in raw_hits if isinstance(row, dict)
                ]
                head = [
                    "【记忆注入】以下为只读投影召回的已核验记忆",
                    "【执行约束】据此收敛作答：停止发散、不展开无关联想、不复述记忆原文；"
                    "未命中的细节按未知处理，禁止编造。",
                ]
                for index, row in enumerate(rows[:3], start=1):
                    text = row["content"]
                    if len(text) > 120:
                        text = text[:120].rstrip() + "…（摘要）"
                    head.append(f"{index}. {text}")
                injection = "\n".join(head) if rows else ""
            # W5 / P4.2：事件骨架作为整体注入，内部成员已由 prefrontal 从各通道移除
            narratives = (list(getattr(self._prefrontal, "last_narratives", []) or [])
                          if mode == "six-channel" else [])
            if narratives:
                blocks = "\n\n".join(
                    f"【事件骨架 {index + 1}】\n{str(item.get('text') or '')}"
                    for index, item in enumerate(narratives[:3])
                )
                injection = (injection + "\n\n" + blocks).strip()
            # 每轮确定性注入的运行时记忆 + 相关档案摘要（dsh-mnemon 的三层 View）
            # bot 通道额外注入 L0 人格层（骨架只读 + 当前可调参数），闲聊/指令双通道
            # C06：compose 产出可拆 = 稳定前缀（runtime 投影 + 分区标签 + bot 人格层，
            # 只随 channel / persona / 层版本变化）+ 查询相关档案块；前缀走 prompt_prefix
            # ns 缓存（ttl=0 永久，指纹换键即失效）。archive 块格式镜像
            # core/context_layers.compose（core/* 归另一成员，本文件不做改动）。
            projection = self._compose_with_prefix_cache(layers, query, channel)
            if projection:
                injection = (injection + "\n\n" + projection).strip()
            return {
                "hits": rows,
                "injection": injection,
                "context_layers": layers.status()["layers"],
                "recall_mode": mode,
                "channel": channel,
                "namespaces": list(ns_filter),
                "narratives": narratives,
                "degraded": bool(getattr(self._prefrontal, "last_degraded", False)),
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
            # 画像同样共享：任何 Agent 读到的都是同一份用户画像
            rows = self._relational.query(
                "neuron", memory_types=("profile",), active_only=True, limit=50
            )
            return {"profiles": [dict(row) for row in rows]}

        if tool == "conflict_report":
            rows = self._relational.query("neuron", active_only=False, limit=200)
            sealed = [dict(row) for row in rows if not row.get("is_active", True)]
            return {"sealed": sealed, "count": len(sealed)}

        if tool == "health_check":
            return {"engines": [status.as_dict() for status in (e.health() for e in self._engines)]}

        if tool == "context_manifest":
            # 能力发现：等价于 DSH Host SDK 的 manifest，Agent 首次接入先调它
            return context_manifest()

        method = RPC_MAP.get(tool)
        if method is not None and self._rpc_fallback is not None:
            args = dict(args)
            if tool == "recall_deep":
                args["deep"] = True
            return self._rpc_fallback(method, args)
        raise ValueError(f"未知工具: {tool}" + (
            "（该工具需要桌面后端在线）" if method is not None else ""))

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
