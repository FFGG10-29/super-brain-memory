"""ui.desktop.app —— 桌面入口：单实例 + 托盘驻留 + 事件循环。

行为约定（v2.2）：
- **单实例**：再次双击 exe 不会起第二个进程（Kùzu 单写锁会直接报错），
  而是通过 QLocalSocket 把已有窗口唤到前台；
- **托盘驻留**：`QApplication.setQuitOnLastWindowClosed(False)`，叉掉窗口只 hide，
  引擎 / HTTP 网关 / MCP 桥接继续在线；真正退出走托盘菜单「完全退出」；
- **`--tray`**：开机自启用，启动后不弹窗、直接驻留托盘；
- **HTTP 自启**：默认在后台起 HTTP 适配器，供多个 Agent 的 MCP stdio 进程转发接入
  （Kùzu 只允许一个进程开库，所以 stdio 必须当瘦客户端）。
"""
from __future__ import annotations

import sys

from PySide6 import QtCore, QtNetwork, QtWidgets

from ui.desktop.backend import DesktopBackend
from ui.desktop.main_window import MainWindow
from ui.desktop.settings import SettingsStore
from ui.desktop.tray import app_icon, install_tray, notify

__all__ = ["run"]

_SERVER_NAME = "super-brain_memory_console_v21"


def _forward_to_existing() -> bool:
    """已有实例在跑：把它唤到前台并返回 True（本进程直接退出）。"""
    socket = QtNetwork.QLocalSocket()
    socket.connectToServer(_SERVER_NAME, QtCore.QIODevice.OpenModeFlag.WriteOnly)
    if not socket.waitForConnected(400):
        return False
    socket.write(b"show\n")
    socket.flush()
    socket.waitForBytesWritten(400)
    socket.disconnectFromServer()
    return True


def _listen_single_instance(window_getter) -> QtNetwork.QLocalServer:
    QtNetwork.QLocalServer.removeServer(_SERVER_NAME)
    server = QtNetwork.QLocalServer()
    server.listen(_SERVER_NAME)

    def _on_connection() -> None:
        connection = server.nextPendingConnection()
        if connection is None:
            return
        connection.readyRead.connect(lambda: connection.readAll())
        connection.disconnected.connect(connection.deleteLater)
        window = window_getter()
        if window is not None:
            window.show_from_tray()

    server.newConnection.connect(_on_connection)
    return server


def _start_http(backend: DesktopBackend) -> dict[str, object] | None:
    gateway = backend.config.gateway
    if not getattr(gateway, "autostart_http", True):
        return None
    result = backend.start_http(gateway.http_host, gateway.http_port)
    if not result.get("ok"):
        # 端口被占用不是致命错误：GUI 仍可用，只是 MCP 桥接不可用
        print(f"[super-brain] HTTP 网关未启动：{result.get('message')}", file=sys.stderr)
    return result


def run(config=None) -> int:
    args = sys.argv[1:]
    start_hidden = "--tray" in args or "--minimized" in args

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    icon = app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)
    # 叉掉窗口不退出进程：引擎与 MCP 接入要留在后台
    app.setQuitOnLastWindowClosed(False)

    if _forward_to_existing():
        return 0

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

    # 启动即刷新 MCP 启动器：Agent 的配置引用固定路径，软件搬家后自动指向新位置
    try:
        from ui.desktop.bridge_launcher import ensure_launcher

        ensure_launcher()
    except Exception as exc:  # noqa: BLE001  启动器失败不影响主流程
        print(f"[super-brain] MCP 启动器写入失败：{exc}", file=sys.stderr)

    window = MainWindow(backend, SettingsStore(backend.config), backend.config)
    tray = install_tray(window, on_quit=window.quit_app)
    window.attach_tray(tray)

    holder: dict[str, MainWindow | None] = {"window": window}
    # 保持引用，否则 QLocalServer 会被 GC 回收
    local_server = _listen_single_instance(lambda: holder["window"])
    app.aboutToQuit.connect(lambda: local_server.close())

    if start_hidden and tray is not None:
        notify(tray, "超级大脑已驻留后台", "双击托盘图标进入控制台；右键可完全退出。")
    else:
        window.show()

    _start_http(backend)
    return app.exec()
