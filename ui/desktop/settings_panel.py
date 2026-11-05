"""ui.desktop.settings_panel —— PAT 与 SYNC_REPO_URL 前端配置。

安全约束（UI 层强制执行）：
- PAT 用密码框，绝不回显明文；留空 = 不改动现有凭据；
- 只经 SettingsStore 写入系统钥匙串，绝不写项目 .env / 事件日志；
- 页面明确提示最小权限 PAT 与短周期轮换。
"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ui.desktop.settings import SettingsStore

__all__ = ["SettingsPanel"]


class SettingsPanel(QtWidgets.QWidget):
    def __init__(self, store: SettingsStore) -> None:
        super().__init__()
        self._store = store
        settings = store.load()

        self._repo = QtWidgets.QLineEdit(settings.repo_url)
        self._repo.setPlaceholderText("https://github.com/<user>/<private-repo>.git")
        self._pat = QtWidgets.QLineEdit()
        self._pat.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self._pat.setPlaceholderText("留空 = 不改动；填入 = 写入系统钥匙串")
        self._status = QtWidgets.QLabel("")
        self._status.setWordWrap(True)

        form = QtWidgets.QFormLayout()
        form.addRow("SYNC_REPO_URL", self._repo)
        form.addRow("GitHub PAT", self._pat)

        save = QtWidgets.QPushButton("保存配置")
        save.clicked.connect(self.save)
        verify = QtWidgets.QPushButton("校验配置")
        verify.clicked.connect(self.verify)
        clear = QtWidgets.QPushButton("清除 PAT")
        clear.clicked.connect(self.clear_pat)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(save)
        buttons.addWidget(verify)
        buttons.addWidget(clear)
        buttons.addStretch(1)

        warning = QtWidgets.QLabel(
            "安全须知：PAT 只存系统钥匙串（Windows Credential Manager），不写入仓库、\n"
            ".env 或事件日志；请使用最小权限 fine-grained token 并定期轮换。\n"
            "仓库地址存用户级 QSettings；设备标识由后端生成，不可在此伪造。"
        )
        warning.setStyleSheet("color: #8FB8FF; padding: 8px;")
        self._device = QtWidgets.QLabel(f"device_id: {settings.device_id} · PAT 来源: {settings.pat_source}")

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(buttons)
        layout.addWidget(self._status)
        layout.addWidget(self._device)
        layout.addStretch(1)
        layout.addWidget(warning)

    def save(self) -> None:
        try:
            settings = self._store.save(self._repo.text(), self._pat.text() or None)
        except RuntimeError as exc:
            self._status.setText(f"保存失败：{exc}")
            return
        self._pat.clear()
        self._device.setText(f"device_id: {settings.device_id} · PAT 来源: {settings.pat_source}")
        self._status.setText("已保存：仓库地址写入用户级配置，PAT 写入系统钥匙串（未入库）")

    def clear_pat(self) -> None:
        self._store.save(self._repo.text(), "")
        self._pat.clear()
        self._status.setText("已清除钥匙串中的 PAT")

    def verify(self) -> None:
        result = self._store.test_connection()
        detail = (
            f"仓库地址格式: {'OK' if result['url_ok'] else '错误'} · "
            f"PAT: {'已配置' if result['pat_present'] else '未配置'} · "
            f"git: {result['git_version'] or '不可用'}"
        )
        self._status.setText(f"{detail}\n{result['message']}")
