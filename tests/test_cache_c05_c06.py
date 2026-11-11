"""C05 情节缓冲快照（chat/engine.py）与 C06 稳定前缀缓存（gateway/mcp_server.py）。

全部 fake toolkit / 无网络：ChatEngine 直接驱动 `_handle_message`（不起 WS 服务），
MCPToolkit 只测纯函数部分（`_compose_with_prefix_cache` / `_context_fingerprint`）。
"""
from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest

import core.cache_manager as cache_module
import core.context_layers as context_layers_module
import core.persona as persona_module
from chat.engine import ChatEngine
from core.cache_manager import CacheManager, configure_cache_manager
from core.context_layers import ContextLayers
from core.persona import PersonaLayer
from gateway.mcp_server import MCPToolkit


# ── fakes ────────────────────────────────────────────────────────────────
class FakeToolkit:
    def recall_for_agent(self, agent, args):
        return {"hits": [{"id": "n-1", "content": "x", "score": 1.0},
                         {"id": "n-2", "content": "y", "score": 0.9}],
                "injection": "INJ", "channel": args.get("channel")}

    def save_for_agent(self, agent, args):
        return {"applied": True}


class FakeRouter:
    def complete(self, prompt, **kwargs):
        return {"text": "reply", "model": "fake", "kind": "local", "fallback": False}


class FakeLayers:
    """纯内存版 ContextLayers：只暴露 C06 用到的三个接口。"""

    def __init__(self, revision: int = 1) -> None:
        self._revision = revision

    def status(self):
        return {"revision": self._revision, "layers": {}}

    def compose(self, query, max_runtime=6, max_docs=3, channel="workbuddy"):
        return f"P[{channel}|r{self._revision}]Q[{query}]"

    def documents_search(self, query, limit=3):
        return []


def make_config():
    chat = SimpleNamespace(host="127.0.0.1", port=0, persona_channel="bot",
                           max_history_turns=5, deep_async=False)
    return SimpleNamespace(chat=chat)


def make_engine(**extra) -> ChatEngine:
    return ChatEngine(config=make_config(), toolkit=FakeToolkit(),
                      router=FakeRouter(), write_gate=None, agent=None,
                      token="t", **extra)


@pytest.fixture()
def isolated_layers(tmp_path, monkeypatch):
    """把 context_layers / persona 单例指到临时目录，避免测试污染真实数据。"""
    layers = ContextLayers(root=tmp_path / "context")
    persona = PersonaLayer(root=tmp_path / "context")
    monkeypatch.setattr(context_layers_module, "_LAYERS", layers)
    monkeypatch.setattr(persona_module, "_PERSONA", persona)
    return layers


@pytest.fixture()
def isolated_cache():
    configure_cache_manager()
    yield cache_module._MANAGER
    cache_module._MANAGER = None


# ── C05：情节缓冲快照 ────────────────────────────────────────────────────
def _turn(engine: ChatEngine, session: str, text: str) -> None:
    asyncio.run(engine._handle_message(session, text, "d1"))


def test_c05_snapshot_incremental_and_persists_to_episodic_ns(isolated_cache):
    engine = make_engine()
    _turn(engine, "s1", "你好")
    _turn(engine, "s1", "在吗")
    snap = engine._snapshots["s1"]
    assert snap["turn_count"] == 2
    assert snap["recalled_memory_ids"] == ["n-1", "n-2"]
    assert len(snap["persona_hash"]) == 16
    assert snap["stale"] is False
    # episodic ns（键=session_id，值 JSON 可序列化）
    stored = isolated_cache.get("episodic", "s1")
    assert stored == snap
    assert isinstance(stored["recalled_memory_ids"], list)


def test_c05_without_cache_manager_falls_back(monkeypatch):
    monkeypatch.setattr(cache_module, "_MANAGER", None)
    engine = make_engine()
    assert engine._cache is None
    _turn(engine, "s1", "你好")  # 不抛异常即回退成功
    assert engine._snapshots["s1"]["turn_count"] == 1


def test_c05_invalidation_marks_stale_precisely(isolated_cache):
    engine = make_engine()
    _turn(engine, "s1", "你好")
    # 命中召回片段 id → stale
    engine._on_memory_updated({"type": "memory_updated", "affected_ids": ["n-2"]})
    assert engine._snapshots["s1"]["stale"] is True
    # 非 memory_updated 事件与空 ids 一律不动
    engine._on_memory_updated({"type": "other", "affected_ids": ["n-1"]})
    engine._on_memory_updated({"type": "memory_updated", "affected_ids": []})
    # 下一轮重建：快照覆盖，stale 复位
    _turn(engine, "s1", "又来了")
    assert engine._snapshots["s1"]["stale"] is False
    assert engine._snapshots["s1"]["turn_count"] == 2


def test_c05_persona_hash_tracks_layers_and_persona(isolated_layers, monkeypatch):
    engine = make_engine()
    base = engine._persona_fingerprint()
    assert base == engine._persona_fingerprint()  # 稳定
    # 层版本变化 → 指纹变化（替换单例并抬高 runtime revision 模拟版本递增）
    engine._snapshots.clear()
    other = ContextLayers(root=isolated_layers.root.parent / "context2")
    bumped = context_layers_module._read_json(other._runtime_path, {})
    bumped["revision"] = 99
    context_layers_module._atomic_json(other._runtime_path, bumped)
    monkeypatch.setattr(context_layers_module, "_LAYERS", other)
    assert engine._persona_fingerprint() != base


# ── C06：prompt_prefix 稳定前缀缓存 ─────────────────────────────────────
def make_toolkit() -> MCPToolkit:
    return MCPToolkit(registry=None, hippocampus=None, prefrontal=None,
                      relational=None, config=make_config())


def test_c06_prefix_cache_hit_and_equivalence(isolated_layers):
    toolkit = make_toolkit()
    layers = isolated_layers
    query = "完全不命中档案的查询zzz"
    channel = "bot"
    # 未装配缓存时回退 compose 原路径
    cache_module._MANAGER = None
    assert toolkit._compose_with_prefix_cache(layers, query, channel) == \
        layers.compose(query, channel=channel)
    # 装配后：命中缓存，结果与 compose 全量一致（档案块为空时前缀即全部）
    configure_cache_manager()
    manager = cache_module._MANAGER
    first = toolkit._compose_with_prefix_cache(layers, query, channel)
    assert first == layers.compose(query, channel=channel)
    second = toolkit._compose_with_prefix_cache(layers, query, channel)
    assert first == second
    # 键定义：sha256(channel + "|" + 层指纹)
    key = hashlib.sha256(
        f"{channel}|{toolkit._context_fingerprint(layers)}".encode("utf-8")).hexdigest()
    assert manager.get("prompt_prefix", key) == layers.compose("", channel=channel)
    cache_module._MANAGER = None


def test_c06_key_changes_with_layer_revision(isolated_cache):
    toolkit = make_toolkit()
    assert toolkit._context_fingerprint(FakeLayers(revision=1)) != \
        toolkit._context_fingerprint(FakeLayers(revision=2))


def test_c06_prefix_persists_ttl_zero(isolated_cache):
    toolkit = make_toolkit()
    layers = FakeLayers()
    toolkit._compose_with_prefix_cache(layers, "q1", "bot")
    key = hashlib.sha256(
        f"bot|{toolkit._context_fingerprint(layers)}".encode("utf-8")).hexdigest()
    stored = isolated_cache.get("prompt_prefix", key)
    assert stored == layers.compose("", channel="bot")  # ttl=0 永久，靠换键失效


def test_c06_without_manager_uses_compose_directly(isolated_layers, monkeypatch):
    toolkit = make_toolkit()
    monkeypatch.setattr(cache_module, "_MANAGER", None)
    assert toolkit._compose_with_prefix_cache(isolated_layers, "q", "workbuddy") == \
        isolated_layers.compose("q", channel="workbuddy")


def test_c05_invalidation_bus_param_kept_unwired(isolated_cache):
    bus = SimpleNamespace(subscribe=lambda fn: None)
    engine = make_engine(invalidation_bus=bus)
    assert engine._invalidation_bus is bus  # 只留参数，接线留给主线程


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
