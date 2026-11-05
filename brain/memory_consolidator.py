"""brain.memory_consolidator —— 语义合并裁决 + 聚簇主题摘要（对标 Mem0 的写入裁决）。

写入时（MemoryConsolidator.decide）：
    召回最相似的旧记忆 → LLM 裁决 add / update / merge / skip →
    update/merge 产出合并正文，supersede_ids 标记被替代的旧记忆；
    LLM 不可用或裁决非法时兜底为 add（丘脑去重仍是第一道防线，绝不静默丢信息）。

夜间巩固（summarize_clusters）：
    对新皮层聚出的主题簇生成一段摘要，以「【主题摘要】」记忆入库（完整写入流），
    LLM 不可用时退化为成员前两句拼接 —— 聚类靠向量，摘要靠 LLM，两者独立降级。
"""
from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from config import BrainConfig, get_config
from core.errors import EngineUnavailable
from core.interfaces import (
    Embedder,
    LocalLLM,
    RecallFilter,
    RelationalRepository,
    VectorRepository,
)

__all__ = ["MemoryConsolidator", "MergeDecision", "TopicSummary"]

_SYSTEM_PROMPT = (
    "你是记忆管理器。给你一条新记忆与若干最相似的旧记忆。"
    "判断新记忆与旧记忆的关系并裁决：\n"
    'add：新信息独立，直接新增；\n'
    'update：新信息更新了某条旧记忆（如换了时间/负责人），action=update，'
    "supersede_ids 填被更新的旧记忆 id，content 给出更新后的完整正文；\n"
    'merge：多条旧记忆与新记忆讲同一件事，合并成一条，action=merge，'
    "supersede_ids 填全部被合并的旧记忆 id，content 给出合并后的完整正文；\n"
    'skip：新记忆的信息已完全包含在某条旧记忆里，action=skip，不写库；\n'
    "拿不准就 add。只输出 JSON。"
)


@dataclass(frozen=True)
class MergeDecision:
    action: Literal["add", "update", "merge", "skip"]
    content: str = ""
    supersede_ids: tuple[str, ...] = ()
    reason: str = ""
    candidates: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TopicSummary:
    label: str
    summary: str
    members: tuple[str, ...]
    applied: bool


class MemoryConsolidator:
    def __init__(
        self,
        *,
        llm: LocalLLM,
        relational: RelationalRepository,
        vector: VectorRepository,
        embedder: Embedder | None,
        config: BrainConfig | None = None,
    ) -> None:
        self._llm = llm
        self._relational = relational
        self._vector = vector
        self._embedder = embedder
        self._config = config or get_config()

    # ── 写入裁决 ──────────────────────────────────────────────────────────
    def decide(self, content: str, *, agent_id: str, namespace: str = "private") -> MergeDecision:
        params = self._config.merge
        if not params.merge_enabled:
            return MergeDecision(action="add", reason="语义合并已关闭")
        candidates = self._similar_candidates(content, limit=params.merge_candidate_limit, namespace=namespace)
        if not candidates:
            return MergeDecision(action="add", reason="无相似旧记忆")
        try:
            response = self._llm.complete_json(
                _SYSTEM_PROMPT,
                json.dumps(
                    {"new_memory": content, "old_memories": candidates}, ensure_ascii=False
                ),
                required_keys=["action", "reason"],
            )
        except Exception as exc:  # noqa: BLE001  LLM 任何失败都兜底为新增，绝不阻断写入
            return MergeDecision(
                action="add", reason=f"LLM 不可用，按新增处理（{type(exc).__name__}: {exc}）",
                candidates=tuple(item["id"] for item in candidates),
            )
        action = str(response.get("action", "add")).lower()
        if action not in ("add", "update", "merge", "skip"):
            return MergeDecision(action="add", reason=f"LLM 裁决非法（{action}），按新增处理")
        known_ids = {item["id"] for item in candidates}
        supersede_ids = tuple(
            item for item in (response.get("supersede_ids") or []) if item in known_ids
        )
        if action in ("update", "merge") and (not str(response.get("content", "")).strip() or not supersede_ids):
            return MergeDecision(
                action="add", reason="裁决缺合并正文或替代目标，按新增处理",
                candidates=tuple(item["id"] for item in candidates),
            )
        return MergeDecision(
            action=action,                                   # type: ignore[arg-type]
            content=str(response.get("content", "")).strip(),
            supersede_ids=supersede_ids,
            reason=str(response.get("reason", "")),
            candidates=tuple(item["id"] for item in candidates),
        )

    def _similar_candidates(
        self, content: str, *, limit: int, namespace: str
    ) -> list[dict[str, Any]]:
        """向量优先、FTS 兜底地取最相似的活跃旧记忆。"""
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        if self._embedder is not None:
            try:
                embedding = self._embedder.embed_query(content)
                for scored in self._vector.search_dense(
                    embedding.dense, limit=limit, where=RecallFilter(namespaces=(namespace,))
                ):
                    row = self._relational.get("neuron", scored.id)
                    if row is not None and row.get("is_active", True):
                        rows.append(dict(row))
                        seen.add(scored.id)
            except EngineUnavailable:
                rows = []
        if len(rows) < limit:
            # FTS 兜底：改写句与旧句没有连续公共前缀，用 4 字滑窗片段探测——
            # 改写/扩写总会保留若干四字片段（相关性最终交给 LLM 裁决）
            import re

            clean = re.sub(r"[^\w]+", "", content)
            probes: list[str] = []
            if len(clean) >= 12:
                probes.append(clean[:12])
            probes.extend(
                clean[start : start + 4] for start in range(0, max(1, len(clean) - 3), 3)
            )
            seen_probes: set[str] = set()
            for probe_text in probes:
                if len(probe_text) < 4 or probe_text in seen_probes:
                    continue
                seen_probes.add(probe_text)
                try:
                    for row in self._relational.full_text_search("neuron", probe_text, limit=limit):
                        if str(row.get("id")) in seen or not row.get("is_active", True):
                            continue
                        rows.append(dict(row))
                        seen.add(str(row.get("id")))
                except Exception:  # noqa: BLE001  FTS 只是兜底通道
                    pass
                if len(rows) >= limit:
                    break
        return rows[:limit]

    # ── 聚簇主题摘要 ──────────────────────────────────────────────────────
    def summarize_clusters(
        self,
        consolidations: Sequence[Any],
        *,
        remember: Callable[..., Any],
        min_members: int | None = None,
    ) -> list[TopicSummary]:
        """对每簇生成主题摘要并以完整写入流入库；返回成功生成的摘要。"""
        params = self._config.merge
        min_count = min_members if min_members is not None else params.cluster_min_members
        if not params.cluster_summary_enabled:
            return []
        summaries: list[TopicSummary] = []
        for consolidation in consolidations:
            members = tuple(consolidation.members)
            if len(members) < min_count:
                continue
            contents: list[str] = []
            for member_id in members[:6]:
                row = self._relational.get("neuron", member_id)
                if row is not None:
                    contents.append(str(row.get("content", "")))
            if not contents:
                continue
            text = self._summarize(consolidation.label, contents)
            try:
                result = remember(f"【主题摘要】{consolidation.label}：{text}", agent_id="consolidator")
                applied = bool(getattr(result, "applied", False))
            except Exception as exc:  # noqa: BLE001  摘要失败不影响巩固流程
                print("SUMMARY EXC:", type(exc).__name__, repr(str(exc))[:200], flush=True)
                applied = False
            summaries.append(TopicSummary(consolidation.label, text, members, applied))
        return summaries

    def _summarize(self, label: str, contents: list[str]) -> str:
        try:
            response = self._llm.complete_json(
                "把同主题的多条记忆提炼成一段 100 字以内的主题摘要，只输出 JSON。",
                json.dumps({"topic": label, "memories": contents}, ensure_ascii=False),
                required_keys=["summary"],
            )
            return str(response["summary"]).strip()
        except Exception:  # noqa: BLE001  摘要失败退化为规则拼接
            return "；".join(contents[:2])  # 规则兜底：拼前两条
