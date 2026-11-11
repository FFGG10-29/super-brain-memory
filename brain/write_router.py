"""brain.write_router —— P1 写入侧分流的单一真源（v4.1 W2）。

裁决依据：Q13 / R1（entity_id）、C5（code 已在枚举）、C6（服务端重算置信度）、R13（二次确认范围）。

硬约束：
- `draft / config / artifact` 是**逻辑分类**，只写 payload `category` + LMDB 旁路索引，
  `neurons.memory_type` 列仍是五类枚举，主库 schema 零变更。
- 事务性覆盖不可逆，低置信度一律回退 `fact` 保留事件溯源（宁可多留历史，不可误删）。
- `code` 的二次确认只对 `source=user_chat` 生效；`source=agent`（MCP / cognition）一律不确认。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

__all__ = ["route_write", "normalized_signature", "LOGICAL_CATEGORIES"]

LOGICAL_CATEGORIES = frozenset({"draft", "config", "artifact"})
# 代码签名：函数/类/方法声明行；提取不到则退化为正文前 200 字哈希
_SIGNATURE = re.compile(
    r"(?m)^\s*(?:def|class|function|fn|func|public|private|static|async\s+def)\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_CODE_FENCE = re.compile(r"```|^\s*(?:import|from)\s+\w+|=>|::\s*\w+", re.MULTILINE)


def normalized_signature(content: str) -> tuple[str, bool]:
    """返回 (归一化签名, 是否模糊退化)。模糊时标 `entity_fuzzy=True`。"""
    match = _SIGNATURE.search(str(content or ""))
    if match:
        return match.group(1), False
    return hashlib.sha1(str(content or "")[:200].encode("utf-8", "replace")).hexdigest(), True


def route_write(
    *,
    content: str,
    cognition: dict[str, Any] | None,
    namespace: str,
    config: Any,
) -> dict[str, Any]:
    """判定一次写入走 `ingest`（可追溯）还是 `overwrite`（覆盖式）。

    返回字段：path / confidence / agent_confidence / routing_fallback /
    entity_id / entity_fuzzy / category / signature / reason。
    不产生副作用，便于单测与 ChatEngine 复用（C6 的 intent 同源）。
    """
    cog = dict(cognition or {})
    routing = getattr(config, "memory_type_routing", None)
    enabled = bool(getattr(routing, "enabled", True))
    threshold = float(getattr(routing, "confidence_threshold", 0.8))
    transactional = set(getattr(routing, "transactional_types", []) or
                        ["code", "draft", "config", "artifact"])
    categories = set(getattr(routing, "logical_categories", []) or LOGICAL_CATEGORIES)

    category = str(cog.get("category") or "").strip().lower()
    memory_type = str(cog.get("memory_type") or "fact").strip().lower()
    is_transactional = memory_type in transactional or category in categories

    agent_confidence = float(cog.get("confidence") if cog.get("confidence") is not None else 0.9)
    signature, fuzzy = normalized_signature(content)

    # C6：Agent 自报的 confidence 只存 agent_confidence 作参考，门控用服务端重算值
    if memory_type == "code":
        has_shape = (not fuzzy) or bool(_CODE_FENCE.search(str(content or "")))
        confidence = 0.92 if has_shape else 0.6
    elif category in categories:
        confidence = 0.9
    else:
        confidence = agent_confidence

    base = {
        "confidence": confidence,
        "agent_confidence": agent_confidence,
        "category": category,
        "signature": signature,
        "entity_fuzzy": fuzzy,
        "memory_type": memory_type,
    }
    if not enabled or not is_transactional:
        return {**base, "path": "ingest", "routing_fallback": False,
                "entity_id": "", "reason": "disabled" if not enabled else "cognitive"}
    if confidence < threshold:
        # 不可逆覆盖宁可不做：回退 fact，并在 payload 标记 routing_fallback
        return {**base, "path": "ingest", "routing_fallback": True,
                "entity_id": "", "reason": "low_confidence"}

    explicit = str(cog.get("entity_id") or "").strip()
    entity_id = explicit or hashlib.sha1(
        f"{namespace}|{cog.get('topic') or ''}|{category}|{signature}".encode("utf-8", "replace")
    ).hexdigest()
    return {**base, "path": "overwrite", "routing_fallback": False,
            "entity_id": entity_id, "reason": "transactional"}
