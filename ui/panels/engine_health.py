"""ui.panels.engine_health —— 六引擎健康卡片数据源。"""
from __future__ import annotations

from core.interfaces import HealthCheckable

__all__ = ["data"]


def data(engines: list[HealthCheckable]) -> dict:
    statuses = [engine.health().as_dict() for engine in engines]
    return {"engines": statuses, "all_ok": all(status["ok"] for status in statuses)}
