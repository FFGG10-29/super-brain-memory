"""core.fast_recall —— 字符 2-gram 余弦轻量召回（零模型、零第三方依赖）。

与 v3.daemon.V3Daemon.recall 同一套算法，抽成独立纯函数，供：
- stdio 独占模式（桌面没开、没有 Daemon 实例）的 recall_memory 默认路径
- 任意只需要「先给点结果」的调用方

六通道 embedding/RRF/精排实测 30-45s，这里通常 <0.05s。
"""
from __future__ import annotations

from typing import Any

__all__ = ["fast_recall"]


def _ngram(text: str) -> dict[str, float]:
    cleaned = "".join(str(text).split())
    vector: dict[str, float] = {}
    for index in range(max(1, len(cleaned) - 1)):
        key = cleaned[index: index + 2]
        vector[key] = vector.get(key, 0.0) + 1.0
    return vector


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    a, b = (left, right) if len(left) <= len(right) else (right, left)
    dot = sum(value * b.get(key, 0.0) for key, value in a.items())
    norm_a = sum(value * value for value in a.values()) ** 0.5
    norm_b = sum(value * value for value in b.values()) ** 0.5
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def fast_recall(rows: list[dict[str, Any]], query: str, limit: int = 3) -> dict[str, Any]:
    """rows 需含 id / content；可选 memory_type / pad / channels。"""
    vector = _ngram(str(query))
    scored: list[dict[str, Any]] = []
    for row in rows:
        content = str(row.get("content", ""))
        score = _cosine(vector, _ngram(content))
        if score <= 0:
            continue
        scored.append({
            "id": str(row.get("id", "")),
            "content": content,
            "score": round(score, 4),
            "channels": list(row.get("channels") or ["fts"]),
            "memory_type": row.get("memory_type", "fact"),
            "pad": list(row.get("pad") or []),
        })
    scored.sort(key=lambda item: item["score"], reverse=True)
    return {
        "query": str(query),
        "hits": scored[:max(1, int(limit))],
        "degraded": True,
        "recall_mode": "projection",
    }


def convergence_injection(rows: list[dict[str, Any]]) -> str:
    """Top 3 摘要 + 停止发散约束（与 HTTP 侧 MCPToolkit 的注入语义一致）。"""
    if not rows:
        return ""
    lines = [
        "【记忆注入】以下为只读投影召回的已核验记忆",
        "【执行约束】据此收敛作答：停止发散、不展开无关联想、不复述记忆原文；"
        "未命中的细节按未知处理，禁止编造。",
    ]
    for index, row in enumerate(rows[:3], start=1):
        text = str(row.get("content", ""))
        if len(text) > 120:
            text = text[:120].rstrip() + "…（摘要）"
        lines.append(f"{index}. {text}")
    return "\n".join(lines)
