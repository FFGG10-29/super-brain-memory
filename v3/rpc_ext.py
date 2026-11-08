"""v3.rpc_ext —— Flutter 新增功能页对应的 RPC 方法（RpcHandler 的 mixin）。

所有方法都由 `RpcHandler.dispatch` 的 `_{method.replace('.', '_')}` 自动命中：
    memory.inspect / memory.correct / memory.forget / memory.audit / memory.sensitive
    memory.list / core_memory.get / core_memory.set / timeline / import_memories
    config.presets / config.apply_preset / config.reset / gateway.status
    system.stats / autostart.* / secrets.* / data.* / tray.* / pad.distribution /
    graph.projection（Flutter 完全替代 Qt 所需的桌面能力）
业务实现全部走 `brain.memory_service` 与 `core.*`（不依赖 Qt / ui.desktop）。
"""
from __future__ import annotations

from typing import Any

__all__ = ["RpcExtension"]


class RpcExtension:
    """与 RpcHandler 组合使用；要求宿主有 `_daemon` 属性。"""

    @property
    def _svc(self) -> Any:
        daemon = self._daemon  # type: ignore[attr-defined]
        app = daemon.app
        if app is None:
            raise RuntimeError("后端内核未就绪：该功能需要无头后端 thread 模式")
        from brain.memory_service import MemoryService

        cached = getattr(daemon, "_memory_service", None)
        if cached is None or cached.app is not app:
            cached = MemoryService(app, daemon._config)
            daemon._memory_service = cached
        return cached

    # ── 记忆检查器 / 用户控制闭环 ───────────────────────────────────────
    def _memory_list(self, limit: int = 200, tier: str = "", memory_type: str = "",
                     namespace: str = "", keyword: str = "", **_: Any) -> list[dict[str, Any]]:
        return self._svc.memories(limit=int(limit), tier=str(tier), memory_type=str(memory_type),
                                  namespace=str(namespace), keyword=str(keyword))

    def _memory_inspect(self, memory_id: str = "", **_: Any) -> dict[str, Any]:
        return self._svc.detail(str(memory_id))

    def _memory_correct(self, memory_id: str = "", changes: dict[str, Any] | None = None,
                        reason: str = "", actor: str = "user", **_: Any) -> dict[str, Any]:
        return self._svc.correct(str(memory_id), changes or {}, reason=str(reason), actor=str(actor))

    def _memory_forget(self, memory_id: str = "", reason: str = "", action: str = "forget",
                       **_: Any) -> dict[str, Any]:
        return self._svc.forget(str(memory_id), reason=str(reason), action=str(action))

    def _memory_audit(self, memory_id: str = "", limit: int = 50, **_: Any) -> list[dict[str, Any]]:
        return self._svc.audit(str(memory_id), limit=int(limit))

    def _memory_sensitive(self, memory_id: str = "", flag: bool = False, **_: Any) -> dict[str, Any]:
        return self._svc.set_sensitive(str(memory_id), bool(flag))

    # ── Core Memory ──────────────────────────────────────────────────────
    def _core_memory_get(self, **_: Any) -> dict[str, str]:
        return self._svc.core_memory_blocks()

    def _core_memory_set(self, blocks: dict[str, str] | None = None, **_: Any) -> dict[str, Any]:
        return self._svc.save_core_memory_blocks(blocks or {})

    # ── 事件时间轴 ───────────────────────────────────────────────────────
    def _timeline(self, types: list[str] | None = None, limit: int = 200,
                  **_: Any) -> list[dict[str, Any]]:
        return self._svc.timeline(types=tuple(types or ()), limit=int(limit))

    # ── 分层上下文（对齐 dsh-mnemon 的 View-based 三层模型）─────────────
    def _layers(self) -> Any:
        from core.context_layers import get_context_layers
        return get_context_layers()

    def _context_status(self, **_: Any) -> dict[str, Any]:
        return self._layers().status()

    def _context_compose(self, query: str = "", max_runtime: int = 6,
                         max_docs: int = 3, **_: Any) -> dict[str, Any]:
        return {"ok": True, "projection": self._layers().compose(
            str(query), max_runtime=int(max_runtime), max_docs=int(max_docs))}

    def _runtime_get(self, **_: Any) -> dict[str, Any]:
        return self._layers().runtime_get()

    def _runtime_mutate(self, action: str = "add", target: str = "memory",
                        content: str = "", old_text: str = "", importance: str = "normal",
                        **_: Any) -> dict[str, Any]:
        return self._layers().runtime_mutate(
            str(action), str(target), str(content), str(old_text), str(importance))

    def _documents_list(self, keyword: str = "", include_archived: bool = False,
                        **_: Any) -> list[dict[str, Any]]:
        return self._layers().documents_list(str(keyword), bool(include_archived))

    def _documents_search(self, query: str = "", limit: int = 8, **_: Any) -> list[dict[str, Any]]:
        return self._layers().documents_search(str(query), int(limit))

    def _documents_read(self, id: str = "", **_: Any) -> dict[str, Any]:  # noqa: A002
        return self._layers().documents_read(str(id))

    def _documents_mutate(self, action: str = "create", id: str = "", title: str = "",  # noqa: A002
                          description: str = "", content: str = "",
                          source_paths: list[str] | None = None, **_: Any) -> dict[str, Any]:
        return self._layers().documents_mutate(
            str(action), str(id), str(title), str(description), str(content), source_paths)

    def _spaces_list(self, **_: Any) -> dict[str, Any]:
        return self._layers().spaces_list()

    def _spaces_create(self, name: str = "", provider: str = "super-brain-native",
                       note: str = "", **_: Any) -> dict[str, Any]:
        return self._layers().spaces_create(str(name), str(provider), str(note))

    def _spaces_activate(self, id: str = "", active: bool = True, **_: Any) -> dict[str, Any]:  # noqa: A002
        return self._layers().spaces_activate(str(id), bool(active))

    def _skills_list(self, **_: Any) -> dict[str, Any]:
        return self._layers().skills_list()

    def _skills_get(self, id: str = "", **_: Any) -> dict[str, Any]:  # noqa: A002
        return self._layers().skills_get(str(id))

    def _skills_upsert(self, id: str = "", name: str = "", description: str = "",  # noqa: A002
                       content: str = "", tags: list[str] | None = None,
                       **_: Any) -> dict[str, Any]:
        return self._layers().skills_upsert(str(id), str(name), str(description), str(content), tags)

    def _skills_evolve(self, id: str = "", success: bool = True, note: str = "",  # noqa: A002
                       **_: Any) -> dict[str, Any]:
        return self._layers().skills_evolve(str(id), bool(success), str(note))

    def _skills_delete(self, id: str = "", **_: Any) -> dict[str, Any]:  # noqa: A002
        return self._layers().skills_delete(str(id))

    def _system_shutdown(self, **_: Any) -> dict[str, Any]:
        """托盘「完全退出」：优雅停掉后端（flush journal / 收缩 WAL / 关六引擎）。

        RPC 只置一个延迟事件，响应能先发回 Flutter；真正清理在 serve() 里。
        没有挂钩子时返回结构化失败，前端会退回到按 PID 兜底终止。
        """
        callback = getattr(self._daemon, "shutdown_callback", None)  # type: ignore[attr-defined]
        if callback is None:
            return {"ok": False, "message": "后端未挂载停机钩子，请用 PID 兜底"}
        try:
            callback()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "message": "后端将在 1 秒内优雅停机"}

    def _event_types(self, **_: Any) -> list[str]:
        from brain.memory_service import EVENT_TYPES

        return EVENT_TYPES + ["recall"]

    # ── 批量导入 ─────────────────────────────────────────────────────────
    def _import_memories(self, text: str = "", suffix: str = ".txt",
                         agent_id: str = "import", fast: bool = True, **_: Any) -> dict[str, Any]:
        return self._svc.import_memories(str(text), str(suffix), agent_id=str(agent_id), fast=bool(fast))

    # ── 全量配置 ─────────────────────────────────────────────────────────
    def _config_presets(self, **_: Any) -> dict[str, Any]:
        from core.user_config import PRESET_NOTES, preset_names

        return {"names": preset_names(), "notes": PRESET_NOTES}

    def _config_apply_preset(self, name: str = "", **_: Any) -> dict[str, Any]:
        from core.user_config import apply_overrides, preset_patch, save_overrides

        patch = preset_patch(str(name))
        if not patch:
            return {"ok": False, "message": f"未知预设：{name}"}
        changed = apply_overrides(self._daemon._config, save_overrides(patch))
        return {"ok": True, "name": name, "changed": changed,
                "message": "已应用预设；启动期参数需重启内核"}

    def _config_reset(self, **_: Any) -> dict[str, Any]:
        from core.user_config import clear_overrides

        clear_overrides()
        return {"ok": True, "message": "已清除用户配置补丁；重启内核后恢复出厂默认"}

    # ── MCP / 网关状态（供「接口与端口」页展示）────────────────────────
    def _gateway_status(self, **_: Any) -> dict[str, Any]:
        from gateway.bridge_launcher import launcher_path

        config = self._daemon._config.gateway
        # 令牌绝不通过 RPC 返回：前端「复制 MCP 配置」直接读本机
        # %APPDATA%\super-brain\mcp_token.txt，避免凭据出现在网络响应里。
        return {
            "mcp_url": f"http://127.0.0.1:{config.http_port}/mcp",
            "bridge_header": "X-Super-Brain-Bridge",
            "token_path": "%APPDATA%\\super-brain\\mcp_token.txt",
            "stdio_launcher": str(launcher_path()),
            "kernel": "headless" if self._daemon.app is not None else "process",
        }

    # ── 实时仪表（CPU / 内存 / 显卡）────────────────────────────────────
    def _system_stats(self, **_: Any) -> dict[str, Any]:
        from core.system_stats import get_system_stats

        return get_system_stats().latest()

    # ── 开机自启 ────────────────────────────────────────────────────────
    def _autostart_get(self, **_: Any) -> dict[str, Any]:
        from core.desktop_features import autostart_command, autostart_enabled

        return {"enabled": autostart_enabled(), "command": autostart_command()}

    def _autostart_set(self, enabled: bool = False, target: str = "", **_: Any) -> dict[str, Any]:
        from core.desktop_features import autostart_enabled, set_autostart

        ok = set_autostart(bool(enabled), str(target))
        return {"ok": ok, "enabled": autostart_enabled(),
                "message": "已写入 Windows 用户级 Run 键" if ok else "写入失败（仅 Windows 可用）"}

    # ── 密钥保管（绝不回传明文）────────────────────────────────────────
    def _secrets_status(self, accounts: list[str] | None = None, **_: Any) -> dict[str, Any]:
        from core.desktop_features import default_secret_store

        names = list(accounts or ["github_pat", "deepseek_api_key", "openai_api_key"])
        return default_secret_store().status(names)

    def _secrets_get(self, account: str = "", **_: Any) -> dict[str, Any]:
        from core.desktop_features import default_secret_store

        return {"account": account, "value": default_secret_store().get(str(account))}

    def _secrets_set(self, account: str = "", value: str = "", **_: Any) -> dict[str, Any]:
        from core.desktop_features import default_secret_store

        backend = default_secret_store().set(str(account), str(value))
        return {"ok": backend != "none", "account": account, "backend": backend}

    def _secrets_delete(self, account: str = "", **_: Any) -> dict[str, Any]:
        from core.desktop_features import default_secret_store

        default_secret_store().delete(str(account))
        return {"ok": True, "account": account}

    # ── 数据目录一键迁移 ────────────────────────────────────────────────
    def _data_status(self, **_: Any) -> dict[str, Any]:
        from core.desktop_features import data_status

        return data_status()

    def _data_migrate(self, target: str = "", move: bool = False, **_: Any) -> dict[str, Any]:
        from core.desktop_features import migrate_data

        return migrate_data(str(target), move=bool(move))

    def _data_reset_pointer(self, **_: Any) -> dict[str, Any]:
        from core.desktop_features import clear_data_pointer, data_status

        clear_data_pointer()
        return {"ok": True, **data_status(), "message": "已恢复默认数据目录；重启后端生效"}

    # ── 系统托盘（菜单动作经 WS 广播给 Flutter）────────────────────────
    def _tray_broadcast(self, action: str) -> None:
        hub = getattr(self._daemon, "hub", None)
        if hub is not None:
            hub.broadcast({"type": "tray_action", "action": action})

    def _tray_status(self, **_: Any) -> dict[str, Any]:
        from core.win32_tray import get_tray

        tray = get_tray()
        return {"available": tray.available, "running": tray.running, "error": tray.error}

    def _tray_enable(self, enabled: bool = True, **_: Any) -> dict[str, Any]:
        from core.win32_tray import get_tray

        tray = get_tray()
        return tray.stop() if not bool(enabled) else tray.start(self._tray_broadcast)

    def _tray_disable(self, **_: Any) -> dict[str, Any]:
        from core.win32_tray import get_tray

        return get_tray().stop()

    # ── PAD 认知分区 / 2D 语义环投影 ───────────────────────────────────
    def _pad_distribution(self, limit: int = 1200, **_: Any) -> dict[str, Any]:
        rows = self._daemon._rows(limit=int(limit))
        samples: list[dict[str, Any]] = []
        by_type: dict[str, dict[str, float]] = {}
        by_tier: dict[str, dict[str, float]] = {}
        for row in rows:
            try:
                pad = [float(row.get("pad_p") or 0), float(row.get("pad_a") or 0),
                       float(row.get("pad_d") or 0)]
            except (TypeError, ValueError):
                pad = [0.0, 0.0, 0.0]
            memory_type = str(row.get("memory_type") or "fact")
            tier = str(row.get("tier") or "buffer")
            confidence = float(row.get("confidence") or 0.5)
            samples.append({
                "id": str(row.get("id") or ""), "type": memory_type, "tier": tier,
                "pad": pad, "confidence": confidence,
                "content": str(row.get("content") or "")[:80],
            })
            for bucket, key in ((by_type, memory_type), (by_tier, tier)):
                acc = bucket.setdefault(key, {"p": 0.0, "a": 0.0, "d": 0.0, "count": 0.0})
                acc["p"] += pad[0]
                acc["a"] += pad[1]
                acc["d"] += pad[2]
                acc["count"] += 1
        for bucket in (by_type, by_tier):
            for acc in bucket.values():
                count = max(acc["count"], 1.0)
                for axis in ("p", "a", "d"):
                    acc[axis] = round(acc[axis] / count, 4)
                acc["count"] = int(acc["count"])
        return {"total": len(samples), "samples": samples,
                "by_type": by_type, "by_tier": by_tier}

    def _graph_projection(self, limit: int = 1200, **_: Any) -> dict[str, Any]:
        """Qt 2D 语义环布局的服务端等价：环=分层，扇区=类型，扇区内角度=形成时间。

        坐标是确定性的（同 id → 同位置），Flutter 直接画 CustomPainter，
        与 3D 星云共享同一份 neurons 数据，实现真正的联动而非另造一份。
        """
        import hashlib
        import math

        from core.ids import uuid7_time_ms

        tier_ring = {
            "core": (60.0, 145.0), "buffer": (145.0, 245.0),
            "archive": (245.0, 325.0), "percept": (245.0, 325.0),
        }
        type_sector = {"fact": 0, "profile": 1, "emotion": 2, "scenario": 3, "code": 4}
        sectors = max(len(type_sector), 1)
        rows = self._daemon._rows(limit=int(limit))
        nodes: list[dict[str, Any]] = []
        for row in rows:
            node_id = str(row.get("id") or "")
            seed = int.from_bytes(hashlib.blake2b(node_id.encode("utf-8"), digest_size=8).digest(), "big")
            tier = str(row.get("tier") or "buffer")
            memory_type = str(row.get("memory_type") or "fact")
            lo, hi = tier_ring.get(tier, (145.0, 245.0))
            radius = lo + (hi - lo) * ((seed % 1000) / 1000.0)
            base = type_sector.get(memory_type, 0) * (2 * math.pi / sectors)
            try:
                created_ms = int(uuid7_time_ms(node_id))
            except (ValueError, TypeError):
                created_ms = 0
            time_frac = ((created_ms // 21_600_000) % 997) / 997.0 if created_ms else 0.5
            jitter = ((seed >> 20) % 100) / 100.0 * 0.12
            angle = base + (time_frac * 0.82 + jitter) * (2 * math.pi / sectors)
            nodes.append({
                "id": node_id, "content": str(row.get("content") or "")[:120],
                "type": memory_type, "tier": tier,
                "confidence": float(row.get("confidence") or 0.5),
                "is_emotion": bool(row.get("is_emotion")),
                "pad": [float(row.get("pad_p") or 0), float(row.get("pad_a") or 0),
                        float(row.get("pad_d") or 0)],
                "x": round(math.cos(angle) * radius, 3),
                "y": round(math.sin(angle) * radius, 3),
            })
        ids = {node["id"] for node in nodes}
        edges: list[dict[str, Any]] = []
        try:
            graph = self._activation_graph()
            for edge in graph:
                source = str(edge.get("source") or edge.get("src") or "")
                target = str(edge.get("target") or edge.get("dst") or "")
                if source in ids and target in ids:
                    edges.append({"source": source, "target": target,
                                  "weight": float(edge.get("weight") or 0.5)})
        except Exception:  # noqa: BLE001  关系图不可用不影响 2D 节点渲染
            pass
        return {"nodes": nodes, "edges": edges[:4000], "layout": "semantic-ring-v1"}

    def _activation_graph(self) -> list[dict[str, Any]]:
        service = self._svc
        graph = service.activation.build()
        result: list[dict[str, Any]] = []
        for edge in getattr(graph, "edges", []) or []:
            if isinstance(edge, dict):
                result.append(edge)
            else:
                result.append({
                    "source": getattr(edge, "source", ""),
                    "target": getattr(edge, "target", ""),
                    "weight": getattr(edge, "weight", 0.5),
                })
        return result
