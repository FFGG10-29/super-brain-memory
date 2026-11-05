"""ui.desktop.pages —— 各功能页（全部只依赖 DesktopBackend 服务层）。"""
from __future__ import annotations

from ui.desktop.pages.about_page import AboutPage
from ui.desktop.pages.data_page import DataPage
from ui.desktop.pages.interface_page import InterfacePage
from ui.desktop.pages.memory_page import MemoryPage
from ui.desktop.pages.model_page import ModelPage
from ui.desktop.pages.nebula_page import NebulaPage
from ui.desktop.pages.recall_page import RecallPage
from ui.desktop.pages.settings_page import SettingsPage
from ui.desktop.pages.sync_page import SyncPage
from ui.desktop.pages.system_page import SystemPage
from ui.desktop.pages.timeline_page import TimelinePage

__all__ = [
    "AboutPage",
    "DataPage",
    "InterfacePage",
    "MemoryPage",
    "ModelPage",
    "NebulaPage",
    "RecallPage",
    "SettingsPage",
    "SyncPage",
    "SystemPage",
    "TimelinePage",
]
