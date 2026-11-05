"""brain.router_extractor —— 感知：LLM 严格 JSON 抽取。

输出契约（`_SYSTEM_PROMPT` 与 `Extraction` 一一对应）：
    memory_type / entities / relations / hyperedge / pad / confidence

降级：LLM 不可用或输出非法时走**规则抽取**（`_rule_extract`），
PAD 用情绪词典估算 —— 保证"每条记忆都带 PAD"这一全链路不变量，
但 `source` 字段明确标注是 rule 而非 llm，便于 eval 区分质量。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
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

    def extract(self, content: str, *, agent_id: str = "") -> Extraction:
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

    # ── LLM 结果 → Extraction ────────────────────────────────────────────
    def _from_payload(self, payload: dict[str, Any]) -> Extraction:
        raw_pad = payload.get("pad") or {}
        pad = PAD(
            p=_clamp_float(raw_pad.get("p", 0.0)),
            a=_clamp_float(raw_pad.get("a", 0.0)),
            d=_clamp_float(raw_pad.get("d", 0.0)),
        )
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
