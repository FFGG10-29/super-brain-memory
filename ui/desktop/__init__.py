"""ui.desktop —— PySide6 桌面前端（星云神经元 / 光脉冲 / 记忆查看 / 安全配置）。

纪律：
- 只通过 `ui.desktop.backend.DesktopBackend`（组合根 + brain 服务层）取数据，
  禁止 import 任何 storage/* 具体实现，禁止直接读写数据库文件。
- 所有动画/配色参数来自 config.UiConfig，不在视图代码里硬编码。
- PAT 只进系统钥匙串或环境变量，永不写入仓库、事件日志或项目 .env。
"""

from __future__ import annotations

__all__ = ["launch"]


def launch(config=None) -> int:
    """桌面入口：`python -m ui.desktop`；返回进程退出码。"""
    from ui.desktop.app import run

    return run(config)
