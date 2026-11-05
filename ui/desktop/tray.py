"""ui.desktop.tray —— 系统托盘驻留：叉掉窗口只隐藏，真正退出走托盘菜单。

设计取舍：
- `QApplication.setQuitOnLastWindowClosed(False)` + 主窗口 closeEvent 改为 hide，
  这样「叉掉」不会结束进程（引擎、HTTP 网关、MCP 接入都还在跑）；
- 托盘菜单给「打开主界面 / 记忆星云 / 事件时间轴 / 完全退出」，双击托盘图标回到前台；
- 图标从 `ui/desktop/assets/app.ico` 读（冻结态在 `_internal/ui/desktop/assets/`）。
"""
from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from core.runtime_paths import is_frozen, resource_root

__all__ = ["app_icon", "install_tray"]

_ASSET_DIR = Path("ui") / "desktop" / "assets"


def _asset(name: str) -> Path:
    if is_frozen():
        return resource_root() / _ASSET_DIR / name
    return Path(__file__).resolve().parent / "assets" / name


def app_icon() -> QtGui.QIcon:
    """应用图标：优先 .ico（多尺寸），退 .png，最后用系统默认。"""
    for name in ("app.ico", "app.png"):
        path = _asset(name)
        if path.is_file():
            return QtGui.QIcon(str(path))
    return QtGui.QIcon()


def install_tray(window, *, on_quit) -> QtWidgets.QSystemTrayIcon | None:
    """创建托盘图标；系统不支持托盘时返回 None（调用方退回普通关窗行为）。"""
    if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
        return None
    tray = QtWidgets.QSystemTrayIcon(app_icon(), window)
    tray.setToolTip("超级大脑 · 记忆服务在后台运行")

    menu = QtWidgets.QMenu(window)

    def _open(page_index: int | None = None) -> None:
        if page_index is not None:
            window.goto_page(page_index)
        window.show_from_tray()

    act_open = menu.addAction("打开主界面")
    act_open.triggered.connect(lambda: _open())
    act_nebula = menu.addAction("记忆星云")
    act_nebula.triggered.connect(lambda: _open(0))
    act_timeline = menu.addAction("事件时间轴")
    act_timeline.triggered.connect(lambda: _open(10))
    menu.addSeparator()
    act_hint = menu.addAction("关闭窗口 = 隐藏到托盘，服务继续运行")
    act_hint.setEnabled(False)
    menu.addSeparator()
    act_quit = menu.addAction("完全退出")
    act_quit.triggered.connect(on_quit)

    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: _open() if reason == QtWidgets.QSystemTrayIcon.ActivationReason.DoubleClick else None
    )
    tray.show()
    return tray


def notify(tray: QtWidgets.QSystemTrayIcon | None, title: str, message: str) -> None:
    if tray is not None:
        tray.showMessage(title, message, app_icon(), 4000)


def app_icon_path() -> str:
    return str(_asset("app.ico"))


def center_on_screen(window) -> None:
    """托盘唤起时把窗口拉回主屏中央（多屏/隐藏后常见跑到屏外）。"""
    screen = QtGui.QGuiApplication.primaryScreen()
    if screen is None:
        return
    geometry = screen.availableGeometry()
    frame = window.frameGeometry()
    frame.moveCenter(geometry.center())
    window.move(frame.topLeft())
    QtCore.QTimer.singleShot(0, window.raise_)
