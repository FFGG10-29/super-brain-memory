"""ui.desktop —— PySide6 桌面前端（LivingMemory 风格星云 + 全功能控制台）。

结构：
    main_window.py     外壳：左侧导航 + 顶栏 + 九页 + 状态栏
    pages/             星云视图 / 记忆管理 / 召回测试 / 系统概览 / 展示数据
                       接口与端口 / 同步与备份 / 设置 / 关于
    neurons.py         神经元节点 + Lerp 光脉冲 + 标签 + 星尘 + 迷你地图
    backend.py         服务层桥接：图谱、召回、HTTP 端口启停、同步、快照、导出
    settings.py        PAT（钥匙串）/ 仓库地址 / 端口 / 界面偏好（QSettings）
    theme.py           统一 QSS
    widgets.py         Card / KpiCard / MiniMap

纪律：
- 只通过 `DesktopBackend`（组合根 + brain 服务层）取数据，禁止 import storage/* 实现。
- 动画/配色/阈值全部来自 config.UiConfig / ActivationConfig。
- PAT 只进系统钥匙串或 GITHUB_PAT，永不写入仓库、事件日志或界面明文。
"""

from __future__ import annotations

__all__ = ["launch"]


def launch(config=None) -> int:
    """桌面入口：`python -m ui.desktop`；返回进程退出码。"""
    from ui.desktop.app import run

    return run(config)
