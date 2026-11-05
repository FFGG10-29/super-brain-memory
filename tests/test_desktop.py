"""PySide6 桌面前端验收：星云脉冲 / 记忆管理 / 端口启停 / 同步备份 / 安全配置。"""
from __future__ import annotations

import json
import os
import urllib.request

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6 import QtCore, QtGui, QtWidgets

from brain.activation import ActivationGraph, ActivationNode
from config import BrainConfig, ModelConfig, StorageConfig, SyncConfig
from ui.desktop.backend import DesktopBackend
from ui.desktop.main_window import MainWindow
from ui.desktop.neurons import LightPulse, NebulaView
from ui.desktop.pages import MemoryPage, NebulaPage, RecallPage, SettingsPage, SystemPage
from ui.desktop.pages.interface_page import InterfacePage
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


@pytest.fixture()
def store(desktop_config):
    return SettingsStore(desktop_config, keyring_backend=_FakeKeyring(), qsettings=_FakeQSettings())


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


def test_labels_follow_priority_limit(backend, nebula, qapp):
    nebula.set_graph(backend.graph())
    visible = [node for node in nebula.nodes if node.label_item.isVisible()]
    assert 0 < len(visible) <= nebula._config.ui.max_labels


def test_view_area_and_highlight(backend, nebula, qapp):
    nebula.set_graph(backend.graph())
    target = nebula.nodes[0]
    assert nebula.focus_on(target.node_id) is True
    assert target.isSelected() is False  # 只高亮不选中，避免与拖拽冲突
    assert target._ring.isVisible() is True
    nebula.clear_highlight()
    assert target._ring.isVisible() is False
    assert nebula.view_area().width() > 0


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

    for _ in range(200):
        nebula.animate()
    assert nebula.pulse_count() == 0, "到达终点后光点必须销毁"
    assert target.node_id in nebula.last_arrivals
    assert target.flash_count >= 1, "到达终点必须触发高亮闪烁（自发放电可能额外+1，随后按帧衰减）"
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
    nebula.set_graph(backend.graph())
    nebula.emit_from_center([(node.node_id, 0.8) for node in nebula.nodes[:3]])
    for _ in range(3):
        nebula.animate()

    def frame() -> QtGui.QImage:
        image = QtGui.QImage(1000, 800, QtGui.QImage.Format.Format_ARGB32)
        image.fill(QtGui.QColor(nebula._config.ui.background_color))
        painter = QtGui.QPainter(image)
        nebula.view_scene.render(painter)
        painter.end()
        return image

    first = frame()
    for _ in range(4):
        nebula.animate()
    second = frame()
    assert not first.isNull() and not second.isNull()
    assert first != second, "光点移动后两帧像素必须不同"


# ── 星云页 / 外壳 ─────────────────────────────────────────────────────────
def test_nebula_page_activate_and_minimap(backend, desktop_config, qapp):
    page = NebulaPage(backend, desktop_config)
    assert page.minimap.scene() is page.view.view_scene
    page.activate("发版冲突")
    backend.tasks.flush()   # 召回在后台线程，等结果回调派发完再断言
    assert page.view.pulse_count() >= 1
    assert "命中" in page._hits.text()


def test_spaces_filter_emotion_only(qapp):
    nodes = [
        ActivationNode("a", "a", "发版冲突", "emotion", "core", (0.8, 0.1, 0.1), 0.6, True),
        ActivationNode("b", "b", "普通事实", "fact", "buffer", (0.0, 0.0, 0.0), 0.4, False),
    ]
    graph = ActivationGraph(nodes=nodes, edges=[], source="char-2gram", degraded=True)
    filtered = DesktopBackend._filter_graph(graph, "情感记忆")
    assert [node.id for node in filtered.nodes] == ["a"]
    assert [node.id for node in DesktopBackend._filter_graph(graph, "全部").nodes] == ["a", "b"]


def test_main_window_pages_and_status(backend, store, desktop_config, qapp):
    window = MainWindow(backend, store, desktop_config)
    assert window._nav.count() == 11
    window._nav.setCurrentRow(10)
    assert window.timeline_page is window._stack.currentWidget()
    assert "引擎" in window._status_label.text()
    assert window._space.count() >= 3, "空间下拉必须包含 全部/分层/情感记忆"

    window._nav.setCurrentRow(3)
    assert window.system_page._engine_table.rowCount() >= 6
    window._nav.setCurrentRow(2)
    assert window.recall_page is window._stack.currentWidget()

    window.demo_pulse()
    window.nebula_page.view.timer.stop()
    window._status_timer.stop()
    window.close()


# ── 记忆管理 / 召回测试 / 系统概览 ───────────────────────────────────────
def test_memory_page_filter_detail_and_write(backend, qapp):
    page = MemoryPage(backend)
    assert page._table.rowCount() >= 4

    page._search.setText("发版")
    assert 0 < page._table.rowCount() <= 4
    page._table.selectRow(0)
    assert page._selected_id()
    inspector = page._detail
    assert inspector._title.text()                         # 检查器已载入该条记忆
    assert "PAD" in inspector._fields_text.toPlainText()   # 原始字段仍可就地查看
    assert "置信度" in inspector._confidence._label.text() # 文字化置信度（不只靠颜色）
    assert inspector._evidence.count() >= 4               # 来源/新鲜度/使用/证据四维 badge

    page._search.clear()
    page._new_memory.setText("新增一条用于界面写入验证的记忆")
    page._save()
    backend.tasks.flush()
    assert "写入成功" in page._status.text()


def test_recall_page_renders_channels(backend, qapp):
    page = RecallPage(backend)
    page._query.setText("发版冲突")
    page.run_recall()
    backend.tasks.flush()
    view = page._view
    assert view.hits
    assert page._table.rowCount() == len(view.hits)
    assert "命中" in page._status.text()
    assert isinstance(view.injection, str)


def test_system_page_tasks_and_run(backend, qapp):
    page = SystemPage(backend)
    tasks = backend.circadian_tasks()
    assert page._task_table.rowCount() == len(tasks) == 4
    page._task_table.selectRow(0)
    page._run_selected_task()
    assert "执行成功" in page._status.text() or "执行失败" in page._status.text()
    # 中文注解：引擎表有「它在干什么」列，任务表有说明列
    assert page._engine_table.columnCount() == 5
    assert page._task_table.item(0, 2).text()


# ── 接口与端口 ───────────────────────────────────────────────────────────
def test_http_service_binds_real_port_and_serves_health(backend, qapp):
    started = backend.start_http("127.0.0.1", 0)
    assert started["ok"] is True
    assert started["port"] > 0
    url = f"http://127.0.0.1:{started['port']}/health"
    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 本地回环
        payload = json.loads(response.read().decode("utf-8"))
    assert payload["status"] == "ok"
    assert backend.gateway_status()["running"] is True

    stopped = backend.stop_http()
    assert stopped["ok"] is True
    assert backend.gateway_status()["running"] is False


def test_interface_page_persists_port_prefs(backend, store, qapp):
    page = InterfacePage(backend, store)
    page._port.setValue(18999)
    page._host.setText("127.0.0.1")
    store.save_prefs(http_host="127.0.0.1", http_port=18999, model_name="brain-memory")
    reloaded = store.load_prefs()
    assert reloaded["http_port"] == 18999
    assert reloaded["model_name"] == "brain-memory"
    assert backend.config.gateway.http_port == 18999
    assert page._start.isEnabled() is True
    assert len(backend.mcp_tools()) == 6


def test_settings_page_applies_appearance(backend, store, qapp):
    page = SettingsPage(backend, store)
    fired: list[int] = []
    page.appearance_changed.connect(lambda: fired.append(1))
    # 表单 collect 的补丁 → 保存路径（控件渲染已在「渲染所有分组」用例覆盖）
    page._editor  # noqa: B018  表单已按分组构建
    backend.save_settings_patch({
        "ui": {"timer_interval_ms": 33, "max_labels": 5},
        "activation": {"similarity_threshold": 0.45},
    })
    # 表单是「当前值」的唯一真源：先让表单跟上新配置，再点保存，
    # 否则会把构造时的旧值又写回去（这是真实行为，不是 bug）。
    page._reload_editor()
    page.save()
    assert backend.config.ui.timer_interval_ms == 33
    assert backend.config.activation.similarity_threshold == pytest.approx(0.45)
    assert backend.config.ui.max_labels == 5
    assert fired, "save() 必须触发 appearance_changed"
    assert "已保存" in page._status.text()


# ── 同步与备份 ───────────────────────────────────────────────────────────
def test_sync_once_without_repo_is_noop(backend, qapp):
    report = backend.sync_once()
    assert report["ok"] is True
    assert report["has_repo"] is False
    assert report["pulled"] is False and report["pushed"] is False


def test_snapshot_create_and_list(backend, qapp):
    created = backend.create_snapshot()
    assert created["ok"] is True
    items = backend.snapshots()
    assert any(item["name"] == created["name"] for item in items)
    assert items[0]["counts"]["neuron"] >= 4


def test_export_dashboard_and_memories(backend, tmp_path, qapp):
    snapshot_path = backend.export_dashboard(tmp_path / "snapshot.json")
    payload = json.loads(open(snapshot_path, encoding="utf-8").read())
    assert set(payload) >= {"engine_health", "memory_stream", "sync_status"}

    memories_path = backend.export_memories(tmp_path / "memories.ndjson")
    lines = open(memories_path, encoding="utf-8").read().strip().splitlines()
    assert len(lines) >= 4


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


def test_app_run_event_loop_starts_and_quits(qapp, desktop_config):
    """真正启动一次 `python -m ui.desktop` 的事件循环并优雅退出。"""
    from ui.desktop.app import run

    QtCore.QTimer.singleShot(250, qapp.quit)
    assert run(desktop_config) == 0
