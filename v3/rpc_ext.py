"""v3.rpc_ext —— Flutter 新增功能页对应的 RPC 方法（RpcHandler 的 mixin）。

所有方法都由 `RpcHandler.dispatch` 的 `_{method.replace('.', '_')}` 自动命中：
    memory.inspect / memory.correct / memory.forget / memory.audit / memory.sensitive
    memory.list / core_memory.get / core_memory.set / timeline / import_memories
    config.presets / config.apply_preset / config.reset / gateway.status
业务实现全部走 `brain.memory_service`（不依赖 Qt）。
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
        return {
            "mcp_url": f"http://127.0.0.1:{config.http_port}/mcp",
            "bridge_header": "X-Super-Brain-Bridge",
            "stdio_launcher": str(launcher_path()),
            "kernel": "headless" if self._daemon.app is not None else "process",
        }
