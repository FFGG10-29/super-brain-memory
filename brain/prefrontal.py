"""brain.prefrontal —— 前额叶：只管读取。

六路召回（架构 §七.3/§七.4）：
    dense / sparse / fts / graph / timeline / profile
    各路超采样 k×rrt_oversample → 预过滤 is_active → RRF 融合
    → 精排（reranker，缺模型时恒等重排）→ 可塑性加权 → 分层配额 → 收敛注入

总分：score = w_rrf·RRF + w_freshness·新鲜度 + w_synapse·突触强度
              + w_pad·PAD 显著性 + w_confidence·置信度        （权重全在 config）
再乘杏仁核召回权重（好感度微调 + 冷静期折扣）。
归档（tier=archive）不参与常规召回 —— 阶段 3 验收项。

E2 元认知（v4.2）：
    - RecallHit.confidence：动态置信度 = 通道多样性 × 新鲜度 × 冲突信号 加权合成；
      权重经 Prefrontal.__init__ 可选参数注入（config 块主线程后补）。
    - last_knowledge_gap：空结果 / 全部低置信时为 True，供呈现层透传。
E4 注入视角加权（v4.2）：
    - pad_tracker 存在且 weight_multiplier()>0 时，按当前 PAD 的 P 维符号对命中
      做稳定微调排序（只调顺序，不改变注入内容本身）；开关 perspective_weight=0 关闭。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass
from typing import Any

from brain.hyperedge_backfill import graph_density
from config import BrainConfig, get_config
from core.cache_manager import get_cache_manager
from core.errors import EngineUnavailable
from core.interfaces import (
    Embedder,
    Embedding,
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
# E2：主库 payload 中可能携带 supersede 痕迹的字段（memory_editor / 冲突消解落库痕迹）
_SUPERSEDE_FIELDS = ("superseded_by", "supersede_ids", "conflict_with")


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
    # E2 动态置信度（0~1）；-1 表示未计算（向后兼容：旧构造处无需改动）
    confidence: float = -1.0


class Prefrontal:
    # E2：置信度低于该值视为「知识缺口」信号（类常量，便于测试与呈现层对齐）
    LOW_CONFIDENCE_THRESHOLD = 0.35

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
        confidence_w_channel: float = 0.3,
        confidence_w_fresh: float = 0.3,
        confidence_w_conflict: float = 0.4,
        perspective_weight: float = 0.1,
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
        # E2：confidence 三权重与 E4 视角强度（config 块主线程后补，先以可选参数落位）
        self._confidence_w_channel = float(confidence_w_channel)
        self._confidence_w_fresh = float(confidence_w_fresh)
        self._confidence_w_conflict = float(confidence_w_conflict)
        self._perspective_weight = float(perspective_weight)
        # 最近一次召回的通道埋点（只读快照，供 UI 解释「为什么命中」）
        self.last_audit: dict[str, Any] = {}
        # E2：最近一次召回的知识缺口标记（呈现层直接 getattr 透传）
        self.last_knowledge_gap: dict[str, Any] = {"gap": False, "query": "", "note": ""}
        # W5：事件骨架（整体注入，成员不单独占名次）与降级标记
        self.last_narratives: list[dict[str, Any]] = []
        self.last_degraded = False
        # W5：当前会话 PAD 追踪器，由 headless_server 注入（聊天与 IDE 查询共用同一召回链路）
        self.pad_tracker: Any | None = None

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

        # C11 召回结果短期缓存：命中则跳过整条链路（embedder/向量库/图库/精排）
        cached = self._recall_cache_get(query, namespaces, top_k)
        if cached is not None:
            self._set_knowledge_gap(query, cached)
            return cached

        # ★ B 包：把 dense 通道的 cosine 相似度带回总分（旧版只以通道名次进 RRF）
        dense_scores: dict[str, float] = {}
        channels = self._collect_channels(query, oversample, namespaces, dense_scores)
        self.last_degraded = False

        # W5 / P4.1：激活扩散。稀疏图自动降 1 跳；超时回退无扩散的 RRF 结果
        spread_ids, degraded, note = self._spreading_channel(channels)
        if spread_ids:
            channels["spreading"] = spread_ids
            self.last_degraded = degraded or bool(note)
        # W5 / P4.2：hyperedge 作为叙事单元整体注入，内部成员从各通道移除
        self.last_narratives = self._absorb_hyperedges(channels)

        rrf = self._rrf(channels)
        if not rrf:
            self._set_knowledge_gap(query, [])
            return []

        channel_total = max(1, len(channels))
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
            candidates.append(RecallHit(
                neuron=neuron,
                score=score,
                channels=channels_of,
                confidence=self._confidence(neuron, channels_of, channel_total, now),
            ))

        candidates.sort(key=lambda hit: hit.score, reverse=True)
        reranked = self._rerank(query, candidates[:oversample], top_k)
        result = self._apply_perspective(self._apply_tier_quota(reranked, top_k))
        self._audit(query, channels, result, agent_id, started_ms=now)
        self.last_audit["degraded"] = self.last_degraded
        self.last_audit["narratives"] = len(self.last_narratives)
        self.last_audit["spreading_note"] = note
        self._set_knowledge_gap(query, result)
        self._recall_cache_put(query, namespaces, top_k, result)
        return result

    # ── C11 召回结果短期缓存 ─────────────────────────────────────────────
    # 纪律：mgr 为 None / 任一读写异常 / 配置关闭 → 一律回退无缓存原路径；
    # 值只存 neuron id + score + channels（id 必在值内，供 C0 总线按值精确失效）。
    @staticmethod
    def _recall_cache_key(query: str, namespaces: tuple[str, ...], top_k: int) -> str:
        query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()
        ns_part = ",".join(sorted(namespaces))
        return f"q={query_hash}|ns={ns_part}|k={top_k}"

    def _recall_cache_get(
        self, query: str, namespaces: tuple[str, ...], top_k: int,
    ) -> list[RecallHit] | None:
        mgr = get_cache_manager()
        if mgr is None:
            return None
        try:
            payload = mgr.get("recall", self._recall_cache_key(query, namespaces, top_k))
        except Exception:  # noqa: BLE001  缓存读失败不阻断召回
            return None
        if payload is None:
            return None
        hits: list[RecallHit] = []
        try:
            for item in payload["items"]:
                record = self._relational.get("neuron", str(item["id"]))
                if record is None:
                    continue
                neuron = Neuron.from_record(record)
                # 缓存命中后重过 is_active / tier / namespace 门：缓存值不含这些状态
                if not neuron.is_active or neuron.tier == _ARCHIVED:
                    continue
                if namespaces and neuron.namespace not in namespaces:
                    continue
                hits.append(RecallHit(
                    neuron=neuron,
                    score=float(item["score"]),
                    channels=tuple(str(channel) for channel in item["channels"]),
                    confidence=float(item.get("confidence", -1.0)),
                ))
        except (KeyError, TypeError, ValueError):
            return None
        self.last_audit = {"cached": True, "query": query, "hits": len(hits)}
        return hits

    def _recall_cache_put(
        self, query: str, namespaces: tuple[str, ...], top_k: int,
        hits: list[RecallHit],
    ) -> None:
        mgr = get_cache_manager()
        if mgr is None or not hits:
            # 空结果不入缓存：空值内无 memory id，新写入无法使其精确失效
            return
        payload = {
            "query": query,
            "items": [
                {"id": hit.neuron.uuid7_id, "score": float(hit.score),
                 "channels": list(hit.channels), "confidence": float(hit.confidence)}
                for hit in hits
            ],
        }
        try:
            mgr.set("recall", self._recall_cache_key(query, namespaces, top_k), payload)
        except Exception:  # noqa: BLE001  缓存写失败绝不影响召回
            pass

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
                    # E3 精确归因：命中的记忆 id（JSON 数组，最多 20 个）
                    "neuron_ids": json.dumps([hit.neuron.uuid7_id for hit in hits][:20]),
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
            # C12 embedding 缓存（lmdb，TTL 30 天已配）：命中则零模型调用
            embedding = self._embedding_cache_get(query)
            if embedding is None:
                try:
                    embedding = self._embedder.embed_query(query)
                except EngineUnavailable:
                    embedding = None
                else:
                    self._embedding_cache_put(query, embedding)

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

    # ── C12 embedding 缓存 ───────────────────────────────────────────────
    # BGE-M3 的 sparse 本就是 dict[str, float]（token_id → lexical 权重，
    # 见 engine/embedding.py _sparse_vectors），JSON 往返无损，无需 SparseVector
    # 重建层：直接存 {"dense": [...], "sparse": {...}}，命中时重建 Embedding。
    def _embedding_cache_key(self, query: str) -> str:
        embedder = self._embedder
        try:
            dim = int(embedder.dense_dim)
        except (AttributeError, TypeError, ValueError):
            dim = -1
        parts = f"{type(embedder).__name__}|{dim}|{query}"
        return hashlib.sha256(parts.encode("utf-8")).hexdigest()

    def _embedding_cache_get(self, query: str) -> Embedding | None:
        mgr = get_cache_manager()
        if mgr is None:
            return None
        try:
            payload = mgr.get("embedding", self._embedding_cache_key(query))
        except Exception:  # noqa: BLE001  缓存读失败不阻断召回
            return None
        if payload is None:
            return None
        try:
            dense = [float(value) for value in payload["dense"]]
            sparse = {
                str(token): float(weight)
                for token, weight in dict(payload["sparse"] or {}).items()
            }
        except (KeyError, TypeError, ValueError):
            return None
        return Embedding(dense=dense, sparse=sparse)

    def _embedding_cache_put(self, query: str, embedding: Embedding) -> None:
        mgr = get_cache_manager()
        if mgr is None:
            return
        payload = {
            "dense": [float(value) for value in embedding.dense],
            "sparse": {
                str(token): float(weight)
                for token, weight in dict(embedding.sparse or {}).items()
            },
        }
        try:
            mgr.set("embedding", self._embedding_cache_key(query), payload)
        except Exception:  # noqa: BLE001  缓存写失败绝不影响召回
            pass

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

    # ── W5 / P4.1：激活扩散 ──────────────────────────────────────────────
    def _spreading_channel(
        self, channels: dict[str, list[str]],
    ) -> tuple[list[str], bool, str]:
        """dense/fts 取 top-5 种子，沿 W4 补出的 MEMBER 边有限跳扩散。

        Q39：扩散候选按激活值降序后**以名次参与 RRF**，不直接拿激活值加权，
        与其余通道的融合逻辑一致。稀疏图降 1 跳，超时回退无扩散结果。
        """
        cfg = self._config.spreading_activation
        if not cfg.enabled:
            return [], False, ""
        started = time.monotonic()
        try:
            density = graph_density(self._graph, self._config)
        except Exception:  # noqa: BLE001  图库异常不阻断召回
            return [], True, "图密度评估失败，跳过扩散"
        hops = min(int(cfg.max_hops), 2)
        note = ""
        if density.sparse:
            hops = 1
            note = "图密度不足，降级 1 跳"
        seeds = list(dict.fromkeys(
            (channels.get("dense") or []) + (channels.get("fts") or [])
        ))[:int(cfg.seed_top_k)]
        if not seeds:
            return [], False, note
        decay = float(cfg.decay.get("fact", 0.6))
        activation: dict[str, float] = {}
        frontier = {seed: 1.0 for seed in seeds}
        seen = set(seeds)
        for _hop in range(1, hops + 1):
            # >= 而非 >：timeout_ms=0 也要能确定性触发（Windows 单调时钟可能两次读数相同）
            if (time.monotonic() - started) * 1000 >= float(cfg.timeout_ms):
                note = (note + "；扩散超时回退").strip("；")
                ordered = sorted(activation, key=lambda key: -activation[key])
                return ordered[:int(cfg.max_candidates)], True, note
            following: dict[str, float] = {}
            for node, value in frontier.items():
                try:
                    hyperedge_ids = self._graph.hyperedges_of(node, limit=20)
                except Exception:  # noqa: BLE001
                    continue
                for hyperedge_id in hyperedge_ids:
                    try:
                        members = self._graph.members(hyperedge_id)
                    except Exception:  # noqa: BLE001
                        continue
                    for rank, member in enumerate(members):
                        if not member or member in seen:
                            continue
                        score = value * decay / (1.0 + 0.1 * rank)
                        if score < float(cfg.threshold):
                            continue
                        following[member] = max(following.get(member, 0.0), score)
                        seen.add(member)
            activation.update(following)
            frontier = following
        ordered = sorted(activation, key=lambda key: -activation[key])
        return ordered[:int(cfg.max_candidates)], False, note

    # ── W5 / P4.2：hyperedge 作为叙事单元 ────────────────────────────────
    def _absorb_hyperedges(self, channels: dict[str, list[str]]) -> list[dict[str, Any]]:
        """命中 ≥2 个成员时整段事件骨架注入，**内部成员不再单独占名次**（Q44）。

        骨架经 `last_narratives` 交给 MCPToolkit 拼进 injection，不改 recall() 的
        `list[RecallHit]` 返回契约（66 工具与 Flutter 端都无需改）。
        """
        hit_ids: set[str] = set()
        for ids in channels.values():
            hit_ids.update(ids)
        if not hit_ids:
            return []
        try:
            rows = self._relational.query("hyperedge", limit=200, since_ms=0)
        except Exception:  # noqa: BLE001  旧库没有 hyperedge 记录时静默跳过
            return []
        from storage.hypergraph_db import GraphHypergraph

        view = GraphHypergraph(self._graph, relational=self._relational)
        narratives: list[dict[str, Any]] = []
        absorbed: set[str] = set()
        for row in rows:
            members = [str(item) for item in (row.get("members") or [])]
            shared = [item for item in members if item in hit_ids]
            if len(shared) < 2:
                continue
            hyperedge_id = str(row.get("id") or "")
            try:
                text = view.get_event_skeleton(hyperedge_id)
            except Exception:  # noqa: BLE001
                continue
            narratives.append({"id": hyperedge_id, "text": text, "members": shared})
            absorbed.update(shared)
        if absorbed:
            for name in list(channels):
                channels[name] = [item for item in channels[name] if item not in absorbed]
            for name in [key for key, value in channels.items() if not value]:
                del channels[name]
        return narratives

    # ── W5 / P4.3 + R4：当前 PAD 分段门控 ────────────────────────────────
    def _pad_bonus(self, neuron: Neuron) -> float:
        tracker = self.pad_tracker
        gating = self._config.pad_gating
        if tracker is None or not gating.enabled:
            return 0.0
        weight = tracker.weight_multiplier()
        if weight == 0.0:
            # 高负唤醒：不是把 w6 调小，而是直接压制负面记忆的召回配额
            if getattr(tracker, "suppress_negative", False) and (
                neuron.memory_type == "emotion" or neuron.pad.p < -0.2
            ):
                return -float(gating.negative_penalty)
            return 0.0
        state = tracker.current()
        if state is None:
            return 0.0
        return float(gating.w6) * state.similarity(neuron.pad.as_tuple()) * weight

    # ── E2：动态置信度 / 知识缺口 ─────────────────────────────────────────
    def _confidence(
        self,
        neuron: Neuron,
        channels_of: tuple[str, ...],
        channel_total: int,
        now_ms: int,
    ) -> float:
        """confidence = w_channel·通道多样性 + w_fresh·时间新鲜度 + w_conflict·冲突信号。

        - 通道多样性：命中通道数 / 本次总通道数（多通道互相印证 → 更可信）；
        - 时间新鲜度：复用可塑性的 retrievability（FSRS 幂律衰减，0~1）；
        - 冲突信号：存在未消解的 supersede 痕迹 → 该项记 0（减分）。
        权重经 __init__ 注入（默认 0.3/0.3/0.4，config 块主线程后补）。
        """
        diversity = len(channels_of) / max(1, channel_total)
        freshness = max(0.0, min(1.0, self._plasticity.retrievability(neuron, now_ms=now_ms)))
        conflict_free = 0.0 if self._supersede_trace(neuron.uuid7_id) else 1.0
        value = (
            self._confidence_w_channel * diversity
            + self._confidence_w_fresh * freshness
            + self._confidence_w_conflict * conflict_free
        )
        return max(0.0, min(1.0, value))

    def _supersede_trace(self, neuron_id: str) -> bool:
        """该 neuron 是否存在**未消解**的 supersede 痕迹（best-effort，异常一律无痕迹）。

        查法（按可靠性顺序）：
        1. 图谱 SUPERSEDES 演进边：接口未提供读取方法，仅当具体实现暴露
           duck-typed 的 `supersede_edges_of(id)`（返回对端 id 列表）时启用；
           对端记忆仍在召回池（is_active）才算「未消解」——已被封存即已裁决。
        2. 主库 payload 的 supersede 相关字段（memory_editor 纠正链落库痕迹）。
        """
        try:
            record = self._relational.get("neuron", neuron_id)
        except Exception:  # noqa: BLE001
            return False
        if not record:
            return False
        for field in _SUPERSEDE_FIELDS:
            if record.get(field):
                return True
        getter = getattr(self._graph, "supersede_edges_of", None)
        if not callable(getter):
            return False
        try:
            partners = [str(item) for item in (getter(neuron_id) or [])]
        except Exception:  # noqa: BLE001
            return False
        for partner in partners:
            if not partner or partner == neuron_id:
                continue
            try:
                other = self._relational.get("neuron", partner)
            except Exception:  # noqa: BLE001
                continue
            if other is not None and other.get("is_active", True):
                return True
        return False

    def _set_knowledge_gap(self, query: str, hits: list[RecallHit]) -> None:
        """gap=True 当且仅当结果为空，或全部命中 confidence < LOW_CONFIDENCE_THRESHOLD。"""
        if not hits:
            self.last_knowledge_gap = {"gap": True, "query": query, "note": "召回结果为空"}
            return
        low = all(hit.confidence >= 0.0 and hit.confidence < self.LOW_CONFIDENCE_THRESHOLD
                  for hit in hits)
        note = ""
        if low:
            note = f"全部命中置信度低于 {self.LOW_CONFIDENCE_THRESHOLD:.2f}"
        self.last_knowledge_gap = {"gap": low, "query": query, "note": note}

    # ── E4：注入视角加权（只调顺序，不改内容）─────────────────────────────
    def _apply_perspective(self, hits: list[RecallHit]) -> list[RecallHit]:
        """当前 PAD 的 P 维符号为正 → 正 valence 记忆前置；为负 → 负 valence 前置。

        稳定微调排序：key=(-符号匹配度, -score)，平局保持原序（Python sort 稳定）；
        perspective_weight=0 / 无 pad_tracker / 冷启动或高负唤醒（weight=0）时关闭。
        """
        tracker = self.pad_tracker
        if (self._perspective_weight <= 0.0 or tracker is None
                or tracker.weight_multiplier() <= 0.0):
            return hits
        state = tracker.current()
        if state is None:
            return hits
        want_positive = state.p > 0.0

        def match(hit: RecallHit) -> int:
            p = hit.neuron.pad.p
            return 1 if p != 0.0 and (p > 0.0) == want_positive else 0

        return sorted(hits, key=lambda hit: (-match(hit), -hit.score))

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
            + self._pad_bonus(neuron)                   # W5：w6·当前PAD相似度（分段门控）
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
            reranked.append(RecallHit(
                neuron=hit.neuron, score=blended,
                channels=hit.channels, confidence=hit.confidence,
            ))
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
