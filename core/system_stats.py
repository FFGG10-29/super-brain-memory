"""core.system_stats —— CPU / 内存 / 显卡实时采样（与 UI 框架无关）。

无头后端与 Flutter 顶栏仪表共用。后端不依赖任何 UI 框架模块，
UI 能力全部落在 core.*，发行包无 GUI。

- CPU / 内存：psutil（缺失/受限时优雅降级为 None，前端显示「—」）
- 显卡：`nvidia-smi`（没有 N 卡或没装驱动时返回 None）
采样在守护线程里每 2 秒跑一次，RPC 只读缓存，绝不在请求路径上跑子进程。
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
from typing import Any

__all__ = ["SystemStats", "get_system_stats"]

_INTERVAL = 2.0


def _read_gpu() -> dict[str, Any] | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
            creationflags=0x08000000 if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
    except (OSError, subprocess.SubprocessError, TypeError):
        return None
    lines = (out.stdout or "").strip().splitlines()
    if not lines:
        return None
    parts = [part.strip() for part in lines[0].split(",")]
    if len(parts) < 5:
        return None
    name, util, used, total, temp = parts[:5]
    try:
        return {
            "name": name,
            "util_percent": float(util),
            "memory_used_mb": float(used),
            "memory_total_mb": float(total),
            "memory_percent": (float(used) / float(total) * 100.0) if float(total) else 0.0,
            "temperature_c": float(temp),
        }
    except ValueError:
        return None


class SystemStats:
    """单例采样器：`latest()` 永不阻塞，返回最近一次采样结果。"""

    def __init__(self, interval: float = _INTERVAL) -> None:
        self._interval = interval
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {
            "cpu_percent": None, "memory_percent": None,
            "memory_used_gb": None, "memory_total_gb": None,
            "gpu": None, "ts_ms": 0, "sampling": False,
        }
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="system-stats", daemon=True)
        self._thread.start()

    def _sample(self) -> dict[str, Any]:
        data: dict[str, Any] = {"ts_ms": int(time.time() * 1000), "sampling": True}
        try:
            import psutil

            data["cpu_percent"] = float(psutil.cpu_percent(interval=None))
            memory = psutil.virtual_memory()
            data["memory_percent"] = float(memory.percent)
            data["memory_used_gb"] = round(memory.used / 1024 ** 3, 1)
            data["memory_total_gb"] = round(memory.total / 1024 ** 3, 1)
        except Exception:  # noqa: BLE001  psutil 缺失/受限时保持 None
            data["cpu_percent"] = None
            data["memory_percent"] = None
        data["gpu"] = _read_gpu()
        return data

    def _loop(self) -> None:
        # 首次先 sleep：psutil.cpu_percent(None) 的第一次调用恒为 0.0
        time.sleep(0.3)
        while not self._stop.is_set():
            try:
                snapshot = self._sample()
            except Exception:  # noqa: BLE001  采样线程不能因为一次异常退出
                snapshot = dict(self._data)
            with self._lock:
                self._data = snapshot
            self._stop.wait(self._interval)

    def latest(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def stop(self) -> None:
        self._stop.set()


_STATS: SystemStats | None = None


def get_system_stats() -> SystemStats:
    global _STATS
    if _STATS is None:
        _STATS = SystemStats()
    return _STATS
