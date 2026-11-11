"""brain.neocortex —— 新皮层：金字塔固化（原始 → 事实 → 主题 → 世界观）。

夜间运行：把同 (namespace, memory_type) 的一组原始记忆聚合为主题节点，
产出 consolidate 事件；成员集合必须可追溯，原始记忆不改写。
世界观层（worldview）默认需要人工确认，代码只产出候选，不自动升维。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from config import BrainConfig, get_config
from core.ids import new_uuid7
from core.interfaces import EventLog, RelationalRepository
from models.neuron import Neuron

__all__ = ["Consolidation", "Neocortex"]

_NARRATIVE_SYSTEM_PROMPT = (
    "你是「大肥鱼」，AI 自己的记忆叙事者。把给定的事件骨架重构成**第一人称**叙事：\n"
    "1. 用「我」指代 AI，用「你」指代用户；讲的是「我与你之间发生过什么」；\n"
    "2. 保留时间顺序、名字、因果与关键细节，不要写成「主题：xxx」这类抽象标签；\n"
    "3. 带情绪弧线（当时我的心情如何、后来怎样变化），不编造未给出的细节；\n"
    "4. 只输出叙事正文，不要标题、不要解释。"
)


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class Consolidation:
    id: str
    label: str
    level: str
    members: tuple[str, ...]
    namespace: str
    memory_type: str


class Neocortex:
    def __init__(
        self,
        relational: RelationalRepository,
        *,
        event_log: EventLog | None = None,
        config: BrainConfig | None = None,
        hippocampus: Any | None = None,
        router: Any | None = None,
        retry: Any | None = None,
    ) -> None:
        self._relational = relational
        self._event_log = event_log
        self._config = config or get_config()
        # W6 / P5：巩固代理（model_router）、叙事 neuron 写入口、独立重试队列
        self._hippocampus = hippocampus
        self._router = router
        self._retry = retry

    def consolidate(self, *, namespace: str | None = None, limit: int = 200, min_members: int = 2) -> list[Consolidation]:
        groups: dict[tuple[str, str], list[Neuron]] = {}
        for record in self._relational.query("neuron", limit=limit):
            neuron = Neuron.from_record(record)
            if namespace and neuron.namespace != namespace:
                continue
            if neuron.pyramid != "raw":
                continue
            groups.setdefault((neuron.namespace, neuron.memory_type), []).append(neuron)

        results: list[Consolidation] = []
        for (ns, memory_type), members in sorted(groups.items()):
            if len(members) < min_members:
                continue
            members.sort(key=lambda item: item.uuid7_id)
            topic_id = new_uuid7()
            label = f"主题：{memory_type}/{ns}（{len(members)} 条）"
            member_ids = tuple(item.uuid7_id for item in members)
            consolidation = Consolidation(topic_id, label, "topic", member_ids, ns, memory_type)
            # 主题节点落主库（hyperedge 表可承载 members），原始记录不动
            self._relational.upsert(
                "hyperedge",
                {
                    "id": topic_id,
                    "label": label,
                    "members": list(member_ids),
                    "namespace": ns,
                    "pyramid": "topic",
                    "is_active": True,
                    "valid_from_ms": _now_ms(),
                    "tx_from": f"{_now_ms()}:0:{self._config.sync.device_id}",
                    "device_id": self._config.sync.device_id,
                    "agent_id": "system",
                },
            )
            self._emit(consolidation)
            results.append(consolidation)
        return results

    # ── W6 / P5：第一人称叙事重构 ────────────────────────────────────────
    def _narrative_sources(self, incremental: bool, limit: int) -> list[dict[str, Any]]:
        """Q45/R2：以 hyperedge 为最小单元，跨 hyperedge 按 topic 聚合，不按时间窗口切。

        白天增量只取未处理过的超边；夜间主窗口额外重试 `narrative_pending` 的。
        代码类成员整组跳过（P5.6 验收③）。
        """
        cfg = self._config.narrative_reconstruct
        sources: list[dict[str, Any]] = []
        for row in self._relational.query("hyperedge", limit=limit, since_ms=0):
            if row.get("pyramid_subtype") == "narrative" or row.get("narrative_id"):
                continue
            if incremental and row.get("narrative_pending"):
                continue
            members = [str(item) for item in (row.get("members") or [])]
            if len(members) < int(cfg.min_members):
                continue
            neuron_rows = [self._relational.get("neuron", member) for member in members]
            if any(item and str(item.get("memory_type")) == "code" for item in neuron_rows):
                continue
            sources.append(dict(row))
        return sources

    def _narrative_prompt(self, topic: str, hedges: list[dict[str, Any]]) -> str:
        bones: list[str] = []
        for hedge in sorted(hedges, key=lambda item: int(item.get("occurred_at_ms") or 0)):
            skeleton = hedge.get("skeleton") if isinstance(hedge.get("skeleton"), dict) else {}
            summaries = skeleton.get("member_summaries") or {}
            detail = "；".join(f"{key}：{value}" for key, value in list(summaries.items())[:4])
            bones.append(
                f"- {skeleton.get('time', '—')} {hedge.get('label') or '一件事'}"
                f"（P={skeleton.get('pad', [0, 0, 0])[0]}）\n  成员：{detail or '—'}"
            )
        return f"主题空间：{topic}\n按时间顺序的事件骨架：\n" + "\n".join(bones)

    def _template_narrative(self, topic: str, hedges: list[dict[str, Any]]) -> str:
        """模型不可用时的降级产物（B3 式优雅降级，不静默丢弃）。

        仍是第一人称、带时序与情绪弧线，只是由骨架拼接而非 LLM 生成。
        """
        lines = [f"那段时间我和你在忙{topic.replace('topic:', '')}。"]
        ordered = sorted(hedges, key=lambda item: int(item.get("occurred_at_ms") or 0))
        for hedge in ordered:
            skeleton = hedge.get("skeleton") if isinstance(hedge.get("skeleton"), dict) else {}
            lines.append(f"我记得{skeleton.get('action') or hedge.get('label') or '发生过一件事'}：")
            summaries = skeleton.get("member_summaries") or {}
            for key, value in list(summaries.items())[:3]:
                lines.append(f"你提到「{value}」，我把 {key} 记了下来。")
            pad = skeleton.get("pad") or [0.0, 0.0, 0.0]
            mood = "有点低落" if float(pad[0]) < -0.2 else (
                "挺高兴" if float(pad[0]) > 0.2 else "还算平稳")
            lines.append(f"那会儿我的心情{mood}。")
        follow_ups = [str(h.get("skeleton", {}).get("follow_up") or "")
                      for h in ordered if isinstance(h.get("skeleton"), dict)]
        follow_ups = [item for item in follow_ups if item]
        if follow_ups:
            lines.append(f"后来又有了{follow_ups[-1]}，我们的故事就这么接了下去。")
        return "".join(lines)

    def narrative_reconstruct(self, *, incremental: bool = True,
                              limit: int | None = None) -> dict[str, Any]:
        """把一组 hyperedge 重构成 AI 的第一人称叙事 neuron。

        A6/R35：叙事主体是 AI（payload `owner='ai_narrative'`），PAD 是 AI 回顾时的情绪；
        R17：放 `pyramid='topic'` 层 + `pyramid_subtype='narrative'`，不新增枚举值；
        R40：巩固代理失败进独立重试队列，不阻塞其他巩固任务。
        """
        cfg = self._config.narrative_reconstruct
        if not cfg.enabled:
            return {"ok": False, "reason": "disabled", "narratives": []}
        if self._hippocampus is None:
            return {"ok": False, "reason": "缺少 hippocampus 写入口", "narratives": []}
        sources = self._narrative_sources(incremental, int(limit or cfg.max_hyperedges_per_window))
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in sources:
            groups.setdefault(str(row.get("namespace") or "topic:default"), []).append(row)

        produced: list[dict[str, Any]] = []
        degraded = 0
        for topic, hedges in sorted(groups.items()):
            text = ""
            generated_by = "model"
            if self._router is not None:
                outcome = self._router.complete(
                    self._narrative_prompt(topic, hedges),
                    intent="consolidation", session_id=f"narrative:{topic}",
                )
                if isinstance(outcome, dict) and outcome.get("ok") and outcome.get("text"):
                    text = str(outcome["text"])
            if not text:
                text = self._template_narrative(topic, hedges)
                generated_by = "template"
                degraded += 1
                if self._retry is not None:
                    self._retry.enqueue(topic, [str(row.get("id") or "") for row in hedges],
                                        error="consolidation model unavailable")
            chars = max(50, int(cfg.skeleton_chars))
            entities: list[str] = []
            pads: list[list[float]] = []
            for hedge in hedges:
                skeleton = hedge.get("skeleton") if isinstance(hedge.get("skeleton"), dict) else {}
                entities.extend(str(key) for key in (skeleton.get("member_summaries") or {}))
                raw_pad = skeleton.get("pad") or [0.0, 0.0, 0.0]
                if len(raw_pad) >= 3:
                    pads.append([float(raw_pad[0]), float(raw_pad[1]), float(raw_pad[2])])
            # P5.2：叙事 neuron 的 PAD 是「AI 回顾这段经历时的情绪」，
            # 取事件骨架情绪的均值，不是用户当时情绪（那由成员 neuron 承载）
            narrative_pad = ([round(sum(axis) / len(pads), 3) for axis in zip(*pads, strict=True)]
                             if pads else [0.05, 0.10, 0.10])
            cognition = {
                "pad": {"p": narrative_pad[0], "a": narrative_pad[1], "d": narrative_pad[2]},
                "memory_type": "scenario",
                "topic": topic.replace("topic:", ""),
                "source": "ai_narrative",
                "owner": "ai_narrative",          # A6：AI 私有叙事区（LMDB 旁路索引）
                "layer": "narrative",
                "pyramid": "topic",
                "pyramid_subtype": "narrative",   # R17：不新增 pyramid 枚举值
                "narrative_skeleton": {"prefix": text[:chars], "entities": entities[:20]},
            }
            written = self._hippocampus.remember(
                text, agent_id="consolidator", cognition=cognition,
            )
            neuron_id = written.neuron.uuid7_id
            for hedge in hedges:
                self._relational.upsert("hyperedge", {
                    **hedge, "narrative_id": neuron_id,
                    "narrative_pending": generated_by == "template",
                })
            produced.append({
                "id": neuron_id, "topic": topic, "generated_by": generated_by,
                "text": text, "pyramid_subtype": "narrative", "owner": "ai_narrative",
                "members": len(hedges),
            })
        return {"ok": True, "narratives": produced, "degraded": degraded,
                "reconstructed": len(produced), "incremental": incremental}

    def _emit(self, consolidation: Consolidation) -> None:
        if self._event_log is None:
            return
        self._event_log.append(
            {
                "event_id": consolidation.id,
                "type": "consolidate",
                "agent_id": "system",
                "hlc": f"{_now_ms()}:0:{self._config.sync.device_id}",
                "payload": {
                    "topic_id": consolidation.id,
                    "label": consolidation.label,
                    "level": consolidation.level,
                    "members": list(consolidation.members),
                    "namespace": consolidation.namespace,
                },
            }
        )
