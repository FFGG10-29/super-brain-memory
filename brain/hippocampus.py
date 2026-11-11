"""brain.hippocampus —— 海马体：只管写入。

流水线（架构 §七 + 阶段 1 验收）：
    RouterExtractor 抽取（含 PAD）→ 杏仁核精算/好感度积分 → 丘脑准入
    → SQLite 主库落库（唯一事实源）→ outbox 六库幂等分发 → 冲突消解

投影可重建原则：向量/图谱/时序/超图任一投影失败都不回滚事实（SQLite 已落库），
失败项留在 outbox 由后续重试；embedder 缺失时向量投影标记为 skipped（可从主库重建）。
冲突消解：同 (namespace, subject, predicate) 但 object 不同 → 置信度高者胜，
旧记录 valid_to 封存 + 图谱 supersedes 演进边，永不物理删除。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from config import BrainConfig, get_config
from core.errors import EngineUnavailable, StorageError
from core.ids import HybridLogicalClock
from core.interfaces import (
    Embedder,
    EventLog,
    GraphRepository,
    HypergraphRepository,
    KVRepository,
    RelationalRepository,
    TimeSeriesRepository,
    VectorRecord,
    VectorRepository,
)
from models.neuron import Neuron
from models.synapse import Synapse
from brain.amygdala import Amygdala
from brain.router_extractor import ExtractedRelation, Extraction, RouterExtractor
from brain.synaptic_plasticity import SynapticPlasticity
from brain.thalamus import Admission, Thalamus
from brain.write_router import route_write

__all__ = ["Hippocampus", "WriteResult"]

_TRIPLE_BUCKET = "triples"
_TARGETS = ("vector", "graph", "timeseries", "hypergraph")


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class WriteResult:
    neuron: Neuron
    admission: Admission
    applied: bool
    superseded: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    merge: Any | None = None  # MergeDecision：本次写入的语义裁决（add/update/merge/skip）

    @property
    def event_worthy(self) -> bool:
        """是否应产生跨设备事件（缓冲层不同步）。"""
        return self.applied and self.admission.creates_sync_event


class Hippocampus:
    def __init__(
        self,
        *,
        relational: RelationalRepository,
        vector: VectorRepository,
        graph: GraphRepository,
        timeseries: TimeSeriesRepository,
        hypergraph: HypergraphRepository,
        kv: KVRepository,
        clock: HybridLogicalClock,
        extractor: RouterExtractor,
        thalamus: Thalamus,
        amygdala: Amygdala,
        plasticity: SynapticPlasticity,
        embedder: Embedder | None = None,
        event_log: EventLog | None = None,
        config: BrainConfig | None = None,
        consolidator: Any | None = None,
    ) -> None:
        self._relational = relational
        self._vector = vector
        self._graph = graph
        self._timeseries = timeseries
        self._hypergraph = hypergraph
        self._kv = kv
        self._clock = clock
        self._extractor = extractor
        self._thalamus = thalamus
        self._amygdala = amygdala
        self._plasticity = plasticity
        self._embedder = embedder
        self._event_log = event_log
        self._config = config or get_config()
        self._consolidator = consolidator

    # ── 主流程 ────────────────────────────────────────────────────────────
    def remember(
        self,
        content: str,
        *,
        agent_id: str,
        namespace: str = "private",
        device_id: str | None = None,
        now_ms: int | None = None,
        use_llm: bool = True,
        emit_event: bool = True,
        cognition: dict | None = None,
    ) -> WriteResult:
        # ★ A 包·空间标记兜底：所有写入最终汇聚于此，杜绝裸 private/document。
        #   优先级 cognition.topic > 显式 topic:<id> > 兜底 topic:default。
        #   这样任何新写入途径（MCP / inbox worker / stdio / 蒸馏）都自带空间归属。
        _cog = cognition if isinstance(cognition, dict) else {}
        _topic = str(_cog.get("topic") or "").strip()
        if _topic:
            namespace = _topic if _topic.startswith("topic:") else f"topic:{_topic}"
        elif not str(namespace or "").startswith("topic:"):
            namespace = "topic:default"
        # ★ W2 / P1：事务性内容（code / draft / config / artifact）自动走覆盖式写入。
        #   同步返回 WriteResult 的语义不变（Q1③ 否掉了改 202 的方案），
        #   只是内部转调独立的 overwrite_remember()，不污染既有认知性写入路径。
        decision = route_write(
            content=content, cognition=cognition, namespace=namespace, config=self._config,
        )
        if decision["path"] == "overwrite":
            return self.overwrite_remember(
                content, agent_id=agent_id, namespace=namespace,
                cognition=cognition, routing=decision,
                device_id=device_id, now_ms=now_ms,
            )
        # ★ 认知契约：调用方（WorkBuddy 等）已给出结构化抽取结果时直接采信，
        #   不再调用 Qwen3-0.6B 做分类 / PAD / 实体抽取。
        #   cognition 缺省时行为与旧版完全一致。
        extraction = (
            self._extractor.from_agent(cognition)
            if cognition
            else self._extractor.extract(content, agent_id=agent_id, use_llm=use_llm)
        )
        affect = self._amygdala.ingest(agent_id, extraction.pad, now_ms=now_ms)

        neuron = self._extractor.to_neuron(
            content,
            extraction,
            device_id=device_id or self._config.sync.device_id,
            agent_id=agent_id,
            namespace=namespace,
            transaction_from=self._clock.tick().to_token(),
        )
        # 杏仁核精算：记忆保留事件自身 PAD（不平滑），好感度用低通积分基线
        neuron = neuron.model_copy(
            update={
                "pad": self._amygdala.refine(extraction.pad),
                "affinity": affect.affinity,
                "valid_from_ms": int(now_ms) if now_ms is not None else neuron.valid_from_ms,
            }
        )

        admission = self._thalamus.admit(neuron, now_ms=now_ms)
        if not admission.accepted:
            return WriteResult(neuron=neuron, admission=admission, applied=False)

        # 语义合并裁决（Mem0 式）：LLM 判定 add/update/merge/skip
        merge = None
        # use_llm=False 是批量导入的快速通道：合并裁决也要调 LLM，一起跳过
        # ★ 认知契约下同样裁决（merge_cognition_enabled 控制，默认开）：
        #   抽取结果仍直接采信调用方 cognition，但同事项演进走 update/merge 收敛为一条，
        #   避免同一事项随时间无限追加版本。LLM 不可用时 decide() 兜底为 add，绝不阻断写入。
        if (
            use_llm
            and self._consolidator is not None
            and self._config.merge.merge_enabled
            and (cognition is None or self._config.merge.merge_cognition_enabled)
        ):
            merge = self._consolidator.decide(neuron.content, agent_id=agent_id, namespace=namespace)
            if merge.action == "skip":
                return WriteResult(neuron=neuron, admission=admission, applied=False, merge=merge)
            if merge.action in ("update", "merge") and merge.content:
                neuron = neuron.model_copy(update={"content": merge.content})

        warnings: list[str] = []
        # 1) 事实先落主库
        self._relational.upsert("neuron", neuron.to_record())
        # 2) PAD 轨迹只写不改入时序库（情绪全链路·存储环节）
        self._append_pad_track(neuron, source=extraction.source, now_ms=now_ms)
        # 3) outbox 分发投影（含抽取出的实体/关系/超边）
        for target in _TARGETS:
            self._relational.outbox_enqueue(
                event_id=neuron.uuid7_id,
                target=target,
                payload={"neuron": neuron.to_record(), "extraction": _extraction_record(extraction)},
            )
        self.dispatch_outbox(warnings=warnings)
        # 4) 冲突消解
        superseded = self._resolve_conflicts(neuron, extraction)
        # 5) 语义合并裁出的被替代旧记忆：封存（不物理删除），随结果返回
        if merge is not None and merge.supersede_ids:
            superseded.extend(self._retire(merge.supersede_ids, by=neuron))
        result = WriteResult(
            neuron=neuron,
            admission=admission,
            applied=True,
            superseded=superseded,
            warnings=warnings,
            merge=merge,
        )
        # ★ A 包：分区元数据旁路索引（layer/source/valence/sensitive/category）。
        #   写 LMDB kv 而不给 Neuron 加字段：零 schema 变更、不改六引擎投影链、不碰 Kùzu 单写锁。
        #   topic 本身落 neuron.namespace 列（topic:<空间id>），可按列过滤下推。
        if cognition:
            try:
                import json as _json
                valence = float(cognition.get("valence") or 0.0)
                meta = {
                    "topic": str(cognition.get("topic") or ""),
                    "layer": str(cognition.get("layer") or "fact"),
                    "source": str(cognition.get("source") or ""),
                    "valence": max(-1.0, min(1.0, valence)),
                    "sensitive": bool(cognition.get("sensitive", False)),
                    "category": str(cognition.get("category") or ""),
                    "namespace": neuron.namespace,
                    "ts_ms": neuron.created_at_ms,
                }
                self._kv.put("meta", neuron.uuid7_id, _json.dumps(meta, ensure_ascii=False).encode("utf-8"))
            except (TypeError, ValueError, OSError) as exc:
                warnings.append(f"meta 索引写入失败（不影响主记忆）：{exc}")
        # v3 Worker 消费事件时关闭再入队，避免 journal → inbox 无限回环
        if emit_event:
            self._emit_event(neuron, extraction, result)
        return result

    def overwrite_remember(
        self,
        content: str,
        *,
        agent_id: str,
        namespace: str = "private",
        cognition: dict | None = None,
        routing: dict | None = None,
        device_id: str | None = None,
        now_ms: int | None = None,
        skip_consolidator: bool = True,
    ) -> WriteResult:
        """W2 / A8：事务性内容覆盖式写入（独立方法，绝不经过 `_consolidator.decide()`）。

        单向安全顺序（P1.3）：主库旧记录 `is_active=0` → 派生库物理删除 → 写新记录。
        中途崩溃最多留下「旧已封存、派生残留」，`is_active` 预过滤会挡住残留，
        下次投影重建时清理；反过来先删派生库会留下「主库有旧、派生已删」的不一致。

        A3：这里**不写任何 journal**。v3 事件日志由受理链路（`V3Daemon.handle_event`）
        负责，且事务性内容不投影到 sync 平面（覆盖式语义不该跨设备重放）。
        """
        _ = skip_consolidator  # A8/R19：独立方法天然不经过合并裁决，参数仅显式标记调用意图
        cog = dict(cognition or {})
        decision = routing or route_write(
            content=content, cognition=cog, namespace=namespace, config=self._config,
        )
        entity_id = str(decision.get("entity_id") or "")
        if decision.get("path") != "overwrite" or not entity_id:
            raise ValueError("overwrite_remember 需要有效的 overwrite 路由决策")

        # 构造 neuron 的流程与 remember 一致，但完全跳过语义合并裁决
        extraction = self._extractor.from_agent({**cog, "routing": decision})
        affect = self._amygdala.ingest(agent_id, extraction.pad, now_ms=now_ms)
        neuron = self._extractor.to_neuron(
            content, extraction,
            device_id=device_id or self._config.sync.device_id,
            agent_id=agent_id,
            namespace=namespace,
            transaction_from=self._clock.tick().to_token(),
        )
        neuron = neuron.model_copy(update={
            "pad": self._amygdala.refine(extraction.pad),
            "affinity": affect.affinity,
            "valid_from_ms": int(now_ms) if now_ms is not None else neuron.valid_from_ms,
        })
        admission = self._thalamus.admit(neuron, now_ms=now_ms)
        if not admission.accepted:
            return WriteResult(neuron=neuron, admission=admission, applied=False)

        # ① 旧记录：主库封存（双时态打点）
        old_id: str | None = None
        try:
            raw = self._kv.get("entity", entity_id)
            if raw:
                old_id = bytes(raw).decode("utf-8", "replace")
        except (OSError, TypeError, ValueError):
            old_id = None
        superseded: list[str] = []
        if old_id:
            # deactivate 内部会调 tx_to.to_token()，这里必须传 HLCStamp 对象而非字符串
            self._relational.deactivate(
                "neuron", old_id,
                valid_to_ms=neuron.valid_from_ms,
                tx_to=self._clock.tick(),
            )
            superseded.append(old_id)
            # ② 派生库物理删除（向量 / 旁路索引）；FTS5 由主库 deactivate 联动
            try:
                self._vector.delete([old_id])
            except Exception:  # noqa: BLE001  向量库不可用时由投影重建兜底
                pass
            try:
                self._kv.delete("meta", old_id)
            except Exception:  # noqa: BLE001
                pass

        # ③ 写新记录 + 派生投影
        self._relational.upsert("neuron", neuron.to_record())
        self._append_pad_track(neuron, source=extraction.source, now_ms=now_ms)
        warnings: list[str] = []
        for target in _TARGETS:
            self._relational.outbox_enqueue(
                event_id=neuron.uuid7_id,
                target=target,
                payload={"neuron": neuron.to_record(), "extraction": _extraction_record(extraction)},
            )
        self.dispatch_outbox(warnings=warnings)

        # ④ entity 索引 + seq（A3 定序用）+ 分区元数据
        try:
            import json as _json

            seq = int(self._kv.get("entity_seq", entity_id) or b"0") + 1
            self._kv.put("entity_seq", entity_id, str(seq).encode("utf-8"))
            self._kv.put("entity", entity_id, neuron.uuid7_id.encode("utf-8"))
            meta = {
                "topic": str(cog.get("topic") or ""),
                "layer": str(cog.get("layer") or "fact"),
                "source": str(cog.get("source") or "agent"),
                "category": decision.get("category", ""),
                "entity_id": entity_id,
                "entity_seq": seq,
                "entity_fuzzy": bool(decision.get("entity_fuzzy")),
                "agent_confidence": decision.get("agent_confidence"),
                "routing_fallback": bool(decision.get("routing_fallback")),
                "namespace": neuron.namespace,
                "ts_ms": neuron.created_at_ms,
            }
            self._kv.put("meta", neuron.uuid7_id, _json.dumps(meta, ensure_ascii=False).encode("utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            warnings.append(f"entity 索引写入失败（不影响主记忆）：{exc}")
        return WriteResult(
            neuron=neuron, admission=admission, applied=True,
            superseded=superseded, warnings=warnings, merge=None,
        )

    def _emit_event(self, neuron: Neuron, extraction: Extraction, result: WriteResult) -> None:
        """写入事件日志：只有"跨设备值得同步"的写入才发事件（缓冲层不同步）。

        fsync 在 journal.append 内完成 —— 事件先落盘，回执才返回（架构 §8.1）。
        payload 里带完整 neuron（含 pad），PAD 由此跨设备流动。
        """
        if self._event_log is None or not result.event_worthy:
            return
        self._event_log.append(
            {
                "event_id": neuron.uuid7_id,
                "agent_id": neuron.agent_id,
                "type": "ingest",
                "hlc": self._clock.last.to_token(),
                "payload": {"neuron": neuron.to_record(), "extraction": _extraction_record(extraction)},
            }
        )

    # ── 投影分发 ──────────────────────────────────────────────────────────
    def dispatch_outbox(self, *, limit: int = 200, warnings: list[str] | None = None) -> int:
        """把 outbox 中的待办投影到四个派生库；返回成功条数。"""
        applied = 0
        for item in self._relational.outbox_claim(limit=limit):
            try:
                skipped = self._project(item.target, item.payload)
            except (EngineUnavailable, StorageError, ValueError) as exc:
                self._relational.outbox_fail(item.outbox_id, f"{type(exc).__name__}: {exc}")
                if warnings is not None:
                    warnings.append(f"{item.target} 投影失败已留待重试: {exc}")
                continue
            self._relational.outbox_ack([item.outbox_id])
            applied += 1
            if skipped and warnings is not None:
                warnings.append(f"{item.target} 投影跳过（{skipped}），可从主库重建")
        return applied

    def _project(self, target: str, payload: dict) -> str | None:
        """返回非空字符串表示"跳过原因"（已 ack，不做无限重试）。"""
        neuron = Neuron.from_record(payload["neuron"])
        extraction = payload.get("extraction") or {}
        if target == "vector":
            if self._embedder is None:
                return "未配置 embedder"
            try:
                embedding = self._embedder.embed_query(neuron.content)
            except EngineUnavailable as exc:
                return f"embedder 不可用: {exc}"
            self._vector.upsert(
                [
                    VectorRecord(
                        id=neuron.uuid7_id,
                        dense=embedding.dense,
                        text=neuron.content,
                        sparse=embedding.sparse,
                        metadata={
                            "namespace": neuron.namespace,
                            "tier": neuron.tier,
                            "memory_type": neuron.memory_type,
                            "agent_id": neuron.agent_id,
                            "device_id": neuron.device_id,
                            "is_active": neuron.is_active,
                        },
                    )
                ]
            )
            return None
        if target == "graph":
            self._project_graph(neuron, extraction)
            return None
        if target == "timeseries":
            self._append_decay(neuron)
            return None
        if target == "hypergraph":
            label = extraction.get("hyperedge_label")
            members = extraction.get("hyperedge_members") or []
            if not label or not members:
                return "无超边内容"
            self._hypergraph.add(
                {
                    "id": neuron.uuid7_id,
                    "label": label,
                    "members": members,
                    "occurred_at_ms": neuron.valid_from_ms,
                    "namespace": neuron.namespace,
                }
            )
            return None
        raise StorageError(f"未知投影目标: {target}")

    def _project_graph(self, neuron: Neuron, extraction: dict) -> None:
        entities = extraction.get("entities") or []
        relations = extraction.get("relations") or []
        for entity in entities:
            name = str(entity.get("name", ""))
            if name:
                self._graph.upsert_entity(name, name=name, entity_type=str(entity.get("type", "concept")))
        for relation in relations:
            self._graph.add_relation(
                str(relation["subject"]),
                str(relation["predicate"]),
                str(relation["object"]),
                tx_from=self._clock.last,
            )
        if entities:
            for entity in entities:
                name = str(entity.get("name", ""))
                if name:
                    self._graph.add_member(neuron.uuid7_id, name)
            self._graph.upsert_hyperedge_node(
                neuron.uuid7_id, label=neuron.content[:40], occurred_at_ms=neuron.valid_from_ms
            )

    # ── 时序投影（PAD 轨迹 / 衰减快照）──────────────────────────────────
    def _append_pad_track(self, neuron: Neuron, *, source: str, now_ms: int | None) -> None:
        self._timeseries.append(
            "pad_track",
            [
                {
                    "ts_ms": _now_ms() if now_ms is None else int(now_ms),
                    "neuron_id": neuron.uuid7_id,
                    "device_id": neuron.device_id,
                    "agent_id": neuron.agent_id,
                    "p": neuron.pad.p,
                    "a": neuron.pad.a,
                    "d": neuron.pad.d,
                    "source": source,
                }
            ],
        )

    def _append_decay(self, neuron: Neuron) -> None:
        state = self._plasticity.state(neuron)
        self._timeseries.append(
            "decay_event",
            [
                {
                    "ts_ms": _now_ms(),
                    "neuron_id": neuron.uuid7_id,
                    "strength": state.strength,
                    "stability": state.stability,
                    "retrievability": state.retrievability,
                    "tier": neuron.tier,
                }
            ],
        )

    # ── 冲突消解 ──────────────────────────────────────────────────────────
    def _resolve_conflicts(self, neuron: Neuron, extraction: Extraction) -> list[str]:
        superseded: list[str] = []
        for relation in extraction.relations:
            key = f"{neuron.namespace}:{relation.subject}:{relation.predicate}"
            current = self._kv.get_json(_TRIPLE_BUCKET, key)
            record = {
                "object": relation.object,
                "neuron_id": neuron.uuid7_id,
                "confidence": neuron.confidence,
            }
            if not current:
                self._kv.put_json(_TRIPLE_BUCKET, key, record)
                continue
            if str(current.get("object")) == relation.object:
                self._kv.put_json(_TRIPLE_BUCKET, key, record)
                continue

            old_neuron_id = str(current.get("neuron_id", ""))
            old_confidence = float(current.get("confidence", 0.0))
            if neuron.confidence < old_confidence:
                # 置信度不足：不裁决，留待 Life Loop 复盘（架构 §七.7）
                continue

            self._supersede(old_neuron_id, neuron, relation)
            if old_neuron_id:
                superseded.append(old_neuron_id)
            self._kv.put_json(_TRIPLE_BUCKET, key, record)
        return superseded

    def _supersede(self, old_neuron_id: str, neuron: Neuron, relation: ExtractedRelation) -> None:
        if old_neuron_id:
            try:
                self._relational.deactivate(
                    "neuron",
                    old_neuron_id,
                    valid_to_ms=_now_ms(),
                    tx_to=self._clock.tick(),
                )
            except StorageError:
                pass
            try:
                self._vector.delete([old_neuron_id])
            except Exception:  # noqa: BLE001  向量投影失败不影响事实封存
                pass
            self._graph.upsert_entity(old_neuron_id, name=f"{relation.subject}.{relation.predicate}", entity_type="fact")
        self._graph.upsert_entity(
            neuron.uuid7_id, name=f"{relation.subject}.{relation.predicate}", entity_type="fact"
        )
        if old_neuron_id:
            self._graph.supersede(neuron.uuid7_id, old_neuron_id, tx_from=self._clock.last)
        # 旧突触边也建一条（二元关系镜像），保证图谱可追溯
        self._graph.add_relation(
            relation.subject, relation.predicate, relation.object, tx_from=self._clock.last
        )

    def _retire(self, ids: tuple[str, ...], *, by: Neuron) -> list[str]:
        """语义合并：把被 update/merge 替代的旧记忆封存（valid_to 打点 + 向量摘除）。"""
        retired: list[str] = []
        for old_id in ids:
            try:
                self._relational.deactivate(
                    "neuron", old_id, valid_to_ms=_now_ms(), tx_to=self._clock.tick()
                )
            except StorageError:
                continue
            try:
                self._vector.delete([old_id])
            except Exception:  # noqa: BLE001  向量摘除失败不影响事实封存
                pass
            self._graph.upsert_entity(by.uuid7_id, name=f"merged:{old_id[:12]}", entity_type="fact")
            try:
                self._graph.supersede(by.uuid7_id, old_id, tx_from=self._clock.last)
            except Exception:  # noqa: BLE001
                pass
            retired.append(old_id)
        return retired

    # ── 召回强化（供前额叶回调，产生 reinforce 事件的写入端）─────────────
    def reinforce(self, neuron_id: str, *, now_ms: int | None = None) -> Neuron | None:
        record = self._relational.get("neuron", neuron_id)
        if record is None:
            return None
        neuron = Neuron.from_record(record)
        updated = self._plasticity.reinforce(neuron, now_ms=now_ms)
        self._relational.upsert("neuron", updated.to_record())
        self._append_decay(updated)
        return updated

    def save_synapse(self, synapse: Synapse) -> None:
        self._relational.upsert("synapse", synapse.to_record())


def _extraction_record(extraction: Extraction) -> dict:
    """抽取结果序列化进 outbox payload（JSON 友好）。"""
    return {
        "entities": [{"name": entity.name, "type": entity.type} for entity in extraction.entities],
        "relations": [
            {"subject": relation.subject, "predicate": relation.predicate, "object": relation.object}
            for relation in extraction.relations
        ],
        "hyperedge_label": extraction.hyperedge_label,
        "hyperedge_members": list(extraction.hyperedge_members),
        "source": extraction.source,
    }
