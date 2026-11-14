"""gateway.mcp_server —— MCP 工具面 + 官方 SDK 装配（Streamable HTTP）。

工具实现与传输层解耦：`MCPToolkit.call()` 是纯函数式入口，可被 MCP SDK、
HTTP 适配器、单元测试共用；`serve()` 才按需 import 官方 mcp SDK（缺失时给出
清晰安装提示，不影响离线测试与 HTTP 备通道）。
"""
from __future__ import annotations

import hashlib
import json
import re
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

# ── C19：幂等读工具结果缓存（独立 ns "tool_read"）────────────────────────
# 设计：CacheConfig.namespaces 里 "tool" 是 enabled=False 预留位（config.py 归
# c17 成员，不可改），故用独立 ns 名 "tool_read" 绕开：CacheManager._settings 对
# 未注册 ns 回退默认 settings（enabled=True / capacity=512 / ttl=600 / memory）。
# TTL 60s 由下方 set(ttl_sec=…) 显式传入覆盖默认 600。
# 局限（如实声明）：
#   ① 仍受 config.cache 总开关约束：总开关关 → get 恒 miss，缓存自动静默；
#   ② E1 衰减曲线默认开启时，高频命中条目的有效 TTL 会按 log1p(access_count)
#      略长于 60s（可接受：读工具结果 60s 陈旧窗口本就允许小幅放大）；
#   ③ 纯进程内，重启即空，不跨进程共享。
_TOOL_CACHE_NS = "tool_read"
_TOOL_CACHE_TTL_SEC = 60.0
# 白名单（逐条核对 gateway/tool_catalog.py：全部为 list/get/status/统计类只读
# 工具，无写库、无审计副作用，结果 60s 内确定；白名单与写标记零交集）：
_TOOL_READ_WHITELIST = frozenset({
    # _dispatch 内置只读工具：
    "health_check", "context_manifest", "profile_read", "conflict_report", "timeline_query",
    # v3 RPC 只读转发：
    "context_status", "runtime_get", "documents_list", "documents_search",
    "spaces_list", "skills_list", "skills_get", "memory_list", "memory_inspect",
    "memory_audit", "core_memory_get", "pad_distribution", "graph_projection",
    "graph_real_edges", "activation_graph", "config_presets", "gateway_status",
    "engine_status", "model_status", "topics_list", "data_status", "secrets_status",
    "tray_status", "autostart_get", "persona_get", "persona_baseline", "persona_status",
})
# ★ 刻意排除（取舍，勿随手加回）：
#   - recall_memory / recall_deep：六通道召回 _audit 写 DuckDB access_log + 结构化
#     日志（prefrontal._audit，只写不改）；fast 路径转 v3 RPC 同理。缓存命中会丢
#     召回审计 → 审计丢失不可接受，故整条召回链路不进白名单。
#   - documents_read：读全文记录 last_read（最久未读优先归档），非幂等。
#   - system_stats：CPU/显卡实时快照，60s 陈旧值有误导性。
#   - 其余全部为写类工具（见 _TOOL_WRITE_MARKERS），绝不缓存。
_TOOL_WRITE_MARKERS = frozenset({
    "save", "set", "delete", "forget", "correct", "mutate", "write", "create",
    "update", "import", "migrate", "apply", "sync", "snapshot", "shutdown",
    "enable", "disable", "upsert", "evolve", "activate", "reclassify",
    "feedback", "rollback", "sensitive",
})


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
        # E6：_agent_brief 的进程内缓存（agent_id -> (time, brief)），TTL 300s。
        # 写路径不做失效——brief 只是低风险摘要，最坏 5 分钟后过期重建，不追实时性。
        self._agent_brief_cache: dict[str, tuple[float, str]] = {}

    # ── E2 呈现层：来源可信度 / E6 Agent 心智模型 ─────────────────────────
    _AGENT_BRIEF_TTL_SEC = 300

    def _source_trust(self, source: str) -> float:
        """hit 来源可信度：按 `config.recall.source_trust` 的键做 source 前缀匹配，缺省 0.8。"""
        for key, weight in self._config.recall.source_trust.items():
            if source and str(source).startswith(str(key)):
                return float(weight)
        return 0.8

    def _trust_note(self) -> str:
        """injection 末尾的「记忆来源可信度」行（值来自 config，供模型选择性提及出处）。"""
        weights = " ".join(
            f"{key}={weight}" for key, weight in self._config.recall.source_trust.items())
        return f"记忆来源可信度：{weights}"

    def _agent_brief(self, agent_id: str) -> str:
        """E6（深度版）Agent 心智模型 brief，三段：

        1.「与该 Agent 的既往协作要点」：画像（profile）前 3 条（与最小可行版一致）；
        2.「它写过：topic×n、…」：该 agent 的 fact 记忆按 topic 聚合 top3
          （topic 优先取行内 topic 字段，缺省回退 namespace 的 "topic:" 前缀）；
        3.「最近一次协作时间」：该 agent 最新一条记忆 created_at_ms 的相对时间。

        数据源：单次 self._relational.query("neuron", memory_types=("profile","fact"),
        limit=50)，本地按 memory_type 分流——保持「每 brief 恰好 1 次 relational
        查询」契约（tests/test_meta_e2e6.py::test_agent_brief_cached_with_ttl 锁定
        次数），不做第二遍 fact 专项查询。brief 仍按 agent_id 缓存，TTL 保持 300s。
        """
        now = time.time()
        cached = self._agent_brief_cache.get(agent_id)
        if cached is not None and now - cached[0] < self._AGENT_BRIEF_TTL_SEC:
            return cached[1]
        # v4.1-C：L2 统一缓存（agent_brief ns，TTL 300s）——让 E6 心智模型进入
        # cache.metrics 可观测。L1 私有 dict 仍是权威（测试锁定"每 brief 恰好 1 次
        # relational 查询"），命中 L2 时回填 L1，两者 TTL 一致不会打架。
        try:
            from core.cache_manager import get_cache_manager

            mgr = get_cache_manager()
        except Exception:  # noqa: BLE001
            mgr = None
        if mgr is not None:
            try:
                hit = mgr.get("agent_brief", str(agent_id))
                if isinstance(hit, str):
                    self._agent_brief_cache[str(agent_id)] = (now, hit)
                    return hit
            except Exception:  # noqa: BLE001
                pass
        try:
            rows = self._relational.query(
                "neuron", memory_types=("profile", "fact"), limit=50)
        except Exception:  # noqa: BLE001  brief 失败不阻断召回
            rows = []
        profile_lines: list[str] = []
        topic_counts: dict[str, int] = {}
        latest_ms = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("agent_id") or "") != agent_id:
                continue
            created = int(row.get("created_at_ms") or 0)
            if created > latest_ms:
                latest_ms = created
            memory_type = str(row.get("memory_type") or "profile")  # 旧行缺类型 → 按画像（兼容）
            if memory_type == "fact":
                topic = str(row.get("topic") or "").strip()
                if not topic:
                    namespace = str(row.get("namespace") or "")
                    if namespace.startswith("topic:"):
                        topic = namespace[len("topic:"):]
                if topic:
                    topic_counts[topic] = topic_counts.get(topic, 0) + 1
                continue
            text = str(row.get("content") or "").strip()
            if text and len(profile_lines) < 3:
                profile_lines.append(f"- {text[:120]}")
        sections: list[str] = []
        if profile_lines:
            sections.append("【与该 Agent 的既往协作要点】\n" + "\n".join(profile_lines))
        if topic_counts:
            top = sorted(topic_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
            sections.append("它写过：" + "、".join(f"{name}×{count}" for name, count in top))
        if latest_ms:
            sections.append(
                f"最近一次协作时间：{self._relative_time(latest_ms, now_ms=int(now * 1000))}")
        brief = "\n".join(sections)
        self._agent_brief_cache[str(agent_id)] = (now, brief)
        if mgr is not None:
            try:
                mgr.set("agent_brief", str(agent_id), brief)
            except Exception:  # noqa: BLE001
                pass
        return brief

    @staticmethod
    def _relative_time(ms: int, *, now_ms: int) -> str:
        """created_at_ms → 「刚刚 / N 分钟前 / N 小时前 / N 天前 / N 个月前」。"""
        diff = max(0, now_ms - int(ms))
        if diff < 60_000:
            return "刚刚"
        if diff < 3_600_000:
            return f"{diff // 60_000} 分钟前"
        if diff < 86_400_000:
            return f"{diff // 3_600_000} 小时前"
        if diff < 30 * 86_400_000:
            return f"{diff // 86_400_000} 天前"
        return f"{diff // (30 * 86_400_000)} 个月前"

    @staticmethod
    def _perspective_of(source: Any) -> str:
        """E6：从 hit / 行记录提取 perspective（整包 meta/payload 或平铺字段均可）。"""
        if isinstance(source, dict):
            value = source.get("perspective")
            if value is None:
                nested = source.get("meta") or source.get("payload") or {}
                value = nested.get("perspective") if isinstance(nested, dict) else None
            return str(value or "")
        nested = getattr(source, "meta", None) or getattr(source, "payload", None)
        if isinstance(nested, dict):
            return str(nested.get("perspective") or "")
        return str(getattr(source, "perspective", "") or "")

    def authenticate(self, headers: dict) -> Agent:
        authorization = str(headers.get("authorization") or headers.get("Authorization") or "")
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        return self._registry.verify(token)

    def call(self, tool: str, args: dict, *, headers: dict) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            agent = self.authenticate(headers)
            result = self._call_with_tool_cache(tool, dict(args or {}), agent)
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
            self._invalidate_tool_cache_after_write(result)
            self._registry.audit(agent.agent_id, "save_memory", ok=True,
                                 latency_ms=(time.perf_counter() - started) * 1000)
            return result
        except (GatewayAuthError, ValueError, KeyError) as exc:
            return {"error": type(exc).__name__, "message": str(exc)}

    def save_bundle_for_agent(self, agent: Agent, bundle: Any,
                              *, namespace: str = "topic:f844c49c0e58") -> dict[str, Any]:
        """一进多出：进程内把 ExtractionBundle 落一组 neuron + 事件超边。

        外层（ChatEngine）已由 WriteGate 串行化；这里保留审计与 tool_read 缓存失效，
        语义与 save_for_agent 一致，只是一次写入多条。bundled 写失败一律返回结构化
        失败，由调用方降级到单条 save_for_agent。
        """
        started = time.perf_counter()
        try:
            result = self._hippocampus.remember_bundle(
                bundle, agent_id=agent.agent_id, namespace=namespace,
            )
            self._invalidate_tool_cache_after_write({"applied": result.get("applied", 0)})
            self._registry.audit(agent.agent_id, "save_bundle", ok=True,
                                 latency_ms=(time.perf_counter() - started) * 1000)
            return result
        except Exception as exc:  # noqa: BLE001  批量写失败返回结构化，调用方降级
            return {"error": type(exc).__name__, "message": str(exc), "applied": 0}

    def _call_with_tool_cache(self, tool: str, args: dict, agent: Agent) -> dict[str, Any]:
        """C19 缓存层：白名单读工具命中直接返回（跳过 dispatch，零重复执行）；
        写类工具成功后清空整个 tool_read ns。

        缓存键 = sha256(tool + json.dumps(args, sort_keys=True))：参数顺序无关、
        跨进程可复算。错误/异常响应一律不缓存。
        与「禁止清空整个命名空间」纪律（Q28-Q36）的取舍：精确失效要求键或值含
        neuron id，而工具缓存键是「工具+参数哈希」，不含任何记忆 id，写事件无法
        按值精确命中；逐键追失效复杂度不划算。工具缓存是低价值高确定性缓存
        （容量 512、TTL 仅 60s），整 ns 清空的代价只是写后第一批读 miss 一次。
        故此处是对纪律的显式、有注释的例外，仅限 tool_read ns。
        """
        from core.cache_manager import get_cache_manager
        mgr = get_cache_manager()
        key = self._tool_cache_key(tool, args)
        if mgr is not None and tool in _TOOL_READ_WHITELIST:
            try:
                cached = mgr.get(_TOOL_CACHE_NS, key)
            except Exception:  # noqa: BLE001  缓存读失败不阻断调用
                cached = None
            if isinstance(cached, dict) and "result" in cached:
                return {**cached["result"], "tool_cache_hit": True}
        result = self._dispatch(tool, args, agent)
        if mgr is None or (isinstance(result, dict) and result.get("error")):
            return result  # 错误响应不缓存、不触发失效
        try:
            if tool in _TOOL_READ_WHITELIST:
                mgr.set(_TOOL_CACHE_NS, key, {"result": result},
                        ttl_sec=_TOOL_CACHE_TTL_SEC)
            elif self._is_write_tool(tool):
                mgr.invalidate(_TOOL_CACHE_NS, "*")
        except Exception:  # noqa: BLE001  缓存读写失败不影响工具结果
            pass
        return result

    @staticmethod
    def _is_write_tool(tool: str) -> bool:
        """写类工具判定：按 -_ . 分词后精确命中写标记。

        已修文档代码：原实现为裸子串匹配，会把只读工具 config_presets 误判为写
        （"preSETs" 含 "set"），白名单与写标记不零交集。改为按分隔符分词精确匹配，
        词表不变；已逐名核对 31 项白名单零命中、常见写工具（save_memory /
        core_memory_set / memory_forget / memory_sensitive 等）仍全部命中。
        """
        tokens = {part for part in re.split(r"[-_.]", tool) if part}
        return bool(tokens & _TOOL_WRITE_MARKERS)

    @staticmethod
    def _tool_cache_key(tool: str, args: dict) -> str:
        blob = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(f"{tool}|{blob}".encode("utf-8")).hexdigest()

    @staticmethod
    def _invalidate_tool_cache_after_write(result: dict[str, Any]) -> None:
        """ChatEngine 等进程内写路径（save_for_agent，绕过 call()）同样清 tool_read。"""
        if isinstance(result, dict) and result.get("error"):
            return
        from core.cache_manager import get_cache_manager
        mgr = get_cache_manager()
        if mgr is None:
            return
        try:
            mgr.invalidate(_TOOL_CACHE_NS, "*")
        except Exception:  # noqa: BLE001
            pass

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
            # E6 社会认知：cognition.perspective（如 "Agent A 认为…"）整体随 cognition
            # 透传给 hippocampus.remember；hippocampus 旁路索引 meta 已含 perspective 键
            # （brain/hippocampus.py），recall 侧按当前 agent 做视角前置排序。
            perspective = str((cognition or {}).get("perspective") or "")

            # ★ 一进多出铁律统一判定（brain.ingest_guard，2026-11-12）：
            #   与「前置 PAD 注入」同款，WorkBuddy 侧对话大模型可前置拆分整组，
            #   随 save_memory 的 bundle 入参提交，后端跳过 extract_multi 直接落组。
            #   判定优先级：bundle(前置已拆) > single(单条认知契约,密度=1) > extract(后端兜底拆)。
            #   —— 原始对话 category 即便带单条 cognition 也强制走 extract，杜绝漏拆。
            from brain.ingest_guard import classify as _classify

            bundle_raw = args.get("bundle")
            plan = _classify(
                cognition=cognition,
                bundle=bundle_raw if isinstance(bundle_raw, dict) else None,
                content=content,
            )

            if plan.mode == "single":
                result = self._hippocampus.remember(
                    content, agent_id=agent.agent_id, namespace=namespace,
                    cognition=plan.cognition, _from_bundle=True,
                )
                return {
                    "applied": result.applied,
                    "neuron_id": result.neuron.uuid7_id,
                    "pad": result.neuron.pad.as_tuple(),
                    "source": "agent",
                    "reason": result.admission.reason,
                    "duplicate_of": result.admission.duplicate_of,
                    "superseded": result.superseded,
                    "merge": (result.merge.action if result.merge is not None else None),
                    "namespace": namespace, "topic": topic,
                    "layer": str((plan.cognition or {}).get("layer") or "fact"),
                    "perspective": perspective or None,
                }

            # bundle / extract 两路都经 extractor 归一：前置 bundle 只做白名单校验，
            # 裸文本才真正跑拆分模型（本地 0.6B / 远程 task_routes["extract"]）。
            from brain.router_extractor import RouterExtractor
            from engine.local_llm import DisabledLLM

            extractor = RouterExtractor(DisabledLLM(), self._config)
            if plan.mode == "bundle":
                bundle_obj = extractor.bundle_from_payload(plan.bundle or {})
                src = "workbuddy-presplit"
            else:
                bundle_obj = extractor.extract_multi(content, agent_id=agent.agent_id)
                src = "local"
            br = self._hippocampus.remember_bundle(
                bundle_obj, agent_id=agent.agent_id, namespace=namespace)
            if not br.get("handled", br.get("applied", 0) > 0):
                self._hippocampus._defer_orphan(content, agent.agent_id, cognition)
                return {"applied": False, "neuron_id": "", "pad": (0, 0, 0),
                        "source": src, "reason": "一进多出失败，原文留项目档案待重试",
                        "namespace": namespace, "topic": topic}
            first_id = (br.get("neuron_ids") or [""])[0]
            return {
                "applied": True, "written": br.get("applied", 0),
                "complexity": br.get("complexity"),
                "neuron_id": first_id, "pad": (0, 0, 0), "source": src,
                "reason": f"一进多出 {br.get('applied')}/{br.get('total')}"
                          + ("（WorkBuddy 前置拆分）" if plan.mode == "bundle" else ""),
                "duplicate_of": None, "superseded": [], "merge": None,
                "namespace": namespace, "topic": topic, "bundle": br,
                "perspective": None, "layer": "episode",
            }

        if tool == "recall_memory":
            # 默认收敛到 Top 3：给 Agent 的注入越少越不容易发散。
            # 顺手兼容历史上被忽略的 top_k 参数；显式传 limit 时完全尊重调用方。
            limit = int(args.get("limit") or args.get("top_k") or min(self._config.recall.top_k, 3))
            query = str(args.get("query", ""))
            # v4.1 裁决：默认全面六通道类人召回（激活扩散/叙事骨架/PAD门控/
            # 置信度/知识缺口/精排），快速投影退出主路径。仅当调用方显式
            # deep=false、且本地未装配 prefrontal 时才降级到只读字符 2-gram
            # （保留作兼容/可视化/测试兜底，不再是生产主链路）。
            deep = bool(args.get("deep", True))
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
                        # E2：来源追溯——source=写入者，trust 按 source 前缀查 config.recall.source_trust
                        "source": str(hit.neuron.agent_id or ""),
                        "trust": self._source_trust(str(hit.neuron.agent_id or "")),
                        "perspective": self._perspective_of(hit),
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
                        # E2：fast 投影行可能缺 agent_id（v3 daemon recall 不带），缺省 source="" → trust=0.8
                        "source": str(row.get("agent_id") or row.get("source") or ""),
                        "trust": self._source_trust(
                            str(row.get("agent_id") or row.get("source") or "")),
                        "perspective": self._perspective_of(row),
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
            # E6 视角选择注入：neuron payload/meta 带 perspective（hippocampus
            # 旁路索引已落盘，brain/hippocampus.py meta 块含 perspective 键）时，
            # 匹配当前 agent 的条目前置。Python sort 稳定 → 只换顺序不改内容。
            current_agent = str(agent.agent_id or "")
            matched = sum(
                1 for row in rows
                if row.get("perspective") and current_agent
                and current_agent in row["perspective"])
            if matched and len(rows) > 1:
                rows.sort(key=lambda row: 0 if (
                    row.get("perspective") and current_agent in row["perspective"]) else 1)
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
            # E2 元认知：六通道模式透传前额叶的知识缺口标记（getattr 防御——
            # prefrontal.last_knowledge_gap 由计算层并行接线，未就绪时取 None 不报错）
            knowledge_gap = (
                getattr(self._prefrontal, "last_knowledge_gap", None)
                if mode == "six-channel" else None
            )
            # E6：Agent 心智模型注入头部（仅当非空）；结果已按 agent_id 缓存（TTL 300s）
            brief = self._agent_brief(agent.agent_id)
            if brief:
                injection = (brief + "\n\n" + injection).strip() if injection else brief
            # E2：injection 末尾固定追加来源可信度行（值来自 config，供模型选择性
            # 提及「根据之前记录…」）；injection 为空（无命中）时不追
            if injection:
                injection = injection + "\n" + self._trust_note()
            return {
                "hits": rows,
                "injection": injection,
                "context_layers": layers.status()["layers"],
                "recall_mode": mode,
                "channel": channel,
                "namespaces": list(ns_filter),
                "narratives": narratives,
                "knowledge_gap": knowledge_gap,
                "perspective_matched": matched,
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
        # v4.1-C 全开：工具面清单（66 项静态 schema）缓存进 ``tool`` ns。
        # 与 tool_read（工具*执行结果*，60s）分工：本 ns 缓存的是 tools/list 协议面本身。
        # schema 在进程内随代码固定，命中即省去逐次 list 拷贝；未装配/异常回退直返。
        try:
            from core.cache_manager import get_cache_manager

            mgr = get_cache_manager()
        except Exception:  # noqa: BLE001
            mgr = None
        if mgr is not None:
            try:
                hit = mgr.get("tool", "tool_list")
                if isinstance(hit, list):
                    return list(hit)
            except Exception:  # noqa: BLE001
                pass
        schemas = list(TOOL_SCHEMAS)
        if mgr is not None:
            try:
                mgr.set("tool", "tool_list", schemas, ttl_sec=0)  # 进程内固定，靠代码变更换进程
            except Exception:  # noqa: BLE001
                pass
        return schemas

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
