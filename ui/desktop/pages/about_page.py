"""ui.desktop.pages.about_page —— 关于 / 快捷键 / 架构速览。"""
from __future__ import annotations

import platform

from PySide6 import QtCore, QtWidgets

from ui.desktop.widgets import Card

__all__ = ["AboutPage"]


class AboutPage(QtWidgets.QWidget):
    def __init__(self) -> None:
        super().__init__()
        brand = QtWidgets.QLabel("超级大脑 v2.1")
        brand.setObjectName("brand")
        subtitle = QtWidgets.QLabel("生物启发式 AI 记忆系统 · PySide6 桌面端")
        subtitle.setObjectName("hint")

        engines = Card("六引擎基座")
        engines.body.addWidget(
            _text(
                "SQLite WAL + FTS5（关系/全文） · LanceDB（向量 1024 维） · Kùzu（图谱）\n"
                "LMDB（KV：情绪/丘脑/注册中心） · DuckDB（时序，append-only） · HyperNetX（超图）"
            )
        )

        shortcuts = Card("快捷键与操作")
        shortcuts.body.addWidget(
            _text(
                "滚轮：缩放星云（0.45x ~ 3.0x）\n"
                "拖拽：平移画布；「返回全景」重置缩放\n"
                "双击记忆 / 召回结果：在星云中定位并高亮\n"
                "顶栏搜索回车：召回 + 从查询中心齐射光脉冲\n"
                "Ctrl+F：聚焦搜索框；F11：全屏预览；Ctrl+R：刷新当前页"
            )
        )

        security = Card("安全与边界")
        security.body.addWidget(
            _text(
                "PAT 只进系统钥匙串或 GITHUB_PAT 环境变量，不写仓库/事件日志/界面明文。\n"
                "SYNC_REPO_URL 与端口等偏好进用户级 QSettings（%APPDATA%\\super-brain）。\n"
                "UI 只通过 DesktopBackend 调用 brain 服务层，不直接读写数据库文件。"
            )
        )

        runtime = Card("运行环境")
        from PySide6 import __version__ as pyside_version  # noqa: PLC0415

        runtime.body.addWidget(
            _text(
                f"Python {platform.python_version()} · PySide6 {pyside_version} · "
                f"Qt {QtCore.qVersion()} · {platform.system()} {platform.release()}"
            )
        )

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 12)
        layout.addWidget(brand)
        layout.addWidget(subtitle)
        layout.addSpacing(6)
        for card in (engines, shortcuts, security, runtime):
            layout.addWidget(card)
        layout.addStretch(1)


def _text(value: str) -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(value)
    label.setWordWrap(True)
    label.setObjectName("hint")
    label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
    return label
