"""ui.desktop.autostart —— 开机自启（Windows 用户级 Run 键，不写系统目录）。

自启命令行永远带 `--tray`：开机后不弹窗、只驻留托盘，点托盘图标才进前端。
非 Windows 平台全部降级为 no-op（返回 False），不影响主流程。
"""
from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["autostart_command", "is_enabled", "set_enabled"]

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "SuperBrain"
_TRAY_FLAG = "--tray"


def _launch_target() -> tuple[str, str]:
    """返回 (可执行文件, 参数前缀)。冻结态用 exe 自己，开发态用 pythonw -m ui.desktop。"""
    if getattr(sys, "frozen", False):
        return sys.executable, ""
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return str(pythonw if pythonw.is_file() else exe), "-m ui.desktop"


def autostart_command() -> str:
    exe, prefix = _launch_target()
    parts = [f'"{exe}"']
    if prefix:
        parts.append(prefix)
    parts.append(_TRAY_FLAG)
    return " ".join(parts)


def is_enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, _VALUE_NAME)
            return bool(value)
    except OSError:
        return False


def set_enabled(enabled: bool) -> bool:
    """写入/删除自启项；返回是否操作成功（失败不抛，调用方只提示）。"""
    if sys.platform != "win32":
        return False
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, _VALUE_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False
