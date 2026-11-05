"""PySide6 桌面前端验收：真实数据节点 / Lerp 光脉冲 / 终点闪烁 / 检索联动 / 安全配置。"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6 import QtCore, QtGui, QtWidgets

from config import BrainConfig, ModelConfig, StorageConfig, SyncConfig
from ui.desktop.backend import DesktopBackend
from ui.desktop.main_window import MainWindow
from ui.desktop.memory_panel import MemoryPanel
from ui.desktop.neurons import LightPulse, NebulaView, NeuronNode
from ui.desktop.settings import SettingsStore
from ui.desktop.settings_panel import SettingsPanel


class _FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.values[(service, account)] = value

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


class _FakeQSettings:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

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
def desktop_config(tmp_path):
    return BrainConfig(
        storage=StorageConfig(
            sqlite_path=tmp_path / "r.db", lancedb_path=tmp_path / "v.lance",
            kuzu_path=tmp_path / "g.kuzu", lmdb_path=tmp_path / "kv.lmdb", duckdb_path=tmp_path / "t.db",
        ),
        model=ModelConfig(dense_dim=8, use_cuda=False, llm_gguf_dir=tmp_path / "models"),
        sync=SyncConfig(device_id="dev-desktop", local_repo_dir=tmp_path / "sync"),
    )


@pytest.fixture()
def backend(desktop_config):
    instance = DesktopBackend(desktop_config)
    for content in (
        "周五发版出现严重冲突，全员加班到凌晨",
        "发版流程负责人更换为 Bob 并重新评审",
        "架构评审确认缓存分层与向量召回方案",
        "用户偏好直接给出结论而不是铺垫",
    ):
        assert instance.save_memory(content)["applied"] is True
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture()
def nebula(desktop_config):
    return NebulaView(desktop_config, auto_animate=False)


# ── 星云视图 ──────────────────────────────────────────────────────────────
def test_graph_nodes_come_from_real_memories(backend, nebula, qapp):
    graph = backend.graph()
    assert len(graph.nodes) >= 4
    nebula.set_graph(graph)
    assert len(nebula.nodes) == len(graph.nodes)
    assert nebula.node(graph.nodes[0].id) is not None
    assert nebula._origin is not None
    assert graph.degraded is True, "无 ONNX 模型时必须明确标注字符 n-gram 降级"
    assert graph.source == "char-2gram"


def test_stable_layout_is_reproducible(backend, nebula, qapp):
    graph = backend.graph()
    nebula.set_graph(graph)
    before = {node.node_id: (node.x(), node.y()) for node in nebula.nodes}
    nebula.set_graph(graph)
    after = {node.node_id: (node.x(), node.y()) for node in nebula.nodes}
    assert before == after, "稳定哈希布局必须跨重建一致"


def test_lerp_pulse_travels_flashes_and_is_destroyed(backend, nebula, qapp):
    nebula.set_graph(backend.graph())
    source, target = nebula.nodes[0], nebula.nodes[1]
    emitted = nebula.emit_pulses(source.node_id, [(target.node_id, 0.9)])
    assert emitted == 1 and nebula.pulse_count() == 1
    pulse: LightPulse = nebula.pulses[0]
    assert pulse.speed > 0

    first_progress = pulse.progress
    nebula.animate()
    assert pulse.progress > first_progress, "每帧必须线性插值前进"
    assert pulse.x() != source.x() or pulse.y() != source.y()

    for _ in range(200):
        nebula.animate()
    assert nebula.pulse_count() == 0, "到达终点后光点必须销毁"
    assert target.node_id in nebula.last_arrivals
    assert target.flash_count == 1, "到达终点必须触发一次高亮闪烁（随后按帧衰减）"
    assert target.flash_intensity == 0.0, "闪烁最终衰减回常态"


def test_pulse_follows_drifting_nodes(backend, nebula, qapp):
    nebula.set_graph(backend.graph())
    source, target = nebula.nodes[0], nebula.nodes[1]
    nebula.emit_pulses(source.node_id, [(target.node_id, 0.5)])
    pulse = nebula.pulses[0]
    nebula.animate()
    before = (pulse.x(), pulse.y())
    source.setPos(source.x() + 120, source.y() + 60)
    nebula.animate()
    assert (pulse.x(), pulse.y()) != before, "光点必须跟随节点最新坐标，不得脱节"


def test_render_scene_to_image(backend, nebula, qapp):
    """真正画一帧：证明光脉冲在深空背景上可见。"""
    nebula.set_graph(backend.graph())
    nebula.emit_from_center([(node.node_id, 0.8) for node in nebula.nodes[:3]])
    for _ in range(3):
        nebula.animate()

    def frame() -> QtGui.QImage:
        image = QtGui.QImage(1000, 800, QtGui.QImage.Format.Format_ARGB32)
        image.fill(QtGui.QColor(nebula._config.ui.background_color))
        painter = QtGui.QPainter(image)
        nebula.scene.render(painter)
        painter.end()
        return image

    first = frame()
    for _ in range(4):
        nebula.animate()
    second = frame()
    assert not first.isNull() and not second.isNull()
    assert first != second, "光点移动后两帧像素必须不同"


# ── 检索联动（记忆查看界面）──────────────────────────────────────────────
def test_recall_emits_pulses_from_center(backend, nebula, qapp):
    view = backend.recall("发版冲突")
    assert view.activations, "召回必须产出 (node_id, 激活强度) 列表"
    nebula.set_graph(backend.graph())
    emitted = nebula.emit_from_center(view.activations)
    assert emitted >= 1
    assert nebula.pulse_count() >= 1


def test_memory_panel_search_and_double_click(backend, nebula, qapp):
    panel = MemoryPanel(backend, nebula)
    assert panel._list.count() >= 4
    panel._search.setText("发版")
    panel.do_search()
    assert nebula.pulse_count() >= 1, "检索必须触发光脉冲"
    assert "激活" in panel._status.text()

    panel.refresh()
    item = panel._list.item(0)
    panel.show_detail(item)
    assert "PAD" in panel._detail.toPlainText()
    panel.activate_related(item)
    assert "突触扩散" in panel._status.text()


def test_main_window_status_and_demo(qapp, backend, desktop_config):
    store = SettingsStore(desktop_config, keyring_backend=_FakeKeyring(), qsettings=_FakeQSettings())
    window = MainWindow(backend, store, desktop_config)
    window.refresh_status()
    assert "引擎" in window._status_label.text()
    assert "engine_health" in window._status_text.toPlainText()
    window.demo_pulse()
    window._nebula.timer.stop()
    window._status_timer.stop()
    window.close()


# ── PAT / SYNC_REPO_URL 配置 ──────────────────────────────────────────────
def test_settings_store_never_leaks_pat(desktop_config, qapp):
    keyring, qsettings = _FakeKeyring(), _FakeQSettings()
    store = SettingsStore(desktop_config, keyring_backend=keyring, qsettings=qsettings)
    assert store.load().pat_present is False

    saved = store.save("https://github.com/example/brain-sync.git", "ghp_SECRET_TOKEN")
    assert saved.repo_url.endswith("brain-sync.git")
    assert saved.pat_present and saved.pat_source == "keyring"
    assert saved.masked_pat == "••••••••", "状态里不得出现明文"
    assert store.pat() == "ghp_SECRET_TOKEN"
    assert desktop_config.sync.repo_url.endswith("brain-sync.git"), "运行期配置必须即时生效"
    assert keyring.values, "PAT 必须进钥匙串"
    assert "ghp_SECRET_TOKEN" not in repr(qsettings.values), "PAT 不得进用户级 QSettings"

    result = store.test_connection()
    assert result["url_ok"] and result["pat_present"]
    store.save("https://github.com/example/brain-sync.git", "")
    assert store.load().pat_present is False


def test_app_run_event_loop_starts_and_quits(qapp, desktop_config):
    """真正启动一次 `python -m ui.desktop` 的事件循环并优雅退出。"""
    from ui.desktop.app import run

    QtCore.QTimer.singleShot(250, qapp.quit)
    assert run(desktop_config) == 0


def test_settings_panel_password_field_masks_pat(desktop_config, qapp):
    store = SettingsStore(desktop_config, keyring_backend=_FakeKeyring(), qsettings=_FakeQSettings())
    panel = SettingsPanel(store)
    assert panel._pat.echoMode() == QtWidgets.QLineEdit.EchoMode.Password
    panel._repo.setText("https://github.com/example/brain-sync.git")
    panel._pat.setText("ghp_PANEL_TOKEN")
    panel.save()
    assert "已保存" in panel._status.text()
    assert panel._pat.text() == "", "保存后必须清空密码框"
    assert store.pat() == "ghp_PANEL_TOKEN"
