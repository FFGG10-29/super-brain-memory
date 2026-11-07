"""gateway.bridge_launcher —— 生成「永远有效」的 MCP stdio 启动器（与 UI 无关）。

问题：把 `super-brain-cli.exe` 的绝对路径写进 Agent 的 mcp.json 后，
软件一旦换盘/换目录（B: → D:），那段配置就失效，得手工改。

做法：无头后端每次启动都在 `%LOCALAPPDATA%\\SuperBrain\\bin\\super-brain-mcp.cmd`
重写一个启动器，里面指向**当前这次运行的真实位置**。Agent 的配置只引用这个固定路径。
开发态（venv）指向 `-m gateway.mcp_stdio`，冻结态指向 `super-brain-cli.exe --mcp-stdio`。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["ensure_launcher", "launcher_path"]


def _bin_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "SuperBrain" / "bin"


def launcher_path() -> Path:
    return _bin_dir() / "super-brain-mcp.cmd"


def _launcher_body() -> str:
    if getattr(sys, "frozen", False):
        app_dir = Path(sys.executable).resolve().parent
        cli = app_dir / "super-brain-cli.exe"
        target = cli if cli.is_file() else Path(sys.executable)
        return f'@echo off\r\n"{target}" --mcp-stdio %*\r\n'
    project_root = Path(__file__).resolve().parents[1]
    python = Path(sys.executable)
    return (
        "@echo off\r\n"
        f'pushd "{project_root}"\r\n'
        f'"{python}" -m gateway.mcp_stdio %*\r\n'
        "popd\r\n"
    )


def ensure_launcher() -> Path | None:
    """写入/刷新启动器；失败返回 None（调用方退回直接给绝对路径）。"""
    path = launcher_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = _launcher_body()
        if not path.is_file() or path.read_text(encoding="utf-8", errors="replace") != body:
            path.write_text(body, encoding="utf-8", newline="")
        return path
    except OSError:
        return None
