"""ui.dashboard —— NiceGUI 神经活性看板入口。

纪律：只消费服务层的纯数据函数（ui/panels/*），**不 import 任何 storage 实现**。
未安装 nicegui 时 `snapshot()` 仍可用（CLI/测试/API 复用），`serve()` 会给出安装提示。
"""
from __future__ import annotations

import json
from typing import Any

from ui.panels import engine_health, memory_stream, pad_axis, synapse_topology, sync_status, tier_distribution

__all__ = ["Dashboard", "nicegui_available"]


def nicegui_available() -> bool:
    try:
        import nicegui  # noqa: F401  # pyright: ignore[reportMissingImports]
    except Exception:  # noqa: BLE001
        return False
    return True


class Dashboard:
    """六个面板的统一数据出口；依赖都是 core.interfaces 抽象。"""

    def __init__(
        self,
        *,
        engines: list[Any],
        relational: Any,
        timeseries: Any,
        event_store: Any,
        journal: Any,
        root: Any,
        device_id: str,
    ) -> None:
        self._engines = engines
        self._relational = relational
        self._timeseries = timeseries
        self._event_store = event_store
        self._journal = journal
        self._root = root
        self._device_id = device_id

    def snapshot(self) -> dict:
        return {
            "engine_health": engine_health.data(self._engines),
            "memory_stream": memory_stream.data(self._relational),
            "pad_axis": pad_axis.data(self._timeseries),
            "synapse_topology": synapse_topology.data(self._relational),
            "tier_distribution": tier_distribution.data(self._relational),
            "sync_status": sync_status.data(
                self._event_store, self._journal, device_id=self._device_id, root=self._root
            ),
        }

    def render(self) -> str:
        """无 UI 依赖的渲染兜底（CLI / 单测 / HTTP 端点）。"""
        return json.dumps(self.snapshot(), ensure_ascii=False, indent=2, default=str)

    def serve(self, *, port: int = 8080) -> None:
        if not nicegui_available():
            raise RuntimeError(
                "NiceGUI 未安装：执行 .venv\\Scripts\\python.exe -m pip install nicegui；"
                "当前可先用 python -c \"from main import ...\" 或 Dashboard.render() 查看数据快照。"
            )
        from nicegui import ui  # pyright: ignore[reportMissingImports]

        snapshot = self.snapshot()
        for panel_name, payload in snapshot.items():
            with ui.card().classes("w-full"):
                ui.label(panel_name).classes("text-h6")
                ui.json_editor({"content": {"json": payload}}) if hasattr(ui, "json_editor") else ui.label(
                    json.dumps(payload, ensure_ascii=False, default=str)[:2000]
                )
        ui.run(port=port)
