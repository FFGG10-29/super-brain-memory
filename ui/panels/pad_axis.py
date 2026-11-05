"""ui.panels.pad_axis —— PAD 时间轴 + 好感度曲线 + 情绪-记忆关联视图。

本模块只做**数据整形**，不 import NiceGUI：面板层（ui.dashboard）负责渲染，
这样 eval/测试可以直接断言曲线数据，而且 UI 永远只消费服务层（架构纪律）。

情绪全链路·可视化环节：pad_track（DuckDB）→ 序列点 → 前端折线。
"""
from __future__ import annotations

import time

from core.interfaces import TimeSeriesRepository
from models.neuron import PAD

__all__ = ["data", "pad_series", "pad_summary"]

_MS_PER_DAY = 86_400_000.0


def pad_series(
    timeseries: TimeSeriesRepository,
    *,
    agent_id: str | None = None,
    days: int = 7,
    limit: int = 2000,
) -> list[dict]:
    """返回按时间升序的 PAD 序列点（前端直接喂折线图）。"""
    since_ms = int(time.time() * 1000) - int(days * _MS_PER_DAY)
    where = "WHERE ts_ms >= ?"
    params: list[object] = [since_ms]
    if agent_id:
        where += " AND agent_id = ?"
        params.append(agent_id)
    rows = timeseries.query(
        f"SELECT ts_ms, neuron_id, agent_id, p, a, d, source FROM pad_track {where} "
        f"ORDER BY ts_ms LIMIT {int(limit)}",
        params,
    )
    return [
        {
            "ts_ms": int(row["ts_ms"]),
            "neuron_id": row["neuron_id"],
            "agent_id": row["agent_id"],
            "p": float(row["p"]),
            "a": float(row["a"]),
            "d": float(row["d"]),
            "source": row["source"],
        }
        for row in rows
    ]


def pad_summary(timeseries: TimeSeriesRepository, *, agent_id: str | None = None, days: int = 7) -> dict:
    """PAD 轴面板顶部指标：均值、极值、样本量（含"最强烈的一条"）。"""
    points = pad_series(timeseries, agent_id=agent_id, days=days)
    if not points:
        return {"samples": 0, "pad": PAD().as_tuple(), "peak": None}
    count = len(points)
    mean = PAD(
        p=sum(point["p"] for point in points) / count,
        a=sum(point["a"] for point in points) / count,
        d=sum(point["d"] for point in points) / count,
    )
    peak = max(points, key=lambda point: abs(point["a"]))
    return {"samples": count, "pad": mean.as_tuple(), "peak": peak}


def data(timeseries: TimeSeriesRepository, *, agent_id: str | None = None, days: int = 7) -> dict:
    """PAD 轴面板统一入口（看板只认这个 dict）。"""
    return {"summary": pad_summary(timeseries, agent_id=agent_id, days=days),
            "series": pad_series(timeseries, agent_id=agent_id, days=days)}
