"""core.fast_recall —— 字符 2-gram 余弦轻量召回（零模型、零第三方依赖）。

与 v3.daemon.V3Daemon.recall 同一套算法，抽成独立纯函数，供：
- stdio 独占模式（桌面没开、没有 Daemon 实例）的 recall_memory 默认路径
- 任意只需要「先给点结果」的调用方

六通道 embedding/RRF/精排实测 30-45s，这里通常 <0.05s。

W8 / A4：新增**进程内叙事投影缓存层**（`build_projection` / `get_projection` /
`invalidate` / `fast_recall_projected`）。`fast_recall(rows, query, limit)` 纯函数保持不动。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Any

__all__ = [
    "ProjectionCache", "build_projection", "convergence_injection",
    "fast_recall", "fast_recall_projected", "get_projection",
    "invalidate", "narrative_ids_hash",
]


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


@dataclass
class ProjectionCache:
    """topic 级投影缓存；派生数据，进程重启重建即可（几十毫秒）。"""

    rows: list[dict[str, Any]]
    narrative_ids_hash: str
    built_at_ms: int


_PROJECTIONS: dict[str, ProjectionCache] = {}
_PROJECTION_LOCK = threading.RLock()


def narrative_ids_hash(topic_id: str, *, kv: Any) -> str:
    """A4/R26：该 topic 下所有叙事 neuron id 排序后 SHA1，新增/删除即变，缓存自动失效。"""
    ids: list[str] = []
    try:
        for key, raw in kv.scan_prefix("meta", "", limit=2000):
            try:
                meta = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if (meta.get("pyramid_subtype") == "narrative"
                    and str(meta.get("namespace") or "") == str(topic_id)):
                ids.append(str(key))
    except Exception:  # noqa: BLE001  KV 不可用时按「无叙事」处理
        return ""
    return hashlib.sha1("|".join(sorted(ids)).encode("utf-8", "replace")).hexdigest()


def build_projection(topic_id: str, *, relational: Any, kv: Any,
                     limit: int = 500) -> list[dict[str, Any]]:
    """A4：只投影认知性内容；叙事 neuron **只对故事骨架建 2-gram 索引**，命中后注入全文。"""
    rows: list[dict[str, Any]] = []
    try:
        records = relational.query("neuron", namespaces=(topic_id,), limit=limit)
    except TypeError:  # 兼容不支持 namespaces 参数的仓储实现
        records = [row for row in relational.query("neuron", limit=limit)
                   if str(row.get("namespace") or "") == str(topic_id)]
    for record in records:
        if record.get("is_active", True) is False:
            continue
        neuron_id = str(record.get("id") or "")
        content = str(record.get("content") or "")
        prefix = ""
        try:
            raw_meta = kv.get("meta", neuron_id) if kv is not None else None
            meta = json.loads(raw_meta) if raw_meta else {}
            skeleton = meta.get("narrative_skeleton") or {}
            prefix = str(skeleton.get("prefix") or "")
        except (ValueError, TypeError, OSError):
            meta = {}
        is_narrative = meta.get("pyramid_subtype") == "narrative"
        rows.append({
            "id": neuron_id,
            # 索引文本：叙事用故事骨架（前 200 字），碎片用正文前 200 字
            "index_text": prefix or content[:200],
            "content": content,               # 命中后注入的是完整叙事文本
            "memory_type": record.get("memory_type", "fact"),
            "pad": [record.get("pad_p", 0.0), record.get("pad_a", 0.0),
                    record.get("pad_d", 0.0)],
            "is_narrative": is_narrative,
            "namespace": record.get("namespace", topic_id),
        })
    return rows


def get_projection(topic_id: str, *, relational: Any, kv: Any) -> ProjectionCache:
    signature = narrative_ids_hash(topic_id, kv=kv)
    with _PROJECTION_LOCK:
        cached = _PROJECTIONS.get(topic_id)
        if cached is not None and cached.narrative_ids_hash == signature:
            return cached
        fresh = ProjectionCache(rows=build_projection(topic_id, relational=relational, kv=kv),
                                narrative_ids_hash=signature, built_at_ms=int(time.time() * 1000))
        _PROJECTIONS[topic_id] = fresh
        return fresh


def invalidate(topic_id: str) -> None:
    """叙事写入后主动失效（R26/R46）；下次同 topic 查询重建投影。"""
    with _PROJECTION_LOCK:
        _PROJECTIONS.pop(topic_id, None)


def fast_recall_projected(topic_id: str, query: str, *, relational: Any,
                          kv: Any, limit: int = 3) -> dict[str, Any]:
    """毫秒级投影召回：命中叙事骨架时注入完整叙事文本，不是只有 deep 才有故事感。"""
    projection = get_projection(topic_id, relational=relational, kv=kv)
    vector = _ngram(str(query))
    scored: list[dict[str, Any]] = []
    for row in projection.rows:
        score = _cosine(vector, _ngram(row["index_text"]))
        if score <= 0:
            continue
        scored.append({
            **row, "score": round(score, 4), "channels": ["projection"],
        })
    scored.sort(key=lambda item: item["score"], reverse=True)
    hits = scored[:max(1, int(limit))]
    return {
        "query": str(query), "hits": hits, "degraded": True,
        "recall_mode": "projection", "topic": str(topic_id),
        "narrative_hits": sum(1 for item in hits if item["is_narrative"]),
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
