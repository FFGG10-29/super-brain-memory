"""ui.desktop.main_window —— LivingMemory 风格外壳：左侧导航 + 顶栏 + 九个功能页。

布局：
    [侧栏 220px]  星云视图 / 记忆管理 / 召回测试 / 系统概览 / 展示数据
                  接口与端口 / 同步与备份 / 设置 / 关于
    [顶栏]        空间选择 · 返回全景 · 全局搜索 · 导出 · 管理数据 · 全屏
    [中央]        当前功能页（默认星云视图）
    [状态栏]      引擎健康 · 记忆总数 · 光脉冲 · 待重放事件
"""
from __future__ import annotations

import time

from PySide6 import QtCore, QtGui, QtWidgets

from config import BrainConfig
from ui.desktop.backend import DesktopBackend
from ui.desktop.pages import (
    AboutPage,
    DataPage,
    InterfacePage,
    MemoryPage,
    ModelPage,
    NebulaPage,
    RecallPage,
    SettingsPage,
    SyncPage,
    SystemPage,
    TimelinePage,
)
from ui.desktop.settings import SettingsStore
from ui.desktop.theme import stylesheet

__all__ = ["MainWindow"]

_NAV = (
    ("星云视图", QtWidgets.QStyle.StandardPixmap.SP_FileDialogContentsView),
    ("记忆管理", QtWidgets.QStyle.StandardPixmap.SP_FileIcon),
    ("召回测试", QtWidgets.QStyle.StandardPixmap.SP_BrowserReload),
    ("系统概览", QtWidgets.QStyle.StandardPixmap.SP_DesktopIcon),
    ("展示数据", QtWidgets.QStyle.StandardPixmap.SP_DriveHDIcon),
    ("模型配置", QtWidgets.QStyle.StandardPixmap.SP_ComputerIcon),
    ("接口与端口", QtWidgets.QStyle.StandardPixmap.SP_DriveNetIcon),
    ("同步与备份", QtWidgets.QStyle.StandardPixmap.SP_DialogSaveButton),
    ("设置", QtWidgets.QStyle.StandardPixmap.SP_FileDialogDetailedView),
    ("关于", QtWidgets.QStyle.StandardPixmap.SP_MessageBoxInformation),
    ("事件时间轴", QtWidgets.QStyle.StandardPixmap.SP_FileDialogListView),
)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(
        self, backend: DesktopBackend, store: SettingsStore, config: BrainConfig | None = None
    ) -> None:
        super().__init__()
        self._backend = backend
        self._store = store
        self._config = config or backend.config
        self.setWindowTitle("超级大脑 v2.1 · Living Memory")
        self.resize(self._config.ui.window_width, self._config.ui.window_height)
        self._fit_to_screen()
        self.setStyleSheet(stylesheet())

        # 托盘驻留状态（app.run 会调 attach_tray 注入图标）
        self.tray = None
        self._quitting = False
        self._tray_hinted = False
        from ui.desktop.tray import app_icon

        icon = app_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)

        self.nebula_page = NebulaPage(backend, self._config)
        self.memory_page = MemoryPage(backend)
        self.recall_page = RecallPage(backend)
        self.system_page = SystemPage(backend)
        self.data_page = DataPage(backend)
        self.model_page = ModelPage(backend)
        self.interface_page = InterfacePage(backend, store)
        self.sync_page = SyncPage(backend, store)
        self.settings_page = SettingsPage(backend, store)
        self.about_page = AboutPage()
        self.timeline_page = TimelinePage(backend)

        self._nav: QtWidgets.QListWidget
        self._space: QtWidgets.QComboBox
        self._search: QtWidgets.QLineEdit

        self._stack = QtWidgets.QStackedWidget()
        for page in (
            self.nebula_page,
            self.memory_page,
            self.recall_page,
            self.system_page,
            self.data_page,
            self.model_page,
            self.interface_page,
            self.sync_page,
            self.settings_page,
            self.about_page,
            self.timeline_page,
        ):
            self._stack.addWidget(page)

        shell = QtWidgets.QWidget()
        shell_layout = QtWidgets.QHBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        shell_layout.addWidget(self._build_sidebar())
        shell_layout.addWidget(self._build_main_area(), 1)
        self.setCentralWidget(shell)

        self._status_label = QtWidgets.QLabel("启动中…")
        self.statusBar().addWidget(self._status_label, 1)
        self._clock = QtWidgets.QLabel("")
        self._clock.setObjectName("hint")
        self.statusBar().addPermanentWidget(self._clock)

        self._wire_signals()
        self._install_shortcuts()
        self._nav.setCurrentRow(0)

        self._status_timer = QtCore.QTimer(self)
        self._status_timer.timeout.connect(self.refresh_status)
        self._status_timer.start(5000)
        self.refresh_status()

    # ── 外壳 ──────────────────────────────────────────────────────────────
    def _build_sidebar(self) -> QtWidgets.QFrame:
        sidebar = QtWidgets.QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(220)
        layout = QtWidgets.QVBoxLayout(sidebar)
        layout.setContentsMargins(10, 18, 10, 12)
        layout.setSpacing(12)

        brand_row = QtWidgets.QHBoxLayout()
        logo = QtWidgets.QLabel("SB")
        logo.setFixedSize(34, 34)
        logo.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        logo.setStyleSheet(
            "background-color: #12384A; border: 1px solid #2E6E8E; border-radius: 9px;"
            "color: #4FC3F7; font-weight: 700; font-size: 13px;"
        )
        brand_box = QtWidgets.QVBoxLayout()
        brand_box.setSpacing(0)
        title = QtWidgets.QLabel("超级大脑")
        title.setObjectName("brand")
        subtitle = QtWidgets.QLabel("Living Memory")
        subtitle.setObjectName("brandSub")
        brand_box.addWidget(title)
        brand_box.addWidget(subtitle)
        brand_row.addWidget(logo)
        brand_row.addLayout(brand_box)
        brand_row.addStretch(1)
        layout.addLayout(brand_row)

        section = QtWidgets.QLabel("导航")
        section.setObjectName("navSection")
        layout.addWidget(section)

        self._nav = QtWidgets.QListWidget()
        self._nav.setObjectName("nav")
        self._nav.setIconSize(QtCore.QSize(15, 15))
        self._nav.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        for label, pixmap in _NAV:
            item = QtWidgets.QListWidgetItem(self.style().standardIcon(pixmap), label)
            self._nav.addItem(item)
        layout.addWidget(self._nav, 1)

        divider = QtWidgets.QFrame()
        divider.setObjectName("divider")
        divider.setFixedHeight(1)
        layout.addWidget(divider)
        footer = QtWidgets.QLabel(f"设备 {self._config.sync.device_id}\nschema v{self._config.sync.schema_version}")
        footer.setObjectName("hint")
        layout.addWidget(footer)
        return sidebar

    def _build_main_area(self) -> QtWidgets.QWidget:
        area = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(area)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        topbar = QtWidgets.QFrame()
        topbar.setObjectName("topbar")
        topbar.setFixedHeight(56)
        top = QtWidgets.QHBoxLayout(topbar)
        top.setContentsMargins(16, 8, 16, 8)
        top.setSpacing(10)

        self._space = QtWidgets.QComboBox()
        self._space.setMinimumWidth(120)
        self._space.addItems(self._backend.spaces())
        reset = QtWidgets.QPushButton("返回全景")
        reset.clicked.connect(self.reset_view)

        self._search = QtWidgets.QLineEdit()
        self._search.setObjectName("search")
        self._search.setPlaceholderText("搜索记忆：回车召回，并从查询中心点亮突触…")
        self._search.setClearButtonEnabled(True)
        self._search.setMinimumWidth(320)
        self._search.setMaximumWidth(620)

        export = QtWidgets.QPushButton("导出")
        export.clicked.connect(self.export_dashboard)
        manage = QtWidgets.QPushButton("管理数据")
        manage.clicked.connect(self.open_data_directory)
        fullscreen = QtWidgets.QPushButton("全屏")
        fullscreen.clicked.connect(self.toggle_fullscreen)

        top.addWidget(self._space)
        top.addWidget(reset)
        top.addStretch(1)
        top.addWidget(self._search, 2)
        top.addStretch(1)
        top.addWidget(export)
        top.addWidget(manage)
        top.addWidget(fullscreen)

        layout.addWidget(topbar)
        layout.addWidget(self._stack, 1)
        return area

    # ── 信号 ──────────────────────────────────────────────────────────────
    def _wire_signals(self) -> None:
        self._nav.currentRowChanged.connect(self._on_nav)
        self._space.currentTextChanged.connect(self.nebula_page.set_space)
        self._search.returnPressed.connect(self._on_search)
        self.memory_page.focus_requested.connect(self.focus_memory)
        self.nebula_page.node_selected.connect(self.open_memory)
        self.timeline_page.focus_requested.connect(self.open_memory)
        self.recall_page.activate_requested.connect(self.activate_query)
        self.recall_page.focus_requested.connect(self.focus_memory)
        self.settings_page.appearance_changed.connect(self._apply_appearance)
        self.model_page.kernel_restarted.connect(self._refresh_current)

    def _install_shortcuts(self) -> None:
        for key, slot in (
            ("Ctrl+F", self._focus_search),
            ("F11", self.toggle_fullscreen),
            ("Ctrl+R", self._refresh_current),
        ):
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(key), self)
            shortcut.activated.connect(slot)

    def _on_nav(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        page = self._stack.currentWidget()
        refresh = getattr(page, "refresh", None)
        if callable(refresh) and index != 0:
            refresh()

    def _refresh_current(self) -> None:
        page = self._stack.currentWidget()
        refresh = getattr(page, "refresh", None)
        if callable(refresh):
            refresh()
        else:
            self.nebula_page.reload()
        self.refresh_status()

    def _focus_search(self) -> None:
        self._search.setFocus()
        self._search.selectAll()

    def _on_search(self) -> None:
        query = self._search.text().strip()
        if query:
            self.activate_query(query)

    def _apply_appearance(self) -> None:
        self.nebula_page.view.timer.setInterval(int(self._config.ui.timer_interval_ms))
        self.nebula_page.reload()

    # ── 公共动作（页面/顶栏调用）─────────────────────────────────────────
    def activate_query(self, query: str) -> dict[str, object]:
        self._nav.setCurrentRow(0)
        result = self.nebula_page.activate(query)
        self._search.setText(query)
        self.refresh_status()
        return result

    def focus_memory(self, memory_id: str) -> None:
        self._nav.setCurrentRow(0)
        self.nebula_page.focus(memory_id)

    def open_memory(self, memory_id: str) -> None:
        """点击星云节点 → 打开记忆检查器（图谱与详情双向可达）。"""
        self._nav.setCurrentRow(1)
        self.memory_page.show_memory(memory_id)

    def reset_view(self) -> None:
        self.nebula_page.reset_view()

    def demo_pulse(self) -> int:
        emitted = self.nebula_page.demo_pulse()
        self.refresh_status()
        return emitted

    def export_dashboard(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "导出全量快照", f"brain_snapshot_{time.strftime('%Y%m%d_%H%M')}.json", "JSON (*.json)"
        )
        if path:
            self._backend.export_dashboard(path)
            self.statusBar().showMessage(f"已导出 {path}", 4000)

    def open_data_directory(self) -> None:
        root = self._config.storage.sqlite_path.parent
        root.mkdir(parents=True, exist_ok=True)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(root.resolve())))

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    # ── 状态栏 ────────────────────────────────────────────────────────────
    def refresh_status(self) -> None:
        # 后台重任务进行中：跳过本轮状态刷新，绝不排队等锁（UI 保持可交互）
        if self._backend.tasks.busy:
            return
        snapshot = self._backend.snapshot()
        engines = snapshot["engine_health"]["engines"]
        ok = sum(1 for engine in engines if engine["ok"])
        stats = self.nebula_page.stats()
        self._status_label.setText(
            f"引擎 {ok}/{len(engines)} 正常 · 记忆 {snapshot['memory_stream'].get('count', 0)} 条 · "
            f"空间 {stats['space']} · 节点 {stats['nodes']} · 突触 {stats['edges']} · "
            f"光点 {stats['pulses']} · 待重放 {snapshot['sync_status'].get('pending_events', 0)}"
        )
        self._clock.setText(time.strftime("%Y-%m-%d %H:%M"))

    # ── 窗口尺寸 ──────────────────────────────────────────────────────────
    def _fit_to_screen(self) -> None:
        """按当前屏幕可用区域收敛窗口尺寸并居中。

        配置里 1400×900 是给大屏的；笔记本 1366×768 / 125% 缩放下直接照搬会让标题栏
        跑到屏幕外（顶部看不见、拖不动）。这里一律以「屏幕可用区 − 边距」为上限。
        """
        screen = QtGui.QGuiApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        min_width, min_height = 900, 600
        width = min(self.width(), max(min_width, available.width() - 60))
        height = min(self.height(), max(min_height, available.height() - 80))
        self.resize(width, height)
        x = available.left() + max(0, (available.width() - width) // 2)
        y = available.top() + max(0, (available.height() - height) // 2)
        self.move(x, y)

    # ── 托盘驻留 ──────────────────────────────────────────────────────────
    def attach_tray(self, tray) -> None:
        """由 app.run 注入托盘；有了它，叉掉窗口只隐藏，服务继续跑。"""
        self.tray = tray

    def goto_page(self, index: int) -> None:
        if 0 <= index < self._nav.count():
            self._nav.setCurrentRow(index)

    def show_from_tray(self) -> None:
        from ui.desktop.tray import center_on_screen, notify

        was_hidden = not self.isVisible()
        self.showNormal()
        self.setWindowState(self.windowState() & ~QtCore.Qt.WindowState.WindowMinimized)
        center_on_screen(self)
        self.activateWindow()
        if was_hidden:
            notify(self.tray, "超级大脑", "已回到前台（关闭窗口只会隐藏到托盘）")

    def quit_app(self) -> None:
        """真正退出：走完整清理（停定时器、等后台任务、关六引擎）。"""
        self._quitting = True
        if self.tray is not None:
            self.tray.hide()
        self.close()
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.quit()

    def closeEvent(self, event) -> None:  # noqa: N802
        # 托盘驻留：默认只隐藏窗口，引擎 / HTTP 网关 / MCP 接入保持在线
        if self.tray is not None and not getattr(self, "_quitting", False):
            self.hide()
            event.ignore()
            if not getattr(self, "_tray_hinted", False):
                self._tray_hinted = True
                from ui.desktop.tray import notify

                notify(self.tray, "超级大脑仍在运行",
                       "窗口已隐藏到托盘：记忆服务与 MCP 接入都没有中断。\n"
                       "要彻底退出请右键托盘图标 →「完全退出」。")
            return
        self._status_timer.stop()
        self.nebula_page.view.timer.stop()
        self.nebula_page.stop_stats()
        self.timeline_page.stop()
        self._backend.tasks.wait()
        self._backend.stop_http()
        self._backend.close()
        super().closeEvent(event)
