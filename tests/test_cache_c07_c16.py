"""C07 路由决策缓存 + C16 精确输出缓存验收（Q29 / Q34）。

C07：同 (intent, caps) 粘性生效、caps 变化突破、fallback 链写回并复用。
C16：output 命名空间默认关（Q34），显式开启才生效；只缓存确定性问答 intent；
值带 affected_ids（memory_ids）供 memory_updated 精确失效。
"""
from __future__ import annotations

import pytest

import core.cache_manager as cache_manager_module
from config import BrainConfig, CacheConfig, ModelRouterConfig
from core.cache_manager import configure_cache_manager
from gateway.model_router import CLOUD, LOCAL, ModelClient, ModelRouter, _output_cache_key


class FakeClient(ModelClient):
    def __init__(self, name: str, kind: str = LOCAL, *,
                 caps=("chat",), weight: float = 1.0, fail: bool = False) -> None:
        super().__init__(name=name, kind=kind, caps=tuple(caps), weight=weight)
        self.fail = fail
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        if self.fail:
            raise RuntimeError("429")
        return f"{self.name}:{prompt}"


def _config(cache_enabled: bool) -> BrainConfig:
    return BrainConfig(_env_file=None, model_router=ModelRouterConfig(enabled=True),
                       cache=CacheConfig(namespaces={"output": {
                           "enabled": cache_enabled, "capacity": 100,
                           "ttl_sec": 3600, "media": "memory"}}))


@pytest.fixture()
def cache_on():
    configure_cache_manager(config=_config(True))
    yield
    # 还原为全关 manager：get 恒 miss / set 无操作，不污染其他测试
    configure_cache_manager(
        config=BrainConfig(_env_file=None, cache=CacheConfig(enabled=False)))


# ── C07：粘性与 caps 突破 ────────────────────────────────────────────────
def test_sticky_same_intent_and_caps_reused() -> None:
    fast = FakeClient("fast-a", caps=("chat", "fast"))
    router = ModelRouter(_config(False), clients=[fast])
    first = router.route("chat", "s1")
    again = router.route("chat", "s1")
    assert first.name == again.name == "fast-a"
    assert router._sticky["s1"][1] == "chat"           # intent
    assert router._sticky["s1"][3] == ()               # 未传 caps → 空 tuple 随 sticky 记录
    caps_router = ModelRouter(_config(False), clients=[fast])
    caps_router.route("chat", "s2", ("chat",))
    assert caps_router._sticky["s2"][3] == ("chat",)   # caps 随 sticky 记录


def test_caps_change_breaks_sticky() -> None:
    plain = FakeClient("plain", caps=("chat",))
    vision = FakeClient("vision", caps=("chat", "vision"))
    router = ModelRouter(_config(False), clients=[plain, vision])
    assert router.route("chat", "s-caps").name == "plain"
    # caps 变化允许突破粘性，重新选出具备 vision 能力的客户端
    assert router.route("chat", "s-caps", ("vision",)).name == "vision"


def test_fallback_chain_cached_and_reused() -> None:
    broken = FakeClient("broken", fail=True, weight=99.0)
    healthy = FakeClient("healthy")
    router = ModelRouter(_config(False), clients=[broken, healthy])
    first = router.complete("你好", intent="chat", session_id="s-chain")
    assert first["ok"] is True and first["text"] == "healthy:你好"
    assert router._sticky["s-chain"][4] == ["broken", "healthy"]  # 链已写回

    # broken 失败后成功率下降，重新打分会偏向 healthy；但缓存的链仍先试 broken
    router.route("chat", "s-chain")  # 刷新 sticky（链保留）
    second = router.complete("你好", intent="chat", session_id="s-chain")
    assert second["text"] == "healthy:你好"
    assert broken.calls == 2                            # 链顺序复用：仍先试 broken
    assert router._sticky["s-chain"][4] == ["broken", "healthy"]


def test_sticky_client_health_change_reruns_eligible() -> None:
    # 粘性客户端从池中消失（健康变化）时，必须重新选路而非盲用 sticky
    plain = FakeClient("plain", caps=("chat",))
    router = ModelRouter(_config(False), clients=[plain])
    assert router.route("chat", "s-health").name == "plain"
    router._pool.remove(plain)
    rescue = FakeClient("rescue", caps=("chat",))
    router._pool.append(rescue)
    assert router.route("chat", "s-health").name == "rescue"
    # caps 过滤同样每次重跑：sticky 客户端不再满足新 caps 即突破
    plain2 = FakeClient("plain2", caps=("chat",))
    vision = FakeClient("vision2", caps=("chat", "vision"))
    router2 = ModelRouter(_config(False), clients=[plain2, vision])
    assert router2.route("chat", "s-v", ("vision",)).name == "vision2"
    assert router2.route("chat", "s-v").name == "plain2"  # caps 变回后粘性对象随之更新


# ── C16：精确输出缓存（显式开启才生效）─────────────────────────────────
def test_c16_hit_skips_model_call(cache_on) -> None:
    client = FakeClient("local-a")
    router = ModelRouter(_config(True), clients=[client])
    first = router.complete("  今天   天气 如何  ", intent="chat", session_id="s-c16")
    assert first["ok"] is True and first["cache_hit"] is False
    assert client.calls == 1
    # normalize（去首尾空白 + 压缩连续空白 + lower）后命中，零模型调用
    second = router.complete("今天 天气 如何", intent="chat", session_id="s-c16")
    assert second == {"ok": True, "text": "local-a:  今天   天气 如何  ",
                      "model": "local-a", "kind": "cache",
                      "fallback": False, "cache_hit": True}
    assert client.calls == 1


def test_c16_intent_mismatch_or_model_pref_change_misses(cache_on) -> None:
    client = FakeClient("local-a")
    router = ModelRouter(_config(True), clients=[client])
    router.complete("问题A", intent="chat", session_id="s-k")
    # intent 不同 → 键不同 → miss
    router.complete("问题A", intent="code", session_id="s-k")
    assert client.calls == 2


def test_c16_emotion_never_cached(cache_on) -> None:
    client = FakeClient("persona-a")
    router = ModelRouter(_config(True), clients=[client])
    for _ in range(2):
        result = router.complete("我很低落", intent="emotion", session_id="s-em")
    assert client.calls == 2          # emotion 不属于确定性问答，不写缓存
    assert result["cache_hit"] is False


def test_c16_disabled_by_default_behaves_unchanged() -> None:
    # Q34：output 默认关——manager 在但 ns 关闭 → 恒 miss，行为与无缓存一致
    configure_cache_manager(config=_config(False))
    try:
        client = FakeClient("local-a")
        router = ModelRouter(_config(False), clients=[client])
        for _ in range(2):
            result = router.complete("你好", intent="chat", session_id="s-off")
        assert client.calls == 2 and result["cache_hit"] is False
    finally:
        configure_cache_manager(
            config=BrainConfig(_env_file=None, cache=CacheConfig(enabled=False)))


def test_c16_no_manager_falls_back(monkeypatch) -> None:
    monkeypatch.setattr(cache_manager_module, "_MANAGER", None)
    client = FakeClient("local-a")
    router = ModelRouter(_config(False), clients=[client])
    for _ in range(2):
        result = router.complete("你好", intent="chat", session_id="s-none")
    assert client.calls == 2 and result["ok"] is True


def test_c16_memory_ids_stored_for_invalidation(cache_on) -> None:
    client = FakeClient("local-a")
    router = ModelRouter(_config(True), clients=[client])
    router.complete("问题B", intent="tool_call", session_id="s-ids",
                    memory_ids=["n1", "n2"])
    key = _output_cache_key("问题B", "tools", "tool_call")
    mgr = cache_manager_module.get_cache_manager()
    value = mgr.get("output", key)
    assert value["affected_ids"] == ["n1", "n2"]
    assert value["model"] == "local-a" and value["text"] == "local-a:问题B"


def test_c16_sensitive_skips_cache(cache_on) -> None:
    client = FakeClient("local-a")
    cloud = FakeClient("free-cloud", kind=CLOUD, weight=99.0)
    router = ModelRouter(_config(True), clients=[client, cloud])
    for _ in range(2):
        result = router.complete("隐私内容", intent="chat", session_id="s-priv",
                                 sensitive=True)
    assert client.calls == 2            # sensitive 不读不写缓存
    assert result["model"] == "local-a" and result["cache_hit"] is False


def test_c07_c16_coexist_fallback_then_cached_text(cache_on) -> None:
    broken = FakeClient("broken", fail=True, weight=99.0)
    healthy = FakeClient("healthy")
    router = ModelRouter(_config(True), clients=[broken, healthy])
    first = router.complete("你好", intent="chat", session_id="s-mix")
    assert first["fallback"] is True and first["cache_hit"] is False
    second = router.complete("你好", intent="chat", session_id="s-mix")
    # 命中的是 healthy 成功结果写进的输出缓存，而非再跑一遍链
    assert second["cache_hit"] is True and second["text"] == "healthy:你好"
    assert broken.calls == 1 and healthy.calls == 1
