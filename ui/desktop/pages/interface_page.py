"""ui.desktop.pages.interface_page —— 接口与端口：HTTP 适配器一键启停 + MCP 工具清单。

端口/主机保存到用户级 QSettings（不进仓库），下次启动自动带出；
启动是真实绑定 socket，端口被占用会明确报错。
"""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from ui.desktop.backend import DesktopBackend
from ui.desktop.settings import SettingsStore
from ui.desktop.widgets import Card

__all__ = ["InterfacePage"]


class InterfacePage(QtWidgets.QWidget):
    def __init__(self, backend: DesktopBackend, store: SettingsStore) -> None:
        super().__init__()
        self._backend = backend
        self._store = store
        prefs = store.load_prefs()

        self._host = QtWidgets.QLineEdit(str(prefs["http_host"]))
        self._host.setPlaceholderText("127.0.0.1")
        self._port = QtWidgets.QSpinBox()
        self._port.setRange(1, 65535)
        self._port.setValue(int(prefs["http_port"]))
        self._model = QtWidgets.QLineEdit(str(prefs["model_name"]))

        form = QtWidgets.QFormLayout()
        form.addRow("监听地址", self._host)
        form.addRow("端口", self._port)
        form.addRow("模型名（OpenAI 兼容）", self._model)

        self._start = QtWidgets.QPushButton("启动 HTTP 服务")
        self._start.setObjectName("primary")
        self._start.clicked.connect(self.start_service)
        self._stop = QtWidgets.QPushButton("停止")
        self._stop.setObjectName("danger")
        self._stop.clicked.connect(self.stop_service)
        self._stop.setEnabled(False)
        self._health = QtWidgets.QPushButton("浏览器打开 /health")
        self._health.clicked.connect(self.open_health)
        self._health.setEnabled(False)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self._start)
        buttons.addWidget(self._stop)
        buttons.addWidget(self._health)
        buttons.addStretch(1)

        self._state = QtWidgets.QLabel("未启动")
        self._state.setObjectName("hint")
        self._endpoint = QtWidgets.QLabel("")
        self._endpoint.setObjectName("accent")
        self._endpoint.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)

        self._curl = QtWidgets.QTextEdit()
        self._curl.setReadOnly(True)
        self._curl.setMaximumHeight(150)
        self._curl.setStyleSheet("font-family: Consolas, 'Cascadia Mono', monospace; font-size: 11px;")

        http_card = Card("HTTP 适配器（OpenAI 兼容 / 记忆插件友好）")
        http_card.body.addLayout(form)
        http_card.body.addLayout(buttons)
        http_card.body.addWidget(self._state)
        http_card.body.addWidget(self._endpoint)
        http_card.body.addWidget(self._curl)

        self._tools = QtWidgets.QTableWidget(0, 2)
        self._tools.setHorizontalHeaderLabels(("MCP 工具", "说明"))
        self._tools.verticalHeader().setVisible(False)
        self._tools.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tools.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self._tools.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        tools = backend.mcp_tools()
        self._tools.setRowCount(len(tools))
        for row, tool in enumerate(tools):
            self._tools.setItem(row, 0, QtWidgets.QTableWidgetItem(tool["name"]))
            self._tools.setItem(row, 1, QtWidgets.QTableWidgetItem(tool["desc"]))
        self._tools.resizeColumnsToContents()
        self._tools.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)

        tools_card = Card("MCP 工具（token 决定 agent 与命名空间，客户端不能自报）")
        tools_card.body.addWidget(self._tools)

        self._credentials = QtWidgets.QLabel("未启动：启动后显示 agent_id 与令牌掩码")
        self._credentials.setObjectName("hint")
        self._credentials.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        cred_card = Card("客户端接入串")
        cred_card.body.addWidget(self._credentials)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 12)
        layout.addWidget(http_card)
        layout.addWidget(tools_card, 1)
        layout.addWidget(cred_card)
        self._refresh_ui()

    # ── 启停 ──────────────────────────────────────────────────────────────
    def start_service(self) -> None:
        host = self._host.text().strip() or "127.0.0.1"
        port = int(self._port.value())
        result = self._backend.start_http(host, port)
        if result.get("ok"):
            self._store.save_prefs(http_host=host, http_port=port, model_name=self._model.text().strip())
            self._state.setText(f"运行中 · {result['message']}")
            self._state.setObjectName("ok")
        else:
            self._state.setText(f"启动失败 · {result.get('message', '')}")
            self._state.setObjectName("danger")
        self._state.style().unpolish(self._state)
        self._state.style().polish(self._state)
        self._refresh_ui()

    def stop_service(self) -> None:
        result = self._backend.stop_http()
        self._state.setText(result.get("message", "已停止"))
        self._state.setObjectName("hint")
        self._state.style().unpolish(self._state)
        self._state.style().polish(self._state)
        self._refresh_ui()

    def open_health(self) -> None:
        status = self._backend.gateway_status()
        if status["running"]:
            QtGui.QDesktopServices.openUrl(QtGui.QUrl(f"http://{status['host']}:{status['port']}/health"))

    # ── 视图刷新 ──────────────────────────────────────────────────────────
    def _refresh_ui(self) -> None:
        status = self._backend.gateway_status()
        running = bool(status["running"])
        self._start.setEnabled(not running)
        self._stop.setEnabled(running)
        self._health.setEnabled(running)
        self._host.setEnabled(not running)
        self._port.setEnabled(not running)
        if running:
            base = f"http://{status['host']}:{status['port']}"
            self._endpoint.setText(f"GET {base}/health · POST {base}/search · POST {base}/v1/chat/completions")
            self._credentials.setText(
                f"agent_id = {status['agent_id']} · token = {status['token_hint']}（完整令牌见返回体/注册中心）"
            )
            model = self._model.text().strip() or "super-brain-memory"
            self._curl.setPlainText(
                f"curl {base}/health\n\n"
                f'curl -X POST {base}/search -H "Authorization: Bearer <token>" '
                f'-H "Content-Type: application/json" -d \'{{"query":"发版冲突"}}\'\n\n'
                f'curl -X POST {base}/v1/chat/completions -H "Authorization: Bearer <token>" '
                f'-H "Content-Type: application/json" '
                f'-d \'{{"model":"{model}","messages":[{{"role":"user","content":"上次发版为什么冲突？"}}]}}\''
            )
        else:
            self._endpoint.setText("")
            self._curl.setPlainText("# 启动服务后这里会给出可直接复制运行的 curl 示例")
