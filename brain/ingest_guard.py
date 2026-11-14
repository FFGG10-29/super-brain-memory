"""brain.ingest_guard —— 一进多出铁律的统一入口判定（WorkBuddy 侧前置拆分优先）。

所有外部写通道（MCP save_memory / stdio / HTTP /save / RPC daemon→worker / 批量导入）
在落库前必须过本模块分类，杜绝「绕过拆分直接单条落库」的旁路。

与前置 PAD 注入同款：认知判定由对话大模型（WorkBuddy 侧）承担，后端只做确定性落库。
前置拆分结果以 ``bundle`` 入参随 save_memory 提交，后端跳过 extract_multi
（本地 0.6B / 远程抽取模型），实现零拆分开销。

四条判定（按优先级）：
  1. bundle.items 非空              → bundle：直接 remember_bundle（WorkBuddy 已拆）
  2. cognition 结构化且非原始对话    → single：视为「一组」的一个成员放行（已判密度=1）
  3. 原始对话 category 且无 bundle   → extract：后端兜底强制拆（对话原文必须拆）
  4. 裸文本（无 bundle / 无 cognition）→ extract：后端兜底拆；失败留项目档案，绝不降级单条

系统派生（叙事 / 合并 update / worker 回放）不走本模块，由各调用点显式
``_from_bundle=True``，避免误伤同事项演进——这是唯一合法的单条写入通道。
蒸馏不走豁免（2026-11-12 用户裁决「任何途径都要符合」）：聊天导出档案经蒸馏器
切块后同样必须 extract_multi 一进多出解剖入池，layer=fact 由 bundle 覆写。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["WritePlan", "classify", "is_structured", "is_raw_chat"]

_RAW_CHAT_CATEGORIES = ("qq_chat", "user_chat", "chat")
_RAW_CHAT_SOURCES = ("user_chat", "qq_bot")
_STRUCTURED_KEYS = ("memory_type", "perspective", "layer", "pad", "entities")


@dataclass(frozen=True)
class WritePlan:
    mode: str  # bundle | single | extract
    reason: str
    cognition: dict[str, Any] | None = None
    bundle: dict[str, Any] | None = None


def is_structured(cognition: dict[str, Any] | None) -> bool:
    """认知契约是否已结构化（对话大模型判过类 / PAD / 实体 / 层）。"""
    return bool(cognition) and any(cognition.get(k) for k in _STRUCTURED_KEYS)


def is_raw_chat(cognition: dict[str, Any] | None) -> bool:
    """是否原始对话原文——即便带单条 cognition 也必须强制拆分。"""
    if not cognition:
        return False
    return (str(cognition.get("category") or "") in _RAW_CHAT_CATEGORIES
            or str(cognition.get("source") or "") in _RAW_CHAT_SOURCES)


def classify(*, cognition: dict[str, Any] | None = None,
             bundle: dict[str, Any] | None = None,
             content: str = "") -> WritePlan:
    """判定一条写入该如何落库。纯函数，不碰存储、不跑模型。

    ``bundle`` 由 WorkBuddy 侧前置拆分产出（与 _MULTI_SYSTEM_PROMPT 同构），
    后端直接用 RouterExtractor.bundle_from_payload 做白名单校验后落组。
    """
    items = bundle.get("items") if isinstance(bundle, dict) else None
    if isinstance(items, list) and items:
        return WritePlan("bundle", "WorkBuddy 前置拆分，后端零开销落组", bundle=bundle)
    if is_structured(cognition) and not is_raw_chat(cognition):
        return WritePlan("single", "单条认知契约（已判信息密度=1）放行", cognition=cognition)
    # 原始对话 / 裸文本一律后端兜底拆分；失败由调用方留项目档案，不降级单条
    return WritePlan("extract", "原始输入，后端兜底一进多出拆分", cognition=cognition)
