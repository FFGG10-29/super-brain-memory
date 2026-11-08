"""core.win32_tray —— 纯 ctypes 系统托盘（无 Qt、无第三方依赖）。

Flutter Windows 端没有内置托盘 API，而发行包又不带 PySide6。这里直接在后端
用 Win32 API 建一个隐藏消息窗口 + Shell_NotifyIconW，菜单动作经回调广播给
Flutter（WS `tray_action` 事件），Flutter 再用 user32 显示/隐藏自己的窗口。

线程模型：托盘消息循环独占一个守护线程；`on_action` 在该线程被调用，
回调必须线程安全（V3Daemon.hub.broadcast 是线程安全的）。
非 Windows 平台整体降级为 no-op。
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable

__all__ = ["TrayBridge", "get_tray"]

_IS_WIN = sys.platform == "win32"

WM_APP = 0x8000
_WM_CALLBACK = WM_APP + 1
_WM_QUIT = 0x0012
_WM_LBUTTONDBLCLK = 0x0203
_WM_RBUTTONUP = 0x0205
_NIM_ADD, _NIM_DELETE = 0, 2
_NIF_MESSAGE, _NIF_ICON, _NIF_TIP = 0x01, 0x02, 0x04
_IDI_APPLICATION = 32512
_TPM_RETURNCMD, _TPM_NONOTIFY, _TPM_RIGHTBUTTON = 0x0100, 0x0080, 0x0002
_CLASS_NAME = "SuperBrainTrayHost"
_MENU_ITEMS = (
    (1001, "打开主界面", "open"),
    (1002, "记忆星云", "nebula"),
    (1003, "事件时间轴", "timeline"),
    (1004, "完全退出", "quit"),
)

if _IS_WIN:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _LRESULT = ctypes.c_ssize_t
    # ctypes.wintypes 并没有定义 HCURSOR/HBRUSH/HMENU 等别名；所有 Win32 句柄
    # 在 x64 下都是 void*，统一用 HANDLE 才能避免结构体对齐错位。
    _HANDLE = wintypes.HANDLE
    _WNDPROC = ctypes.WINFUNCTYPE(
        _LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )

    class _POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class _MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND), ("message", wintypes.UINT),
            ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
            ("time", wintypes.DWORD), ("pt", _POINT),
        ]

    class _WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT), ("lpfnWndProc", _WNDPROC),
            ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
            ("hInstance", _HANDLE), ("hIcon", _HANDLE),
            ("hCursor", _HANDLE), ("hbrBackground", _HANDLE),
            ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
        ]

    class _NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT), ("hIcon", _HANDLE),
            ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
            ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_byte * 16),
            ("hBalloonIcon", _HANDLE),
        ]

    _user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    _user32.DefWindowProcW.restype = _LRESULT
    _user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
    _user32.RegisterClassW.restype = wintypes.ATOM
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, _HANDLE, _HANDLE, ctypes.c_void_p,
    ]
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.DestroyWindow.argtypes = [wintypes.HWND]
    _user32.DestroyWindow.restype = wintypes.BOOL
    _user32.GetMessageW.argtypes = [
        ctypes.POINTER(_MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT
    ]
    _user32.GetMessageW.restype = ctypes.c_int
    _user32.TranslateMessage.argtypes = [ctypes.POINTER(_MSG)]
    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(_MSG)]
    _user32.DispatchMessageW.restype = _LRESULT
    _user32.PostThreadMessageW.argtypes = [
        wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    _user32.LoadIconW.argtypes = [_HANDLE, wintypes.LPCWSTR]
    _user32.LoadIconW.restype = _HANDLE

    # 品牌图标：沿用旧 Qt/Python 前端的 ui/desktop/assets/app.ico。
    # 冻结态由 build.ps1 放一份到 backend\app.ico；找不到才退回系统默认图标。
    _IMAGE_ICON = 1
    _LR_LOADFROMFILE = 0x0010
    _LR_DEFAULTSIZE = 0x0040
    _user32.LoadImageW.argtypes = [
        _HANDLE, wintypes.LPCWSTR, wintypes.UINT,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    _user32.LoadImageW.restype = _HANDLE

    def _load_brand_icon() -> _HANDLE:
        exe_dir = Path(sys.executable).resolve().parent
        candidates: list[Path] = []
        override = os.environ.get("SUPER_BRAIN_ICON")
        if override:
            candidates.append(Path(override))
        candidates += [exe_dir / "app.ico", exe_dir.parent / "app.ico"]
        candidates.append(
            Path(__file__).resolve().parents[1] / "ui" / "desktop" / "assets" / "app.ico"
        )
        for path in candidates:
            if not path.is_file():
                continue
            handle = _user32.LoadImageW(
                None, str(path), _IMAGE_ICON, 0, 0,
                _LR_LOADFROMFILE | _LR_DEFAULTSIZE,
            )
            if handle:
                return handle
        return _user32.LoadIconW(
            None, ctypes.cast(ctypes.c_void_p(_IDI_APPLICATION), wintypes.LPCWSTR)
        )
    _user32.CreatePopupMenu.restype = _HANDLE
    _user32.AppendMenuW.argtypes = [
        _HANDLE, wintypes.UINT, wintypes.UINT, wintypes.LPCWSTR
    ]
    _user32.TrackPopupMenu.argtypes = [
        _HANDLE, wintypes.UINT, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, wintypes.HWND, ctypes.c_void_p,
    ]
    _user32.TrackPopupMenu.restype = wintypes.UINT
    _user32.DestroyMenu.argtypes = [_HANDLE]
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _shell32.Shell_NotifyIconW.argtypes = [
        wintypes.DWORD, ctypes.POINTER(_NOTIFYICONDATAW)
    ]
    _shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    # 64 位下 HMODULE 是指针；默认 restype=c_int 会把句柄截断成 32 位。
    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetModuleHandleW.restype = _HANDLE


class TrayBridge:
    """托盘单例；`start()` 可重复调用，`stop()` 可再次 `start()`。"""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._on_action: Callable[[str], None] | None = None
        self._wndproc: Any = None
        self._error = ""

    @property
    def available(self) -> bool:
        return _IS_WIN

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def error(self) -> str:
        return self._error

    def start(self, on_action: Callable[[str], None]) -> dict[str, Any]:
        if not _IS_WIN:
            return {"ok": False, "message": "系统托盘仅支持 Windows"}
        if self.running:
            self._on_action = on_action
            return {"ok": True, "message": "托盘已在运行", "running": True}
        self._on_action = on_action
        self._error = ""
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="super-brain-tray", daemon=True)
        self._thread.start()
        ok = self._ready.wait(timeout=5.0)
        return {"ok": bool(ok), "running": bool(ok),
                "message": "托盘已启用：关闭窗口只隐藏，托盘菜单可完全退出"
                if ok else f"托盘启动失败：{self._error or '超时'}"}

    def stop(self) -> dict[str, Any]:
        if not self.running:
            return {"ok": True, "message": "托盘未运行"}
        if _IS_WIN and self._thread_id:
            _user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
        return {"ok": True, "message": "已发出托盘退出信号"}

    # ── 内部 ────────────────────────────────────────────────────────────
    def _emit(self, action: str) -> None:
        callback = self._on_action
        if callback is not None:
            try:
                callback(action)
            except Exception:  # noqa: BLE001  托盘线程不能被业务异常拖死
                pass

    def _show_menu(self, hwnd: int) -> None:
        try:
            menu = _user32.CreatePopupMenu()
            if not menu:
                return
            for cmd, label, _action in _MENU_ITEMS:
                _user32.AppendMenuW(menu, 0x0000, cmd, label)
            point = _POINT()
            _user32.GetCursorPos(ctypes.byref(point))
            _user32.SetForegroundWindow(hwnd)
            selected = _user32.TrackPopupMenu(
                menu, _TPM_RETURNCMD | _TPM_NONOTIFY | _TPM_RIGHTBUTTON,
                point.x, point.y, 0, hwnd, None,
            )
            _user32.DestroyMenu(menu)
            for cmd, _label, action in _MENU_ITEMS:
                if selected == cmd:
                    self._emit(action)
                    break
        except Exception as exc:  # noqa: BLE001
            self._error = f"菜单失败：{exc}"

    def _run(self) -> None:
        try:
            self._thread_id = int(_kernel32.GetCurrentThreadId())
            self._wndproc = _WNDPROC(self._wnd_proc)

            wc = _WNDCLASSW()
            wc.style = 0
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = _kernel32.GetModuleHandleW(None)
            wc.lpszClassName = _CLASS_NAME
            wc.hIcon = _load_brand_icon()
            _user32.RegisterClassW(ctypes.byref(wc))

            hwnd = _user32.CreateWindowExW(
                0, _CLASS_NAME, "super-brain-tray", 0,
                0, 0, 0, 0, None, None, wc.hInstance, None,
            )
            if not hwnd:
                self._error = f"CreateWindowExW 失败（{ctypes.get_last_error()}）"
                self._ready.set()
                return

            nid = _NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
            nid.hWnd = hwnd
            nid.uID = 1
            nid.uFlags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP
            nid.uCallbackMessage = _WM_CALLBACK
            nid.hIcon = wc.hIcon
            nid.szTip = "超级大脑 · 记忆服务在后台运行"
            if not _shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(nid)):
                self._error = f"Shell_NotifyIconW 失败（{ctypes.get_last_error()}）"
                _user32.DestroyWindow(hwnd)
                self._ready.set()
                return
            self._ready.set()

            msg = _MSG()
            while True:
                result = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result in (0, -1):
                    break
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
            _shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(nid))
            _user32.DestroyWindow(hwnd)
        except Exception as exc:  # noqa: BLE001
            self._error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
        finally:
            self._thread_id = 0

    def _wnd_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        if message == _WM_CALLBACK:
            event = int(lparam) & 0xFFFF
            if event == _WM_LBUTTONDBLCLK:
                self._emit("open")
            elif event == _WM_RBUTTONUP:
                self._show_menu(hwnd)
        return int(_user32.DefWindowProcW(hwnd, message, wparam, lparam))


_TRAY: TrayBridge | None = None


def get_tray() -> TrayBridge:
    global _TRAY
    if _TRAY is None:
        _TRAY = TrayBridge()
    return _TRAY
