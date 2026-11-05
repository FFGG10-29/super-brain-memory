"""ui.desktop.app —— 桌面入口：QApplication + 主窗口 + 事件循环。"""
from __future__ import annotations

import sys

from PySide6 import QtWidgets

from ui.desktop.backend import DesktopBackend
from ui.desktop.main_window import MainWindow
from ui.desktop.settings import SettingsStore

__all__ = ["run"]


def run(config=None) -> int:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    backend = DesktopBackend(config)
    window = MainWindow(backend, SettingsStore(backend.config), backend.config)
    window.show()
    return app.exec()
