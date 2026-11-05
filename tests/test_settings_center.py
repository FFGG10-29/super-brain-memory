"""配置中心验收：用户配置持久化 / 预设 / 存储引擎开关 / 云端模型 / 内核重启 / 设置页。"""
from __future__ import annotations

import http.server
import json
import threading

import pytest
from PySide6 import QtWidgets

from config import BrainConfig, ModelConfig, StorageConfig, SyncConfig
from main import build_app
from ui.desktop.backend import DesktopBackend
from ui.desktop.pages import SettingsPage
from ui.desktop.settings import SettingsStore
from ui.desktop.user_config import (
    apply_overrides,
    clear_overrides,
    load_overrides,
    preset_names,
    save_overrides,
)


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


@pytest.fixture(autouse=True)
def isolated_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPER_BRAIN_CONFIG_DIR", str(tmp_path / "user-config"))
    clear_overrides()
    yield
    clear_overrides()


@pytest.fixture()
def desktop_config(tmp_path):
    return BrainConfig(
        storage=StorageConfig(
            sqlite_path=tmp_path / "r.db", lancedb_path=tmp_path / "v.lance",
            kuzu_path=tmp_path / "g.kuzu", lmdb_path=tmp_path / "kv.lmdb", duckdb_path=tmp_path / "t.db",
        ),
        model=ModelConfig(dense_dim=8, use_cuda=False, llm_gguf_dir=tmp_path / "models"),
        sync=SyncConfig(device_id="dev-settings", local_repo_dir=tmp_path / "sync"),
    )


@pytest.fixture()
def backend(desktop_config):
    instance = DesktopBackend(desktop_config)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


# ── 用户配置持久化 ────────────────────────────────────────────────────────
def test_overrides_roundtrip_and_apply(desktop_config):
    save_overrides({"recall": {"top_k": 9}, "unknown_section": {"x": 1}})
    merged = load_overrides()
    assert merged["recall"]["top_k"] == 9

    changed = apply_overrides(desktop_config, merged)
    assert desktop_config.recall.top_k == 9
    assert "recall.top_k" in changed and all(not name.startswith("unknown") for name in changed)


def test_deep_merge_keeps_sibling_fields():
    save_overrides({"model": {"dense_dim": 16}})
    save_overrides({"model": {"use_cuda": True}})
    merged = load_overrides()
    assert merged["model"]["dense_dim"] == 16 and merged["model"]["use_cuda"] is True


# ── 预设 ─────────────────────────────────────────────────────────────────
def test_preset_lite_disables_heavy_engines(backend):
    result = backend.apply_preset("lite")
    assert result["ok"] is True
    assert backend.config.storage.engines_enabled["vector"] is False
    assert backend.config.recall.rerank_enabled is False
    assert backend.config.ui.timer_interval_ms == 33
    assert "vector" in load_overrides()["storage"]["engines_enabled"]
    assert set(preset_names()) == {"balanced", "performance", "lite"}


# ── 存储引擎开关 ─────────────────────────────────────────────────────────
def test_disabled_vector_keeps_write_stream_alive(desktop_config):
    desktop_config.storage.engines_enabled = {
        "relational": True, "vector": False, "graph": True,
        "kv": True, "timeseries": True, "hypergraph": True,
    }
    app = build_app(desktop_config)
    try:
        result = app.hippocampus.remember("向量引擎关闭时写入必须不中断", agent_id="tester")
        assert result.applied is True
        assert app.storage.relational.count("neuron", active_only=False) >= 1
        assert app.storage.vector.search_text("任意", limit=5) == []
        health = {status.engine: status for status in app.health_report()}
        assert any("已关闭" in status.detail for status in health.values())
    finally:
        app.close()


def test_disabled_kv_and_timeseries(backend):
    backend.close()  # 释放 Kùzu 文件锁后再按新开关重建
    backend.config.storage.engines_enabled = {
        "relational": True, "vector": True, "graph": True,
        "kv": False, "timeseries": False, "hypergraph": False,
    }
    app = build_app(backend.config)
    try:
        # KV 关闭：画像/情绪读默认值但不崩
        assert app.storage.kv.get("amygdala", "anything") is None
        assert app.storage.timeseries.count("pad_track") == 0
        assert app.storage.relational.count("neuron", active_only=False) >= 0
    finally:
        app.close()


# ── 云端模型（本地假 OpenAI 服务器）──────────────────────────────────────
class _FakeOpenAI(http.server.BaseHTTPRequestHandler):
    embed_dim = 8

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/embeddings"):
            payload = {
                "data": [
                    {"index": index, "embedding": [0.1] * self.embed_dim}
                    for index in range(len(body.get("input", [])))
                ]
            }
        else:
            payload = {
                "choices": [{
                    "message": {"content": json.dumps({"ok": True, "answer": "云端可用"})}
                }]
            }
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):  # noqa: N802
        return None


@pytest.fixture(scope="module")
def fake_openai():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAI)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_cloud_mode_embedding_and_llm(desktop_config, fake_openai):
    desktop_config.model.model_mode = "cloud"
    desktop_config.model.cloud_base_url = fake_openai
    desktop_config.model.cloud_embedding_model = "text-embed-test"
    desktop_config.model.cloud_llm_model = "chat-test"
    desktop_config.model.cloud_embed_dim = 8

    app = build_app(desktop_config)
    try:
        assert type(app.embedder).__name__ == "CloudEmbedder"
        assert app.embedder.dense_dim == 8
        embedding = app.embedder.embed_query("云端向量")
        assert len(embedding.dense) == 8

        result = app.llm.complete_json("系统", "用户", required_keys=["ok"])
        assert result["answer"] == "云端可用"

        # 写入流走云端向量通道不炸（伪向量维度匹配）
        stored = app.hippocampus.remember("云端模式写入链路验证", agent_id="tester")
        assert stored.applied is True
    finally:
        app.close()


def test_cloud_mode_missing_model_falls_back_to_local(desktop_config):
    """cloud 模式但没配 embedding 模型名 → 退本地实现并正常启动。"""
    desktop_config.model.model_mode = "cloud"
    desktop_config.model.cloud_base_url = "http://127.0.0.1:9"  # 不会真请求
    app = build_app(desktop_config)
    try:
        assert type(app.embedder).__name__ == "BgeM3Embedder", "未配置云端 embedding 必须退本地实现"
        assert type(app.llm).__name__ == "QwenLocalLLM"
    finally:
        app.close()


# ── 内核重启 / 设置页 ─────────────────────────────────────────────────────
def test_backend_restart_applies_saved_overrides(backend, monkeypatch):
    monkeypatch.delenv("CLOUD_API_KEY", raising=False)
    backend._keyring = _FakeKeyring()
    backend.save_memory("重启前的记忆，重启后必须还在", agent_id="tester")
    backend.save_settings_patch({"ui": {"max_labels": 5}, "recall": {"top_k": 7}})
    assert backend.restart()["ok"] is True
    assert backend.config.ui.max_labels == 5
    assert backend.config.recall.top_k == 7
    assert any("重启前的记忆" in row["content"] for row in backend.memories(limit=50))
    assert backend.model_status()["cloud_key_present"] is False


def test_backend_save_cloud_key_uses_keyring(backend):
    ring = _FakeKeyring()
    backend._keyring = ring
    result = backend.save_cloud_key("sk-test-123")
    assert result["ok"] is True
    assert ring.values[("super-brain-v21", "cloud_api_key")] == "sk-test-123"
    assert backend.model_status()["cloud_key_present"] is True
    backend.save_cloud_key("")
    assert backend.model_status()["cloud_key_present"] is False


def test_settings_page_renders_all_groups_and_saves(backend, desktop_config, qapp, tmp_path):
    store = SettingsStore(desktop_config, keyring_backend=_FakeKeyring(), qsettings=_FakeQSettings())
    page = SettingsPage(backend, store)
    patch = page._editor.collect()
    for section in ("存储引擎", "模型（本地 / 云端）", "召回与精排", "遗忘与强化（FSRS）",
                    "分层流动", "丘脑准入与去重", "情感阈值（PAD / 冷静期）", "同步参数", "网关",
                    "星云激活", "外观与动画"):
        assert section in patch, f"缺分组：{section}"

    # 修改一个运行期参数 + 一个启动期参数
    page._editor._bindings  # noqa: B018  确认绑定存在
    backend_save = backend.save_settings_patch({
        "recall": {"top_k": 6},
        "storage": {"engines_enabled": {
            "relational": True, "vector": False, "graph": True,
            "kv": True, "timeseries": True, "hypergraph": True,
        }},
    })
    assert backend_save["ok"] is True
    assert "storage.engines_enabled" in backend_save["restart_needed"]
    assert backend.config.recall.top_k == 6

    fired: list[int] = []
    page.kernel_restarted.connect(lambda: fired.append(1))
    page.restart_kernel()
    assert fired == [1]
    assert backend.config.storage.engines_enabled["vector"] is False
    assert backend.applied_overrides  # 重启时应用了用户补丁
