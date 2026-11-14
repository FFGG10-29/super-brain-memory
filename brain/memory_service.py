"""brain.memory_service —— 与 UI 框架无关的桌面级业务服务。

无头后端 / Flutter RPC 通过它访问检查器、用户控制闭环、事件时间轴、
批量导入、真实六通道召回等能力；实现对齐历史后端设计，
不依赖任何 UI 框架模块。
"""
from __future__ import annotations

import csv
import io
import json
import math
import time
from pathlib import Path
from typing import Any, Sequence

from brain.activation import ActivationService
from brain.memory_browser import MemoryBrowser
from brain.memory_editor import MemoryEditor
from core.ids import new_uuid7

__all__ = ["MemoryService", "parse_memory_text", "EVENT_TYPES"]

_CORE_BLOCKS = ("human", "persona", "preferences", "constraints")
EVENT_TYPES = ["ingest", "supersede", "reinforce", "tier_change",
               "consolidate", "policy", "tombstone",
               "overwrite", "media_interaction", "reconsolidate"]


def parse_memory_text(text: str, suffix: str = ".txt") -> list[str]:
    """把常见记忆文件的**内容**解析成一条条正文（容错优先，解析不了就跳过）。"""
    suffix = suffix.lower()
    if not suffix.startswith("."):
        suffix = "." + suffix
    entries: list[str] = []
    if suffix == ".json":
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return []
        items: object = payload
        if isinstance(payload, dict):
            stream = payload.get("memory_stream")
            if isinstance(stream, dict) and isinstance(stream.get("items"), list):
                items = stream["items"]
            elif isinstance(payload.get("memories"), list):
                items = payload["memories"]
            elif isinstance(payload.get("neurons"), list):
                items = payload["neurons"]
            elif isinstance(payload.get("items"), list):
                items = payload["items"]
            else:
                items = []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, str) and item.strip():
                    entries.append(item.strip())
                elif isinstance(item, dict) and str(item.get("content", "")).strip():
                    entries.append(str(item["content"]).strip())
        return entries
    if suffix in (".ndjson", ".jsonl"):
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, str) and item.strip():
                entries.append(item.strip())
            elif isinstance(item, dict) and str(item.get("content", "")).strip():
                entries.append(str(item["content"]).strip())
        return entries
    if suffix == ".csv":
        try:
            reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
            if reader.fieldnames and "content" in reader.fieldnames:
                return [str(row["content"]).strip() for row in reader
                        if str(row.get("content", "")).strip()]
            plain = csv.reader(io.StringIO(text))
            return [row[0].strip() for row in plain if row and row[0].strip()]
        except (csv.Error, OSError):
            return []
    if suffix == ".md":
        return [block.strip() for block in text.split("\n\n") if len(block.strip()) >= 4]
    return [line.strip() for line in text.splitlines() if len(line.strip()) >= 4]


def parse_memory_file(path: str | Path) -> list[str]:
    file_path = Path(path)
    if not file_path.exists():
        return []
    return parse_memory_text(file_path.read_text(encoding="utf-8", errors="replace"), file_path.suffix)


class MemoryService:
    def __init__(self, app: Any, config: Any, *, write_gate: Any | None = None) -> None:
        self.app = app
        self.config = config
        self.browser = MemoryBrowser(app.storage.relational)
        # W1（Q25）：纠正/遗忘的写主库部分也经统一闸门
        self.editor = MemoryEditor(app=app, write_gate=write_gate)
        self.activation = ActivationService(app.storage.relational, app.embedder, config)

    # ── 列表 / 检查器 ────────────────────────────────────────────────────
    def memories(self, *, limit: int = 200, tier: str = "", memory_type: str = "",
                 namespace: str = "", keyword: str = "", since_ms: int = 0) -> list[dict[str, Any]]:
        rows = self.browser.list(
            limit=int(limit), tier=tier or None, memory_type=memory_type or None,
            since_ms=int(since_ms),
        )
        if namespace:
            rows = [row for row in rows if str(row.get("namespace") or "") == namespace]
        if keyword:
            needle = keyword.lower()
            rows = [row for row in rows if needle in str(row.get("content") or "").lower()]
        sensitive = set(self.sensitive_ids())
        for row in rows:
            row["sensitive"] = str(row.get("id")) in sensitive
            if row["sensitive"]:
                row["content"] = "••••••（敏感记忆，在检查器中显示）"
        return rows

    def detail(self, memory_id: str) -> dict[str, Any]:
        row = self.browser.get(memory_id)
        if row is None:
            return {"error": "记忆不存在或已封存"}
        now_ms = int(time.time() * 1000)
        last_recall = int(row.get("last_recall_ms") or 0)
        anchor = last_recall or int(row.get("valid_from_ms") or row.get("created_at_ms") or 0)
        age_days = max(0.0, (now_ms - anchor) / 86_400_000.0) if anchor else 0.0
        confidence = float(row.get("confidence") or 0.0)
        strength = float(row.get("strength") or 0.0)
        stability = float(row.get("stability") or 0.0)
        provenance = 1.0 if (row.get("agent_id") or row.get("device_id")) else 0.4
        freshness = math.exp(-age_days / 30.0)
        usage = 1.0 - math.exp(-int(row.get("recall_count") or 0) / 5.0)
        evidence = min(1.0, 0.5 * confidence + 0.25 * strength + 0.25 * stability)
        overall = 0.30 * provenance + 0.25 * freshness + 0.20 * usage + 0.25 * evidence
        try:
            graph = self.activation.build()
            edges = [edge for edge in graph.edges
                     if edge.source == memory_id or edge.target == memory_id]
            names = {str(item["id"]): str(item["content"])
                     for item in self.browser.list(limit=500)}
            related = [
                {"id": str(edge.target if edge.source == memory_id else edge.source),
                 "content": names.get(str(edge.target if edge.source == memory_id else edge.source), ""),
                 "weight": round(min(1.0, float(edge.weight)), 3)}
                for edge in sorted(edges, key=lambda item: item.weight, reverse=True)
            ][: self.config.activation.max_pulses]
        except Exception:  # noqa: BLE001  构图失败不阻断检查器
            related = []
        return {
            **row,
            "age_days": round(age_days, 1),
            "stale": bool(age_days > 30 and row.get("is_active", True)),
            "confidence_breakdown": {
                "provenance": round(provenance, 3), "freshness": round(freshness, 3),
                "usage": round(usage, 3), "evidence": round(evidence, 3),
                "overall": round(overall, 3),
            },
            "related": related,
            "detail_text": self.browser.detail(row),
            "audit": self.editor.history(memory_id),
            "sensitive": str(memory_id) in set(self.sensitive_ids()),
        }

    # ── 用户控制闭环 ─────────────────────────────────────────────────────
    def correct(self, memory_id: str, changes: dict[str, Any] | None = None,
                *, reason: str = "", actor: str = "user") -> dict[str, Any]:
        result = self.editor.correct(memory_id, changes=changes or {}, actor=actor, reason=reason)
        if result.get("ok"):
            self.activation.invalidate()
        return result

    def forget(self, memory_id: str, *, reason: str = "", action: str = "forget") -> dict[str, Any]:
        result = self.editor.forget(memory_id, reason=reason, action=action)
        if result.get("ok"):
            self.activation.invalidate()
        return result

    def audit(self, memory_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        return self.editor.history(memory_id, limit=int(limit))

    def sensitive_ids(self) -> list[str]:
        value = self.app.storage.kv.get_json("sensitive", "ids")
        return [str(item) for item in value] if isinstance(value, list) else []

    def set_sensitive(self, memory_id: str, flag: bool) -> dict[str, Any]:
        ids = set(self.sensitive_ids())
        if flag:
            ids.add(str(memory_id))
        else:
            ids.discard(str(memory_id))
        self.app.storage.kv.put_json("sensitive", "ids", sorted(ids))
        return {"ok": True, "sensitive": bool(flag), "id": str(memory_id)}

    def core_memory_blocks(self) -> dict[str, str]:
        value = self.app.storage.kv.get_json("core_memory", "blocks")
        saved = value if isinstance(value, dict) else {}
        return {name: str(saved.get(name, "")) for name in _CORE_BLOCKS}

    def save_core_memory_blocks(self, blocks: dict[str, str]) -> dict[str, Any]:
        merged = self.core_memory_blocks()
        merged.update({key: str(value) for key, value in blocks.items() if key in _CORE_BLOCKS})
        self.app.storage.kv.put_json("core_memory", "blocks", merged)
        self.app.journal.append({
            "event_id": new_uuid7(), "agent_id": "user", "type": "policy",
            "hlc": self.app.clock.tick().to_token(),
            "payload": {"bucket": "core_memory", "key": "blocks", "blocks": merged,
                        "reason": "edit_core_memory"},
        })
        return {"ok": True, "blocks": merged}

    # ── 事件时间轴 ───────────────────────────────────────────────────────
    @staticmethod
    def _event_summary(kind: str, payload: dict[str, Any]) -> str:
        neuron = payload.get("neuron") or {}
        text = str(neuron.get("content") or payload.get("reason") or "")
        if kind == "supersede":
            return f"纠正 {str(payload.get('old_id', ''))[:8]} → {str(payload.get('new_id', ''))[:8]}"
        if kind == "tombstone":
            return f"遗忘 / 封存 {str(payload.get('neuron_id') or '')[:8]}：{payload.get('reason') or '—'}"
        if kind == "tier_change":
            return f"分层变更 → {payload.get('to') or '—'}"
        return text[:80] or kind

    def timeline(self, *, types: Sequence[str] = (), limit: int = 200) -> list[dict[str, Any]]:
        try:
            events = list(self.app.event_store.scan(include_self=True))
        except Exception:  # noqa: BLE001
            events = []
        wanted = set(types)
        rows: list[dict[str, Any]] = []
        for event in events:
            kind = str(event.get("type") or "")
            if wanted and kind not in wanted:
                continue
            payload = dict(event.get("payload") or {})
            hlc = str(event.get("hlc") or "")
            try:
                ts_ms = int(hlc.split(":")[0])
            except (ValueError, IndexError):
                ts_ms = 0
            neuron = payload.get("neuron") or {}
            rows.append({
                "event_id": str(event.get("event_id") or ""), "type": kind, "hlc": hlc,
                "ts_ms": ts_ms, "device_id": str(event.get("device_id") or ""),
                "agent_id": str(event.get("agent_id") or ""),
                "memory_id": str(neuron.get("id") or payload.get("neuron_id")
                                 or payload.get("new_id") or payload.get("old_id") or ""),
                "actor": str(payload.get("actor") or event.get("agent_id") or ""),
                "reason": str(payload.get("reason") or ""),
                "summary": self._event_summary(kind, payload),
            })
        known_ingest = {item["memory_id"] for item in rows if item["type"] == "ingest"}
        if not wanted or "ingest" in wanted:
            for record in self.browser.list(limit=int(limit)):
                memory_id = str(record.get("id") or "")
                if not memory_id or memory_id in known_ingest:
                    continue
                rows.append({
                    "event_id": "", "type": "ingest", "hlc": "",
                    "ts_ms": int(record.get("valid_from_ms") or 0),
                    "device_id": "", "agent_id": "", "memory_id": memory_id,
                    "actor": "", "reason": "",
                    "summary": f"{str(record.get('content') or '')[:60]}（缓冲层 · 本地事件）",
                })
        if not wanted or "recall" in wanted:
            try:
                logs = self.app.storage.timeseries.query(
                    "SELECT ts_ms, agent_id, query, hits, latency_ms FROM access_log "
                    "ORDER BY ts_ms DESC LIMIT ?", [int(limit)],
                )
            except Exception:  # noqa: BLE001
                logs = []
            for log in logs:
                rows.append({
                    "event_id": "", "type": "recall", "hlc": "",
                    "ts_ms": int(log.get("ts_ms") or 0),
                    "device_id": "", "agent_id": str(log.get("agent_id") or ""),
                    "memory_id": "", "actor": str(log.get("agent_id") or ""), "reason": "",
                    "summary": f"召回「{str(log.get('query') or '')[:24]}」命中 {log.get('hits')} 条"
                               f" · {log.get('latency_ms')} ms",
                })
        rows.sort(key=lambda item: item["ts_ms"], reverse=True)
        return rows[: int(limit)]

    # ── 真实召回（六通道 + 精排 + 归因）─────────────────────────────────
    def recall(self, query: str, *, limit: int = 8, agent_id: str = "flutter",
               namespaces: Sequence[str] = ()) -> dict[str, Any]:
        if not str(query).strip():
            return {"query": query, "hits": [], "degraded": False}
        graph = self.activation.build()
        hits = self.app.prefrontal.recall(
            query, agent_id=agent_id, limit=int(limit) or self.config.recall.top_k,
            namespaces=tuple(namespaces),
        )
        rows = [{
            "id": str(hit.neuron.uuid7_id), "content": hit.neuron.content,
            "score": round(float(hit.score), 4), "channels": list(hit.channels),
            "memory_type": hit.neuron.memory_type, "pad": hit.neuron.pad.as_tuple(),
        } for hit in hits]
        audit = dict(getattr(self.app.prefrontal, "last_audit", {}) or {})
        audit["weights"] = {
            "rrf": self.config.recall.w_rrf, "freshness": self.config.recall.w_freshness,
            "synapse": self.config.recall.w_synapse, "pad": self.config.recall.w_pad,
            "confidence": self.config.recall.w_confidence,
        }
        audit["rerank"] = bool(self.config.recall.rerank_enabled)
        return {
            "query": query, "hits": rows, "degraded": bool(graph.degraded),
            "injection": self.app.prefrontal.inject(hits, agent_id=agent_id),
            "audit": audit,
        }

    # ── 写入 / 批量导入（一进多出铁律统一入口）─────────────────────────
    def _ingest_one(self, content: str, *, agent_id: str,
                    cognition: dict | None = None, bundle: dict | None = None,
                    namespace: str = "topic:default", fast: bool = True) -> dict[str, Any]:
        """单条原文/前置包 → 一进多出铁律判定 → 落组；失败留项目档案，绝不降级单条。"""
        from brain.ingest_guard import classify as _classify
        from brain.router_extractor import RouterExtractor

        plan = _classify(cognition=cognition, bundle=bundle, content=content)
        if plan.mode == "single":
            r = self.app.hippocampus.remember(
                content, agent_id=agent_id, namespace=namespace,
                cognition=plan.cognition, _from_bundle=True, use_llm=not fast)
            return {"applied": r.applied, "written": 1 if r.applied else 0,
                    "id": r.neuron.uuid7_id, "reason": r.admission.reason, "mode": "single"}
        # 批量导入 fast 通道复用 DisabledLLM 规则拆分，不让几百条拖成几分钟
        from engine.local_llm import DisabledLLM
        extractor = RouterExtractor(DisabledLLM() if fast else None, self.config)
        if plan.mode == "bundle":
            obj = extractor.bundle_from_payload(plan.bundle or {})
        else:
            obj = extractor.extract_multi(content, agent_id=agent_id)
        br = self.app.hippocampus.remember_bundle(
            obj, agent_id=agent_id, namespace=namespace)
        if not br.get("handled", br.get("applied", 0) > 0):
            self.app.hippocampus._defer_orphan(content, agent_id, cognition)
            return {"applied": False, "written": 0, "id": "",
                    "reason": "一进多出失败，原文留项目档案待重试", "mode": plan.mode}
        ids = br.get("neuron_ids") or []
        return {"applied": True, "written": br.get("applied", 0),
                "id": ids[0] if ids else "", "reason": f"一进多出 {br.get('applied')}/{br.get('total')}",
                "mode": plan.mode, "complexity": br.get("complexity")}

    def save_memory(self, content: str, *, agent_id: str = "flutter",
                    cognition: dict | None = None, bundle: dict | None = None) -> dict[str, Any]:
        result = self._ingest_one(content, agent_id=agent_id, cognition=cognition, bundle=bundle)
        self.activation.invalidate()
        return result

    def import_memories(self, text: str, suffix: str = ".txt", *,
                        agent_id: str = "import", fast: bool = True) -> dict[str, Any]:
        entries = parse_memory_text(text, suffix)
        if not entries:
            return {"ok": False, "message": "没有解析到可导入的内容"}
        applied = skipped = failed = 0
        errors: list[str] = []
        for index, item in enumerate(entries, start=1):
            try:
                result = self._ingest_one(item, agent_id=agent_id, fast=fast)
                if result.get("applied"):
                    applied += 1
                else:
                    skipped += 1
            except Exception as exc:  # noqa: BLE001  单条失败不中断整批
                failed += 1
                if len(errors) < 5:
                    errors.append(f"第 {index} 条 {type(exc).__name__}: {exc}")
        self.activation.invalidate()
        return {
            "ok": True, "parsed": len(entries), "applied": applied,
            "skipped": skipped, "failed": failed, "errors": errors,
            "message": f"导入完成：解析 {len(entries)} 条 → 写入 {applied}，"
                       f"重复跳过 {skipped}，失败 {failed}",
        }
