"""v3.rpc —— WebSocket 双向 RPC：Flutter 前端的所有功能页只走这一个通道。

协议：
    → {"type":"rpc","id":"1","method":"graph","args":{...}}
    ← {"type":"rpc_result","id":"1","result":{...}}

方法覆盖 Qt 十页的能力：graph/memories/recall/detail/spaces/health/
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

__all__ = ["RpcHandler"]


class RpcHandler:
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

    def _memories(self, limit: int = 200, **_: Any) -> list[dict[str, Any]]:
        return self._daemon.memories(limit=int(limit))

    def _recall(self, q: str = "", limit: int = 8, **_: Any) -> dict[str, Any]:
        return self._daemon.recall(str(q), limit=int(limit))

    def _health(self, **_: Any) -> dict[str, Any]:
        return self._daemon.health()

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

    def _save_memory(self, content: str = "", agent_id: str = "flutter", namespace: str = "private", **_: Any):
        return self._daemon.handle_event(str(content), agent_id=str(agent_id), namespace=str(namespace))

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
        for name in ("storage", "model", "recall", "plasticity", "tier", "thalamus",
                     "amygdala", "sync", "gateway", "merge", "activation", "ui"):
            section = getattr(config, name, None)
            if section is None or not hasattr(section, "model_dump"):
                continue
            data = section.model_dump()
            sections[name] = {k: _safe(v) for k, v in data.items() if "secret" not in k and "token" not in k}
        return {"sections": sections, "client": self._daemon.client_config()}

    def _config_set(self, patches: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        from ui.desktop.user_config import apply_overrides, save_overrides  # noqa: PLC0415

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
