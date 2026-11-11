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
_INJECT_TOP_N = 3
_INJECT_SUMMARY_CHARS = 120


def _summarize(text: str) -> str:
    """注入文本只给摘要：长记忆截断，避免 Agent 拿到原文后自由发散。"""
    value = " ".join((text or "").split())
    if len(value) <= _INJECT_SUMMARY_CHARS:
        return value
    return value[:_INJECT_SUMMARY_CHARS].rstrip() + "…（摘要）"


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
        # 最近一次召回的通道埋点（只读快照，供 UI 解释「为什么命中」）
        self.last_audit: dict[str, Any] = {}

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

        # ★ B 包：把 dense 通道的 cosine 相似度带回总分（旧版只以通道名次进 RRF）
        dense_scores: dict[str, float] = {}
        channels = self._collect_channels(query, oversample, namespaces, dense_scores)
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
            if namespaces and neuron.namespace not in namespaces:
                continue
            score = self._score(neuron, rrf_score, now,
                                dense_score=dense_scores.get(neuron_id, 0.0))
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
        self.last_audit = {
            "counts": counts,
            "injected": injected,
            "channels": list(channels),
            "hits": len(hits),
            "query": query,
        }
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
        self, query: str, oversample: int, namespaces: tuple[str, ...],
        dense_scores: dict[str, float] | None = None,
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
            dense_hits = self._vector.search_dense(embedding.dense, limit=oversample, where=recall_filter)
            channels["dense"] = [hit.id for hit in dense_hits]
            if dense_scores is not None:
                for hit in dense_hits:
                    dense_scores[hit.id] = max(0.0, min(1.0, float(hit.score)))
            if embedding.sparse:
                channels["sparse"] = [
                    hit.id
                    for hit in self._vector.search_sparse(embedding.sparse, limit=oversample, where=recall_filter)
                ]

        channels["fts"] = [
            str(record["id"])
            for record in self._relational.full_text_search(
                "neuron", query, limit=oversample, namespaces=namespaces
            )
        ]

        graph_ids = self._graph_channel(query, oversample, namespaces)
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

    def _graph_channel(
        self, query: str, oversample: int, namespaces: tuple[str, ...] = ()
    ) -> list[str]:
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
                str(record["id"])
                for record in self._relational.full_text_search(
                    "neuron", name, limit=oversample, namespaces=namespaces
                )
            )
        return list(dict.fromkeys(collected))

    def _rrf(self, channels: dict[str, list[str]]) -> dict[str, float]:
        constant = self._config.recall.rrf_constant
        scores: dict[str, float] = {}
        for hits in channels.values():
            for rank, neuron_id in enumerate(hits):
                scores[neuron_id] = scores.get(neuron_id, 0.0) + 1.0 / (constant + rank + 1)
        return scores

    def _score(self, neuron: Neuron, rrf_score: float, now_ms: int,
               dense_score: float = 0.0) -> float:
        recall = self._config.recall
        anchor = neuron.last_recall_ms or neuron.valid_from_ms or neuron.created_at_ms
        age_days = max(0.0, (now_ms - anchor) / _MS_PER_DAY)
        freshness = math.exp(-age_days / 30.0)
        retrievability = self._plasticity.retrievability(neuron, now_ms=now_ms)
        return (
            recall.w_rrf * rrf_score * recall.rrf_constant  # 归一到 ~O(1)
            + recall.w_dense * dense_score                   # ★ B 包：真实 cosine 相似度
            + recall.w_freshness * freshness
            + recall.w_synapse * retrievability
            + recall.w_pad * neuron.pad.salience()
            + recall.w_confidence * neuron.confidence
        )

    def _rerank(self, query: str, candidates: list[RecallHit], top_k: int) -> list[RecallHit]:
        if not candidates:
            return []
        recall = self._config.recall
        # ★ B 包：交叉编码器候选硬上限（旧版把 k×4 甚至全部候选灌进去，是 deep 22.7s 的大头）
        candidates = candidates[: max(int(recall.rerank_top_n), top_k)]
        fallback = candidates[: max(top_k * recall.rrf_k_oversample, top_k)]
        if not recall.rerank_enabled:
            # 低配设备/追求低延迟：跳过交叉编码精排，保留 RRF 顺序
            return fallback
        # 硬超时：超时直接回退 RRF 顺序，绝不让 deep 卡死 Agent 一轮对话
        from concurrent.futures import ThreadPoolExecutor
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(
            self._reranker.rerank, query,
            [(hit.neuron.uuid7_id, hit.neuron.content) for hit in candidates],
            top_k=len(candidates),
        )
        try:
            order = future.result(timeout=max(0.2, float(recall.rerank_timeout_ms) / 1000.0))
        except Exception:  # noqa: BLE001 - 超时 / 模型异常一律回退，不阻断召回
            pool.shutdown(wait=False)
            return fallback
        pool.shutdown(wait=False)
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
        """收敛注入：只给 Top 3 摘要 + 一条抑制发散的执行约束。

        前额叶的职责不是把记忆原文全倒给 Agent，而是给出最小充分上下文并
        管住输出：否则 Agent 会复述原文、自由联想、越答越远（发散）。
        `recall_memory` 的 hits 仍返回全文，供检查器/UI 查看。
        """
        if not hits:
            return ""
        tone = self._amygdala.inject_tone(agent_id)
        lines = [
            f"【记忆注入】{tone}",
            "【执行约束】以下仅 3 条已核验记忆，据此收敛作答：停止发散、不展开无关联想、"
            "不复述记忆原文；未命中的细节按未知处理，禁止编造。",
        ]
        for index, hit in enumerate(hits[:_INJECT_TOP_N], start=1):
            neuron = hit.neuron
            mood = f"P{neuron.pad.p:+.1f}/A{neuron.pad.a:+.1f}/D{neuron.pad.d:+.1f}"
            lines.append(
                f"{index}. [{neuron.memory_type}/{neuron.tier} {mood}] {_summarize(neuron.content)}"
            )
        return "\n".join(lines)
