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
    try:
        backend = DesktopBackend(config)
    except Exception as exc:  # noqa: BLE001  pythonw 无控制台：启动失败必须弹窗说明
        QtWidgets.QMessageBox.critical(
            None,
            "启动失败",
            "内核启动失败：\n\n"
            f"{type(exc).__name__}: {exc}\n\n"
            "常见原因：\n"
            "· 向量维度与现有向量库不一致 —— 到 设置→恢复出厂默认，"
            "或删除 data/vector.lance 后重建；\n"
            "· 上一个实例还没退干净（等几秒重试）。",
        )
        return 1
    window = MainWindow(backend, SettingsStore(backend.config), backend.config)
    window.show()
    return app.exec()
