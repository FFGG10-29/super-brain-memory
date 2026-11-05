"""ui.desktop.backend —— 桌面前端唯一的数据出口（服务层桥接）。

UI 只允许 import 本模块；它内部通过组合根 `build_app()` 装配，
再只调用 brain 服务层与 `Dashboard` 纯数据视图，绝不暴露 storage 实现。
"""
from __future__ import annotations

import csv
import json
import math
import secrets
import shutil
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from pathlib import Path

from brain.activation import ActivationGraph, ActivationService
from brain.cloud_models import load_cloud_api_key
from brain.memory_browser import MemoryBrowser
from config import BrainConfig, get_config
from core.ids import new_uuid7
from core.runtime_paths import (
    clear_local_bridge,
    read_local_token,
    write_local_bridge,
    write_local_token,
)
from gateway.http_adapter import HTTPAdapter
from gateway.mcp_server import MCPToolkit
from gateway.registry import AgentRegistry
from ui.desktop.settings import SettingsStore
from main import build_app, gpu_runtime_info
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

_CORE_BLOCKS = ("human", "persona", "preferences", "constraints")
_SECTORS = (
    ("情景 episodic", ("scenario",), "#6EC6FF"),
    ("语义 semantic", ("fact",), "#3DDC97"),
    ("程序 procedural", ("code",), "#B08CFF"),
    ("情绪 emotional", ("emotion",), "#FF6B9D"),
    ("画像 profile", ("profile",), "#FFB84D"),
)


def _parse_memory_file(path: Path) -> list[str]:
    """把常见记忆文件解析成一条条正文；容错优先：解析不了的行跳过。"""
    suffix = path.suffix.lower()
    entries: list[str] = []
    if suffix == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        # 「原汤化原食」：导出过的文件都能原样导回来
        # - export_memories → NDJSON（每行一条记录，走下面的 ndjson 分支）
        # - export_dashboard → {"memory_stream": {"items": [...]}, ...}
        # - 手写/第三方 → {"memories": [...]} 或直接数组
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
    audit: dict[str, Any] = field(default_factory=dict)   # 六路通道 / 注入 / 权重解释


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
        self._memory_editor = None
        self._applied_overrides: list[str] = []
        self._keyring = default_keyring()
        self._tasks = None  # 惰性创建，避免非 GUI 场景依赖 Qt 信号
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

    # ── 存储位置（换盘 / 迁移）────────────────────────────────────────────
    def storage_root(self) -> Path:
        return Path(self._config.storage.sqlite_path).parent

    def _storage_targets(self, root: Path) -> dict[str, Path]:
        return {
            "sqlite_path": root / "relational.db",
            "lancedb_path": root / "vector.lance",
            "kuzu_path": root / "graph.kuzu",
            "lmdb_path": root / "kv.lmdb",
            "duckdb_path": root / "timeseries.db",
        }

    def set_storage_root(self, target_dir: str | Path, *, migrate: bool = True) -> dict[str, object]:
        """一键把整个数据目录搬到新位置（可连带复制现有数据），并立即重启内核。

        为什么单独做：存储路径是**启动期参数**，改完必须重启才生效；而且换库不会自动搬数据。
        界面上散落在「存储引擎」卡片里的五个路径很容易让人以为「点保存就换好了」，
        这里把「改路径 + 关内核 + 拷数据 + 重启 + 回读」合成一次点击。
        """
        target = Path(target_dir).expanduser()
        current_root = self.storage_root()
        try:
            target = target.resolve()
            current_root_resolved = current_root.resolve()
        except OSError as exc:
            return {"ok": False, "message": f"路径无效：{exc}"}
        if target == current_root_resolved:
            return {"ok": False, "message": f"目录未变化：{target}"}
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"ok": False, "message": f"目标目录不可写：{exc}"}

        old_sync = Path(self._config.sync.local_repo_dir)
        # 旧路径必须取运行时配置（历史库可能叫 r.db / v.lance，不能按新命名猜）
        old_paths = {
            "sqlite_path": Path(self._config.storage.sqlite_path),
            "lancedb_path": Path(self._config.storage.lancedb_path),
            "kuzu_path": Path(self._config.storage.kuzu_path),
            "lmdb_path": Path(self._config.storage.lmdb_path),
            "duckdb_path": Path(self._config.storage.duckdb_path),
        }
        new_paths = self._storage_targets(target)
        new_sync = target / "brain-sync"
        # 按「旧文件 → 新目录下的规范文件名」成对迁移：老库可能叫 r.db，新目录统一叫 relational.db
        pairs = [(old_paths[name], new_paths[name]) for name in new_paths]
        pairs.append((old_sync, new_sync))

        # 先关内核：Kùzu / DuckDB / LMDB 都有写锁，开着拷会得到半截文件
        self.close()
        self._gateway = None
        self._sync_worker = None

        copied: list[str] = []
        try:
            if migrate:
                for source, destination in pairs:
                    source = Path(source)
                    if not source.exists():
                        continue
                    if source.is_dir():
                        shutil.copytree(source, destination, dirs_exist_ok=True)
                    else:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, destination)
                        for suffix in ("-wal", "-shm"):
                            sidecar = source.with_name(source.name + suffix)
                            if sidecar.exists():
                                shutil.copy2(sidecar, destination.with_name(destination.name + suffix))
                    copied.append(source.name)
            save_overrides({
                "storage": {name: str(path) for name, path in new_paths.items()},
                "sync": {"local_repo_dir": str(new_sync)},
            })
            apply_overrides(self._config, load_overrides())
            self.start()
        except Exception as exc:  # noqa: BLE001  迁移失败要回滚并重开旧库，不能把用户留在无内核状态
            save_overrides({
                "storage": {name: str(path) for name, path in self._storage_targets(current_root).items()},
                "sync": {"local_repo_dir": str(old_sync)},
            })
            apply_overrides(self._config, load_overrides())
            try:
                self.start()
            except Exception:  # noqa: BLE001
                pass
            return {
                "ok": False,
                "message": f"迁移失败并已回滚到原目录：{type(exc).__name__}: {exc}",
            }
        detail = (
            f"已复制 {len(copied)} 项数据到新目录" if migrate and copied
            else "未迁移旧数据（新目录是空库）"
        )
        return {
            "ok": True,
            "root": str(target),
            "copied": copied,
            "message": f"存储目录已切换到 {target}；{detail}；内核已重启",
        }

    @property
    def applied_overrides(self) -> list[str]:
        return list(self._applied_overrides)

    @applied_overrides.setter
    def applied_overrides(self, value: list[str]) -> None:
        self._applied_overrides = value

    @property
    def config(self) -> BrainConfig:
        return self._config

    @property
    def tasks(self):
        """重操作任务队列（GUI 页面用；非 GUI 场景不会触碰）。"""
        if self._tasks is None:
            from ui.desktop.async_backend import AsyncBackend

            self._tasks = AsyncBackend()
        return self._tasks

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

    def memory_inspect(self, memory_id: str) -> dict[str, Any]:
        """记忆检查器：来源 / 置信度证据 / 双时态 / 使用历史 / 关联。

        借鉴业界「让人能信任的记忆」交互模式：区分记录与推断、用文字而非
        纯颜色表达置信度、显式标记陈旧记忆、把历史与纠正入口放在记忆旁边。
        全部基于主库已有字段整形，不引入新存储，也不伪造精确概率。
        """
        self.start()
        assert self._browser is not None
        row = self._browser.get(memory_id)
        if row is None:
            return {}
        now_ms = int(time.time() * 1000)
        last_recall = int(row.get("last_recall_ms") or 0)
        anchor = last_recall or int(row.get("valid_from_ms") or row.get("created_at_ms") or 0)
        age_days = max(0.0, (now_ms - anchor) / 86_400_000.0) if anchor else 0.0
        recall_count = int(row.get("recall_count") or 0)
        confidence = float(row.get("confidence") or 0.0)
        strength = float(row.get("strength") or 0.0)
        stability = float(row.get("stability") or 0.0)
        provenance = 1.0 if (row.get("agent_id") or row.get("device_id")) else 0.4
        freshness = math.exp(-age_days / 30.0)
        usage = 1.0 - math.exp(-recall_count / 5.0)
        evidence = min(1.0, 0.5 * confidence + 0.25 * strength + 0.25 * stability)
        overall = 0.30 * provenance + 0.25 * freshness + 0.20 * usage + 0.25 * evidence
        names = {str(item["id"]): str(item["content"]) for item in self._browser.list(limit=500)}
        related = [
            {"id": str(rid), "content": names.get(str(rid), str(rid)), "weight": round(float(weight), 3)}
            for rid, weight in self.related(memory_id)
        ]
        return {
            **row,
            "age_days": round(age_days, 1),
            "stale": bool(age_days > 30 and row.get("is_active", True)),
            "confidence_breakdown": {
                "provenance": round(provenance, 3),
                "freshness": round(freshness, 3),
                "usage": round(usage, 3),
                "evidence": round(evidence, 3),
                "overall": round(overall, 3),
            },
            "related": related,
            "detail_text": self._browser.detail(row),
            "audit": self._editor.history(memory_id),
            "sensitive": str(memory_id) in set(self.sensitive_ids()),
        }

    # ── 用户控制闭环 ──────────────────────────────────────────────────────
    @property
    def _editor(self):
        if self._app is None:
            self.start()
        if self._memory_editor is None:
            from brain.memory_editor import MemoryEditor

            self._memory_editor = MemoryEditor(app=self._app)
        return self._memory_editor

    def correct_memory(
        self, memory_id: str, changes: dict[str, Any], *, reason: str = "", actor: str = "user"
    ) -> dict[str, Any]:
        result = self._editor.correct(memory_id, changes=changes, actor=actor, reason=reason)
        if result.get("ok") and self._activation is not None:
            self._activation.invalidate()
        return result

    def forget_memory(
        self, memory_id: str, *, reason: str = "", action: str = "forget"
    ) -> dict[str, Any]:
        result = self._editor.forget(memory_id, reason=reason, action=action)
        if result.get("ok") and self._activation is not None:
            self._activation.invalidate()
        return result

    def memory_audit(self, memory_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """该记忆的创建 / 纠正 / 封存轨迹（来自 append-only journal）。"""
        return self._editor.history(memory_id, limit=limit)

    # ── 事件时间轴 ────────────────────────────────────────────────────────
    @staticmethod
    def _event_summary(kind: str, payload: dict[str, Any]) -> str:
        neuron = payload.get("neuron") or {}
        text = str(neuron.get("content") or payload.get("reason") or "")
        if kind == "supersede":
            return f"纠正 {payload.get('old_id', '')[:8]} → {payload.get('new_id', '')[:8]}（{', '.join(payload.get('changed') or [])}）"
        if kind == "tombstone":
            return f"遗忘 / 封存 {str(payload.get('neuron_id') or '')[:8]}：{payload.get('reason') or '—'}"
        if kind == "tier_change":
            return f"分层变更 → {payload.get('to') or '—'}"
        return text[:80] or kind

    def event_timeline(self, *, types: Sequence[str] = (), limit: int = 200) -> list[dict[str, Any]]:
        """事件视角的时间轴：HLC 全序 + 类型筛选（不是记忆 valid_from 视角）。"""
        self.start()
        assert self._app is not None
        try:
            events = list(self._app.event_store.scan(include_self=True))
        except Exception:  # noqa: BLE001  事件库未就绪时给空列表，不炸 UI
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
                "event_id": str(event.get("event_id") or ""),
                "type": kind,
                "hlc": hlc,
                "ts_ms": ts_ms,
                "device_id": str(event.get("device_id") or ""),
                "agent_id": str(event.get("agent_id") or ""),
                "memory_id": str(
                    neuron.get("id") or payload.get("neuron_id")
                    or payload.get("new_id") or payload.get("old_id") or ""
                ),
                "actor": str(payload.get("actor") or event.get("agent_id") or ""),
                "reason": str(payload.get("reason") or ""),
                "summary": self._event_summary(kind, payload),
            })

        # journal 只记「跨设备值得同步」的事件（缓冲层不同步）。为了让时间轴
        # 不空白，用主库与 access_log 补齐本地事件，并显式标注来源，不伪造成 journal。
        known_ingest = {item["memory_id"] for item in rows if item["type"] == "ingest"}
        if not wanted or "ingest" in wanted:
            assert self._browser is not None
            for record in self._browser.list(limit=int(limit)):
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
                logs = self._app.storage.timeseries.query(
                    "SELECT ts_ms, agent_id, query, hits, latency_ms FROM access_log "
                    "ORDER BY ts_ms DESC LIMIT ?",
                    [int(limit)],
                )
            except Exception:  # noqa: BLE001
                logs = []
            for log in logs:
                rows.append({
                    "event_id": "", "type": "recall", "hlc": "",
                    "ts_ms": int(log.get("ts_ms") or 0),
                    "device_id": "", "agent_id": str(log.get("agent_id") or ""),
                    "memory_id": "", "actor": str(log.get("agent_id") or ""), "reason": "",
                    "summary": (
                        f"召回「{str(log.get('query') or '')[:24]}」命中 {log.get('hits')} 条"
                        f" · {log.get('latency_ms')} ms"
                    ),
                })
        rows.sort(key=lambda item: item["ts_ms"], reverse=True)
        return rows[: int(limit)]

    def event_types(self) -> list[str]:
        return ["ingest", "supersede", "reinforce", "tier_change", "consolidate", "policy", "tombstone"]

    # ── 敏感记忆 / Core Memory / 命名空间 ────────────────────────────────
    def sensitive_ids(self) -> list[str]:
        self.start()
        assert self._app is not None
        value = self._app.storage.kv.get_json("sensitive", "ids")
        return [str(item) for item in value] if isinstance(value, list) else []

    def set_sensitive(self, memory_id: str, flag: bool) -> dict[str, Any]:
        self.start()
        assert self._app is not None
        ids = set(self.sensitive_ids())
        if flag:
            ids.add(str(memory_id))
        else:
            ids.discard(str(memory_id))
        self._app.storage.kv.put_json("sensitive", "ids", sorted(ids))
        return {"ok": True, "sensitive": bool(flag), "id": str(memory_id)}

    def core_memory_blocks(self) -> dict[str, str]:
        """可编辑核心记忆块（对齐 Letta ADE 的 human / persona blocks）。"""
        self.start()
        assert self._app is not None
        value = self._app.storage.kv.get_json("core_memory", "blocks")
        saved = value if isinstance(value, dict) else {}
        return {name: str(saved.get(name, "")) for name in _CORE_BLOCKS}

    def save_core_memory(self, block: str, text: str) -> dict[str, Any]:
        if block not in _CORE_BLOCKS:
            return {"ok": False, "message": f"未知核心记忆块: {block}"}
        self.start()
        assert self._app is not None
        blocks = self.core_memory_blocks()
        blocks[block] = str(text)
        self._app.storage.kv.put_json("core_memory", "blocks", blocks)
        self._app.journal.append({
            "event_id": new_uuid7(),
            "agent_id": "user",
            "type": "policy",
            "hlc": self._app.clock.tick().to_token(),
            "payload": {"bucket": "core_memory", "key": "blocks", "blocks": blocks, "reason": "edit_core_memory"},
        })
        return {"ok": True, "block": block}

    def save_core_memory_blocks(self, blocks: dict[str, str]) -> dict[str, Any]:
        """一次保存全部核心记忆块，只发一条 policy 事件，避免审计噪音。"""
        self.start()
        assert self._app is not None
        merged = self.core_memory_blocks()
        merged.update({key: str(value) for key, value in blocks.items() if key in _CORE_BLOCKS})
        self._app.storage.kv.put_json("core_memory", "blocks", merged)
        self._app.journal.append({
            "event_id": new_uuid7(),
            "agent_id": "user",
            "type": "policy",
            "hlc": self._app.clock.tick().to_token(),
            "payload": {"bucket": "core_memory", "key": "blocks", "blocks": merged,
                        "reason": "edit_core_memory"},
        })
        return {"ok": True, "blocks": merged}

    def namespaces(self) -> list[str]:
        return sorted({str(row.get("namespace") or "private") for row in self.memories(limit=500)})

    def cognitive_sectors(self) -> dict[str, int]:
        """把五类 memory_type 映射成认知分区，供看板可视化。"""
        counts = {name: 0 for name, _types, _color in _SECTORS}
        for row in self.memories(limit=500):
            kind = str(row.get("memory_type") or "fact")
            for name, types, _color in _SECTORS:
                if kind in types:
                    counts[name] += 1
                    break
        return counts

    def recall_history(self, limit: int = 20) -> list[dict[str, Any]]:
        """最近召回审计轨迹（access_log 只写不改），供检查器「查看历史」。"""
        self.start()
        assert self._app is not None
        try:
            rows = self._app.storage.timeseries.query(
                "SELECT ts_ms, agent_id, query, hits, latency_ms, ok "
                "FROM access_log ORDER BY ts_ms DESC LIMIT ?",
                [int(limit)],
            )
        except Exception:  # noqa: BLE001  时序库未启用时静默
            return []
        return [dict(row) for row in rows]

    # ── 检索 → 光脉冲 ────────────────────────────────────────────────────
    def recall(
        self,
        query: str,
        *,
        limit: int | None = None,
        agent_id: str = "desktop",
        namespaces: Sequence[str] = (),
    ) -> RecallView:
        self.start()
        assert self._app is not None and self._activation is not None
        if not query.strip():
            return RecallView(query=query)
        graph = self._activation.build()  # 刷新向量缓存，保证新写入的记忆也能被激活
        hits = self._app.prefrontal.recall(
            query,
            agent_id=agent_id,
            limit=limit or self._config.recall.top_k,
            namespaces=tuple(namespaces),
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
        recall_cfg = self._config.recall
        audit = dict(getattr(self._app.prefrontal, "last_audit", {}) or {})
        audit["weights"] = {
            "rrf": recall_cfg.w_rrf,
            "freshness": recall_cfg.w_freshness,
            "synapse": recall_cfg.w_synapse,
            "pad": recall_cfg.w_pad,
            "confidence": recall_cfg.w_confidence,
        }
        audit["rerank"] = bool(recall_cfg.rerank_enabled)
        audit["similarities"] = dict(similarities)
        return RecallView(
            query=query,
            hits=rows,
            activations=activations,
            injection=self._app.prefrontal.inject(hits, agent_id=agent_id),
            degraded=graph.degraded,
            audit=audit,
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
        if self._activation is not None:
            self._activation.invalidate()
            try:
                # 写入时顺手把增量构图落盘：下次冷启动缓存仍命中，不必加载模型
                self._activation.build()
            except Exception:  # noqa: BLE001  构图失败不影响写入结果
                pass
        return {"applied": result.applied, "id": result.neuron.uuid7_id, "reason": result.admission.reason}

    def import_memories(
        self,
        path: str | Path,
        *,
        agent_id: str = "import",
        should_stop: Callable[[], bool] | None = None,
        progress_cb: Callable[[int, int], None] | None = None,
        fast: bool = True,
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
                # fast=True：跳过 LLM 抽取与语义合并裁决（每条约省 0.5~2s），
                # 仍保留丘脑准入/SimHash 去重/embedding/六引擎投影/事件日志
                result = self._app.hippocampus.remember(text, agent_id=agent_id, use_llm=not fast)
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
        if self._activation is not None:
            self._activation.invalidate()
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
        """运行时模型状态：只读取已加载信息，不主动预热（大权重加载会卡住 UI）。"""
        self.start()
        assert self._app is not None
        model = self._config.model
        app = self._app
        embed_providers = list(getattr(app.embedder, "providers", []))
        rerank_providers = list(getattr(app.reranker, "providers", []))
        return {
            "embed_backend": model.embed_backend,
            "llm_backend": model.llm_backend,
            "use_cuda": model.use_cuda,
            "runtime": gpu_runtime_info(),
            "embedder": {
                "kind": type(app.embedder).__name__,
                "dim": app.embedder.dense_dim,
                "degraded": app.embedder.degraded,
                "loaded": bool(getattr(app.embedder, "loaded", False)),
                "providers": embed_providers,
                "on_gpu": "CUDAExecutionProvider" in embed_providers,
                "model_file": getattr(app.embedder, "model_file", ""),
                "load_error": getattr(app.embedder, "load_error", ""),
                "sparse_enabled": bool(getattr(app.embedder, "sparse_enabled", False)),
                "sparse_error": getattr(app.embedder, "sparse_error", ""),
            },
            "reranker": {
                "kind": type(app.reranker).__name__,
                "degraded": app.reranker.degraded,
                "loaded": bool(getattr(app.reranker, "loaded", False)),
                "providers": rerank_providers,
                "on_gpu": "CUDAExecutionProvider" in rerank_providers,
                "model_file": getattr(app.reranker, "model_file", ""),
                "load_error": getattr(app.reranker, "load_error", ""),
            },
            "llm": {
                "kind": type(app.llm).__name__,
                "degraded": app.llm.degraded,
                "loaded": bool(getattr(app.llm, "loaded", False)),
                "active_gpu_layers": getattr(app.llm, "active_gpu_layers", None),
            },
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
        # 补丁的 section 键必须是 BrainConfig 的字段名（storage/model/recall...）。
        # UI 标题（「存储引擎」）混进来时会静默失效——这里显式报出来，避免「保存了但什么都没变」。
        unknown = sorted(str(name) for name in patches if not hasattr(self._config, str(name)))
        merged = save_overrides({k: v for k, v in patches.items() if str(k) not in unknown})
        changed = apply_overrides(self._config, merged)
        restart_needed = [
            name for name in changed
            if name.split(".", 1)[0] in ("storage", "model") or name == "recall.rerank_enabled"
        ]
        if unknown:
            message = (
                f"已保存 {len(changed)} 项；{len(unknown)} 组参数名无法识别已忽略（{', '.join(unknown)}）"
            )
        elif restart_needed:
            message = (
                f"已保存 {len(changed)} 项；其中 {len(restart_needed)} 项属启动期参数，"
                "请点「应用并重启内核」"
            )
        else:
            message = f"已保存并即时生效（{len(changed)} 项）"
        return {
            "ok": True,
            "changed": changed,
            "restart_needed": restart_needed,
            "ignored": unknown,
            "message": message,
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
        queue_depth = 0
        if self._gateway is not None:
            queue_depth = int(getattr(self._gateway[2], "queue_depth", 0) or 0)
        return {
            "running": self._http_state.running,
            "host": self._http_state.host,
            "port": self._http_state.port,
            "agent_id": self._http_state.agent_id,
            "token_hint": self._http_state.token_hint,
            "error": self._http_state.error,
            "queue_depth": queue_depth,
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
        agent_id, token = registry.register("desktop-mcp")
        actual_host, actual_port = server.server_address[0], int(server.server_address[1])
        self._http_state = HttpState(
            running=True, host=str(actual_host), port=actual_port,
            agent_id=agent_id, token_hint=f"{token[:6]}…",
        )
        self._config.gateway.http_host = host
        self._config.gateway.http_port = int(port)
        # 桥接密钥用 adapter 自己的（与 registry 令牌解耦：未配 GW_MCP_TOKEN_SECRET 时，
        # registry 的签名密钥是进程内临时值，重启即失效，不适合给常驻的 stdio 代理用）
        bridge_token = getattr(adapter, "bridge_token", token)
        # 监听地址可能是 0.0.0.0（通配），本机客户端连不上它 —— 桥接文件一律写回环地址
        bridge_host = "127.0.0.1" if str(actual_host) in ("", "0.0.0.0", "::", "::0") else str(actual_host)
        write_local_bridge(
            host=bridge_host, port=actual_port, token=str(bridge_token), agent_id=agent_id
        )
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
        clear_local_bridge()
        return {**self.gateway_status(), "ok": True, "message": "HTTP 适配器已停止（MCP 桥接同时失效）"}

    def _bridge_token(self) -> str:
        """持久化令牌：优先沿用已存的，没有才生成并落盘（重启软件不变）。"""
        token = read_local_token()
        if not token:
            token = secrets.token_urlsafe(24)
            write_local_token(token)
        return token

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
        adapter = HTTPAdapter(
            registry=registry, toolkit=toolkit, config=self._config,
            bridge_token=self._bridge_token(),  # 持久化令牌：重启软件后复制的 JSON 仍有效
        )
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
            # 仓库地址与 PAT 存在 QSettings / 钥匙串：必须在构造 worker 时喂进去，
            # 否则 has_repo 永远 False，点「立即同步」只会 SKIP（历史 bug）。
            store = SettingsStore(self._config, keyring_backend=self._keyring)
            sync_settings = store.load()
            self._sync_worker = GitSyncWorker(
                root=self._config.sync.local_repo_dir,
                device_id=self._config.sync.device_id,
                event_store=self._app.event_store,
                projector=projector,
                repo_url=sync_settings.repo_url,
                token=store.pat(),
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

    def backfill_events(self, *, limit: int = 5000) -> dict[str, Any]:
        """把本地已有记忆补写成 ingest 事件，让它们能参与跨设备同步。

        为什么需要它：同步只搬**事件日志**，不搬 data 目录里的二进制库；而默认
        `tier.buffer_no_sync=True` 时，新写入的 buffer 层记忆本就不进事件流。
        所以「以前写在本机的记忆」在配置好仓库后不会自动出现，必须显式回填一次。

        幂等：event_id 由 neuron id 派生（`backfill-<id>`），重复执行只补缺的部分。
        """
        self.start()
        assert self._app is not None
        existing = {
            str(event.get("event_id"))
            for event in self._app.journal.iter_events(include_self=True)
        }
        rows = self._app.storage.relational.query("neuron", active_only=True, limit=limit)
        appended = 0
        for row in rows:
            event_id = f"backfill-{row['id']}"
            if event_id in existing:
                continue
            self._app.journal.append({
                "event_id": event_id,
                "agent_id": str(row.get("source_agent") or "backfill"),
                "type": "ingest",
                "payload": {
                    "neuron": dict(row),
                    "extraction": {"source": "backfill", "entities": [], "relations": []},
                },
            })
            existing.add(event_id)
            appended += 1
        self._app.journal.flush()
        return {
            "ok": True,
            "scanned": len(rows),
            "appended": appended,
            "message": (
                f"扫描 {len(rows)} 条活跃记忆，补写 {appended} 条事件"
                if appended else f"扫描 {len(rows)} 条记忆，没有需要补的事件"
            ),
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
