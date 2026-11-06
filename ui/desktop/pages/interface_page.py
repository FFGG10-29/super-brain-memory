"""ui.desktop.pages.interface_page —— 接口与端口：HTTP 适配器一键启停 + MCP 工具清单。

端口/主机保存到用户级 QSettings（不进仓库），下次启动自动带出；
启动是真实绑定 socket，端口被占用会明确报错。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from ui.desktop.backend import DesktopBackend
from ui.desktop.settings import SettingsStore
from ui.desktop.widgets import Card

__all__ = ["InterfacePage", "mcp_command", "mcp_json_text"]


def bridge_token_text() -> str:
    """读出本机桥接令牌全文（桌面内核启动 HTTP 时写入 mcp_bridge.json）。"""
    from core.runtime_paths import read_local_token

    try:
        from core.runtime_paths import local_bridge_path

        payload = json.loads(local_bridge_path().read_text(encoding="utf-8"))
        token = str(payload.get("token") or "")
        if token:
            return token
    except (OSError, ValueError):
        pass
    # 服务没开也要能复制：令牌是持久化的，与运行状态无关
    return read_local_token()


def mcp_command() -> tuple[str, list[str]]:
    """MCP 启动命令：优先用自更新的固定路径启动器。

    `%LOCALAPPDATA%\\SuperBrain\\bin\\super-brain-mcp.cmd` 每次启动桌面端都会按当前安装位置重写，
    所以这段配置在软件换盘/换目录（B: → D:）后依然有效，Agent 侧不用改。
    只有启动器写不出来时才退回绝对路径。
    """
    from ui.desktop.bridge_launcher import ensure_launcher

    launcher = ensure_launcher()
    if launcher is not None:
        # .cmd 不能直接 CreateProcess，必须经 cmd.exe 拉起（MCP 客户端普遍这么处理）
        cmd_exe = os.environ.get("COMSPEC") or r"C:\Windows\System32\cmd.exe"
        return cmd_exe, ["/c", str(launcher)]
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        cli = exe_dir / "super-brain-cli.exe"
        return str(cli if cli.is_file() else sys.executable), ["--mcp-stdio"]
    return str(Path(sys.executable)), ["-m", "gateway.mcp_stdio"]


def mcp_json_text(mode: str = "http") -> str:
    """标准 MCP 配置片段：直接粘进 Agent 的「添加 MCP 连接 → 导入 JSON」。

    http 模式：Streamable HTTP，一个 URL + Bearer 令牌，跨语言客户端零依赖；
    stdio 模式：本机启动器，不需要令牌（进程即信任）。
    """
    if mode == "stdio":
        command, args = mcp_command()
        server: dict[str, object] = {"type": "stdio", "command": command}
        if args:
            server["args"] = args
        return json.dumps({"mcpServers": {"super-brain-memory": server}}, ensure_ascii=False, indent=2)

    from config import get_config

    gateway = get_config().gateway
    host = gateway.http_host if gateway.http_host not in ("", "0.0.0.0", "::") else "127.0.0.1"
    url = f"http://{host}:{gateway.http_port}/mcp"
    return json.dumps(
        {
            "mcpServers": {
                "super-brain-memory": {
                    "type": "streamable-http",
                    "url": url,
                    # 本机桥接的鉴权头：X-Super-Brain-Bridge（服务端也兼容 Authorization: Bearer）
                    "headers": {"X-Super-Brain-Bridge": bridge_token_text()},
                }
            }
        },
        ensure_ascii=False,
        indent=2,
    )


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
        self._copy_token = QtWidgets.QPushButton("复制桥接令牌")
        self._copy_token.setToolTip("复制完整令牌：给需要直接调 HTTP /mcp/call 的 Agent 用")
        self._copy_token.clicked.connect(self.copy_bridge_token)
        self._health.setEnabled(False)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self._start)
        buttons.addWidget(self._stop)
        buttons.addWidget(self._health)
        buttons.addWidget(self._copy_token)
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

        http_card = Card("HTTP 适配器（随软件自动启动 · OpenAI 兼容 / MCP 桥接）")
        auto_note = QtWidgets.QLabel(
            "默认在启动时就开好（设置 → 网关 →「启动即开 HTTP」），不需要手动点。"
            "它是所有 Agent 的**唯一入口**：请求在这里排队，交给丘脑逐条裁决。"
        )
        auto_note.setObjectName("hint")
        auto_note.setWordWrap(True)
        http_card.body.addWidget(auto_note)
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

        tools_card = Card("MCP 工具（所有 Agent 共享同一份记忆；agent_id 仅用于审计与限流）")
        tools_card.body.addWidget(self._tools)

        self._credentials = QtWidgets.QLabel("未启动：启动后显示 agent_id 与令牌掩码")
        self._credentials.setObjectName("hint")
        self._credentials.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        cred_card = Card("客户端接入串")
        cred_card.body.addWidget(self._credentials)

        # ── 一键复制给 Agent（MCP 配置片段 + 一段说明）────────────────────
        self._mode = QtWidgets.QComboBox()
        self._mode.addItem("streamable-http（URL + 令牌，推荐）", "http")
        self._mode.addItem("stdio（本机启动器，不需要令牌）", "stdio")
        self._mode.currentIndexChanged.connect(lambda _index: self._refresh_snippet())

        self._agent_snippet = QtWidgets.QTextEdit()
        self._agent_snippet.setReadOnly(True)
        self._agent_snippet.setPlainText(mcp_json_text("http"))
        self._agent_snippet.setMinimumHeight(190)
        self._agent_snippet.setStyleSheet(
            "font-family: Consolas, 'Cascadia Mono', monospace; font-size: 11px;"
        )
        copy_btn = QtWidgets.QPushButton("复制 MCP JSON")
        copy_btn.setObjectName("primary")
        copy_btn.setToolTip("复制标准 mcpServers 片段，粘贴到 Agent 的「添加 MCP 连接 → 导入 JSON」")
        copy_btn.clicked.connect(self.copy_agent_config)
        self._copy_hint = QtWidgets.QLabel("")
        self._copy_hint.setObjectName("hint")
        copy_row = QtWidgets.QHBoxLayout()
        copy_row.addWidget(self._mode, 1)
        copy_row.addWidget(copy_btn)
        copy_row.addWidget(self._copy_hint, 1)

        agent_card = Card("复制给 Agent —— 标准 MCP JSON（粘贴即用）")
        agent_note = QtWidgets.QLabel(
            "把下面的 JSON 粘到 Agent 的「添加 MCP 连接 → 导入 JSON」即可。"
            "streamable-http 走 http://127.0.0.1:8766/mcp（令牌已内置）；stdio 走本机启动器。"
            "两种方式都是**同一份记忆**：所有 Agent 共享，写入统一由丘脑准入、入口排队执行。"
        )
        agent_note.setObjectName("hint")
        agent_note.setWordWrap(True)
        agent_card.body.addWidget(agent_note)
        agent_card.body.addWidget(self._agent_snippet)
        agent_card.body.addLayout(copy_row)

        # 内容必须可滚动：MCP 工具表 + 复制卡片叠起来的最小高度会把主窗口顶到 900+，
        # 笔记本（768/864 高）上标题栏直接跑到屏幕外。滚动后页面最小高度只受视口约束。
        content = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(18, 16, 18, 12)
        layout.addWidget(http_card)
        layout.addWidget(agent_card)
        layout.addWidget(tools_card, 1)
        layout.addWidget(cred_card)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setWidget(content)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        self._refresh_ui()
        # 服务启停 / 令牌变化后自动刷新 JSON，避免复制到过期配置
        self._token_timer = QtCore.QTimer(self)
        self._token_timer.timeout.connect(self._refresh_ui)
        self._token_timer.start(5000)

    # ── 复制接入配置 ──────────────────────────────────────────────────────
    def _refresh_snippet(self) -> None:
        mode = str(self._mode.currentData() or "http")
        self._agent_snippet.setPlainText(mcp_json_text(mode))

    def copy_agent_config(self) -> None:
        self._refresh_snippet()
        QtGui.QGuiApplication.clipboard().setText(self._agent_snippet.toPlainText())
        if self._mode.currentData() == "http" and not bridge_token_text():
            self._copy_hint.setText("已复制，但令牌为空：先启动 HTTP 服务再复制")
            return
        self._copy_hint.setText("已复制：粘到 Agent 的「添加 MCP 连接 → 导入 JSON」即可")

    def copy_bridge_token(self) -> None:
        token = bridge_token_text()
        if not token:
            self._copy_hint.setText("令牌尚未生成：HTTP 服务启动后才有")
            return
        QtGui.QGuiApplication.clipboard().setText(token)
        self._copy_hint.setText("已复制完整桥接令牌（调 /mcp/call 时放 X-Super-Brain-Bridge 头）")

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
            token = bridge_token_text()
            self._credentials.setText(
                f"agent_id = {status['agent_id']}\n"
                f"桥接令牌（完整，可直接复制）：{token or '（尚未生成）'}\n"
                f"入口排队 {status.get('queue_depth', 0)} 条 · POST /mcp/call 时放 "
                f"X-Super-Brain-Bridge 头（或 Authorization: Bearer <token>）"
            )
            self._copy_token.setEnabled(bool(token))
            self._refresh_snippet()
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
            self._credentials.setText("服务未运行：默认应随软件启动；检查 设置 → 网关 →「启动即开 HTTP」")
            self._copy_token.setEnabled(False)
            self._curl.setPlainText("# 启动服务后这里会给出可直接复制运行的 curl 示例")
