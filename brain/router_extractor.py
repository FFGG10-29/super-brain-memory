"""brain.router_extractor —— 感知：LLM 严格 JSON 抽取。

输出契约（`_SYSTEM_PROMPT` 与 `Extraction` 一一对应）：
    memory_type / entities / relations / hyperedge / pad / confidence

降级：LLM 不可用或输出非法时走**规则抽取**（`_rule_extract`），
PAD 用情绪词典估算 —— 保证"每条记忆都带 PAD"这一全链路不变量，
但 `source` 字段明确标注是 rule 而非 llm，便于 eval 区分质量。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from config import BrainConfig, get_config
from core.errors import EngineUnavailable
from core.interfaces import LocalLLM, MemoryType
from models.neuron import PAD, Neuron

__all__ = ["Extraction", "ExtractedEntity", "ExtractedRelation", "RouterExtractor"]

_SYSTEM_PROMPT = """你是记忆抽取器。只输出一个 JSON 对象，不要解释、不要 Markdown 围栏。
字段：
{
  "memory_type": "fact|code|scenario|emotion|profile",
  "entities": [{"name": "实体名", "type": "person|project|tool|concept"}],
  "relations": [{"subject": "实体名", "predicate": "关系动词", "object": "实体名"}],
  "hyperedge": {"label": "事件标签", "members": ["实体名"]} 或 null,
  "pad": {"p": -1~1 的愉悦度, "a": -1~1 的唤醒度, "d": -1~1 的支配度},
  "confidence": 0~1 的置信度
}
规则：pad 必须给出三个数值；不确定时置信度低于 0.6。"""


@dataclass(frozen=True)
class ExtractedEntity:
    name: str
    type: str = "concept"


@dataclass(frozen=True)
class ExtractedRelation:
    subject: str
    predicate: str
    object: str


@dataclass(frozen=True)
class Extraction:
    memory_type: MemoryType
    pad: PAD
    confidence: float
    source: str = "llm"                                  # llm | rule
    entities: tuple[ExtractedEntity, ...] = field(default_factory=tuple)
    relations: tuple[ExtractedRelation, ...] = field(default_factory=tuple)
    hyperedge_label: str | None = None
    hyperedge_members: tuple[str, ...] = field(default_factory=tuple)


# 规则词典：只在 LLM 不可用时使用，命中即调 PAD（中文情绪词典简化版）
_NEGATIVE = ("讨厌", "崩溃", "生气", "吵", "冲突", "失败", "报错", "糟糕", "烦", "拒绝", "骂")
_POSITIVE = ("喜欢", "感谢", "顺利", "开心", "搞定", "漂亮", "赞", "成功", "通过", "舒服")
_HIGH_AROUSAL = ("!", "！", "赶紧", "马上", "立刻", "急", "崩溃", "吵", "炸", "救命")
_LOW_AROUSAL = ("可能", "也许", "大概", "顺便", "有空")
_CODE_HINTS = ("def ", "class ", "函数", "报错", "堆栈", "commit", "bug", "编译", "测试")
_PROFILE_HINTS = ("我是", "我叫", "我的习惯", "我偏好", "我不喜欢", "记住我")
_SCENARIO_HINTS = ("那天", "当时", "后来", "昨天", "今天", "上次", "会议")


class RouterExtractor:
    def __init__(self, llm: LocalLLM, config: BrainConfig | None = None) -> None:
        self._llm = llm
        self._config = config or get_config()

    def from_agent(self, cognition: dict[str, Any]) -> Extraction:
        """★ 认知契约入口：外部 Agent 直接提供抽取结果，跳过本地 Qwen3-0.6B。

        契约字段与 _SYSTEM_PROMPT 的 JSON 完全一致，且全部可选：
            memory_type / pad{p,a,d} / confidence / entities / relations / hyperedge
        复用 _from_payload 的 clamp 与白名单校验；source 标为 "agent"，
        让 PAD 轨迹与 eval 能区分「本地模型估的」还是「外部大脑判的」。
        """
        payload = dict(cognition or {})
        # 强容错：外部 Agent 字段类型不合法时收敛为空，绝不让写入链抛异常
        if not isinstance(payload.get("pad"), dict):
            payload["pad"] = {}
        if not isinstance(payload.get("entities"), list):
            payload["entities"] = []
        else:
            payload["entities"] = [x for x in payload["entities"] if isinstance(x, dict)]
        if not isinstance(payload.get("relations"), list):
            payload["relations"] = []
        else:
            payload["relations"] = [x for x in payload["relations"] if isinstance(x, dict)]
        if not isinstance(payload.get("hyperedge"), dict):
            payload["hyperedge"] = {}
        elif not isinstance(payload["hyperedge"].get("members"), list):
            payload["hyperedge"]["members"] = []
        # 外部 Agent 已完成理解，缺省置信度高于 LLM 抽取的 0.6
        payload.setdefault("confidence", 0.9)
        # C6 / Q13：Agent 自报的 confidence 只作参考存 agent_confidence，
        # 门控用 write_router 服务端重算后的值（由 hippocampus 注入 routing 决策）。
        routing = payload.get("routing")
        if isinstance(routing, dict):
            payload["agent_confidence"] = payload.get("confidence", 0.9)
            payload["confidence"] = float(routing.get("confidence", payload["confidence"]))
        extraction = self._from_payload(payload)
        # ★ 零 PAD 兜底已统一下沉到 _from_payload（覆盖 llm/rule/agent 全部来源），
        #   这里只负责把来源如实标为 "agent"。
        return replace(extraction, source="agent")

    def extract(self, content: str, *, agent_id: str = "", use_llm: bool = True) -> Extraction:
        # 批量导入走规则抽取：LLM 单条 0.3~1s，几百条会拖成几分钟，
        # 而导入内容本来就是用户自己的正文，不需要再让模型"理解"一遍。
        if not use_llm:
            return self._rule_extract(content)
        # task_routes["extract"] 配了远程供应商（模型配置页）→ 先走远程；
        # 失败/未配置回落本地 0.6B，规则抽取仍是最后防线。
        route = str((self._config.model_router.task_routes or {}).get("extract") or "")
        if route:
            try:
                from gateway.model_router import get_model_router

                raw = get_model_router().complete_task(
                    "extract", f"Agent={agent_id}\n内容：{content}")
                payload = self._json_from_text(raw)
                return self._from_payload(payload)
            except Exception:  # noqa: BLE001  远程失败回落本地，绝不阻塞登记
                pass
        try:
            payload = self._llm.complete_json(
                _SYSTEM_PROMPT,
                f"Agent={agent_id}\n内容：{content}",
                required_keys=("memory_type", "pad", "confidence"),
                max_retries=2,
            )
            return self._from_payload(payload)
        except (EngineUnavailable, ValueError, KeyError, TypeError):
            return self._rule_extract(content)

    @staticmethod
    def _json_from_text(raw: str) -> dict[str, Any]:
        """远程模型返回文本 → JSON payload（容忍 ```json 围栏与前后杂文）。"""
        import json as _json

        text = str(raw or "").strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("远程抽取结果不含 JSON 对象")
        payload = _json.loads(text[start:end + 1])
        if not isinstance(payload, dict):
            raise ValueError("远程抽取结果不是对象")
        return payload

    # ── LLM 结果 → Extraction ────────────────────────────────────────────
    def _from_payload(self, payload: dict[str, Any]) -> Extraction:
        raw_pad = payload.get("pad") or {}
        pad = PAD(
            p=_clamp_float(raw_pad.get("p", 0.0)),
            a=_clamp_float(raw_pad.get("a", 0.0)),
            d=_clamp_float(raw_pad.get("d", 0.0)),
        )
        # ★【2026-11-10 缺口1·统一最后防线】_from_payload 是本地0.6B(llm)、
        #   规则(rule)、外部Agent(agent)三条抽取来源的共同出口。任何来源给出
        #   PAD 三轴全 0（模型返回全0 / 调用方漏填 / 显式填零）一律兜中性非零基线，
        #   从抽取出口根治「零 PAD 记忆落库」。17:26 distiller 那条零PAD即 0.6B 返回全0。
        #   不冒充语义判定：仅满足「每条记忆 PAD 非零」不变量。
        if pad.p == 0.0 and pad.a == 0.0 and pad.d == 0.0:
            pad = PAD(p=0.05, a=0.10, d=0.10)
        entities = tuple(
            ExtractedEntity(name=str(item.get("name", "")).strip(), type=str(item.get("type", "concept")))
            for item in payload.get("entities") or []
            if str(item.get("name", "")).strip()
        )
        relations = tuple(
            ExtractedRelation(
                subject=str(item.get("subject", "")).strip(),
                predicate=str(item.get("predicate", "")).strip(),
                object=str(item.get("object", "")).strip(),
            )
            for item in payload.get("relations") or []
            if item.get("subject") and item.get("predicate") and item.get("object")
        )
        hyperedge = payload.get("hyperedge") or {}
        members = tuple(str(m).strip() for m in (hyperedge.get("members") or []) if str(m).strip())
        memory_type = str(payload.get("memory_type", "fact"))
        if memory_type not in ("fact", "code", "scenario", "emotion", "profile"):
            memory_type = "fact"
        return Extraction(
            memory_type=memory_type,  # type: ignore[arg-type]
            pad=pad,
            confidence=_clamp01(float(payload.get("confidence", 0.6))),
            source="llm",
            entities=entities,
            relations=relations,
            hyperedge_label=(str(hyperedge.get("label")) if hyperedge.get("label") else None),
            hyperedge_members=members,
        )

    # ── 规则兜底 ──────────────────────────────────────────────────────────
    def _rule_extract(self, content: str) -> Extraction:
        text = content.strip()
        negative = sum(1 for word in _NEGATIVE if word in text)
        positive = sum(1 for word in _POSITIVE if word in text)
        arousal_hits = sum(1 for word in _HIGH_AROUSAL if word in text)
        calm_hits = sum(1 for word in _LOW_AROUSAL if word in text)

        valence = (positive - negative) / max(positive + negative, 1)
        arousal = (arousal_hits - calm_hits) / max(arousal_hits + calm_hits, 1)
        arousal = max(arousal, 0.4 * arousal_hits / max(arousal_hits + 1, 1))
        dominance = 0.2 if negative else 0.0

        # ★ 中性文本（情绪词典零命中）原本会得到 PAD=(0,0,0)，破坏「每条记忆自带 PAD」
        #   这一全链路不变量。给极小非零基线：p=0.05（微正）、a=0.10（轻微唤醒）、
        #   d=0.10（中性偏可控）。confidence 仍 0.45 低权重，不影响召回排序，
        #   只是保证零 PAD 活跃记忆恒为 0（用户硬指标）。带 cognition 的写入不走这里。
        if positive == 0 and negative == 0 and arousal_hits == 0 and calm_hits == 0:
            valence, arousal, dominance = 0.05, 0.10, 0.10

        return Extraction(
            memory_type=self._rule_memory_type(text),
            pad=PAD(p=_clamp_float(valence), a=_clamp_float(arousal), d=_clamp_float(dominance)),
            confidence=0.45,          # 规则抽取置信度天然低，触发复核/低权重
            source="rule",
            entities=self._rule_entities(text),
        )

    @staticmethod
    def _rule_memory_type(text: str) -> MemoryType:
        if any(hint in text for hint in _PROFILE_HINTS):
            return "profile"
        if any(hint in text for hint in _CODE_HINTS):
            return "code"
        if any(hint in text for hint in _SCENARIO_HINTS):
            return "scenario"
        if any(word in text for word in _NEGATIVE + _POSITIVE):
            return "emotion"
        return "fact"

    @staticmethod
    def _rule_entities(text: str) -> tuple[ExtractedEntity, ...]:
        # 抓英文标识符与书名号/引号内实体；中文实体抽取留给 LLM 通道
        candidates = re.findall(r"[《「\"]([^》」\"]{2,20})[》」\"]", text)
        candidates += re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,30}\b", text)
        unique = list(dict.fromkeys(candidate.strip() for candidate in candidates if candidate.strip()))
        return tuple(ExtractedEntity(name=name, type="concept") for name in unique[:8])

    # ── 组装 Neuron（PAD 已含在内，保证"每条记忆自带 PAD"）──────────────
    def to_neuron(
        self,
        content: str,
        extraction: Extraction,
        *,
        device_id: str,
        agent_id: str,
        namespace: str = "private",
        transaction_from: str = "",
    ) -> Neuron:
        return Neuron(
            device_id=device_id,
            agent_id=agent_id,
            memory_type=extraction.memory_type,
            content=content.strip(),
            pad=extraction.pad,
            confidence=extraction.confidence,
            namespace=namespace,  # type: ignore[arg-type]
            transaction_from=transaction_from,
        )


def _clamp_float(value: Any, bound: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(-bound, min(bound, number))


def _clamp01(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, number))
