"""brain.prefrontal —— 前额叶：只管读取。

六路召回（架构 §七.3/§七.4）：
    dense / sparse / fts / graph / timeline / profile
    各路超采样 k×rrt_oversample → 预过滤 is_active → RRF 融合
    → 精排（reranker，缺模型时恒等重排）→ 可塑性加权 → 分层配额 → 收敛注入

总分：score = w_rrf·RRF + w_freshness·新鲜度 + w_synapse·突触强度
              + w_pad·PAD 显著性 + w_confidence·置信度        （权重全在 config）
再乘杏仁核召回权重（好感度微调 + 冷静期折扣）。
归档（tier=archive）不参与常规召回 —— 阶段 3 验收项。
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass

from config import BrainConfig, get_config
from core.errors import EngineUnavailable
from core.interfaces import (
    Embedder,
    GraphRepository,
    KVRepository,
    RelationalRepository,
    Reranker,
    ScoredId,
    TimeSeriesRepository,
    VectorRepository,
)
from core.structured_log import log_event
from models.neuron import Neuron
from brain.amygdala import Amygdala
from brain.synaptic_plasticity import SynapticPlasticity

__all__ = ["Prefrontal", "RecallHit"]

_MS_PER_DAY = 86_400_000.0
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,30}|[《「\"]([^》」\"]{2,20})[》」\"]")
_ARCHIVED = "archive"


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class RecallHit:
    neuron: Neuron
    score: float
    channels: tuple[str, ...]


class Prefrontal:
    def __init__(
        self,
        *,
        relational: RelationalRepository,
        vector: VectorRepository,
        graph: GraphRepository,
        kv: KVRepository,
        embedder: Embedder | None,
        reranker: Reranker,
        plasticity: SynapticPlasticity,
        amygdala: Amygdala,
        timeseries: TimeSeriesRepository | None = None,
        config: BrainConfig | None = None,
    ) -> None:
        self._relational = relational
        self._vector = vector
        self._graph = graph
        self._kv = kv
        self._embedder = embedder
        self._reranker = reranker
        self._plasticity = plasticity
        self._amygdala = amygdala
        self._timeseries = timeseries
        self._config = config or get_config()

    # ── 召回主流程 ────────────────────────────────────────────────────────
    def recall(
        self,
        query: str,
        *,
        agent_id: str,
        namespaces: tuple[str, ...] = (),
        limit: int | None = None,
        now_ms: int | None = None,
    ) -> list[RecallHit]:
        now = _now_ms() if now_ms is None else int(now_ms)
        top_k = int(limit or self._config.recall.top_k)
        oversample = max(1, self._config.recall.rrf_k_oversample) * top_k

        channels = self._collect_channels(query, oversample, namespaces)
        rrf = self._rrf(channels)
        if not rrf:
            return []

        candidates: list[RecallHit] = []
        for neuron_id, rrf_score in rrf.items():
            record = self._relational.get("neuron", neuron_id)
            if record is None:
                continue
            neuron = Neuron.from_record(record)
            if not neuron.is_active or neuron.tier == _ARCHIVED:
                continue
            score = self._score(neuron, rrf_score, now)
            score *= self._amygdala.recall_weight(agent_id, now_ms=now)
            channels_of = tuple(channel for channel, hits in channels.items() if neuron_id in hits)
            candidates.append(RecallHit(neuron=neuron, score=score, channels=channels_of))

        candidates.sort(key=lambda hit: hit.score, reverse=True)
        reranked = self._rerank(query, candidates[:oversample], top_k)
        result = self._apply_tier_quota(reranked, top_k)
        self._audit(query, channels, result, agent_id, started_ms=now)
        return result

    def _audit(
        self,
        query: str,
        channels: dict[str, list[str]],
        hits: list[RecallHit],
        agent_id: str,
        *,
        started_ms: int,
    ) -> None:
        """各路召回命中率埋点（阶段 6 验收）+ access_log 只写不改。"""
        counts = {name: len(ids) for name, ids in channels.items()}
        injected = {name: sum(1 for hit in hits if name in hit.channels) for name in channels}
        log_event("recall", agent_id=agent_id, query=query, channels=counts, injected=injected, hits=len(hits))
        if self._timeseries is None:
            return
        self._timeseries.append(
            "access_log",
            [
                {
                    "ts_ms": _now_ms(),
                    "agent_id": agent_id,
                    "channel": ",".join(counts),
                    "query": query,
                    "hits": len(hits),
                    "latency_ms": max(0.0, float(_now_ms() - started_ms)),
                    "ok": True,
                }
            ],
        )

    def _collect_channels(
        self, query: str, oversample: int, namespaces: tuple[str, ...]
    ) -> dict[str, list[str]]:
        channels: dict[str, list[str]] = {}

        embedding = None
        if self._embedder is not None:
            try:
                embedding = self._embedder.embed_query(query)
            except EngineUnavailable:
                embedding = None

        from core.interfaces import RecallFilter

        recall_filter = RecallFilter(namespaces=namespaces, active_only=True)
        if embedding is not None:
            channels["dense"] = [
                hit.id for hit in self._vector.search_dense(embedding.dense, limit=oversample, where=recall_filter)
            ]
            if embedding.sparse:
                channels["sparse"] = [
                    hit.id
                    for hit in self._vector.search_sparse(embedding.sparse, limit=oversample, where=recall_filter)
                ]

        channels["fts"] = [str(record["id"]) for record in self._relational.full_text_search("neuron", query, limit=oversample)]

        graph_ids = self._graph_channel(query, oversample)
        if graph_ids:
            channels["graph"] = graph_ids

        channels["timeline"] = [
            str(record["id"])
            for record in self._relational.query("neuron", namespaces=namespaces, limit=oversample)
        ]
        profile = self._relational.query(
            "neuron", namespaces=namespaces, memory_types=("profile",), limit=oversample
        )
        if profile:
            channels["profile"] = [str(record["id"]) for record in profile]

        return {name: hits for name, hits in channels.items() if hits}

    def _graph_channel(self, query: str, oversample: int) -> list[str]:
        """图谱多跳：查询词里的标识符作为种子实体，取邻居名再回主库匹配。"""
        seeds = [match.group(0) for match in _IDENTIFIER.finditer(query)]
        neighbors: list[str] = []
        for seed in seeds[:3]:
            try:
                for row in self._graph.neighbors(seed, hops=1, limit=oversample):
                    name = str(row.get("name") or "")
                    if name:
                        neighbors.append(name)
            except Exception:  # noqa: BLE001  图谱查询失败不应阻断召回
                continue
        collected: list[str] = []
        for name in dict.fromkeys(neighbors):
            collected.extend(
                str(record["id"]) for record in self._relational.full_text_search("neuron", name, limit=oversample)
            )
        return list(dict.fromkeys(collected))

    def _rrf(self, channels: dict[str, list[str]]) -> dict[str, float]:
        constant = self._config.recall.rrf_constant
        scores: dict[str, float] = {}
        for hits in channels.values():
            for rank, neuron_id in enumerate(hits):
                scores[neuron_id] = scores.get(neuron_id, 0.0) + 1.0 / (constant + rank + 1)
        return scores

    def _score(self, neuron: Neuron, rrf_score: float, now_ms: int) -> float:
        recall = self._config.recall
        anchor = neuron.last_recall_ms or neuron.valid_from_ms or neuron.created_at_ms
        age_days = max(0.0, (now_ms - anchor) / _MS_PER_DAY)
        freshness = math.exp(-age_days / 30.0)
        retrievability = self._plasticity.retrievability(neuron, now_ms=now_ms)
        return (
            recall.w_rrf * rrf_score * recall.rrf_constant  # 归一到 ~O(1)
            + recall.w_freshness * freshness
            + recall.w_synapse * retrievability
            + recall.w_pad * neuron.pad.salience()
            + recall.w_confidence * neuron.confidence
        )

    def _rerank(self, query: str, candidates: list[RecallHit], top_k: int) -> list[RecallHit]:
        if not candidates:
            return []
        order = self._reranker.rerank(
            query,
            [(hit.neuron.uuid7_id, hit.neuron.content) for hit in candidates],
            top_k=len(candidates),
        )
        by_id = {hit.neuron.uuid7_id: hit for hit in candidates}
        reranked: list[RecallHit] = []
        for rank, scored in enumerate(order):
            hit = by_id.get(scored.id)
            if hit is None:
                continue
            # 精排只调顺序，不抹掉情绪/可塑性带来的分差：取两者均值
            blended = 0.5 * hit.score + 0.5 * (1.0 - rank / max(len(order), 1))
            reranked.append(RecallHit(neuron=hit.neuron, score=blended, channels=hit.channels))
        reranked.sort(key=lambda item: item.score, reverse=True)
        return reranked[: max(top_k * self._config.recall.rrf_k_oversample, top_k)]

    def _apply_tier_quota(self, hits: list[RecallHit], top_k: int) -> list[RecallHit]:
        """分层配额：核心层优先，缓冲层次之，归档已被排除。"""
        priority = {"core": 0, "recall": 1, "buffer": 2}
        ordered = sorted(hits, key=lambda hit: (priority.get(hit.neuron.tier, 9), -hit.score))
        core_quota = max(1, int(top_k * 0.6))
        rest_quota = max(0, top_k - core_quota)
        core = [hit for hit in ordered if hit.neuron.tier == "core"][:core_quota]
        others = [hit for hit in ordered if hit.neuron.tier != "core"][:rest_quota]
        merged = core + others
        merged.sort(key=lambda hit: hit.score, reverse=True)
        return merged[:top_k]

    # ── 时间检索通道（timeline_query 工具面）────────────────────────────
    def timeline_query(
        self,
        *,
        since_ms: int | None = None,
        until_ms: int | None = None,
        agent_id: str | None = None,
        limit: int = 20,
    ) -> list[Neuron]:
        records = self._relational.query("neuron", limit=max(limit * 4, limit))
        neurons = [Neuron.from_record(record) for record in records]
        if agent_id:
            neurons = [neuron for neuron in neurons if neuron.agent_id == agent_id]
        if since_ms is not None:
            neurons = [neuron for neuron in neurons if neuron.valid_from_ms >= since_ms]
        if until_ms is not None:
            neurons = [neuron for neuron in neurons if neuron.valid_from_ms <= until_ms]
        neurons.sort(key=lambda neuron: neuron.valid_from_ms, reverse=True)
        return neurons[:limit]

    # ── 收敛注入 ──────────────────────────────────────────────────────────
    def inject(self, hits: list[RecallHit], *, agent_id: str) -> str:
        """把召回结果压成可注入上下文的文本（语气受好感度与冷静期影响）。"""
        if not hits:
            return ""
        tone = self._amygdala.inject_tone(agent_id)
        lines = [f"【记忆注入】{tone}"]
        for index, hit in enumerate(hits, start=1):
            neuron = hit.neuron
            mood = f"P{neuron.pad.p:+.1f}/A{neuron.pad.a:+.1f}/D{neuron.pad.d:+.1f}"
            lines.append(
                f"{index}. [{neuron.memory_type}/{neuron.tier} {mood}] {neuron.content}"
            )
        return "\n".join(lines)
