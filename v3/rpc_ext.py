"""v3.rpc_ext —— Flutter 新增功能页对应的 RPC 方法（RpcHandler 的 mixin）。

所有方法都由 `RpcHandler.dispatch` 的 `_{method.replace('.', '_')}` 自动命中：
    memory.inspect / memory.correct / memory.forget / memory.audit / memory.sensitive
    memory.list / core_memory.get / core_memory.set / timeline / import_memories
    config.presets / config.apply_preset / config.reset / gateway.status
    system.stats / autostart.* / secrets.* / data.* / tray.* / pad.distribution /
    graph.projection（Flutter 桌面能力）
业务实现全部走 `brain.memory_service` 与 `core.*`（不依赖任何 UI 框架模块）。
"""
from __future__ import annotations

import time
from pathlib import Path
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
            cached = MemoryService(app, daemon._config,
                                   write_gate=getattr(daemon, "write_gate", None))
            daemon._memory_service = cached
        return cached

    # ── 记忆检查器 / 用户控制闭环 ───────────────────────────────────────
    def _memory_list(self, limit: int = 200, tier: str = "", memory_type: str = "",
                     namespace: str = "", keyword: str = "", since_ms: int = 0,
                     **_: Any) -> list[dict[str, Any]]:
        return self._svc.memories(limit=int(limit), tier=str(tier), memory_type=str(memory_type),
                                  namespace=str(namespace), keyword=str(keyword),
                                  since_ms=int(since_ms))

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
        """2D 语义环布局的服务端等价：环=分层，扇区=类型，扇区内角度=形成时间。

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

    # ── A 包：主题分区（空间 = 标签，不是隔离容器）────────────────────
    def _topics_list(self, **_: Any) -> dict[str, Any]:
        layers = self._layers()
        spaces = layers.spaces_list().get("spaces", [])
        by_namespace: dict[str, int] = {}
        try:
            app = self._daemon.app
            if app is not None:
                # 主库实时计数，避免投影延迟让前端空间数量滞后
                conn = app.storage.relational._pool.get()
                by_namespace = {str(r[0]): int(r[1]) for r in conn.execute(
                    "SELECT namespace,count(*) FROM neurons WHERE is_active=1 GROUP BY namespace"
                ).fetchall()}
            else:
                for row in self._daemon._rows(limit=5000):
                    key = str(row.get("namespace") or "private")
                    by_namespace[key] = by_namespace.get(key, 0) + 1
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "spaces": spaces, "active_topics": layers.active_topics(),
                "by_namespace": by_namespace, "binding_mode": "topic-label",
                "isolation": False, "bot_reads_all": True}

    def _topics_migrate(self, **_: Any) -> dict[str, Any]:
        return self._layers().topics_migrate()

    def _spaces_update(self, id: str = "", name: str | None = None,  # noqa: A002
                       note: str | None = None, active: bool | None = None,
                       sampleable: bool | None = None, scope: str | None = None,
                       **_: Any) -> dict[str, Any]:
        patch: dict[str, Any] = {}
        for key, value in (("name", name), ("note", note), ("active", active),
                           ("sampleable", sampleable), ("scope", scope)):
            if value is not None:
                patch[key] = value
        return self._layers().spaces_update(str(id), **patch)

    def _spaces_delete(self, id: str = "", migrate_to: str = "default", **_: Any) -> dict[str, Any]:
        """删除空间：先把该空间活跃记忆版本化更正迁到目标空间，再删元数据。

        迁移走 MemoryEditor.correct（封存旧版本 + 建新活跃版本 + 写审计），
        不直连数据库、不碰 Kùzu 单写锁。default 空间禁止删除；
        迁移有失败则整单取消，避免留下 namespace 指向已删空间的孤儿记忆。
        """
        sid = str(id).strip()
        if not sid or sid == "default":
            return {"ok": False, "error": "默认空间不可删除 / id 不能为空"}
        layers = self._layers()
        spaces = layers.spaces_list().get("spaces", [])
        row = next((s for s in spaces
                    if str(s.get("id")) == sid or str(s.get("topic")) == sid), None)
        if row is None:
            return {"ok": False, "error": f"空间不存在：{sid}"}
        topic_id = str(row.get("topic") or row.get("id"))
        target = str(migrate_to or "default")
        target_row = next((s for s in spaces
                           if str(s.get("id")) == target or str(s.get("topic")) == target), None)
        if target_row is None:
            return {"ok": False, "error": f"迁移目标空间不存在：{target}"}
        target_topic = str(target_row.get("topic") or target_row.get("id"))
        src_ns, dst_ns = f"topic:{topic_id}", f"topic:{target_topic}"
        try:
            app = self._daemon.app
            if app is None:
                raise RuntimeError("后端内核未就绪")
            # ★ 必须读主库实时数据：投影库 _rows 有 outbox 延迟，
            #   刚写入的记忆可能尚未投影，用它扫描会漏迁并留下孤儿 namespace。
            conn = app.storage.relational._pool.get()
            ids = [str(r[0]) for r in conn.execute(
                "SELECT id FROM neurons WHERE is_active=1 AND namespace=?", (src_ns,)
            ).fetchall()]
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"扫描待迁移记忆失败：{exc}"}
        migrated = failed = 0
        errors: list[str] = []
        for mid in ids:
            try:
                res = self._svc.correct(
                    mid, {"namespace": dst_ns},
                    reason=f"空间删除迁移：{row.get('name') or topic_id} → {target_topic}",
                    actor="spaces_delete",
                )
                if res.get("ok", True):
                    migrated += 1
                else:
                    failed += 1
                    if len(errors) < 5:
                        errors.append(f"{mid}: {res.get('message', '')}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                if len(errors) < 5:
                    errors.append(f"{mid}: {exc}")
        if failed:
            return {"ok": False, "error": f"迁移失败 {failed}/{len(ids)}，已取消删除（避免孤儿记忆）",
                    "scanned": len(ids), "migrated": migrated, "failed": failed, "errors": errors}
        deleted = layers.spaces_delete(sid)
        result: dict[str, Any] = {
            "ok": bool(deleted.get("ok")), "id": sid, "name": row.get("name", ""),
            "from": src_ns, "to": dst_ns, "scanned": len(ids),
            "migrated": migrated, "failed": failed,
        }
        if not deleted.get("ok"):
            result["delete_error"] = deleted.get("error")
        return result

    def _memory_reclassify(self, items: list[dict[str, Any]] | None = None,
                           dry_run: bool = False, **_: Any) -> dict[str, Any]:
        """分批回填类别索引：每条由调用方（对话大模型）判定 topic / PAD / 类型。

        走后端 MemoryEditor.correct 版本化更正，保留审计，不直连数据库、不碰 Kùzu 锁。
        dry_run=True 只返回将要写入的变更，用于前端预览与用户确认。
        """
        rows = [x for x in (items or []) if isinstance(x, dict)][:200]
        out: list[dict[str, Any]] = []
        for item in rows:
            mid = str(item.get("id") or "")
            if not mid:
                continue
            changes: dict[str, Any] = {}
            topic = str(item.get("topic") or "")
            if topic:
                changes["namespace"] = topic if topic.startswith("topic:") else f"topic:{topic}"
            for key in ("memory_type", "confidence", "pad_p", "pad_a", "pad_d", "tier"):
                if item.get(key) is not None:
                    changes[key] = item[key]
            if not changes:
                continue
            if dry_run:
                out.append({"id": mid, "would_apply": changes})
                continue
            try:
                result = self._svc.correct(mid, changes, reason="A 包类别索引回填", actor="reclassify")
                out.append({"id": mid, "ok": bool(result.get("ok", True)),
                            "message": result.get("message", "")})
            except Exception as exc:  # noqa: BLE001
                out.append({"id": mid, "ok": False, "message": str(exc)})
        return {"ok": True, "dry_run": bool(dry_run), "count": len(out), "results": out,
                "note": "每条经后端版本化更正；补 PAD 请同时传 pad_p/pad_a/pad_d"}

    # ── C 包：真实关联边（带方向 / 谓词 / 权重）────────────────────────
    def _graph_real_edges(self, limit: int = 500, **_: Any) -> dict[str, Any]:
        app = self._daemon.app
        if app is None:
            return {"ok": False, "error": "后端内核未就绪", "edges": []}
        try:
            edges = app.storage.graph.real_edges(limit=int(limit))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "edges": []}
        return {
            "ok": True, "source": "kuzu", "count": len(edges), "edges": edges,
            "relates": sum(1 for e in edges if e["kind"] == "RELATES"),
            "member": sum(1 for e in edges if e["kind"] == "MEMBER"),
            "note": "旧 graph() 的边是 2-gram 两两现算（无方向无谓词）；此处为 Kùzu 真实关系",
        }

    # ── W4 / P4.0：补边与图密度（扩散上线前置）─────────────────────────
    def _graph_backfill(self, limit: int = 500, **_: Any) -> dict[str, Any]:
        app = self._daemon.app
        if app is None:
            return {"ok": False, "error": "后端内核未就绪", "clusters": 0}
        try:
            from brain.hyperedge_backfill import HyperedgeBackfill

            return HyperedgeBackfill(
                relational=app.storage.relational, graph=app.storage.graph,
                config=app.config,
            ).run(limit=int(limit))
        except Exception as exc:  # noqa: BLE001  RPC 不能杀连接
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "clusters": 0}

    def _graph_density(self, **_: Any) -> dict[str, Any]:
        app = self._daemon.app
        if app is None:
            return {"ok": False, "error": "后端内核未就绪"}
        try:
            from brain.hyperedge_backfill import graph_density

            density = graph_density(app.storage.graph, app.config)
            return {"ok": True, **density.as_dict()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _graph_event_skeleton(self, hyperedge_id: str = "", **_: Any) -> dict[str, Any]:
        app = self._daemon.app
        if app is None or not hyperedge_id:
            return {"ok": False, "error": "需要 hyperedge_id 与就绪内核"}
        try:
            from storage.hypergraph_db import GraphHypergraph

            view = GraphHypergraph(app.storage.graph, relational=app.storage.relational)
            return {"ok": True, "text": view.get_event_skeleton(str(hyperedge_id))}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # ── W6 / P5：叙事重构与 AI 私有叙事区 ───────────────────────────────
    def _narrative_reconstruct(self, incremental: bool = True,
                               limit: int | None = None, **_: Any) -> dict[str, Any]:
        app = self._daemon.app
        if app is None:
            return {"ok": False, "error": "后端内核未就绪", "narratives": []}
        try:
            return app.neocortex.narrative_reconstruct(
                incremental=bool(incremental), limit=limit)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "narratives": []}

    def _narrative_list(self, limit: int = 50, **_: Any) -> dict[str, Any]:
        app = self._daemon.app
        if app is None:
            return {"ok": False, "error": "后端内核未就绪", "rows": []}
        import json as _json

        rows: list[dict[str, Any]] = []
        try:
            for key, raw in app.storage.kv.scan_prefix("meta", "", limit=max(200, int(limit) * 5)):
                try:
                    meta = _json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if meta.get("owner") != "ai_narrative":
                    continue
                record = app.storage.relational.get("neuron", key)
                if record is None:
                    continue
                rows.append({
                    "id": key, "content": str(record.get("content") or ""),
                    "memory_type": record.get("memory_type", "scenario"),
                    "pyramid_subtype": meta.get("pyramid_subtype", ""),
                    "namespace": record.get("namespace", ""),
                })
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "rows": []}
        return {"ok": True, "count": len(rows), "rows": rows[:int(limit)]}

    def _narrative_retry_status(self, **_: Any) -> dict[str, Any]:
        app = self._daemon.app
        if app is None:
            return {"ok": False, "error": "后端内核未就绪", "pending": []}
        retry = getattr(app.neocortex, "_retry", None)
        if retry is None:
            return {"ok": True, "pending": []}
        return {"ok": True, "pending": retry.pending()}

    # ── W5 / W9：当前会话 PAD 状态（管理端可视化）───────────────────────
    def _pad_current(self, **_: Any) -> dict[str, Any]:
        app = self._daemon.app
        tracker = getattr(getattr(app, "prefrontal", None), "pad_tracker", None)
        if tracker is None:
            return {"ok": True, "available": False, "turns": 0,
                    "warmup_done": False, "suppress_negative": False,
                    "weight_multiplier": 0.0, "pad": None}
        state = tracker.current()
        return {
            "ok": True, "available": True, "turns": tracker.turns,
            "warmup_done": tracker.warmup_done,
            "suppress_negative": tracker.suppress_negative,
            "weight_multiplier": tracker.weight_multiplier(),
            "pad": list(state.as_tuple()) if state is not None else None,
        }

    # ── 前端监控页：号池状态与缓存使用情况（全真数据，无桩）──────────────
    def _pool_status(self, **_: Any) -> dict[str, Any]:
        try:
            from gateway.model_router import get_model_router

            router = get_model_router()
        except Exception:  # noqa: BLE001  路由未装配时按未启用展示
            router = None
        pool = getattr(router, "_account_pool", None)
        cfg = getattr(self._daemon, "_config", None)
        pool_cfg = getattr(cfg, "account_pool", None) if cfg is not None else None
        now = time.time()
        ignored = set(getattr(pool, "ignored", None) or ())
        served = getattr(pool, "served", None) or {}
        accounts = []
        for account in (getattr(pool, "accounts", None) or []):
            account_id = account.account_id()
            accounts.append({
                "id": account_id,
                "label": account.label(),
                "region": account.region,
                "dead": now < account.dead_until,
                "dead_remaining_sec": max(0, round(account.dead_until - now)),
                "ignored": account_id in ignored,
                "served": int(served.get(account_id, 0)),
                "model_cooldowns": {key: max(0, round(value - now))
                                    for key, value in account.cooldowns.items()
                                    if value > now},
            })
        return {
            "ok": True,
            "enabled": bool(getattr(pool_cfg, "enabled", False)),
            "free_lane": bool(getattr(pool_cfg, "free_lane", False)),
            "default_model": str(getattr(pool_cfg, "default_model", "") or ""),
            "account_count": len(accounts),
            "ignored_count": len(ignored),
            "accounts": accounts,
            "in_router": pool is not None,
        }

    def _pool_rescan(self, **_: Any) -> dict[str, Any]:
        """重新扫描凭证：xdpool accounts.json + WorkBuddy 登录态重新入池。"""
        pool = self._account_pool()
        if pool is None:
            return {"ok": False, "error": "号池未装配（config.account_pool.enabled）"}
        try:
            return {"ok": True, **pool.rescan()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _pool_logout(self, account_id: str = "", **_: Any) -> dict[str, Any]:
        """「退出登录」＝移出号池（凭证保留，可恢复）。"""
        pool = self._account_pool()
        if pool is None:
            return {"ok": False, "error": "号池未装配"}
        try:
            pool.logout(account_id)
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _pool_restore(self, account_id: str = "", **_: Any) -> dict[str, Any]:
        """从忽略清单恢复账号并重新扫描入池。"""
        pool = self._account_pool()
        if pool is None:
            return {"ok": False, "error": "号池未装配"}
        try:
            pool.restore(account_id)
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _account_pool(self) -> Any:
        try:
            from gateway.model_router import get_model_router

            return getattr(get_model_router(), "_account_pool", None)
        except Exception:  # noqa: BLE001  路由未装配
            return None

    # ── 号池目录 / 签到 / 登录（xdpool 功能移植）──────────────────────────
    def _pool_catalog(self, force: bool = False, **_: Any) -> dict[str, Any]:
        """模型目录：WorkBuddy 实时（倍率/上下文/限时免费）+ 免密 lane 全量。

        缓存 1 小时；force=True 立即重拉（「同步模型表」按钮）。
        """
        import time as _time

        from gateway.account_pool import catalog

        cached = None if force else catalog.load_cached_catalog()
        fresh_enough = (cached is not None
                        and _time.time() * 1000 - (cached.get("saved_at_ms") or 0) < 3_600_000)
        if cached is not None and fresh_enough:
            cached["cached"] = True
            return cached
        pool = self._account_pool()
        account = next((a for a in (getattr(pool, "accounts", None) or [])
                        if a.account_id() not in (getattr(pool, "ignored", None) or ())),
                       None)
        region = getattr(account, "region", "cn")
        wb = catalog.fetch_workbuddy_models(
            region=region,
            access_token=getattr(account, "access_token", "") or "",
            domain=getattr(account, "domain", "") or "")
        free = catalog.fetch_free_models()
        data = {"ok": True, "wb": wb, "free": free,
                "saved_at_ms": int(_time.time() * 1000),
                "disabled_models": sorted(getattr(pool, "disabled_models", None) or ()),
                "model_prefs": dict(getattr(pool, "model_prefs", None) or {})}
        try:
            catalog.save_cached_catalog(data)
        except (OSError, TypeError, ValueError):
            pass
        return data

    def _pool_model_toggle(self, model_id: str = "", enabled: bool = True, **_: Any) -> dict[str, Any]:
        """目录模型开关（禁用持久化进 accounts.json）。"""
        return self._pool_model_pref(model_id=model_id, enabled=enabled)

    def _pool_model_pref(self, model_id: str = "", enabled: bool | None = None,
                         ctx_budget: int | None = None, image: bool | None = None,
                         thinking: str | None = None, **_: Any) -> dict[str, Any]:
        """per-model 偏好：启用 / 上下文预算 / 图片 / 思考档位（一体持久化）。"""
        pool = self._account_pool()
        if pool is None:
            return {"ok": False, "error": "号池未装配"}
        try:
            pref = pool.set_model_pref(
                model_id,
                enabled=None if enabled is None else bool(enabled),
                ctx_budget=None if ctx_budget in (None, 0) else int(ctx_budget),
                image=None if image is None else bool(image),
                thinking=thinking)
            return {"ok": True, "model_id": model_id, "pref": pref}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _pool_invoke(self, model: str = "", prompt: str = "", **_: Any) -> dict[str, Any]:
        """模型拉起：单次真实调用（free:* 走免密 lane，其余走账号池 failover）。"""
        pool = self._account_pool()
        if pool is None:
            return {"ok": False, "error": "号池未装配"}
        try:
            text = pool.invoke(str(prompt or ""), model=str(model or ""))
            return {"ok": True, "model": model, "text": text}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _pool_checkin(self, account_id: str = "", **_: Any) -> dict[str, Any]:
        """一键签到：全部活跃账号（或指定账号）逐个领取每日奖励。"""
        pool = self._account_pool()
        if pool is None:
            return {"ok": False, "error": "号池未装配"}
        accounts = [a for a in (getattr(pool, "accounts", None) or [])
                    if a.account_id() not in (getattr(pool, "ignored", None) or ())]
        if account_id:
            accounts = [a for a in accounts if a.account_id() == str(account_id)]
            if not accounts:
                return {"ok": False, "error": f"账号不在池中：{account_id}"}
        if not accounts:
            return {"ok": False, "error": "账号池为空，无法签到"}
        results = [pool.checkin(a) for a in accounts]
        ok_count = sum(1 for r in results if r.get("ok"))
        return {"ok": ok_count > 0, "results": results,
                "ok_count": ok_count, "total": len(results)}

    def _pool_login_start(self, **_: Any) -> dict[str, Any]:
        """拉起 WorkBuddy 桌面端完成登录；登录态落盘后「重新扫描凭证」入池。"""
        import subprocess as _subprocess

        from gateway.account_pool import atrest

        executable = atrest.find_workbuddy_executable()
        if executable is None:
            return {"ok": False,
                    "error": "未找到 WorkBuddy 桌面端（WorkBuddy.exe / WorkBuddyAI.exe），"
                             "可设 WORKBUDDY_APP_EXECUTABLE"}
        try:
            _subprocess.Popen([executable], creationflags=_subprocess.DETACHED_PROCESS
                              if hasattr(_subprocess, "DETACHED_PROCESS") else 0)
            return {"ok": True, "executable": executable,
                    "hint": "桌面端登录完成后回到本页点「重新扫描凭证」"}
        except OSError as exc:
            return {"ok": False, "error": f"启动失败：{exc}"}

    def _pool_login_url(self, url: str = "", **_: Any) -> dict[str, Any]:
        """浏览器登录：用系统默认浏览器打开厂商登录页（wbmod 同款做法）。"""
        url = str(url or "")
        if not url.startswith(("http://", "https://")):
            return {"ok": False, "error": "非法 URL"}
        try:
            import os

            os.startfile(url)  # noqa: S606  Windows 默认浏览器
            return {"ok": True, "url": url,
                    "hint": "登录完成后点「重新扫描凭证」"}
        except OSError as exc:
            return {"ok": False, "error": f"打开浏览器失败：{exc}"}

    def _pool_import_accounts(self, text: str = "", **_: Any) -> dict[str, Any]:
        """导入 xdpool/兄弟工具的账号转移文件（多账号来源，支持 {accounts:[...]} 包装）。"""
        from gateway.account_pool import store

        try:
            result = store.import_transfer(text)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        pool = self._account_pool()
        if pool is not None:
            try:
                pool.discover()  # 导入即入池
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, **result}

    def _pool_provider_registry(self, **_: Any) -> dict[str, Any]:
        """77 家云供应商注册表（xdpool providers-registry.json 移植）。"""
        from gateway.account_pool import catalog

        return {"ok": True, "providers": catalog.provider_registry()}

    def _pool_export_accounts(self, **_: Any) -> dict[str, Any]:
        """导出账号转移文件（xdpool accounts.json 原样，含凭证，注意保密）。"""
        pool = self._account_pool()
        path = getattr(pool, "_accounts_path", None)
        try:
            target = path() if callable(path) else None
            if target is not None and Path(str(target)).is_file():
                return {"ok": True,
                        "json": Path(str(target)).read_text(encoding="utf-8")}
            return {"ok": False, "error": "accounts.json 尚未生成（先入池一个账号）"}
        except OSError as exc:
            return {"ok": False, "error": f"读取失败：{exc}"}

    # ── 模型配置页：云供应商 CRUD + 内部消费者路由 ────────────────────────
    def _router_config(self, **_: Any) -> dict[str, Any]:
        """供应商列表（key 只回传是否已存）+ 内部消费者路由 + 本地模型。"""
        router = self._router()
        cfg = self._daemon._config.model_router
        overlay = router._providers_overlay()
        providers = overlay or list(cfg.providers or [])
        rows = []
        for item in providers:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            name = str(item["name"])
            has_key = bool(self._secret_value(f"model_router.{name}"))
            rows.append({"name": name, "endpoint": str(item.get("endpoint") or ""),
                         "kind": str(item.get("kind") or "cloud"),
                         "model": str(item.get("model") or ""),
                         "caps": [str(c) for c in (item.get("caps") or ["chat"])],
                         "weight": item.get("weight", 1.0), "has_key": has_key})
        return {"ok": True, "enabled": bool(cfg.enabled),
                "local_endpoint": str(cfg.local_endpoint),
                "local_model": str(cfg.local_model),
                "providers": rows,
                "task_routes": router._task_routes_overlay()
                               or dict(cfg.task_routes or {})}

    def _router_provider_set(self, name: str = "", endpoint: str = "",
                             kind: str = "cloud", model: str = "",
                             caps: list[str] | None = None,
                             api_key: str = "", **_: Any) -> dict[str, Any]:
        """新增/更新云供应商（key 存 secret store，明文不落配置）。"""
        name = str(name or "").strip()
        if not name:
            return {"ok": False, "error": "缺少 name"}
        router = self._router()
        cfg = self._daemon._config.model_router
        providers = router._providers_overlay() or list(cfg.providers or [])
        row = {"name": name, "endpoint": str(endpoint), "kind": str(kind or "cloud"),
               "model": str(model), "caps": caps or ["chat"], "weight": 1.0}
        providers = [p for p in providers if not (isinstance(p, dict) and p.get("name") == name)]
        providers.append(row)
        router.save_providers_overlay(providers)
        if api_key:
            self._secret_set(f"model_router.{name}", str(api_key))
        router.reload()
        return {"ok": True, "name": name}

    def _router_provider_delete(self, name: str = "", **_: Any) -> dict[str, Any]:
        router = self._router()
        providers = router._providers_overlay() or list(
            self._daemon._config.model_router.providers or [])
        providers = [p for p in providers if not (isinstance(p, dict) and p.get("name") == name)]
        router.save_providers_overlay(providers)
        router.reload()
        return {"ok": True, "name": name}

    def _router_route_set(self, task: str = "", spec: str = "", **_: Any) -> dict[str, Any]:
        """内部消费者路由：extract/rerank/... → pool:模型 | free:模型 | provider:名 | 空(本地)。"""
        task = str(task or "").strip()
        if not task:
            return {"ok": False, "error": "缺少 task"}
        router = self._router()
        routes = router._task_routes_overlay() or dict(
            self._daemon._config.model_router.task_routes or {})
        if str(spec or "").strip():
            routes[task] = str(spec).strip()
        else:
            routes.pop(task, None)
        router.save_task_routes_overlay(routes)
        return {"ok": True, "task": task, "spec": routes.get(task, "")}

    def _router(self) -> Any:
        from gateway.model_router import get_model_router

        return get_model_router()

    def _secret_value(self, key: str) -> str:
        try:
            from core.desktop_features import default_secret_store

            return default_secret_store().get(key) or ""
        except Exception:  # noqa: BLE001  无桌面能力时无 key
            return ""

    def _secret_set(self, key: str, value: str) -> None:
        from core.desktop_features import default_secret_store

        default_secret_store().set(key, value)

    def _cache_metrics(self, **_: Any) -> dict[str, Any]:
        from core.cache_manager import get_cache_manager

        manager = get_cache_manager()
        if manager is None:
            return {"ok": True, "available": False}
        return {"ok": True, "available": True, **manager.snapshot_metrics()}

    def _cache_toggle(self, ns: str = "", enabled: bool = True, **_: Any) -> dict[str, Any]:
        """缓存页运行时开关：per-ns 覆盖，不落盘（重启回到 config）。"""
        from core.cache_manager import get_cache_manager

        manager = get_cache_manager()
        if manager is None:
            return {"ok": False, "error": "缓存管理器未装配"}
        ns = str(ns or "")
        if not ns:
            return {"ok": False, "error": "缺少 ns"}
        manager.set_enabled(ns, bool(enabled))
        return {"ok": True, "ns": ns, "enabled": bool(enabled)}

    def _chat_token(self, **_: Any) -> dict[str, Any]:
        """前端聊天入口测试页取 chat 专用令牌（8767 管理端同机可信）。"""
        try:
            from headless_server import ensure_chat_token

            return {"ok": True, "token": ensure_chat_token()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _chat_history(self, session_id: str = "", limit: int = 200, **_: Any) -> dict[str, Any]:
        """聊天记录回放（sessions.db messages 表，跨重启）。"""
        engine = getattr(self._daemon, "chat_engine", None)
        if engine is None:
            return {"ok": False, "error": "聊天引擎未启用（config.chat.enabled）"}
        session_id = str(session_id or "")
        if not session_id:
            return {"ok": False, "error": "缺少 session_id"}
        return {"ok": True, "messages": engine.history(session_id, limit=int(limit))}

    # ── AI 空间（QQ 空间环境：全字段资料 / AI 自写日记 / 说说·配图）───────
    # 与「叙事洞察」严格分离：日记是 AI 自己想写的东西（kv ai_space/diary/*），
    # 画像是 AI 的自我资料（kv ai_space/profile），说说带图（kv ai_space/moment/*）。

    _AI_PROFILE_DEFAULTS: dict[str, str] = {
        "nickname": "大肥鱼", "avatar_emoji": "🐟", "signature": "",
        "age": "", "school": "", "gender": "", "city": "", "hobby": "",
    }

    def _ai_space_app(self) -> Any:
        return self._daemon.app

    def _ai_kv_json(self, key: str) -> Any:
        app = self._ai_space_app()
        if app is None:
            return None
        try:
            raw = app.storage.kv.get("ai_space", key)
            if not raw:
                return None
            import json as _json

            return _json.loads(bytes(raw).decode("utf-8"))
        except (OSError, TypeError, ValueError):
            return None

    def _ai_kv_put_json(self, key: str, value: Any) -> bool:
        app = self._ai_space_app()
        if app is None:
            return False
        try:
            import json as _json

            app.storage.kv.put("ai_space", key,
                               _json.dumps(value, ensure_ascii=False).encode("utf-8"))
            return True
        except (OSError, TypeError, ValueError):
            return False

    def _ai_profile(self, **_: Any) -> dict[str, Any]:
        profile = dict(self._AI_PROFILE_DEFAULTS)
        stored = self._ai_kv_json("profile")
        if isinstance(stored, dict):
            profile.update({str(k): str(v) for k, v in stored.items()})
        diary_count = len(self._ai_diary_entries(limit=500))
        moment_count = len(self._ai_moment_entries(limit=500))
        return {"ok": True, **profile,
                "diary_count": diary_count, "moment_count": moment_count}

    def _ai_profile_write(self, fields: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        """全字段资料编辑（头像/昵称/年龄/学校/性别/城市/爱好/签名…）。"""
        if not isinstance(fields, dict) or not fields:
            return {"ok": False, "error": "缺少 fields"}
        profile = dict(self._AI_PROFILE_DEFAULTS)
        stored = self._ai_kv_json("profile")
        if isinstance(stored, dict):
            profile.update({str(k): str(v) for k, v in stored.items()})
        allowed = set(self._AI_PROFILE_DEFAULTS) | {"avatar_image"}
        for key, value in fields.items():
            if key in allowed:
                profile[str(key)] = str(value)[:200]
        if self._ai_kv_put_json("profile", profile):
            return {"ok": True, "profile": profile}
        return {"ok": False, "error": "后端内核未就绪或写入失败"}

    def _ai_signature_write(self, signature: str = "", **_: Any) -> dict[str, Any]:
        """兼容旧入口：写 profile.signature。"""
        result = self._ai_profile_write(fields={"signature": str(signature)[:120]})
        if result.get("ok"):
            return {"ok": True, "signature": str(signature)[:120]}
        return result

    def _ai_diary_entries(self, limit: int = 30) -> list[dict[str, Any]]:
        """AI 自写日记（kv ai_space/diary/<ts>），按时间倒序。"""
        app = self._ai_space_app()
        out: list[dict[str, Any]] = []
        if app is None:
            return out
        try:
            for key, raw in app.storage.kv.scan_prefix("ai_space", "diary/",
                                                       limit=2000):
                try:
                    import json as _json

                    row = _json.loads(bytes(raw).decode("utf-8"))
                except (ValueError, TypeError):
                    continue
                if isinstance(row, dict):
                    row["id"] = str(key)
                    out.append(row)
        except Exception:  # noqa: BLE001
            pass
        out.sort(key=lambda item: int(item.get("ts_ms") or 0), reverse=True)
        return out[: max(1, int(limit))]

    def _ai_diary(self, limit: int = 30, **_: Any) -> dict[str, Any]:
        return {"ok": True, "entries": self._ai_diary_entries(limit=limit)}

    def _ai_diary_write(self, text: str = "", mood: str = "", **_: Any) -> dict[str, Any]:
        """AI 自己写日记（想写点什么的时候用；MCP/聊天侧都可调）。"""
        text = str(text or "").strip()
        if not text:
            return {"ok": False, "error": "日记内容为空"}
        import time as _time

        ts = int(_time.time() * 1000)
        entry = {"text": text[:2000], "mood": str(mood or "")[:20], "ts_ms": ts}
        if self._ai_kv_put_json(f"diary/{ts}", entry):
            return {"ok": True, "entry": entry}
        return {"ok": False, "error": "后端内核未就绪或写入失败"}

    def _ai_moment_entries(self, limit: int = 20) -> list[dict[str, Any]]:
        """说说（kv ai_space/moment/<ts>），按时间倒序。"""
        app = self._ai_space_app()
        out: list[dict[str, Any]] = []
        if app is None:
            return out
        try:
            for key, raw in app.storage.kv.scan_prefix("ai_space", "moment/",
                                                       limit=2000):
                try:
                    import json as _json

                    row = _json.loads(bytes(raw).decode("utf-8"))
                except (ValueError, TypeError):
                    continue
                if isinstance(row, dict):
                    row["id"] = str(key)
                    out.append(row)
        except Exception:  # noqa: BLE001
            pass
        out.sort(key=lambda item: int(item.get("ts_ms") or 0), reverse=True)
        return out[: max(1, int(limit))]

    def _ai_moments(self, limit: int = 20, **_: Any) -> dict[str, Any]:
        return {"ok": True, "moments": self._ai_moment_entries(limit=limit)}

    def _ai_moment_write(self, text: str = "", image: str = "", **_: Any) -> dict[str, Any]:
        """发说说（可配图，image=后端生成的图片绝对路径）。"""
        text = str(text or "").strip()
        if not text and not image:
            return {"ok": False, "error": "说说内容为空"}
        import time as _time

        ts = int(_time.time() * 1000)
        row = {"text": text[:2000], "image": str(image or "")[:500], "ts_ms": ts}
        if self._ai_kv_put_json(f"moment/{ts}", row):
            return {"ok": True, "moment": row}
        return {"ok": False, "error": "后端内核未就绪或写入失败"}

    def _ai_gen_image(self, prompt: str = "", **_: Any) -> dict[str, Any]:
        """说说配图：经媒体服务生成，返回绝对路径（前端 Image.file 渲染）。"""
        media = getattr(self._daemon, "media_service", None)
        if media is None:
            return {"ok": False, "error": "媒体服务未装配"}
        try:
            result = media.generate("image", {"prompt": str(prompt or "")[:500]})
            path = str((result or {}).get("path") or "")
            if not path:
                return {"ok": False, "error": "生成失败：无输出路径"}
            return {"ok": True, "path": path}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _ai_signature_write(self, signature: str = "", **_: Any) -> dict[str, Any]:
        app = self._daemon.app
        if app is None:
            return {"ok": False, "error": "后端内核未就绪"}
        try:
            app.storage.kv.put("ai_space", "signature",
                               str(signature)[:120].encode("utf-8"))
            return {"ok": True, "signature": str(signature)[:120]}
        except (OSError, TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    # ── L0 人格层（前端可视化 + 手动微调）──────────────────────────────
    def _mirror_persona(self, params: dict[str, Any]) -> None:
        """镜像到 LMDB kv persona/fish/params，供 AstrBot 侧读取生成提示词。"""
        try:
            import json
            app = self._daemon.app
            if app is not None:
                app.storage.kv.put("persona", "fish/params",
                                   json.dumps(params, ensure_ascii=False).encode("utf-8"))
        except Exception:  # noqa: BLE001 - 镜像失败不影响人格层主存
            pass

    def _persona_get(self, **_: Any) -> dict[str, Any]:
        from core.persona import get_persona_layer
        return get_persona_layer().get()

    def _persona_baseline(self, **_: Any) -> dict[str, Any]:
        from core.persona import get_persona_layer
        return {"ok": True, "content": get_persona_layer().baseline_text()}

    def _persona_baseline_write(self, content: str = "", confirm: str = "",
                                actor: str = "frontend", **_: Any) -> dict[str, Any]:
        """骨架唯一写入口：仅前端人格设定页调用，需授权短语。MCP 侧不注册本方法。"""
        from core.persona import get_persona_layer
        return get_persona_layer().set_baseline(str(content), confirm=str(confirm), actor=str(actor))

    def _persona_status(self, **_: Any) -> dict[str, Any]:
        from core.persona import get_persona_layer
        return get_persona_layer().status()

    def _persona_set_params(self, patch: dict[str, Any] | None = None,
                            actor: str = "frontend", reason: str = "",
                            confirm_skeleton: bool = False, **_: Any) -> dict[str, Any]:
        from core.persona import get_persona_layer
        result = get_persona_layer().set_params(
            patch or {}, actor=str(actor), reason=str(reason),
            confirm_skeleton=bool(confirm_skeleton))
        if result.get("ok"):
            self._mirror_persona(result.get("params") or {})
        return result

    def _persona_rollback(self, version: int = 0, **_: Any) -> dict[str, Any]:
        from core.persona import get_persona_layer
        result = get_persona_layer().rollback(int(version))
        if result.get("ok"):
            self._mirror_persona(result.get("params") or {})
        return result

    def _persona_feedback(self, signal: str = "", valence: float = 0.0,
                          target: str = "sassiness", note: str = "",
                          session_id: str = "", actor: str = "bot", **_: Any) -> dict[str, Any]:
        from core.persona import get_persona_layer
        return get_persona_layer().feedback(
            signal=str(signal), valence=float(valence or 0.0), target=str(target),
            note=str(note), session_id=str(session_id), actor=str(actor))

    def _persona_proposals(self, **_: Any) -> dict[str, Any]:
        from core.persona import get_persona_layer
        return get_persona_layer().proposals()

    def _persona_apply_proposal(self, id: str = "", actor: str = "frontend",  # noqa: A002
                                **_: Any) -> dict[str, Any]:
        from core.persona import get_persona_layer
        result = get_persona_layer().apply_proposal(str(id), actor=str(actor))
        if result.get("ok"):
            self._mirror_persona(result.get("params") or {})
        return result
