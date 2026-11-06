"""v3.supervisor —— Windows 进程监管：Job Object 防孤儿 + 指数退避重启 + 熔断。

SCM（WinSW/NSSM）负责 Daemon 自身；Daemon 用本类管 Model Worker。
非 Windows 平台退化为普通 subprocess（便于在显卡机 Linux 上复用）。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

__all__ = ["ProcessSupervisor", "SpawnSpec"]


@dataclass
class SpawnSpec:
    name: str
    args: list[str]
    env: dict[str, str] | None = None
    cwd: str | None = None


class ProcessSupervisor:
    def __init__(self, *, max_restarts: int = 8, base_backoff_s: float = 0.5) -> None:
        self._specs: dict[str, SpawnSpec] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._restarts: dict[str, int] = {}
        self._stop = threading.Event()
        self._max_restarts = max_restarts
        self._base_backoff = base_backoff_s
        self._job: Any = None
        self._thread: threading.Thread | None = None
        self._init_job_object()

    def _init_job_object(self) -> None:
        if os.name != "nt":
            return
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            self._job = kernel32.CreateJobObjectW(None, None)
            if not self._job:
                return
            # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
            class JobObjectExtendedLimit(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", ctypes.c_ubyte * 144),
                    ("IoInfo", ctypes.c_ubyte * 48),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            info = JobObjectExtendedLimit()
            kernel32.QueryInformationJobObject(
                self._job, 9, ctypes.byref(info), ctypes.sizeof(info), None
            )
            # 直接设置 LimitFlags 字段（结构偏移 144+48 处的 DWORD + 后续），用简易写入
            raw = bytearray(ctypes.sizeof(info))
            ctypes.memmove(raw, ctypes.byref(info), len(raw))
            flags_offset = 144 + 48 + ctypes.sizeof(ctypes.c_size_t) * 4
            ctypes.memset(ctypes.byref(info), 0, ctypes.sizeof(info))
            # 0x2000 写入 BasicLimitInformation.LimitFlags（偏移 0 的 DWORD 之后：结构内部第 2 个字段）
            ctypes.cast(ctypes.byref(info), ctypes.POINTER(ctypes.c_uint32))[4] = 0x2000
            kernel32.SetInformationJobObject(self._job, 9, ctypes.byref(info), ctypes.sizeof(info))
            kernel32.AssignProcessToJobObject(self._job, kernel32.GetCurrentProcess())
        except Exception:  # noqa: BLE001  Job Object 是加固项，失败不影响主流程
            self._job = None

    def register(self, spec: SpawnSpec) -> None:
        self._specs[spec.name] = spec
        self._restarts.setdefault(spec.name, 0)

    def start(self) -> None:
        for name in self._specs:
            self._spawn(name)
        self._thread = threading.Thread(target=self._monitor, name="v3-supervisor", daemon=True)
        self._thread.start()

    def _spawn(self, name: str) -> None:
        spec = self._specs[name]
        env = {**os.environ, **(spec.env or {})}
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        self._procs[name] = subprocess.Popen(
            [sys.executable, *spec.args], env=env, cwd=spec.cwd,
            creationflags=creationflags,
        )
        if self._job:
            try:
                ctypes.windll.kernel32.AssignProcessToJobObject(  # type: ignore[attr-defined]
                    self._job, int(self._procs[name]._handle)  # noqa: SLF001
                )
            except Exception:  # noqa: BLE001
                pass

    def _monitor(self) -> None:
        while not self._stop.wait(0.5):
            for name, proc in list(self._procs.items()):
                if proc.poll() is None:
                    continue
                if self._restarts[name] >= self._max_restarts:
                    continue  # 熔断：连续崩溃不再刷屏
                backoff = self._base_backoff * (2 ** min(self._restarts[name], 5))
                self._restarts[name] += 1
                self._stop.wait(backoff)
                if not self._stop.is_set():
                    self._spawn(name)

    def stop(self) -> None:
        self._stop.set()
        for proc in self._procs.values():
            if proc.poll() is None:
                proc.terminate()
        if self._job:
            try:
                ctypes.windll.kernel32.CloseHandle(self._job)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
