"""brain.pad_lexicon —— 轻量文本→PAD 词典（v4.1 W5 / C7 / R10）。

`brain/amygdala.py` 只有 PAD 混合积分，没有「文本 → PAD」的估算规则；
聊天热路径不能等 0.6B LLM 推理，所以用规则 + 词典，单次 <1ms。
词典规模刻意保持小（可扩展），不引第三方情感分析依赖。
"""
from __future__ import annotations

__all__ = ["estimate_pad", "lexicon_size"]

_POSITIVE = frozenset({
    "开心", "高兴", "愉快", "喜欢", "感谢", "太好了", "不错", "顺利", "成功",
    "满意", "惊喜", "轻松", "安心", "温暖", "期待", "厉害", "棒", "赞",
    "和解", "解决", "完成", "好转", "不错", "哈哈", "嘿",
})
_NEGATIVE = frozenset({
    "难过", "生气", "愤怒", "焦虑", "委屈", "失望", "沮丧", "害怕", "担心",
    "烦躁", "崩溃", "吵架", "冷战", "冲突", "痛苦", "疲惫", "累", "糟",
    "糟了", "失败", "出错", "报错", "问题", "麻烦", "讨厌", "后悔",
})
_AROUSAL = frozenset({"!", "！", "?", "？", "居然", "竟然", "突然", "立刻", "马上", "太", "非常"})
_AGENCY = frozenset({"我", "我们", "决定", "要求", "必须", "应该", "打算", "负责"})


def lexicon_size() -> int:
    return len(_POSITIVE) + len(_NEGATIVE) + len(_AROUSAL) + len(_AGENCY)


def _clamp(value: float) -> float:
    return max(-1.0, min(1.0, value))


def estimate_pad(text: str) -> tuple[float, float, float]:
    """返回 (P 愉悦度, A 唤醒度, D 支配度)，全部 ∈ [-1, 1]。

    纯规则：情感词决定 P，标点/程度词决定 A，主体与意志词决定 D。
    中性文本返回低唤醒的中性基线，不会全零（与 `_from_payload` 的不变量一致）。
    """
    content = str(text or "")
    positive = sum(1 for word in _POSITIVE if word in content)
    negative = sum(1 for word in _NEGATIVE if word in content)
    hits = positive + negative
    p = _clamp((positive - negative) / max(1.0, hits) * 0.8) if hits else 0.0
    a = _clamp(0.10 + 0.18 * hits + 0.12 * sum(1 for mark in _AROUSAL if mark in content))
    d = _clamp(0.10 + 0.15 * sum(1 for word in _AGENCY if word in content))
    return (round(p, 3), round(a, 3), round(d, 3))
