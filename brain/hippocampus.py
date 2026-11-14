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

from array import array
import time
from dataclasses import dataclass, field
from typing import Any

from config import BrainConfig, get_config
from core.errors import EngineUnavailable, StorageError
from core.ids import HybridLogicalClock, new_uuid7
from core.runtime_paths import data_dir
from core.structured_log import log_event
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
    reconsolidated: bool = False  # E5/Q32：本次写入走了再巩固窗口的原地合并

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
        reconsolidation_window_sec: int = 1800,  # E5/Q32：再巩固窗口，默认 30min（config 块由主线程后补）
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
        self._reconsolidation_window_sec = int(reconsolidation_window_sec)

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
        _from_bundle: bool = False,
    ) -> WriteResult:
        # ★ 一进多出铁律的执行点在【外部输入入口层】（MCP save_memory / stdio /
        #   chat._remember），不是这个最底层——remember 是所有写入（含内部派生：
        #   语义合并 update、蒸馏、叙事、同步 worker）的汇聚点，在此一刀切会误伤
        #   同事项演进，导致合并/蒸馏链路全线失效。入口已强制 extract_multi →
        #   remember_bundle；这里保留 _from_bundle 参数仅为派生写入显式标注语义。
        _ = _from_bundle  # 语义标注，底层不拦截
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
                # ★ E5 / Q32：再巩固窗口——update（同一事件的更新，单目标）且目标记忆
                #   满足「认知性 + 刚被召回」时，原 id 原地合并并显式产生 reconsolidate
                #   事件，不新开版本行；任一条件不满足则回落到既有 supersede 语义。
                if merge.action == "update" and len(merge.supersede_ids) == 1:
                    reconsolidated = self._try_reconsolidate(
                        merge.supersede_ids[0],
                        neuron=neuron,
                        merge=merge,
                        admission=admission,
                        extraction=extraction,
                        emit_event=emit_event,
                        now_ms=now_ms,
                    )
                    if reconsolidated is not None:
                        return reconsolidated

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
                    # W6 / A6：AI 叙事私有区与 pyramid 子类（不给 neurons 表加列）
                    "owner": str(cognition.get("owner") or ""),
                    "pyramid_subtype": str(cognition.get("pyramid_subtype") or ""),
                    "narrative_skeleton": cognition.get("narrative_skeleton"),
                    # E6：Agent 视角标记（"Agent A 认为…"），供按对话对象选择视角
                    "perspective": str(cognition.get("perspective") or ""),
                    # 叙事属性（flush 提纯产物挂 scenario）：旁路索引，不给 neurons 加列。
                    # ★ is_micro_narrative：白天会话日记标记，夜间 neocortex 优先当
                    #   高密度摘要喂给 LLM（省 Token、减幻觉），不做二次缝合。
                    "narrative": str(cognition.get("narrative") or ""),
                    "is_micro_narrative": bool(str(cognition.get("narrative") or "").strip()),
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

    def remember_bundle(
        self,
        bundle: Any,
        *,
        agent_id: str,
        namespace: str = "private",
        device_id: str | None = None,
        now_ms: int | None = None,
        emit_event: bool = True,
    ) -> dict[str, Any]:
        """一进多出：把一条输入拆出的一组记忆原子化落库 + 一张事件超边。

        复用 remember() 的完整写入链（丘脑准入 / PAD 轨迹 / 六引擎投影 / 合并裁决），
        每条 item 自带 memory_type + PAD + entities；同一 bundle 的条目共享一个
        hyperedge（label + members），并显式补一张 Kùzu 事件节点，使 W5 激活扩散/
        叙事骨架召回能把整组当一个事件单元取出（而不是散落的独立 neuron）。

        `bundle` 是 brain.router_extractor.ExtractionBundle（duck-typed，避免循环 import）。
        """
        items = list(getattr(bundle, "items", []) or [])
        complexity = str(getattr(bundle, "complexity", "light") or "light")
        # 第一梯队·事务性：走 overwrite 覆盖式，只留摘要+原文，绝不进认知池/超边/叙事
        if getattr(bundle, "transactional", False):
            raw = str(getattr(bundle, "raw_content", "") or "")
            summary = str(getattr(bundle, "summary", "") or "") or raw[:40]
            try:
                decision = route_write(content=raw, cognition={"memory_type": "code"},
                                       namespace=namespace, config=self._config)
                if decision.get("path") == "overwrite":
                    self.overwrite_remember(raw, agent_id=agent_id, namespace=namespace,
                                            cognition={"memory_type": "code", "summary": summary},
                                            routing=decision)
                else:
                    # 无 overwrite 实体键（如纯摘要）：按 code 单条但打 _from_bundle 标记落库
                    self.remember(summary or raw, agent_id=agent_id, namespace=namespace,
                                  cognition={"memory_type": "code", "layer": "fact",
                                             "source": "system", "summary": summary},
                                  _from_bundle=True, use_llm=False)
                return {"applied": 1, "neuron_ids": [], "complexity": "none",
                        "transactional": True, "handled": True,
                        "reason": "事务性→覆盖式，不进认知池"}
            except Exception as exc:  # noqa: BLE001
                return {"applied": 0, "neuron_ids": [], "transactional": True,
                        "handled": False, "reason": f"事务性写入失败：{type(exc).__name__}: {exc}"}
        # 第一梯队·寒暄/系统指令：不进六引擎，working-context 丢弃即可
        if getattr(bundle, "ephemeral", False):
            return {"applied": 0, "neuron_ids": [], "complexity": "none",
                    "ephemeral": True, "handled": True,
                    "reason": "寒暄/系统指令，不进长期记忆"}
        if not items:
            return {"applied": 0, "neuron_ids": [], "complexity": complexity,
                    "handled": True, "reason": "信息密度不足以入池（已判定无需拆解）"}
        event_label = str(getattr(bundle, "event_label", "") or "")
        event_members = [str(m) for m in (getattr(bundle, "event_members", ()) or [])]
        source = str(getattr(bundle, "source", "llm") or "llm")
        # 分层语义跟随 bundle：聊天情节=episode；蒸馏知识=fact（蒸馏器覆写）。
        # persona 不允许经 bundle 通道写入（L0 只能走显式授权的单条契约）。
        layer = str(getattr(bundle, "layer", "") or "episode")
        if layer not in ("fact", "episode"):
            layer = "episode"
        # ★ 叙事属性：flush 提纯出的第一人称叙事挂在交互 scenario 上（payload 属性），
        #   夜间 neocortex 直接读它做跨天交织，不用再从碎片反推。
        narrative = str(getattr(bundle, "narrative", "") or "").strip()
        base_ns = namespace
        results: list[WriteResult] = []
        ids: list[str] = []
        for item in items:
            cog: dict[str, Any] = {
                "memory_type": item.memory_type,
                "pad": {"p": item.pad.p, "a": item.pad.a, "d": item.pad.d},
                "confidence": item.confidence,
                "source": "bot" if source == "bot" else source,
                "layer": layer,
                "entities": [{"name": e.name, "type": e.type} for e in item.entities],
                # 每条共享同一事件骨架，W5 的 _absorb_hyperedges 按 members 聚合
                "hyperedge": ({"label": event_label, "members": event_members}
                              if event_label and event_members else None),
            }
            if narrative and item.memory_type == "scenario":
                cog["narrative"] = narrative
            # topic 走 base_ns（调用方传的是 topic:<id>），保持不变
            res = self.remember(item.content, agent_id=agent_id, namespace=base_ns,
                                device_id=device_id, now_ms=now_ms,
                                use_llm=False, emit_event=emit_event, cognition=cog,
                                _from_bundle=True)
            results.append(res)
            if res.applied:
                ids.append(res.neuron.uuid7_id)
        # 显式补一张 bundle 级事件超边节点（即便某条被丘脑拒收，事件骨架仍在）
        if event_label and ids:
            try:
                self._hypergraph.add({
                    "id": ids[0], "label": event_label, "members": event_members or ids,
                    "occurred_at_ms": now_ms if now_ms is not None else _now_ms(),
                    "namespace": base_ns, "kind": "bundle_event",
                })
            except Exception:  # noqa: BLE001  超边补建失败不影响已落库的 neuron
                pass
        applied = sum(1 for r in results if r.applied)
        # 全军覆没（丘脑全拒）= 不合规 → 原文留项目档案待重试
        if applied == 0:
            try:
                self._defer_orphan(" | ".join(i.content for i in items), agent_id,
                                   {"topic": base_ns})
            except Exception:  # noqa: BLE001
                pass
        return {"applied": applied, "total": len(items), "neuron_ids": ids,
                "event_label": event_label or None, "complexity": complexity,
                "handled": applied > 0}

    def _defer_orphan(self, content: str, agent_id: str,
                      cognition: dict | None = None) -> None:
        """铁律拒收的原文 → 只落结构化日志（2026-11-12 用户裁决：系统文档存都不准存）。

        曾经的实现写 [待重试 项目档案，与蒸馏触发器构成死循环泵（留档→触发蒸馏→
        再失败→再留档），制造了大量垃圾记忆。现在失败只记日志与审计，绝不落档案；
        聊天链路自身的 WAL（pending_ingest.ndjson）与清洗管线 JSONL 已各自兜底重试。
        """
        import json as _json

        from core.structured_log import log_event

        log_event("ingest_deferred", **{
            "agent_id": agent_id,
            "topic": str((cognition or {}).get("topic") or ""),
            "chars": len(str(content or "")),
            "preview": str(content or "")[:200],
        })
        try:
            data_dir().mkdir(parents=True, exist_ok=True)
            with (data_dir() / "ingest_deferred.ndjson").open("a", encoding="utf-8") as fh:
                fh.write(_json.dumps({
                    "ts": int(time.time() * 1000), "agent_id": agent_id,
                    "topic": str((cognition or {}).get("topic") or ""),
                    "content": str(content or "")[:4000],
                }, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001  日志失败不影响主链路
            pass

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

    # ── E5 / Q32：再巩固窗口内的原地合并 ─────────────────────────────────
    def _try_reconsolidate(
        self,
        target_id: str,
        *,
        neuron: Neuron,
        merge: Any,
        admission: Admission,
        extraction: Extraction,
        emit_event: bool,
        now_ms: int | None,
    ) -> WriteResult | None:
        """再巩固（reconsolidation）：新写入被裁决为「同一事件的更新」（decide()
        返回单目标 update），且目标记忆满足——
            ① 认知性：memory_type ∈ (scenario, emotion)；
            ② 可塑窗口：now - last_recall_ms ≤ reconsolidation_window_sec
               （last_recall_ms=0 表示从未被召回，不在窗口内）；
            ③ 未处于覆盖式路径（本方法只在 remember() 的认知性分支被调用）；
        时，**不新开版本行**：原记录 id 不变，payload 原地更新（打
        reconsolidation / reconsolidated_at_ms 标记），is_active 保持 1；
        并向 event_log 显式写一条 reconsolidate 事件（新旧内容摘要）——
        Q32 裁决：journal 完整性不破，绝不静默改 payload。

        任一条件不满足返回 None，调用方回落既有 supersede 语义（行为不变）。
        """
        window_sec = self._reconsolidation_window_sec
        if window_sec <= 0 or not target_id:
            return None
        try:
            record = self._relational.get("neuron", target_id)
        except StorageError:
            return None
        if not record or not record.get("is_active", False):
            return None
        if str(record.get("memory_type", "")) not in ("scenario", "emotion"):
            return None
        last_recall_ms = int(record.get("last_recall_ms") or 0)
        if last_recall_ms <= 0:
            return None
        now = int(now_ms) if now_ms is not None else _now_ms()
        if now - last_recall_ms > window_sec * 1000:
            return None

        old_content = str(record.get("content", ""))
        new_content = str(neuron.content)
        updated = dict(record)
        updated["content"] = new_content
        updated["reconsolidation"] = True
        updated["reconsolidated_at_ms"] = now
        updated["updated_at_ms"] = now
        # 主库：id 不变原地更新（upsert 同 id），无 deactivate、无新版本行
        self._relational.upsert("neuron", updated)
        target_neuron = Neuron.from_record(updated)

        # 派生库：outbox 重新投影同一 id（向量 / 图谱 / 时序 / 超图）
        warnings: list[str] = []
        for target in _TARGETS:
            self._relational.outbox_enqueue(
                event_id=target_id,
                target=target,
                payload={"neuron": updated, "extraction": _extraction_record(extraction)},
            )
        self.dispatch_outbox(warnings=warnings)

        result = WriteResult(
            neuron=target_neuron,
            admission=admission,
            applied=True,
            superseded=[],
            warnings=warnings,
            merge=merge,
            reconsolidated=True,
        )
        # 事件：payload 兼容 sync/projections._on_ingest 的 {"neuron": ...} 形状，
        # 远端补 _on_reconsolidate 别名后即可无损重放；extra 摘要供审计。
        if emit_event and self._event_log is not None:
            self._event_log.append(
                {
                    "event_id": new_uuid7(),
                    "agent_id": target_neuron.agent_id,
                    "type": "reconsolidate",
                    "hlc": self._clock.last.to_token(),
                    "payload": {
                        "neuron": updated,
                        "extraction": _extraction_record(extraction),
                        "old_content": old_content[:200],
                        "new_content": new_content[:200],
                        "target_id": target_id,
                    },
                }
            )
        return result

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
        # ★ 实体消歧（2026-11-12 架构裁决）："阿鱼/用户/宝儿"不许各自为战建四个节点。
        #   写图前先把别名对齐到唯一 canonical 节点：LMDB 别名精确匹配 → 向量相似
        #   （≥0.92 视为同一实体，合并）；保证同一话题只长一个核心节点，激活扩散不断链。
        entities = [
            {**e, "name": self._resolve_entity(str(e.get("name", "")))}
            for e in entities if str(e.get("name", "")).strip()
        ]
        relations = [
            {**r, "subject": self._resolve_entity(str(r.get("subject", ""))),
             "object": self._resolve_entity(str(r.get("object", "")))}
            for r in relations if r.get("subject") and r.get("object")
        ]
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

    # ── 实体消歧（Entity Resolution）─────────────────────────────────────
    # LMDB 桶：entity_alias=别名→canonical；entity_names=canonical 成员键；
    #          entity_vec=canonical→float32 向量（bge 嵌入，注册时算一次）。
    _ENTITY_SIM_THRESHOLD = 0.92
    _ENTITY_INDEX_CAP = 1000

    def _resolve_entity(self, name: str) -> str:
        """把实体名对齐到唯一 canonical 节点：别名精确 → 本名已注册 → 向量 ≥0.92 合并。

        向量通道依赖 embedder（现为云端 bge）：只在新实体名首见时调一次，
        命中别名/已注册不产生任何模型调用；embedder 不可用时退化为纯精确匹配。
        """
        name = str(name or "").strip()
        if not name:
            return name
        try:
            hit = self._kv.get("entity_alias", name)
            if hit:
                return bytes(hit).decode("utf-8", "replace")
            if self._kv.get("entity_names", name):
                return name
        except Exception:  # noqa: BLE001  消歧失败绝不阻断写入，按原名落库
            return name
        # 新实体名：向量比对既有 canonical，≥0.92 判同一实体（别名归并）
        canonical = name
        try:
            qvec = self._entity_vector(name)
            if qvec is not None:
                index = self._kv.get_json("entity_index", "names") or []
                best, best_sim = "", 0.0
                for known in index:
                    raw = self._kv.get("entity_vec", str(known))
                    if not raw:
                        continue
                    sim = self._cosine(qvec, raw)
                    if sim > best_sim:
                        best, best_sim = str(known), sim
                if best and best_sim >= self._ENTITY_SIM_THRESHOLD:
                    canonical = best
        except Exception:  # noqa: BLE001  向量通道失败退精确匹配
            canonical = name
        try:
            self._kv.put("entity_alias", name, canonical.encode("utf-8"))
            if not self._kv.get("entity_names", canonical):
                self._kv.put("entity_names", canonical, b"1")
                index = self._kv.get_json("entity_index", "names") or []
                index.append(canonical)
                self._kv.put_json("entity_index", "names", index[-self._ENTITY_INDEX_CAP:])
                vec = self._entity_vector(canonical)
                if vec is not None:
                    self._kv.put("entity_vec", canonical,
                                 array("f", vec).tobytes())
        except Exception:  # noqa: BLE001  注册失败不影响本次写入
            pass
        return canonical

    def _entity_vector(self, name: str) -> list[float] | None:
        """实体名向量（云端 bge，一次 HTTP）；embedder 不可用返回 None 跳过比对。"""
        if self._embedder is None:
            return None
        try:
            emb = self._embedder.embed_query(name)
            dense = [float(x) for x in (emb.dense or [])]
            return dense or None
        except Exception:  # noqa: BLE001  云端失败不阻断写入
            return None

    @staticmethod
    def _cosine(query: list[float], raw: bytes) -> float:
        from array import array

        other = array("f")
        other.frombytes(raw)
        if len(other) != len(query):
            return 0.0
        dot = sum(a * b for a, b in zip(query, other, strict=True))
        nq = sum(a * a for a in query) ** 0.5
        no = sum(b * b for b in other) ** 0.5
        if nq <= 0 or no <= 0:
            return 0.0
        return dot / (nq * no)

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
