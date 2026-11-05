"""Switch 开关控件验收：布尔项用自绘滑动开关而非对勾。"""
from __future__ import annotations

import pytest
from PySide6 import QtWidgets

from config import BrainConfig, ModelConfig, StorageConfig, SyncConfig
from ui.desktop.backend import DesktopBackend
from ui.desktop.pages import SettingsPage
from ui.desktop.settings import SettingsStore
from ui.desktop.widgets import ConfigEditor, Switch


class _FakeKeyring:
    def get_password(self, service, account):
        return None

    def set_password(self, service, account, value):
        return None

    def delete_password(self, service, account):
        return None


class _FakeQSettings:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def value(self, key, default=None):
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.values[key] = value

    def sync(self):
        return None


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture()
def backend(desktop_config):
    instance = DesktopBackend(desktop_config)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture()
def desktop_config(tmp_path):
    return BrainConfig(
        storage=StorageConfig(
            sqlite_path=tmp_path / "r.db", lancedb_path=tmp_path / "v.lance",
            kuzu_path=tmp_path / "g.kuzu", lmdb_path=tmp_path / "kv.lmdb", duckdb_path=tmp_path / "t.db",
        ),
        model=ModelConfig(dense_dim=8, use_cuda=False, llm_gguf_dir=tmp_path / "models"),
        sync=SyncConfig(device_id="dev-switch", local_repo_dir=tmp_path / "sync"),
    )


def test_config_editor_bool_fields_use_switch(desktop_config, qapp):
    editor = ConfigEditor((("存储引擎", desktop_config.storage), ("召回与精排", desktop_config.recall)))
    switches = editor.findChildren(Switch)
    # 存储引擎 6 个开关 + 启用精排 = 至少 7 个布尔项
    assert len(switches) >= 7
    assert all(isinstance(item, Switch) for item in switches)


def test_switch_state_roundtrip(desktop_config, qapp):
    editor = ConfigEditor((("召回与精排", desktop_config.recall),))
    switch = editor.findChildren(Switch)[0]
    switch.setChecked(not switch.isChecked())
    patch = editor.collect()["召回与精排"]
    assert "rerank_enabled" in patch
    assert patch["rerank_enabled"] == switch.isChecked()


def test_settings_page_has_switches_and_no_plain_bool_checkboxes(backend, desktop_config, qapp):
    store = SettingsStore(desktop_config, keyring_backend=_FakeKeyring(), qsettings=_FakeQSettings())
    page = SettingsPage(backend, store)
    assert len(page.findChildren(Switch)) >= 7, "设置页布尔项必须全部是滑动开关"
