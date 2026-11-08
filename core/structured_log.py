"""core.structured_log —— 结构化日志（JSON 行）+ 各路召回命中率埋点。

统一入口：所有层都只调用 log_event()，不各自 print；日志字段稳定，
便于 DuckDB/看板后续做命中率与延迟统计。

长时挂机保护：日志同时写一份按 10MB 切割（保留 5 份）的文件，避免
stdout 被重定向后无限增长把 C 盘塞满。structlog 的 PrintLoggerFactory
直接对 stream 调 write()，绕开了 logging handler，所以切割必须在 stream 层做。
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import structlog

__all__ = ["configure_logging", "log_event", "log_file_path"]

_CONFIGURED = False
_MAX_BYTES = 10 * 1024 * 1024   # 单文件 10MB
_BACKUPS = 5                    # 最多 5 份历史，总上限 ~60MB


def log_file_path() -> Path | None:
    """日志目录：环境变量 > data_dir()/logs。解析失败时静默降级为不落盘。"""
    try:
        override = os.environ.get("SUPER_BRAIN_LOG_DIR")
        if override:
            base = Path(override)
        else:
            from core.runtime_paths import data_dir
            base = data_dir() / "logs"
        base.mkdir(parents=True, exist_ok=True)
        return base / "super-brain.log"
    except Exception:  # noqa: BLE001  日志系统本身绝不能拖垮启动
        return None


class _RotatingTee:
    """把每一行日志同时写原 stream 与按大小切割的文件。

    macOS/Linux 同样可用；`isatty`/`encoding` 是为了满足部分库对 file-like 的检查。
    """

    def __init__(self, stream: Any, path: Path) -> None:
        self._stream = stream
        self._path = path
        self._lock = threading.Lock()
        self._fh: Any = None
        self._open()

    def _open(self) -> None:
        self._fh = self._path.open("a", encoding="utf-8", errors="replace")

    def _backup(self, index: int) -> Path:
        return self._path.parent / f"{self._path.name}.{index}"

    def _rotate(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None
        oldest = self._backup(_BACKUPS)
        try:
            if oldest.exists():
                oldest.unlink()
            for index in range(_BACKUPS - 1, 0, -1):
                source = self._backup(index)
                if source.exists():
                    source.rename(self._backup(index + 1))
            if self._path.exists():
                self._path.rename(self._backup(1))
        except OSError:
            pass  # 轮转失败不能丢当前日志
        self._open()

    def write(self, text: str) -> int:
        try:
            self._stream.write(text)
        except Exception:  # noqa: BLE001  原 stream 可能是已关闭的 stdio 管道
            pass
        with self._lock:
            try:
                if self._fh is None:
                    self._open()
                self._fh.write(text)
                self._fh.flush()
                if self._fh.tell() >= _MAX_BYTES:
                    self._rotate()
            except Exception:  # noqa: BLE001
                pass
        return len(text)

    def flush(self) -> None:
        try:
            self._stream.flush()
        except Exception:  # noqa: BLE001
            pass
        with self._lock:
            try:
                if self._fh is not None:
                    self._fh.flush()
            except Exception:  # noqa: BLE001
                pass

    def isatty(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        return "utf-8"


def configure_logging(level: str = "INFO", *, json_output: bool = True, stream=None) -> None:
    """初始化结构化日志。

    stream 默认 stdout；**MCP stdio 服务器必须传 sys.stderr**——stdout 是 JSON-RPC 通道，
    混进一行日志就会让客户端解析失败（表现为「工具调用无响应」）。
    先调用的那次生效（_CONFIGURED 锁），所以 stdio 入口要在 build_app 之前调。
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    target = stream or sys.stdout
    path = log_file_path()
    if path is not None:
        try:
            target = _RotatingTee(target, path)
        except Exception:  # noqa: BLE001  落盘失败不影响控制台日志
            pass
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO), stream=target
    )
    structlog.configure(
        # structlog 默认 PrintLoggerFactory 直接写 stdout，绕开 logging 的 stream 设置；
        # 不显式绑定 file=target，stdio 模式下 JSON 日志会插进 JSON-RPC 通道。
        logger_factory=structlog.PrintLoggerFactory(file=target),
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
