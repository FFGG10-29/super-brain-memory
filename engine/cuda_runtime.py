"""engine.cuda_runtime —— CUDA 运行库发现与登记（开发态 / PyInstaller 冻结态通用）。

onnxruntime-gpu 的 CUDA provider 运行时用 LoadLibrary 找 cudnn64_9.dll 及子库，
llama-cpp-python 的 CUDA 轮子需要 cudart64_12 / cublas64_12；这些目录必须进入
PATH / DLL 搜索列表，否则会静默回落 CPU 或加载失败。

开发态从 venv 的 site-packages 找；冻结态从 sys._MEIPASS（onedir 的 _internal）、
exe 同目录、以及打包脚本归集的 cuda_bin/ 找。找不到时调用方自动降级，不拒绝启动。
"""
from __future__ import annotations

import ctypes
import os
import sys
import sysconfig
from pathlib import Path

from core.runtime_paths import is_frozen, project_root, resource_root

__all__ = [
    "cuda_dll_dirs", "cuda_driver_version", "cuda_ready",
    "cuda_runtime_status", "prepare_cuda_dll_path",
]

_prepared = False

# 只要目录里出现其中任一个，就认为它是 CUDA 运行库目录
_CUDA_DLL_MARKERS = (
    "cudnn64_9.dll", "cublas64_12.dll", "cublasLt64_12.dll", "cudart64_12.dll",
    "cufft64_11.dll", "ggml-cuda.dll", "onnxruntime_providers_cuda.dll",
)
# GPU 三通道真正需要的最小集合
_REQUIRED_DLLS = (
    "cudart64_12.dll", "cublas64_12.dll", "cublasLt64_12.dll",
    "cudnn64_9.dll", "cufft64_11.dll", "ggml-cuda.dll",
)
_MIN_CUDA_API = (12, 4)  # cu124 轮子；对应 NVIDIA 驱动 >= 550


def _candidate_roots() -> list[Path]:
    roots: list[Path] = []
    override = os.environ.get("SUPER_BRAIN_CUDA_DIR")
    if override:
        roots.append(Path(override))
    if is_frozen():
        roots += [resource_root(), project_root(), resource_root() / "cuda_bin"]
    else:
        roots.append(Path(sysconfig.get_paths().get("purelib", "")))
    return roots


def cuda_dll_dirs() -> list[str]:
    """返回实际含 CUDA 运行库的目录；非 Windows 恒为空。"""
    if sys.platform != "win32":
        return []
    found: list[str] = []
    for root in _candidate_roots():
        candidates = [
            root,
            root / "cuda_bin",
            root / "llama_cpp" / "lib",
            root / "onnxruntime" / "capi",
        ]
        nvidia = root / "nvidia"
        if nvidia.is_dir():
            candidates.extend(p / "bin" for p in sorted(nvidia.iterdir()) if (p / "bin").is_dir())
        for path in candidates:
            if not path.is_dir():
                continue
            if not any((path / name).is_file() for name in _CUDA_DLL_MARKERS):
                continue
            value = str(path)
            if value not in found:
                found.append(value)
    return found


def cuda_driver_version() -> str | None:
    """驱动支持的最高 CUDA 版本（如 "13.0"）；无 N 卡 / 非 Windows 返回 None。"""
    if sys.platform != "win32":
        return None
    try:
        nvcuda = ctypes.WinDLL("nvcuda.dll")
        version = ctypes.c_int()
        if nvcuda.cuDriverGetVersion(ctypes.byref(version)) != 0:
            return None
        return f"{version.value // 1000}.{(version.value % 1000) // 10}"
    except Exception:  # noqa: BLE001
        return None


def _driver_sufficient(version: str | None) -> bool:
    if not version:
        return False
    try:
        major, minor = (int(part) for part in version.split(".")[:2])
    except (ValueError, TypeError):
        return False
    return (major, minor) >= _MIN_CUDA_API


def cuda_runtime_status() -> dict[str, object]:
    """打包后自检用：DLL 是否齐、驱动是否够新。"""
    dirs = [Path(item) for item in cuda_dll_dirs()]
    present = {name: any((path / name).is_file() for path in dirs) for name in _REQUIRED_DLLS}
    version = cuda_driver_version()
    return {
        "dll_dirs": [str(path) for path in dirs],
        "dll_present": present,
        "dll_complete": all(present.values()),
        "cuda_driver_api": version,
        "driver_sufficient": _driver_sufficient(version),
    }


def cuda_ready() -> bool:
    status = cuda_runtime_status()
    return bool(status["dll_complete"] and status["driver_sufficient"])


def prepare_cuda_dll_path() -> list[str]:
    """登记 CUDA DLL 目录；返回本次加入的目录列表（已准备过则返回空列表）。"""
    global _prepared
    if _prepared or sys.platform != "win32":
        return []
    _prepared = True
    dirs = cuda_dll_dirs()
    add = getattr(os, "add_dll_directory", None)
    for directory in dirs:
        if add is not None:
            try:
                add(directory)
            except OSError:
                pass
    if dirs:
        os.environ["PATH"] = os.pathsep.join(dirs) + os.pathsep + os.environ.get("PATH", "")
    return dirs
