"""C17 语义输出缓存验收（Q34 缓一档的实现补齐）。

fake embedder 提供可控向量：同义 prompt 近邻命中、正交 miss、阈值按 intent
分层、时效性 prompt 绕过、sensitive 跳过、memory_updated 精确失效、
无 embedder 回退、精确键优先于语义命中。
"""
from __future__ import annotations

import pytest

import core.cache_manager as cache_manager_module
from config import BrainConfig, CacheConfig, ModelRouterConfig
from core.cache_invalidation import CacheInvalidationBus
from core.cache_manager import configure_cache_manager
from gateway.model_router import ModelClient, ModelRouter, _semantic_key


class FakeClient(ModelClient):
    def __init__(self, name: str = "local-a", *, caps=("chat",), weight: float = 1.0,
                 fail: bool = False) -> None:
        super().__init__(name=name, caps=tuple(caps), weight=weight)
        self.fail = fail
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        if self.fail:
            raise RuntimeError("429")
        return f"{self.name}:{prompt}"


class FakeEmbedder:
    """可控向量 embedder：按 normalize 后的 prompt 查表，缺省回 default。"""

    def __init__(self, table: dict[str, list[float]] | None = None,
                 default: list[float] | None = None) -> None:
        self.table = dict(table or {})
        self.default = list(default) if default is not None else [1.0, 0.0]
        self.calls = 0

    def embed_query(self, text: str) -> list[float]:
        self.calls += 1
        return list(self.table.get(text, self.default))


def _config() -> BrainConfig:
    return BrainConfig(_env_file=None, model_router=ModelRouterConfig(enabled=True),
                       cache=CacheConfig(namespaces={
                           "output": {"enabled": True, "capacity": 100,
                                      "ttl_sec": 3600, "media": "memory"},
                           "semantic": {"enabled": True, "capacity": 64,
                                        "ttl_sec": 86400, "media": "memory"}}))


@pytest.fixture()
def cache_on():
    configure_cache_manager(config=_config())
    yield
    # 还原为全关 manager：get 恒 miss / set 无操作，不污染其他测试
    configure_cache_manager(
        config=BrainConfig(_env_file=None, cache=CacheConfig(enabled=False)))


def _mgr():
    return cache_manager_module.get_cache_manager()


def test_semantic_hit_same_meaning(cache_on) -> None:
    client = FakeClient()
    embedder = FakeEmbedder({"苹果 是 什么 水果": [1.0, 0.0],
                             "请问苹果是什么水果": [1.0, 0.0]})
    router = ModelRouter(_config(), clients=[client], embedder=embedder)
    first = router.complete("苹果 是 什么 水果", intent="chat", session_id="s1")
    assert first["cache_hit"] is False and client.calls == 1
    # normalize 后键不同（精确缓存 miss），但 embedding 相同 → 语义命中
    second = router.complete("请问苹果是什么水果", intent="chat", session_id="s2")
    assert client.calls == 1
    assert second["cache_hit"] is True and second["kind"] == "cache"
    assert second["cache_kind"] == "semantic"
    assert second["text"] == first["text"]
    assert embedder.calls == 2   # 查询各编码一次；写入复用查询向量不再编码


def test_orthogonal_prompt_misses(cache_on) -> None:
    client = FakeClient()
    embedder = FakeEmbedder({"a": [1.0, 0.0]}, default=[0.0, 1.0])
    router = ModelRouter(_config(), clients=[client], embedder=embedder)
    router.complete("a", intent="chat", session_id="s1")   # 写入向量 [1,0]
    second = router.complete("b", intent="chat", session_id="s2")  # 正交 [0,1]
    assert client.calls == 2 and second["cache_hit"] is False


def test_threshold_layered_by_intent(cache_on) -> None:
    client = FakeClient()
    # cosine 恰为 0.9：介于 chat(0.85) 与 code(0.93) 之间（向量模长 1）
    vec = [0.9, 0.435889894354067]
    embedder = FakeEmbedder({"p-chat": vec, "p-code": vec})
    router = ModelRouter(_config(), clients=[client], embedder=embedder)
    _mgr().set("semantic", "seed",
               {"embedding": [1.0, 0.0], "text": "seed", "model": "local-a"})
    hit = router.complete("p-chat", intent="chat", session_id="s1")
    assert hit["cache_hit"] is True and hit["cache_kind"] == "semantic"
    assert client.calls == 0
    miss = router.complete("p-code", intent="code", session_id="s2")
    assert miss["cache_hit"] is False and client.calls == 1


def test_time_sensitive_prompt_bypasses_semantic(cache_on) -> None:
    client = FakeClient()
    embedder = FakeEmbedder()
    router = ModelRouter(_config(), clients=[client], embedder=embedder)
    router.complete("今天天气如何", intent="chat", session_id="s1")
    assert embedder.calls == 0                       # 不查也不写
    assert _mgr().iter_entries("semantic") == []
    again = router.complete("今天天气如何", intent="chat", session_id="s1")
    assert again["cache_hit"] is True and client.calls == 1   # 仍走精确缓存


def test_sensitive_skips_semantic_read_and_write(cache_on) -> None:
    client = FakeClient()
    embedder = FakeEmbedder()
    router = ModelRouter(_config(), clients=[client], embedder=embedder)
    for _ in range(2):
        router.complete("隐私内容", intent="chat", session_id="s1", sensitive=True)
    assert embedder.calls == 0 and client.calls == 2
    assert _mgr().iter_entries("semantic") == []


def test_memory_updated_invalidates_semantic_entry(cache_on) -> None:
    bus = CacheInvalidationBus()
    configure_cache_manager(config=_config(), bus=bus)
    client = FakeClient()
    embedder = FakeEmbedder(default=[1.0, 0.0])
    router = ModelRouter(_config(), clients=[client], embedder=embedder)
    router.complete("苹果是什么", intent="chat", session_id="s1", memory_ids=["n1"])
    value = _mgr().get("semantic", _semantic_key("苹果是什么"))
    assert value["affected_ids"] == ["n1"] and value["text"] == "local-a:苹果是什么"
    delivered = bus.publish({"type": "memory_updated", "event_type": "overwrite",
                             "affected_ids": ["n1"],
                             "affected_namespaces": ["semantic", "output"]})
    assert delivered >= 1
    second = router.complete("请问苹果是什么", intent="chat", session_id="s2")
    assert second["cache_hit"] is False and client.calls == 2   # 失效后 miss


def test_no_embedder_falls_back(cache_on) -> None:
    client = FakeClient()
    router = ModelRouter(_config(), clients=[client])   # 未注入 embedder
    router.complete("甲问题", intent="chat", session_id="s1")
    second = router.complete("甲问题2", intent="chat", session_id="s2")
    assert client.calls == 2 and second["cache_hit"] is False
    assert _mgr().iter_entries("semantic") == []


def test_exact_key_beats_semantic_hit(cache_on) -> None:
    client = FakeClient()
    embedder = FakeEmbedder(default=[1.0, 0.0])
    router = ModelRouter(_config(), clients=[client], embedder=embedder)
    first = router.complete("苹果是什么", intent="chat", session_id="s1")
    # 埋一个高相似（cosine=1.0）但文本不同的语义诱饵
    _mgr().set("semantic", "decoy",
               {"embedding": [1.0, 0.0], "text": "语义诱饵", "model": "local-a"})
    second = router.complete("苹果是什么", intent="chat", session_id="s1")
    assert client.calls == 1
    assert second["text"] == first["text"] != "语义诱饵"
    assert second.get("cache_kind") != "semantic"    # 走的是精确键路径
