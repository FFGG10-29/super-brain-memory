"""v3.rpc —— WebSocket 双向 RPC：Flutter 前端的所有功能页只走这一个通道。

协议：
    → {"type":"rpc","id":"1","method":"graph","args":{...}}
    ← {"type":"rpc_result","id":"1","result":{...}}

方法覆盖前端十页的能力：graph/memories/recall/detail/spaces/health/
snapshot/tasks/run_task/engines/model_status/config.get/config.set/
sync_once/snapshots/create_snapshot/export/save_memory/restart/gateway。
thread 模式下用 daemon.app（真实六引擎）执行；process 模式无引擎时明确返回提示，
绝不伪造数据。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from v3.rpc_ext import RpcExtension

__all__ = ["RpcHandler"]

# 六引擎的「职责 / 写入策略 / 实际存什么」。
# 依据：2026-11-10 全链路源码审计（写入 = SQLite 事实源 → outbox 无条件投影），
# 不是配置推断。前端「系统概览」页据此展示，避免再用容易误读的简短标签。
_STORAGE_META: dict[str, tuple[str, str, str]] = {
    "relational": ("主记忆库 · 唯一事实源", "每条必写", "neurons 全字段 + payload(JSON) + outbox + FTS5"),
    "vector": ("BGE-M3 1024 维 dense + sparse 向量", "每条必写（outbox 投影）",
               "向量与正文；只建 text FTS / 标量索引，无 IVF/HNSW → 精确 KNN 而非 ANN"),
    "graph": ("关系图谱 Kùzu（单写锁）", "条件写：抽到 entities/relations 才写",
              "Entity / HyperEvent / RELATES(predicate,weight) / MEMBER"),
    "kv": ("画像与运行时状态 LMDB", "部分键写入", "core_memory / runtime / profile 等键值"),
    "timeseries": ("情绪与访问轨迹 DuckDB", "每条必写（append-only）", "access_log、emotion 等时序表"),
    "hypergraph": ("多主体超边 Kùzu（复用图谱基座）", "条件写：抽到超边才写", "MEMBER 成员边 / 超边分组"),
    "clock": ("混合逻辑时钟 HLC", "只读元数据", "时间令牌，不存记忆"),
}


class RpcHandler(RpcExtension):
    def __init__(self, daemon: Any) -> None:
        self._daemon = daemon

    def dispatch(self, message: dict[str, Any]) -> dict[str, Any]:
        method = str(message.get("method", ""))
        args = message.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        handler = getattr(self, f"_{method.replace('.', '_')}", None)
        if handler is None:
            return {"ok": False, "error": f"未知方法：{method}"}
        try:
            result = handler(**args)
            return {"ok": True, "result": result}
        except TypeError as exc:
            return {"ok": False, "error": f"参数错误：{exc}"}
        except Exception as exc:  # noqa: BLE001  RPC 边界必须返回结构化错误
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # ── 数据面 ────────────────────────────────────────────────────────────
    def _graph(self, **_: Any) -> dict[str, Any]:
        return self._daemon.graph()

    def _graph_chunk(self, offset: int = 0, limit: int = 500, **_: Any) -> dict[str, Any]:
        """全量渲染：后端每批推 500 个轻量节点，前端边收边渲染。"""
        return self._daemon.graph_chunk(offset=int(offset), limit=int(limit))

    def _memories(self, limit: int = 200, offset: int = 0, **_: Any) -> list[dict[str, Any]]:
        return self._daemon.memories(limit=int(limit), offset=int(offset))

    def _recall(self, q: str = "", limit: int = 8, deep: bool = False,
                namespaces: list[str] | None = None, topics: list[str] | None = None,
                **_: Any) -> dict[str, Any]:
        """默认走只读投影的字符 2-gram 召回（毫秒级，前端点查询立刻有结果）。

        deep=True 才走真实六通道（embedding / RRF / 精排），供「召回归因」页使用。
        ★ A 包：namespaces / topics 都会下推到只读投影过滤，实现按类召回省开销；
        空列表 = Bot 通道全量可读（不隔离）。
        """
        ns = [str(x) for x in (namespaces or [])]
        for topic in topics or []:
            value = str(topic)
            ns.append(value if value.startswith("topic:") else f"topic:{value}")
        if deep and self._daemon.app is not None:
            return self._svc.recall(str(q), limit=int(limit))
        result = self._daemon.recall(str(q), limit=int(limit), namespaces=tuple(ns))
        result["deep"] = False
        result["namespaces"] = ns
        return result

    def _health(self, **_: Any) -> dict[str, Any]:
        return self._daemon.health()

    def _refine_stats(self, **_: Any) -> dict[str, Any]:
        """记忆提炼进度：inbox 队列（pending→processing→done|failed）+ Worker 实时状态。

        前端记忆工作台用此接口把「后端模型慢慢提炼」的进度可视化。
        """
        h = self._daemon.health()
        inbox = h.get("inbox", {}) or {}
        total = sum(v for v in inbox.values() if isinstance(v, int))
        processed = inbox.get("done", 0) + inbox.get("failed", 0)
        pct = round(100.0 * processed / total, 1) if total else 100.0
        return {
            "inbox": inbox,
            "pending": inbox.get("pending", 0),
            "processing": inbox.get("processing", 0),
            "done": inbox.get("done", 0),
            "failed": inbox.get("failed", 0),
            "total": total,
            "refined_pct": pct,
            "worker": h.get("worker", {}),
            "worker_mode": h.get("worker_mode"),
        }

    def _refine_trigger(self, limit: int = 50, **_: Any) -> dict[str, Any]:
        """手动触发一轮 inbox 提炼：立即 claim 并处理最多 limit 条 pending 事件。

        供前端「手动开始提炼」按钮调用；separate-process 模式下 Worker 不在本进程。
        """
        worker = self._daemon.worker
        if worker is None:
            return {"ok": False,
                    "error": "Worker 未在本进程（separate-process 模式），请由 Worker 进程触发"}
        before = self._daemon.inbox.counts().get("pending", 0)
        n = worker.run_once(limit=int(limit))
        after = self._daemon.inbox.counts().get("pending", 0)
        return {"ok": True, "processed": n, "pending_before": before, "pending_after": after}

    def _spaces(self, **_: Any) -> dict[str, Any]:
        rows = self._daemon.memories(limit=1000)
        tiers: dict[str, int] = {}
        types: dict[str, int] = {}
        for row in rows:
            tiers[str(row.get("tier"))] = tiers.get(str(row.get("tier")), 0) + 1
            types[str(row.get("memory_type"))] = types.get(str(row.get("memory_type")), 0) + 1
        return {"total": len(rows), "tiers": tiers, "types": types}

    def _detail(self, memory_id: str = "", **_: Any) -> dict[str, Any]:
        for row in self._daemon._rows(limit=1000):
            if str(row.get("id")) == str(memory_id):
                return {
                    "id": row.get("id"), "content": self._daemon._content(row),
                    "memory_type": row.get("memory_type"), "tier": row.get("tier"),
                    "pyramid": row.get("pyramid"), "confidence": row.get("confidence"),
                    "pad": (row.get("pad_p"), row.get("pad_a"), row.get("pad_d")),
                    "affinity": row.get("affinity"),
                    "valid_from_ms": row.get("valid_from_ms"), "valid_to_ms": row.get("valid_to_ms"),
                    "tx_from": row.get("tx_from"), "tx_to": row.get("tx_to"),
                    "stability": row.get("stability"), "strength": row.get("strength"),
                    "is_active": row.get("is_active"), "device_id": row.get("device_id"),
                    "agent_id": row.get("agent_id"),
                }
        return {"error": "记忆不存在或已封存"}

    def _save_memory(self, content: str = "", agent_id: str = "flutter",
                     namespace: str = "topic:default", **_: Any):
        # ★ 新写入默认带空间标记：非 topic: 前缀一律归默认空间
        ns = str(namespace or "topic:default")
        if not ns.startswith("topic:"):
            ns = "topic:default"
        return self._daemon.handle_event(str(content), agent_id=str(agent_id), namespace=ns)

    # ── 系统概览 ──────────────────────────────────────────────────────────
    def _engines(self, **_: Any) -> dict[str, Any]:
        app = self._daemon.app
        if app is None:
            return {"engines": [], "note": "进程分离模式：引擎数据由 Worker 独占，Daemon 只读投影"}
        rows = []
        descriptions = {
            "relational": "主记忆库（唯一事实源）", "vector": "语义向量检索",
            "graph": "关系图谱", "kv": "画像与状态", "timeseries": "情绪/访问轨迹",
            "hypergraph": "多主体超边",
        }
        try:
            health = app.health_report()
            for status in health:
                row = status.as_dict() if hasattr(status, "as_dict") else dict(status)
                name = str(row.get("name", row.get("engine", "?")))
                rows.append({**row, "description": descriptions.get(name, "")})
        except Exception as exc:  # noqa: BLE001
            rows.append({"name": "health", "ok": False, "detail": str(exc)})
        return {"engines": rows}

    def _storage_overview(self, **_: Any) -> dict[str, Any]:
        """系统概览增强数据（前端 method `storage.overview`）。

        全部取自后端自有存储层与现有服务：六引擎 health() 的实时计数、主库只读聚合、
        空间清单、快照清单。不新建数据库连接、不读 UI 私有文件、不伪造数值。
        """
        app = self._daemon.app
        result: dict[str, Any] = {
            "generated_at_ms": int(time.time() * 1000),
            "mode": "thread" if app is not None else "process",
            "engines": [], "totals": {}, "spaces_binding": {},
            "projection": {
                "mode": "全量冗余投影（不是按记忆种类分别存储）",
                "source_of_truth": "relational:sqlite",
                "every_write": ["relational:sqlite", "vector:lancedb", "timeseries:duckdb",
                                "kv:lmdb(部分键)", "sparse_index(SQLite 派生)"],
                "conditional": ["graph:kuzu(需 entities/relations)", "hypergraph:kuzu(需超边)"],
                "no_disk": ["v3 只读投影(ro 连接，不落盘)"],
                "note": "memory_type / tier / namespace 只是元数据列，不参与存储路由",
            },
            "recall": {
                "fast": "recall_memory：字符 2-gram 余弦 + FTS5 的零向量投影，实测 156~176ms",
                "deep": "recall_deep：dense/sparse/fts/graph/timeline/profile 六通道 → RRF → 规则加权 → bge-reranker，实测约 22.7s",
                "vector_truth": "向量真实参与 deep 排序，但 cosine 分值被丢弃、只以通道名次进 RRF；LanceDB 无 ANN 索引",
                "graph_truth": "真实关系在 Kùzu RELATES(predicate,weight)/MEMBER；前端星云图走 v3 两两 2-gram 现算，丢掉方向与谓词",
                "measured_at": "2026-11-10 审计实测",
            },
            "cleanup": {
                "at": "2026-11-10",
                "action": "后端 create_snapshot 建一致性快照后，经 MCP memory_forget 软封存 25 条测试/探针/保活残留",
                "result": "活跃 1296 → 1271；零 PAD 618 → 597；主库保留审计记录，未硬删",
                "untouched": ["597 条零 PAD（0.6B 降级期认知缺失，非脏数据，需逐条由对话大模型判定）",
                              "项目档案 1600 条（soft_limit 200，另一条清洗线）"],
            },
        }
        if app is None:
            result["note"] = "进程分离模式：引擎数据由 Worker 独占，Daemon 只读投影"
            return result

        # 1) 六引擎实时明细（health() 的 extra 已由 as_dict 展平）
        engines: list[dict[str, Any]] = []
        for status in app.health_report():
            row = status.as_dict() if hasattr(status, "as_dict") else dict(status)
            name = str(row.get("engine") or row.get("name") or "?")
            key = name.split(":", 1)[0]
            role, policy, stores = _STORAGE_META.get(key, ("—", "—", "—"))
            counts = row.get("counts")
            if isinstance(counts, dict) and counts:
                counts_text = " / ".join(f"{k}={v}" for k, v in counts.items())
                rows_total: Any = sum(int(v) for v in counts.values() if isinstance(v, (int, float)))
            else:
                parts = [f"{k}={row[k]}" for k in
                         ("rows", "entries", "nodes", "relations", "members",
                          "hyperevents", "member_edges", "map_size_mb") if k in row]
                counts_text = " / ".join(parts) or str(row.get("detail") or "—")
                rows_total = row.get("rows") or row.get("entries") or row.get("relations") or "—"
            engines.append({
                "name": name, "ok": bool(row.get("ok", True)),
                "latency_ms": row.get("latency_ms"), "version": str(row.get("version") or ""),
                "role": role, "write_policy": policy, "stores": stores,
                "counts_text": counts_text, "rows": rows_total,
                "raw_detail": str(row.get("detail") or ""),
            })
        result["engines"] = engines

        # 2) 主库真实计数（复用后端自己的 thread-local 连接池，不新建连接）
        try:
            conn = app.storage.relational._pool.get()

            def one(sql: str) -> int:
                return int(conn.execute(sql).fetchone()[0])

            def group(sql: str) -> dict[str, int]:
                return {str(r[0]): int(r[1]) for r in conn.execute(sql).fetchall()}

            result["totals"] = {
                "neurons_all": one("SELECT count(*) FROM neurons"),
                "active": one("SELECT count(*) FROM neurons WHERE is_active=1"),
                "archived": one("SELECT count(*) FROM neurons WHERE is_active=0"),
                "zero_pad_active": one("SELECT count(*) FROM neurons WHERE is_active=1 "
                                       "AND pad_p=0 AND pad_a=0 AND pad_d=0"),
                "confidence_045_active": one("SELECT count(*) FROM neurons WHERE is_active=1 AND confidence=0.45"),
                "synapses": one("SELECT count(*) FROM synapses"),
                "hyperedges": one("SELECT count(*) FROM hyperedges"),
                "outbox_pending": one("SELECT count(*) FROM outbox WHERE state='pending'"),
                "by_namespace": group("SELECT namespace,count(*) FROM neurons WHERE is_active=1 GROUP BY namespace"),
                "by_type": group("SELECT memory_type,count(*) FROM neurons WHERE is_active=1 GROUP BY memory_type"),
                "by_tier": group("SELECT tier,count(*) FROM neurons WHERE is_active=1 GROUP BY tier"),
            }
        except Exception as exc:  # noqa: BLE001  仪表不能拖垮 RPC
            result["totals"] = {"error": f"{type(exc).__name__}: {exc}"}

        # 3) 记忆空间绑定真相：空间清单 vs 实际 namespace 分布
        try:
            listed = self._spaces_list()
            spaces = listed.get("spaces", []) if isinstance(listed, dict) else []
            active_spaces = [s for s in spaces if isinstance(s, dict) and s.get("active", True)]
            # 已绑定记忆 = namespace 为 topic:<id> 的活跃记忆总数（空间=主题标签，绑定落在 namespace）
            by_ns = result["totals"].get("by_namespace", {}) if isinstance(result.get("totals"), dict) else {}
            bound = sum(int(v) for k, v in by_ns.items() if str(k).startswith("topic:"))
            result["spaces_binding"] = {
                "spaces_total": len(spaces), "spaces_active": len(active_spaces),
                "bound_memories": bound,
                "namespace_distribution": result["totals"].get("by_namespace", {}),
                "truth": "★ A 包后：空间 = 主题标签，绑定落在 neuron.namespace=topic:<id>；"
                         "activate 参与 scope=active 过滤下推，Bot 通道默认全量可读；不再做隔离容器",
                "names": [str(s.get("name") or s.get("id") or "?") for s in spaces[:12]],
                "topics": [str(s.get("topic") or s.get("id") or "?") for s in spaces[:12]],
            }
        except Exception as exc:  # noqa: BLE001
            result["spaces_binding"] = {"error": f"{type(exc).__name__}: {exc}"}

        # 4) 最新一致性快照
        try:
            snaps = self._snapshots()
            result["cleanup"]["latest_snapshot"] = snaps[0]["name"] if snaps else "—"
        except Exception:  # noqa: BLE001
            result["cleanup"]["latest_snapshot"] = "—"

        # 5) L0 人格层 + 主题分区（A 包落地后的真实状态）
        try:
            from core.persona import get_persona_layer
            result["persona"] = get_persona_layer().status()
            result["topics"] = {
                "mode": "topic-label",
                "active": self._layers().active_topics(),
                "distribution": result["totals"].get("by_namespace", {}),
                "bot_reads_all": True,
                "isolation": False,
            }
        except Exception as exc:  # noqa: BLE001
            result["persona"] = {"error": f"{type(exc).__name__}: {exc}"}
        return result

    def _snapshot(self, **_: Any) -> dict[str, Any]:
        health = self._daemon.health()
        engines = self._engines().get("engines", [])
        spaces = self._spaces()
        return {
            "engine_health": {"engines": engines},
            "memory_stream": {"count": spaces["total"]},
            "pad_axis": {"summary": {"samples": spaces["total"]}},
            "sync_status": {"pending_events": health["inbox"].get("pending", 0)},
            "spaces": spaces,
            "inbox": health["inbox"], "journal_events": health["journal_events"],
            "worker": health["worker"],
        }

    def _tasks(self, **_: Any) -> list[dict[str, Any]]:
        app = self._daemon.app
        if app is None:
            return [{"name": name, "at": "—", "last_run": "—", "description": "由 Worker 侧调度"}
                    for name in ("consolidate", "decay_sweep", "snapshot", "morning_brief")]
        return [
            {"name": task.name, "at": f"{task.hour:02d}:{task.minute:02d}",
             "last_run": task.last_run or "—", "description": _TASK_NOTES.get(task.name, "")}
            for task in app.circadian.tasks
        ]

    def _run_task(self, name: str = "", **_: Any) -> dict[str, Any]:
        app = self._daemon.app
        if app is None:
            return {"ok": False, "message": "进程分离模式下请由 Worker 触发"}
        for task in app.circadian.tasks:
            if task.name == name:
                try:
                    return {"ok": True, "task": name, "message": str(task.callback())[:300] or "已执行"}
                except Exception as exc:  # noqa: BLE001
                    return {"ok": False, "task": name, "message": f"{type(exc).__name__}: {exc}"}
        return {"ok": False, "message": "任务不存在"}

    def _model_status(self, **_: Any) -> dict[str, Any]:
        app = self._daemon.app
        config = self._daemon._config.model
        model = {"embedding_backend": getattr(config, "embed_backend", "local"),
                 "llm_backend": getattr(config, "llm_backend", "local"),
                 "use_cuda": config.use_cuda, "dense_dim": config.dense_dim}
        if app is None:
            return {**model, "note": "进程分离模式：模型由 Worker 进程加载"}
        return {
            **model,
            "embedder": {"kind": type(app.embedder).__name__, "degraded": app.embedder.degraded},
            "reranker": {"kind": type(app.reranker).__name__, "degraded": app.reranker.degraded},
            "llm": {"kind": type(app.llm).__name__, "degraded": app.llm.degraded},
        }

    # ── 配置（全量参数，走用户差异补丁）──────────────────────────────────
    def _config_get(self, **_: Any) -> dict[str, Any]:
        config = self._daemon._config
        sections = {}
        # 全量 BrainConfig 分组（§15/§16 新增块随版本自动出现）：
        # 类脑缓存 / 媒体生成 / 模型路由 / 号池网关 / 聊天 / 事件回填 /
        # 扩散激活 / PAD 门控 / 再巩固 / 叙事重建 / 主动遗忘
        for name in ("storage", "model", "recall", "plasticity", "tier", "thalamus",
                     "amygdala", "sync", "gateway", "merge", "activation", "ui",
                     "cache", "media_service", "model_router", "account_pool",
                     "chat", "hyperedge_backfill", "spreading_activation",
                     "pad_gating", "reconsolidation", "narrative_reconstruct",
                     "forget"):
            section = getattr(config, name, None)
            if section is None or not hasattr(section, "model_dump"):
                continue
            data = section.model_dump()
            sections[name] = {k: _safe(v) for k, v in data.items() if "secret" not in k and "token" not in k}
        return {"sections": sections, "client": self._daemon.client_config()}

    def _config_set(self, patches: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        from core.user_config import apply_overrides, save_overrides  # noqa: PLC0415

        patch = patches or {}
        merged = save_overrides(patch)
        changed = apply_overrides(self._daemon._config, merged)
        restart_needed = [item for item in changed if item.split(".", 1)[0] in ("storage", "model", "merge")]
        return {"changed": changed, "restart_needed": restart_needed,
                "message": "已保存并即时生效" if not restart_needed else "已保存；启动期参数需重启内核"}

    def _client_config_set(self, nebula: dict[str, Any] | None = None, **_: Any):
        return self._daemon.save_client_config({"nebula": nebula or {}})

    def _restart(self, **_: Any) -> dict[str, Any]:
        daemon = self._daemon
        if daemon.worker_mode != "thread" or daemon.app is None:
            return {"ok": False, "message": "进程分离模式请重启 Windows 服务"}
        # 无头后端：MCP 网关与 v3 Worker 共用同一 app，必须整体换核，不能只换 daemon.app
        hook = getattr(daemon, "restart_kernel", None)
        if hook is not None:
            return hook()
        from main import build_app  # noqa: PLC0415

        old = daemon.app
        try:
            fresh = build_app(daemon._config)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"内核重启失败：{type(exc).__name__}: {exc}"}
        daemon.app = fresh
        if daemon.worker is not None:
            daemon.worker._app = fresh
        old.close()
        return {"ok": True, "message": "内核已按新配置重启"}

    # ── 同步与备份 ────────────────────────────────────────────────────────
    def _snapshots(self, **_: Any) -> list[dict[str, Any]]:
        root = Path(self._daemon._config.sync.local_repo_dir) / "snapshots"
        if not root.exists():
            return []
        items = []
        for folder in sorted(root.iterdir(), reverse=True):
            manifest = folder / "manifest.json"
            if not manifest.exists():
                continue
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            items.append({"name": folder.name, "created_at_ms": payload.get("created_at_ms", 0),
                          "counts": payload.get("counts", {})})
        return items

    def _create_snapshot(self, **_: Any) -> dict[str, Any]:
        app = self._daemon.app
        if app is None:
            return {"ok": False, "message": "进程分离模式下快照由 Worker 侧生成"}
        from sync.snapshot import SnapshotManager  # noqa: PLC0415

        manager = SnapshotManager(self._daemon._config.sync.local_repo_dir,
                                  schema_version=self._daemon._config.sync.schema_version)
        stamp = app.journal.last_stamp()
        path = manager.create(app.storage.relational,
                              hlc_cutoff=stamp.to_token() if stamp is not None else "0:0:local")
        return {"ok": True, "path": str(path), "name": path.name}

    def _sync_once(self, **_: Any) -> dict[str, Any]:
        app = self._daemon.app
        if app is None:
            return {"ok": False, "message": "进程分离模式下同步由 Worker 侧执行"}
        from sync.git_worker import GitSyncWorker  # noqa: PLC0415
        from sync.projections import EventProjector  # noqa: PLC0415

        config = self._daemon._config
        projector = EventProjector(
            relational=app.storage.relational, vector=app.storage.vector,
            graph=app.storage.graph, hypergraph=app.storage.hypergraph,
            timeseries=app.storage.timeseries, kv=app.storage.kv, embedder=app.embedder,
        )
        worker = GitSyncWorker(
            root=config.sync.local_repo_dir, device_id=config.sync.device_id,
            event_store=app.event_store, projector=projector, remote=config.sync.remote_name,
            sync_interval_sec=config.sync.sync_interval_sec,
            write_debounce_sec=config.sync.write_debounce_sec,
        )
        report = worker.sync_once()
        return {"ok": report.ok, "pulled": report.pulled, "replayed": report.replayed,
                "skipped": report.skipped, "pushed": report.pushed, "errors": report.errors}

    def _export(self, **_: Any) -> dict[str, Any]:
        root = self._daemon.root / "exports"
        root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dashboard = root / f"dashboard-{stamp}.json"
        dashboard.write_text(json.dumps(self._snapshot(), ensure_ascii=False, indent=2, default=str),
                             encoding="utf-8")
        memories = root / f"memories-{stamp}.ndjson"
        with open(memories, "w", encoding="utf-8") as handle:
            for row in self._daemon.memories(limit=10000):
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        return {"ok": True, "dashboard": str(dashboard), "memories": str(memories)}

    def _gateway(self, **_: Any) -> dict[str, Any]:
        return {"http": f"http://127.0.0.1:{self._daemon._http_port}",
                "ws": f"ws://127.0.0.1:{self._daemon.hub.port}/ws",
                "mcp_tools": ["save_memory", "recall_memory", "timeline_query",
                              "profile_read", "conflict_report", "health_check"]}


_TASK_NOTES = {
    "consolidate": "聚簇主题摘要与金字塔固化",
    "decay_sweep": "按 FSRS 曲线清扫低可提取性记忆",
    "snapshot": "生成状态快照（事件日志仍是真源）",
    "morning_brief": "生成晨报",
}


def _safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    return value
