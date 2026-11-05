"""ui.desktop.backend —— 桌面前端唯一的数据出口（服务层桥接）。

UI 只允许 import 本模块；它内部通过组合根 `build_app()` 装配，
再只调用 brain 服务层与 `Dashboard` 纯数据视图，绝不暴露 storage 实现。
"""
from __future__ import annotations

import csv
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from pathlib import Path

from brain.activation import ActivationGraph, ActivationService
from brain.cloud_models import load_cloud_api_key
from brain.memory_browser import MemoryBrowser
from config import BrainConfig, get_config
from gateway.http_adapter import HTTPAdapter
from gateway.mcp_server import MCPToolkit
from gateway.registry import AgentRegistry
from main import build_app
from sync.git_worker import GitSyncWorker
from sync.projections import EventProjector
from sync.snapshot import SnapshotManager
from ui.dashboard import Dashboard
from ui.desktop.settings import default_keyring
from ui.desktop.user_config import (
    apply_overrides,
    clear_overrides,
    load_overrides,
    preset_patch,
    save_overrides,
)

__all__ = ["DesktopBackend", "HttpState", "RecallView"]


def _parse_memory_file(path: Path) -> list[str]:
    """把常见记忆文件解析成一条条正文；容错优先：解析不了的行跳过。"""
    suffix = path.suffix.lower()
    entries: list[str] = []
    if suffix == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        if isinstance(payload, dict):
            payload = payload.get("memories", [])
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, str) and item.strip():
                    entries.append(item.strip())
                elif isinstance(item, dict) and str(item.get("content", "")).strip():
                    entries.append(str(item["content"]).strip())
        return entries
    if suffix in (".ndjson", ".jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
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
            with open(path, encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames and "content" in reader.fieldnames:
                    entries = [str(row["content"]).strip() for row in reader if str(row.get("content", "")).strip()]
                else:
                    handle.seek(0)
                    plain = csv.reader(handle)
                    entries = [row[0].strip() for row in plain if row and row[0].strip()]
        except OSError:
            return []
        return entries
    if suffix == ".md":
        blocks = path.read_text(encoding="utf-8").split("\n\n")
        return [block.strip() for block in blocks if len(block.strip()) >= 4]
    # .txt 及其它：每行一条
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if len(line.strip()) >= 4]


@dataclass
class RecallView:
    query: str
    hits: list[dict[str, Any]] = field(default_factory=list)
    activations: list[tuple[str, float]] = field(default_factory=list)
    injection: str = ""
    degraded: bool = False


@dataclass
class HttpState:
    running: bool = False
    host: str = ""
    port: int = 0
    agent_id: str = ""
    token_hint: str = ""
    error: str = ""


class DesktopBackend:
    def __init__(self, config: BrainConfig | None = None, *, auto_start: bool = True) -> None:
        self._config = config or get_config()
        self._app = None
        self._activation: ActivationService | None = None
        self._browser: MemoryBrowser | None = None
        self._dashboard: Dashboard | None = None
        self._gateway = None
        self._http_server = None
        self._http_thread: threading.Thread | None = None
        self._http_state = HttpState()
        self._sync_worker: GitSyncWorker | None = None
        self._applied_overrides: list[str] = []
        self._keyring = default_keyring()
        if auto_start:
            self.start()

    # ── 生命周期 ──────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._app is not None:
            return
        # 打包后免重编译的关键：启动前把用户目录里的配置补丁应用到运行期 config
        self.applied_overrides = apply_overrides(self._config, load_overrides())
        app = build_app(self._config)
        self._app = app
        self._activation = ActivationService(app.storage.relational, app.embedder, self._config)
        self._browser = MemoryBrowser(app.storage.relational)
        self._dashboard = Dashboard(
            engines=app.storage.all(), relational=app.storage.relational,
            timeseries=app.storage.timeseries, event_store=app.event_store,
            journal=app.journal, root=self._config.sync.local_repo_dir,
            device_id=self._config.sync.device_id,
        )

    def close(self) -> None:
        if self._app is not None:
            self.stop_http()
            self._app.circadian.stop()
            self._app.journal.flush()
            self._app.close()
            self._app = None

    def restart(self) -> dict[str, object]:
        """应用配置后重启内核（存储开关 / 模型模式 / 维度等启动期参数）。"""
        self.close()
        self._gateway = None
        self._sync_worker = None
        try:
            self.start()
        except Exception as exc:  # noqa: BLE001  重启失败要把错误带回 UI
            return {"ok": False, "message": f"内核重启失败：{type(exc).__name__}: {exc}"}
        return {"ok": True, "message": f"内核已按新配置重启（应用 {len(self.applied_overrides)} 项改动）"}

    @property
    def applied_overrides(self) -> list[str]:
        return list(self._applied_overrides)

    @applied_overrides.setter
    def applied_overrides(self, value: list[str]) -> None:
        self._applied_overrides = value

    @property
    def config(self) -> BrainConfig:
        return self._config

    # ── 星云数据 ──────────────────────────────────────────────────────────
    def spaces(self) -> list[str]:
        """顶栏"空间"下拉：全部 + 实际存在的分层 + 情感记忆。"""
        self.start()
        assert self._browser is not None
        tiers = sorted({str(row["tier"]) for row in self._browser.list(limit=500)})
        return ["全部", *tiers, "情感记忆"]

    def graph(self, *, space: str = "全部") -> ActivationGraph:
        self.start()
        assert self._activation is not None
        return self._filter_graph(self._activation.build(), space)

    @staticmethod
    def _filter_graph(graph: ActivationGraph, space: str) -> ActivationGraph:
        if space in ("", "全部", "all"):
            return graph
        if space == "情感记忆":
            nodes = [node for node in graph.nodes if node.is_emotion]
        else:
            nodes = [node for node in graph.nodes if node.tier == space]
        keep = {node.id for node in nodes}
        return ActivationGraph(
            nodes=nodes,
            edges=[edge for edge in graph.edges if edge.source in keep and edge.target in keep],
            source=graph.source,
            degraded=graph.degraded,
        )

    def memories(
        self, *, limit: int = 80, tier: str | None = None, memory_type: str | None = None
    ) -> list[dict[str, Any]]:
        self.start()
        assert self._browser is not None
        return self._browser.list(limit=limit, tier=tier, memory_type=memory_type)

    def memory_detail(self, memory_id: str) -> str:
        self.start()
        assert self._browser is not None
        row = self._browser.get(memory_id)
        return self._browser.detail(row) if row is not None else "记忆不存在或已被封存"

    # ── 检索 → 光脉冲 ────────────────────────────────────────────────────
    def recall(self, query: str, *, limit: int | None = None, agent_id: str = "desktop") -> RecallView:
        self.start()
        assert self._app is not None and self._activation is not None
        if not query.strip():
            return RecallView(query=query)
        graph = self._activation.build()  # 刷新向量缓存，保证新写入的记忆也能被激活
        hits = self._app.prefrontal.recall(
            query, agent_id=agent_id, limit=limit or self._config.recall.top_k
        )
        rows: list[dict[str, Any]] = [
            {
                "id": str(hit.neuron.uuid7_id),
                "content": hit.neuron.content,
                "score": round(float(hit.score), 4),
                "channels": list(hit.channels),
                "pad": hit.neuron.pad.as_tuple(),
            }
            for hit in hits
        ]
        hit_ids = [str(row["id"]) for row in rows]
        similarities = dict(self._activation.activation_scores(query, hit_ids))
        max_score = max((float(row["score"]) for row in rows), default=1.0) or 1.0
        activations: list[tuple[str, float]] = [
            (
                hit_ids[index],
                round(
                    0.5 * similarities.get(hit_ids[index], 0.0)
                    + 0.5 * (float(rows[index]["score"]) / max_score),
                    4,
                ),
            )
            for index in range(len(rows))
        ]
        return RecallView(
            query=query,
            hits=rows,
            activations=activations,
            injection=self._app.prefrontal.inject(hits, agent_id=agent_id),
            degraded=graph.degraded,
        )

    def related(self, memory_id: str) -> list[tuple[str, float]]:
        """点击某条记忆 → 与它最相似的其他记忆（光点齐射目标）。"""
        self.start()
        assert self._activation is not None
        graph = self._activation.build()
        edges = [edge for edge in graph.edges if edge.source == memory_id or edge.target == memory_id]
        targets = [
            (edge.target if edge.source == memory_id else edge.source, min(1.0, edge.weight))
            for edge in sorted(edges, key=lambda item: item.weight, reverse=True)
        ]
        return targets[: self._config.activation.max_pulses]

    def save_memory(self, content: str, *, agent_id: str = "desktop") -> dict[str, Any]:
        self.start()
        assert self._app is not None
        result = self._app.hippocampus.remember(content, agent_id=agent_id)
        return {"applied": result.applied, "id": result.neuron.uuid7_id, "reason": result.admission.reason}

    def import_memories(
        self,
        path: str | Path,
        *,
        agent_id: str = "import",
        should_stop: Callable[[], bool] | None = None,
        progress_cb: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        """批量导入记忆文件，逐条走完整写入流（丘脑路由 + 去重 + 六引擎 + 事件日志）。

        支持格式：.txt（每行一条）/ .md（空行分段）/ .json（数组或 {"memories":[...]}）
                  / .ndjson .jsonl（每行一个对象，兼容本软件导出的 memories.ndjson）/ .csv（含 content 列）。
        """
        self.start()
        assert self._app is not None
        file_path = Path(path)
        if not file_path.exists():
            return {"ok": False, "message": f"文件不存在：{file_path}"}
        entries = _parse_memory_file(file_path)
        if not entries:
            return {
                "ok": False,
                "message": "没有解析到可导入的内容（txt 每行一条；md 空行分段；json 数组；ndjson 每行一个对象）",
            }
        applied = skipped = failed = 0
        errors: list[str] = []
        total = len(entries)
        for index, text in enumerate(entries, start=1):
            if should_stop is not None and should_stop():
                break
            try:
                result = self._app.hippocampus.remember(text, agent_id=agent_id)
                if result.applied:
                    applied += 1
                else:
                    skipped += 1
            except Exception as exc:  # noqa: BLE001  单条失败不中断整批
                failed += 1
                if len(errors) < 5:
                    errors.append(f"第 {index} 条 {type(exc).__name__}: {exc}")
            if progress_cb is not None:
                progress_cb(index, total)
        stopped = should_stop is not None and should_stop()
        return {
            "ok": True,
            "parsed": total,
            "applied": applied,
            "skipped": skipped,
            "failed": failed,
            "errors": errors,
            "message": (
                f"导入完成{'（已手动停止）' if stopped else ''}：解析 {total} 条 → "
                f"写入 {applied}，重复跳过 {skipped}，失败 {failed}"
            ),
        }

    # ── 配置中心（打包后免重编译）────────────────────────────────────────
    def model_status(self) -> dict[str, object]:
        self.start()
        assert self._app is not None
        model = self._config.model
        return {
            "embed_backend": model.embed_backend,
            "llm_backend": model.llm_backend,
            "embedder": {
                "kind": type(self._app.embedder).__name__,
                "dim": self._app.embedder.dense_dim,
                "degraded": self._app.embedder.degraded,
            },
            "reranker": {"kind": type(self._app.reranker).__name__, "degraded": self._app.reranker.degraded},
            "llm": {"kind": type(self._app.llm).__name__, "degraded": self._app.llm.degraded},
            "embed_ready": bool(model.embed_base_url and model.embed_model),
            "llm_ready": bool(model.llm_base_url and model.llm_model),
            "embed_key_present": bool(load_cloud_api_key(self._config, "embed", keyring_backend=self._keyring)),
            "llm_key_present": bool(load_cloud_api_key(self._config, "llm", keyring_backend=self._keyring)),
        }

    def save_settings_patch(self, patches: dict[str, dict[str, object]]) -> dict[str, object]:
        """保存到用户配置文件（差异补丁）+ 即时写回运行期 config。

        存储开关 / 模型模式 / 维度等启动期参数需要重启内核才完全生效，
        返回值里列出这些项，UI 提示用户点「应用并重启内核」。
        """
        merged = save_overrides(patches)
        changed = apply_overrides(self._config, merged)
        restart_needed = [
            name for name in changed
            if name.split(".", 1)[0] in ("storage", "model") or name == "recall.rerank_enabled"
        ]
        return {
            "ok": True,
            "changed": changed,
            "restart_needed": restart_needed,
            "message": (
                "已保存并即时生效"
                if not restart_needed
                else f"已保存；{len(restart_needed)} 项属启动期参数，请点「应用并重启内核」"
            ),
        }

    def reset_settings(self) -> dict[str, object]:
        clear_overrides()
        return {"ok": True, "message": "已清除用户配置（恢复出厂默认；重启内核后完全生效）"}

    def apply_preset(self, name: str) -> dict[str, object]:
        patch = preset_patch(name)
        if not patch:
            return {"ok": False, "message": f"预设不存在：{name}"}
        save_overrides(patch)
        changed = apply_overrides(self._config, patch)
        return {
            "ok": True,
            "changed": changed,
            "message": f"已应用预设「{name}」（{len(changed)} 项）；存储/模型参数需重启内核",
        }

    def save_cloud_key(self, channel: str, key: str) -> dict[str, object]:
        """按通道（embed / llm）保存各自供应商的 API key 到系统钥匙串。"""
        ring = self._keyring
        if ring is None:
            env = getattr(self._config.model, f"{channel}_api_key_env", "")
            return {"ok": False, "message": f"系统钥匙串不可用：请改用环境变量 {env}"}
        if key.strip():
            ring.set_password("super-brain-v21", f"{channel}_api_key", key.strip())
            message = f"{channel} 通道 API key 已写入系统钥匙串（不入配置文件/事件日志）"
        else:
            try:
                ring.delete_password("super-brain-v21", f"{channel}_api_key")
            except Exception:  # noqa: BLE001
                pass
            message = f"已清除钥匙串中的 {channel} 通道 API key"
        return {"ok": True, "message": message}

    # ── 状态面板 ──────────────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        self.start()
        assert self._dashboard is not None
        return self._dashboard.snapshot()

    # ── 网关：端口配置 + 一键启停 ────────────────────────────────────────
    def gateway_status(self) -> dict[str, Any]:
        return {
            "running": self._http_state.running,
            "host": self._http_state.host,
            "port": self._http_state.port,
            "agent_id": self._http_state.agent_id,
            "token_hint": self._http_state.token_hint,
            "error": self._http_state.error,
            "config_host": self._config.gateway.http_host,
            "config_port": self._config.gateway.http_port,
        }

    def start_http(self, host: str, port: int) -> dict[str, Any]:
        """真实绑定端口并起后台线程；端口被占用时返回结构化错误而非抛栈。"""
        self.start()
        if self._http_state.running:
            return {**self.gateway_status(), "ok": True, "message": "服务已在运行"}
        try:
            registry, _, adapter = self._ensure_gateway()
            server = adapter.make_server(host=host, port=int(port))
        except OSError as exc:
            self._http_state = HttpState(error=f"端口绑定失败：{exc}")
            return {"ok": False, "message": self._http_state.error, **self.gateway_status()}
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
        thread.start()
        self._http_server, self._http_thread = server, thread
        agent_id, token = registry.register("desktop-ui")
        actual_host, actual_port = server.server_address[0], int(server.server_address[1])
        self._http_state = HttpState(
            running=True, host=str(actual_host), port=actual_port,
            agent_id=agent_id, token_hint=f"{token[:6]}…",
        )
        self._config.gateway.http_host = host
        self._config.gateway.http_port = int(port)
        return {**self.gateway_status(), "ok": True, "message": f"HTTP 适配器已启动 http://{actual_host}:{actual_port}"}

    def stop_http(self) -> dict[str, Any]:
        server, thread = self._http_server, self._http_thread
        self._http_server, self._http_thread = None, None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2.0)
        self._http_state = HttpState()
        return {**self.gateway_status(), "ok": True, "message": "HTTP 适配器已停止"}

    def _ensure_gateway(self):
        if self._gateway is not None:
            return self._gateway
        self.start()
        assert self._app is not None
        registry = AgentRegistry(
            self._app.storage.kv, timeseries=self._app.storage.timeseries, config=self._config
        )
        toolkit = MCPToolkit(
            registry=registry,
            hippocampus=self._app.hippocampus,
            prefrontal=self._app.prefrontal,
            relational=self._app.storage.relational,
            engines=self._app.storage.all(),
            config=self._config,
        )
        adapter = HTTPAdapter(registry=registry, toolkit=toolkit, config=self._config)
        self._gateway = (registry, toolkit, adapter)
        return self._gateway

    @staticmethod
    def mcp_tools() -> list[dict[str, Any]]:
        return [
            {"name": "save_memory", "desc": "写入记忆：丘脑路由 + SimHash 去重 + 六引擎落库"},
            {"name": "recall_memory", "desc": "召回：FTS/向量/图谱/时间四通道 RRF + 重排序 + 注入"},
            {"name": "timeline_query", "desc": "时间轴查询：按事件时间窗口取记忆"},
            {"name": "profile_read", "desc": "读取 profile 类型记忆（画像槽位）"},
            {"name": "conflict_report", "desc": "冲突报告：被封存/被推翻的记忆清单"},
            {"name": "health_check", "desc": "六引擎健康检查"},
        ]

    # ── 生命循环：手动触发调度任务 ──────────────────────────────────────
    def circadian_tasks(self) -> list[dict[str, Any]]:
        self.start()
        assert self._app is not None
        return [
            {"name": task.name, "at": f"{task.hour:02d}:{task.minute:02d}", "last_run": task.last_run or "—"}
            for task in self._app.circadian.tasks
        ]

    def run_task(self, name: str) -> dict[str, Any]:
        self.start()
        assert self._app is not None
        for task in self._app.circadian.tasks:
            if task.name == name:
                try:
                    result = task.callback()
                except Exception as exc:  # noqa: BLE001  单任务失败不拖垮 UI
                    return {"ok": False, "task": name, "message": f"{type(exc).__name__}: {exc}"}
                return {"ok": True, "task": name, "message": str(result)[:300] or "已执行"}
        return {"ok": False, "task": name, "message": "任务不存在"}

    # ── 同步与备份 ────────────────────────────────────────────────────────
    def sync_once(self) -> dict[str, Any]:
        """手动 pull → replay → push（无仓库/无网络时 git 步骤自动 no-op）。"""
        self.start()
        assert self._app is not None
        if self._sync_worker is None:
            projector = EventProjector(
                relational=self._app.storage.relational,
                vector=self._app.storage.vector,
                graph=self._app.storage.graph,
                hypergraph=self._app.storage.hypergraph,
                timeseries=self._app.storage.timeseries,
                kv=self._app.storage.kv,
                embedder=self._app.embedder,
            )
            self._sync_worker = GitSyncWorker(
                root=self._config.sync.local_repo_dir,
                device_id=self._config.sync.device_id,
                event_store=self._app.event_store,
                projector=projector,
                remote=self._config.sync.remote_name,
                sync_interval_sec=self._config.sync.sync_interval_sec,
                write_debounce_sec=self._config.sync.write_debounce_sec,
            )
        report = self._sync_worker.sync_once()
        return {
            "ok": report.ok,
            "pulled": report.pulled,
            "replayed": report.replayed,
            "skipped": report.skipped,
            "pushed": report.pushed,
            "errors": report.errors,
            "has_repo": self._sync_worker.has_repo,
        }

    def create_snapshot(self) -> dict[str, Any]:
        self.start()
        assert self._app is not None
        manager = SnapshotManager(
            self._config.sync.local_repo_dir, schema_version=self._config.sync.schema_version
        )
        stamp = self._app.journal.last_stamp()
        path = manager.create(
            self._app.storage.relational,
            hlc_cutoff=stamp.to_token() if stamp is not None else "0:0:local",
        )
        return {"ok": True, "path": str(path), "name": path.name}

    def snapshots(self) -> list[dict[str, Any]]:
        root = Path(self._config.sync.local_repo_dir) / "snapshots"
        if not root.exists():
            return []
        items: list[dict[str, Any]] = []
        for folder in sorted(root.iterdir(), reverse=True):
            manifest = folder / "manifest.json"
            if not manifest.exists():
                continue
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            items.append(
                {
                    "name": folder.name,
                    "created_at_ms": payload.get("created_at_ms", 0),
                    "hlc_cutoff": payload.get("hlc_cutoff", ""),
                    "counts": payload.get("counts", {}),
                }
            )
        return items

    # ── 导出 ──────────────────────────────────────────────────────────────
    def export_dashboard(self, path: str | Path) -> str:
        target = Path(path)
        target.write_text(
            json.dumps(self.snapshot(), ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        return str(target)

    def export_memories(self, path: str | Path, *, limit: int = 5000) -> str:
        target = Path(path)
        rows = self.memories(limit=limit)
        with open(target, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        return str(target)
