"""gateway.tool_catalog —— MCP 工具面单一真源（等价于 DSH Host SDK 的 manifest）。

DSH Host SDK 里，Source 通过 manifest 声明 role / capabilities / actions / routes，
Host 组合成 View，Agent 再从 Host 暴露的 headless tools 里发现并调用。MCP 没有
进程内 Host，但 `tools/list` + `tools/call` + `resources/*` 就是同一件事的跨进程标准版：

    manifest  → tools/list（能力发现）+ context_manifest（机器可读清单）
    actions   → tools/call（写操作）
    routes    → tools/call（召回/检索）
    views     → resources/read（只读投影，可直接挂进 Agent 上下文）

本模块只维护一张表，stdio 与 HTTP 两条通道共用；`RPC_MAP` 把工具名映射到
v3 RpcHandler 方法，因此 **v3 已有的全部独特能力自动成为 MCP 工具**，不需要
在每个传输层重复实现一遍。
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "CORE_TOOL_DEFS", "EXTENDED_TOOL_DEFS", "MCP_TOOL_DEFS", "TOOL_SCHEMAS",
    "RPC_MAP", "manifest", "resource_list", "resource_read", "dispatch_standalone",
]

RPC_MAP: dict[str, str] = {}


def _t(name: str, description: str, properties: dict[str, Any] | None = None,
       required: list[str] | None = None, rpc: str | None = None) -> dict[str, Any]:
    if rpc:
        RPC_MAP[name] = rpc
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties or {},
            **({"required": required} if required else {}),
        },
    }


_S = {"type": "string"}
_I = {"type": "integer"}
_B = {"type": "boolean"}
_O = {"type": "object"}
_A = {"type": "array", "items": {"type": "string"}}


def _d(key: str, description: str, kind: dict[str, Any] | None = None) -> dict[str, Any]:
    return {**(kind or _S), "description": description}


# ── 原有 6 个核心工具（保持名字与契约不变）────────────────────────────────
CORE_TOOL_DEFS: list[dict[str, Any]] = [
    _t("save_memory", "写入一条记忆：自动分类、SimHash 去重、PAD 情绪标注、六引擎落库",
       {"content": _d("content", "记忆正文"), "agent_id": _d("agent_id", "调用方标识")}, ["content"]),
    _t("recall_memory", "召回记忆：默认走只读投影的 2-gram 召回（毫秒级）；deep=true 才走六通道精排（30-45s，非必要不用）。返回 hits + 收敛注入 injection（含运行时记忆与档案摘要）+ context_layers + recall_mode",
       {"query": _d("query", "查询语句"), "limit": _d("limit", "返回条数，默认 3", _I),
        "deep": _d("deep", "是否启用六通道精排，默认 false", _B),
        "namespace": _d("namespace", "可选命名空间（需权限）")}, ["query"]),
    _t("timeline_query", "按事件时间窗口查询记忆",
       {"since_ms": _d("since_ms", "起始毫秒时间戳", _I), "until_ms": _d("until_ms", "结束毫秒时间戳", _I),
        "limit": _d("limit", "条数", _I)}),
    _t("profile_read", "读取用户画像槽位（偏好、习惯等 profile 类型记忆）"),
    _t("conflict_report", "查看被推翻 / 冲突 / 封存的历史事实",
       {"limit": _d("limit", "条数", _I)}),
    _t("health_check", "六引擎健康检查（关系/向量/图谱/KV/时序/超图）"),
]

# ── 分层上下文（对齐 dsh-mnemon 三层 View + 技能层）──────────────────────
_CONTEXT: list[dict[str, Any]] = [
    _t("context_manifest", "【能力发现】返回本机全部上下文能力的机器可读清单：层、角色、capabilities、actions、routes、预算。Agent 首次接入应先调它，等价于 DSH Host SDK 的 manifest",
       None, None),
    _t("context_status", "【状态】各层条目数 / 字节用量 / 上限 / Provider / revision / 归档数",
       None, None, "context.status"),
    _t("context_compose", "【View 组合】把运行时记忆 + 相关档案摘要 + 激活空间拼成一段可直接注入的上下文",
       {"query": _d("query", "当前任务/问题，用于挑相关档案"),
        "max_runtime": _d("max_runtime", "运行时记忆最多几条，默认 6", _I),
        "max_docs": _d("max_docs", "档案摘要最多几条，默认 3", _I)}, None, "context.compose"),
    _t("runtime_get", "【运行时记忆】读取 MEMORY.md / USER.md 全部条目、limits、archived、revision",
       None, None, "runtime.get"),
    _t("runtime_mutate", "【运行时记忆】add / replace / remove 条目。写满时自动把低优先级旧条目归档（不删除）",
       {"action": _d("action", "add | replace | remove"),
        "target": _d("target", "memory（项目约定）| user（用户画像）"),
        "content": _d("content", "条目正文"),
        "old_text": _d("old_text", "replace/remove 时用于定位的原文或唯一子串"),
        "importance": _d("importance", "critical | normal | low")}, ["action", "target"], "runtime.mutate"),
    _t("documents_list", "【项目档案】只返回 id/标题/摘要/大小/时间，不返回全文；配合 documents_read 按需读",
       {"keyword": _d("keyword", "标题/摘要关键词"),
        "include_archived": _d("include_archived", "是否含已归档", _B)}, None, "documents.list"),
    _t("documents_search", "【项目档案】检索摘要，返回按相关度排序的候选（先检索后读全文）",
       {"query": _d("query", "检索词"), "limit": _d("limit", "条数", _I)}, ["query"], "documents.search"),
    _t("documents_read", "【项目档案】按 id 读取全文 Markdown，并记录 last_read（最久未读优先归档）",
       {"id": _d("id", "档案 id")}, ["id"], "documents.read"),
    _t("documents_mutate", "【项目档案】create / update / archive / delete",
       {"action": _d("action", "create | update | archive | delete"), "id": _d("id", "档案 id"),
        "title": _d("title", "标题"), "description": _d("description", "摘要（列表只显示这一行）"),
        "content": _d("content", "全文 Markdown")}, ["action"], "documents.mutate"),
    _t("spaces_list", "【记忆空间】列出全部空间、Provider 与激活状态",
       None, None, "spaces.list"),
    _t("spaces_create", "【记忆空间】新建空间并指定 Provider（默认 super-brain-native）",
       {"name": _d("name", "空间名"), "provider": _d("provider", "super-brain-native | mnemon-native | hindsight | mem0"),
        "note": _d("note", "备注")}, ["name"], "spaces.create"),
    _t("spaces_activate", "【记忆空间】激活/停用空间；只有激活的空间参与召回",
       {"id": _d("id", "空间 id"), "active": _d("active", "true 激活 / false 停用", _B)}, ["id"], "spaces.activate"),
    _t("skills_list", "【技能】列出版本化技能及使用次数 / 成功率",
       None, None, "skills.list"),
    _t("skills_get", "【技能】读取单个技能全文（含版本与使用反馈）",
       {"id": _d("id", "技能 id")}, ["id"], "skills.get"),
    _t("skills_upsert", "【技能】新建或编辑技能；传 id 且修改正文时版本号 +1",
       {"id": _d("id", "留空新建"), "name": _d("name", "技能名"),
        "description": _d("description", "描述"), "content": _d("content", "正文 Markdown")}, None, "skills.upsert"),
    _t("skills_evolve", "【技能】上报一次使用结果，累计成功率；成功率<50% 且使用≥5 次自动标待改进",
       {"id": _d("id", "技能 id"), "success": _d("success", "本次是否成功", _B),
        "note": _d("note", "反馈备注")}, ["id"], "skills.evolve"),
    _t("skills_delete", "【技能】删除技能（索引 + 正文，不可恢复）",
       {"id": _d("id", "技能 id")}, ["id"], "skills.delete"),
]

# ── 超级大脑独特能力（全部由 v3 RPC 提供，MCP 只需转发）──────────────────
_UNIQUE: list[dict[str, Any]] = [
    _t("memory_list", "【记忆管理】按条件列出记忆（分页/关键词/分层/类型/命名空间）",
       {"limit": _d("limit", "条数", _I), "keyword": _d("keyword", "关键词"), "tier": _d("tier", "core|working|archive"),
        "memory_type": _d("memory_type", "记忆类型"), "namespace": _d("namespace", "命名空间")}, None, "memory.list"),
    _t("memory_inspect", "【检查器】单条记忆全字段：PAD、置信度、突触强度、双时态、审计链",
       {"memory_id": _d("memory_id", "记忆 id")}, ["memory_id"], "memory.inspect"),
    _t("memory_correct", "【用户控制】纠正记忆内容并留审计痕迹",
       {"memory_id": _d("memory_id", "记忆 id"), "changes": _d("changes", "要改的字段对象", _O),
        "reason": _d("reason", "纠正原因"), "actor": _d("actor", "操作者")}, ["memory_id"], "memory.correct"),
    _t("memory_forget", "【用户控制】遗忘 / 封存 / 墓碑化记忆",
       {"memory_id": _d("memory_id", "记忆 id"), "reason": _d("reason", "原因"),
        "action": _d("action", "forget | seal | tombstone")}, ["memory_id"], "memory.forget"),
    _t("memory_audit", "【检查器】某条记忆的完整事件审计链",
       {"memory_id": _d("memory_id", "记忆 id"), "limit": _d("limit", "条数", _I)}, ["memory_id"], "memory.audit"),
    _t("memory_sensitive", "【检查器】标记/取消敏感（敏感内容默认遮蔽）",
       {"memory_id": _d("memory_id", "记忆 id"), "flag": _d("flag", "是否敏感", _B)}, ["memory_id"], "memory.sensitive"),
    _t("core_memory_get", "【Core Memory】读取常驻核心记忆区块",
       None, None, "core_memory.get"),
    _t("core_memory_set", "【Core Memory】写入常驻核心记忆区块",
       {"blocks": _d("blocks", "区块名→内容 的对象", _O)}, ["blocks"], "core_memory.set"),
    _t("pad_distribution", "【PAD 认知分区】全部记忆在愉悦度/唤醒度/支配度三维的分布统计",
       None, None, "pad.distribution"),
    _t("graph_projection", "【2D 语义环】图谱节点的确定性二维投影（与 Qt 语义环布局一致）",
       None, None, "graph.projection"),
    _t("activation_graph", "【激活图】当前语义激活子图（节点/边/degraded 标记）",
       None, None, "activation.graph"),
    _t("recall_deep", "【深度召回】六通道（dense/sparse/fts/graph/timeline/profile）+ RRF + 精排，返回 hits + audit 归因；慢，非必要不用",
       {"q": _d("q", "查询语句"), "limit": _d("limit", "条数", _I)}, ["q"], "recall"),
    _t("import_memories", "【批量导入】一次导入多条记忆（可跳过 LLM 抽取）",
       {"items": _d("items", "正文数组", _A)}, ["items"], "import_memories"),
    _t("config_presets", "【配置】列出可用预设（轻量/均衡/质量）",
       None, None, "config.presets"),
    _t("config_apply_preset", "【配置】应用预设并重启内核",
       {"name": _d("name", "预设名")}, ["name"], "config.apply_preset"),
    _t("gateway_status", "【接口】MCP / HTTP / WebSocket 端点与令牌状态",
       None, None, "gateway.status"),
    _t("system_stats", "【硬件仪表】CPU / 内存 / NVIDIA 显卡利用率、显存、温度实时快照",
       None, None, "system.stats"),
    _t("autostart_get", "【桌面能力】读取开机自启状态", None, None, "autostart.get"),
    _t("autostart_set", "【桌面能力】开启/关闭开机自启",
       {"enabled": _d("enabled", "是否启用", _B)}, ["enabled"], "autostart.set"),
    _t("secrets_status", "【钥匙串】密钥后端状态（DPAPI / keyring）",
       None, None, "secrets.status"),
    _t("data_status", "【数据目录】当前数据落点、占用、是否可迁移",
       None, None, "data.status"),
    _t("data_migrate", "【数据目录】一键迁移到新目录（复制 + 写指针，重启生效）",
       {"target": _d("target", "目标目录绝对路径")}, ["target"], "data.migrate"),
    _t("tray_status", "【托盘】托盘启用状态与后端进程信息",
       None, None, "tray.status"),
    _t("tray_enable", "【托盘】启用托盘（关窗隐藏，服务常驻）",
       None, None, "tray.enable"),
    _t("tray_disable", "【托盘】停用托盘", None, None, "tray.disable"),
    _t("sync_once", "【同步】立即执行一次同步", None, None, "sync_once"),
    _t("create_snapshot", "【备份】创建一份快照", None, None, "create_snapshot"),
    _t("engine_status", "【引擎】六引擎详细状态", None, None, "engines"),
    _t("model_status", "【模型】Embedding / LLM / Reranker 装载状态",
       None, None, "model_status"),
    _t("system_shutdown", "【危险】让后端优雅停机（flush journal / WAL TRUNCATE / 关六引擎）。调用后所有 MCP 工具不可用，除非手动重启后端",
       None, None, "system.shutdown"),
]

EXTENDED_TOOL_DEFS: list[dict[str, Any]] = _CONTEXT + _UNIQUE
MCP_TOOL_DEFS: list[dict[str, Any]] = CORE_TOOL_DEFS + EXTENDED_TOOL_DEFS
# MCPToolkit.tool_list() 与 stdio/HTTP tools/list 共用同一份
TOOL_SCHEMAS: list[dict[str, Any]] = MCP_TOOL_DEFS


def manifest() -> dict[str, Any]:
    """机器可读能力清单（DSH Host SDK manifest 的 MCP 等价物）。"""
    from core.context_layers import get_context_layers

    def layer(role: str, sources: list[str], capabilities: list[str], actions: list[str],
              routes: list[str]) -> dict[str, Any]:
        return {"role": role, "sources": sources, "capabilities": capabilities,
                "actions": actions, "routes": routes}

    status = get_context_layers().status()
    return {
        "apiVersion": "super-brain-context/v1",
        "serverInfo": {"name": "super-brain-memory", "version": "3.0"},
        "model": "view-based composition（对齐 dsh-mnemon 的三层 Source 角色）",
        "layers": [
            layer("working-context", ["runtime/MEMORY.md", "runtime/USER.md"],
                  ["status", "project", "write"],
                  ["runtime_mutate"], ["runtime_get", "context_compose"]),
            layer("narrative", ["documents/index.json", "documents/*.md"],
                  ["status", "search", "read-grant", "write", "archive"],
                  ["documents_mutate"], ["documents_search", "documents_read"]),
            layer("durable-evidence", ["spaces.json", "六引擎主库"],
                  ["status", "recall", "activate", "write"],
                  ["spaces_create", "spaces_activate"], ["recall_memory", "recall_deep"]),
            layer("skills", ["skills/index.json", "skills/*.md"],
                  ["status", "version", "evolve"],
                  ["skills_upsert", "skills_evolve"], ["skills_list", "skills_get"]),
        ],
        "budget": {"runtime": "8000 B / 4000 B（超限归档低优先级旧条目）",
                   "injection": "Top 3 摘要 + 停止发散约束",
                   "documents": "列表只给摘要，全文按需读取"},
        "guidance": [
            "每轮必读运行时记忆；需要设计/流程材料先 documents_search 摘要再 documents_read 全文；"
            "跨会话事实走 recall_memory，不要跨层复制同一份内容。",
            "未知细节按未知处理，禁止编造；当前指令、仓库文件与实时工具结果优先于历史记忆。",
        ],
        "toolCount": len(MCP_TOOL_DEFS),
        "coreTools": [t["name"] for t in CORE_TOOL_DEFS],
        "contextTools": [t["name"] for t in _CONTEXT],
        "uniqueTools": [t["name"] for t in _UNIQUE],
        "rpcBacked": len(RPC_MAP),
        "resources": [r["uri"] for r in resource_list()],
        "status": status,
    }


# ── MCP resources（只读 View 投影，可直接挂进 Agent 上下文）───────────────
def resource_list() -> list[dict[str, Any]]:
    return [
        {"uri": "brain://runtime/memory", "name": "MEMORY.md 项目约定", "mimeType": "text/markdown"},
        {"uri": "brain://runtime/user", "name": "USER.md 用户画像", "mimeType": "text/markdown"},
        {"uri": "brain://documents/index", "name": "项目档案摘要索引", "mimeType": "application/json"},
        {"uri": "brain://spaces/active", "name": "已激活记忆空间", "mimeType": "application/json"},
        {"uri": "brain://skills/index", "name": "技能索引", "mimeType": "application/json"},
        {"uri": "brain://context/manifest", "name": "上下文能力清单", "mimeType": "application/json"},
    ]


def resource_read(uri: str) -> dict[str, Any]:
    """返回 {"contents":[{"uri", "mimeType", "text"}]}；失败返回 {"error":...}。"""
    import json
    from core.context_layers import get_context_layers

    layers = get_context_layers()
    payload: Any
    if uri == "brain://runtime/memory":
        payload = layers.runtime_get().get("memory", [])
    elif uri == "brain://runtime/user":
        payload = layers.runtime_get().get("user", [])
    elif uri == "brain://documents/index":
        payload = layers.documents_list()
    elif uri == "brain://spaces/active":
        payload = [s for s in layers.spaces_list().get("spaces", []) if s.get("active")]
    elif uri == "brain://skills/index":
        payload = layers.skills_list().get("skills", [])
    elif uri == "brain://context/manifest":
        payload = manifest()
    else:
        return {"error": f"未知资源：{uri}"}
    return {"contents": [{"uri": uri, "mimeType": "application/json",
                          "text": json.dumps(payload, ensure_ascii=False, default=str)}]}


def dispatch_standalone(name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """stdio 独占模式（没有桌面内核）也能用的本地层工具。

    返回 None 表示该工具需要桌面后端，由调用方给出明确提示。
    """
    from core.context_layers import get_context_layers

    a = dict(args or {})
    layers = get_context_layers()
    if name == "context_manifest":
        return manifest()
    if name == "system_stats":
        from core.system_stats import get_system_stats
        return get_system_stats().latest()

    handlers: dict[str, Any] = {
        "context_status": layers.status,
        "context_compose": lambda: {"projection": layers.compose(
            str(a.get("query", "")), int(a.get("max_runtime", 6)), int(a.get("max_docs", 3)))},
        "runtime_get": layers.runtime_get,
        "runtime_mutate": lambda: layers.runtime_mutate(
            str(a.get("action", "add")), str(a.get("target", "memory")),
            str(a.get("content", "")), str(a.get("old_text", "")),
            str(a.get("importance", "normal"))),
        "documents_list": lambda: {"documents": layers.documents_list(
            str(a.get("keyword", "")), bool(a.get("include_archived", False)))},
        "documents_search": lambda: {"documents": layers.documents_search(
            str(a.get("query", "")), int(a.get("limit", 8)))},
        "documents_read": lambda: layers.documents_read(str(a.get("id", ""))),
        "documents_mutate": lambda: layers.documents_mutate(
            str(a.get("action", "create")), str(a.get("id", "")),
            str(a.get("title", "")), str(a.get("description", "")), str(a.get("content", ""))),
        "spaces_list": layers.spaces_list,
        "spaces_create": lambda: layers.spaces_create(
            str(a.get("name", "")), str(a.get("provider", "super-brain-native")), str(a.get("note", ""))),
        "spaces_activate": lambda: layers.spaces_activate(
            str(a.get("id", "")), bool(a.get("active", True))),
        "skills_list": layers.skills_list,
        "skills_get": lambda: layers.skills_get(str(a.get("id", ""))),
        "skills_upsert": lambda: layers.skills_upsert(
            str(a.get("id", "")), str(a.get("name", "")), str(a.get("description", "")),
            str(a.get("content", "")), a.get("tags")),
        "skills_evolve": lambda: layers.skills_evolve(
            str(a.get("id", "")), bool(a.get("success", True)), str(a.get("note", ""))),
        "skills_delete": lambda: layers.skills_delete(str(a.get("id", ""))),
    }
    handler = handlers.get(name)
    if handler is None:
        return None
    result = handler()
    return result if isinstance(result, dict) else {"result": result}
