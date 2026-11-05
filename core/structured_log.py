"""core.structured_log —— 结构化日志（JSON 行）+ 各路召回命中率埋点。

统一入口：所有层都只调用 log_event()，不各自 print；日志字段稳定，
便于 DuckDB/看板后续做命中率与延迟统计。
"""
from __future__ import annotations

import logging
import sys
import time
from typing import Any

import structlog

__all__ = ["configure_logging", "log_event"]

_CONFIGURED = False


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), stream=sys.stdout)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper(), logging.INFO)),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def log_event(event: str, **fields: Any) -> None:
    configure_logging()
    # structlog 的第一个位置参数就是 event 字段，kwargs 里不能再带同名键
    structlog.get_logger("brain").info(event, event_type=event, ts_ms=int(time.time() * 1000), **fields)
