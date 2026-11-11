"""W3 / P3 模型路由验收：默认本地、粘性/intent 突破、能力过滤、隐私、fallback、热重载。"""
from __future__ import annotations

from config import BrainConfig, ModelRouterConfig
from gateway.model_router import CLOUD, LOCAL, ModelClient, ModelRouter


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
        return f"{self.name}:ok"


def _config(enabled: bool = False) -> BrainConfig:
    return BrainConfig(model_router=ModelRouterConfig(enabled=enabled))


def _router(clients: list[ModelClient], enabled: bool = True) -> ModelRouter:
    return ModelRouter(_config(enabled), clients=clients)


def test_disabled_router_still_returns_local_client() -> None:
    """Q15/R3：enabled=false 时路由层必须始终可用（默认关闭云模型，巩固代理才不会停摆）。"""
    router = ModelRouter(_config(False))
    client = router.route("consolidation", "s1")
    assert client.kind == LOCAL and client.name == "local-ollama"
    assert all(item.kind == LOCAL for item in router._pool)


def test_sticky_within_intent_and_break_on_intent_change() -> None:
    fast = FakeClient("fast-a", caps=("chat", "fast"))
    strong = FakeClient("strong-b", caps=("chat", "strong"))
    router = _router([fast, strong])
    first = router.route("chat", "session-1")       # intent_map: chat -> fast
    again = router.route("chat", "session-1")       # 同 intent，粘性生效
    assert first.name == again.name == "fast-a"
    switched = router.route("deep_recall", "session-1")  # intent 变化允许突破粘性
    assert switched.name == "strong-b"


def test_required_caps_filters_candidates() -> None:
    plain = FakeClient("plain", caps=("chat",))
    vision = FakeClient("vision", caps=("chat", "vision"))
    router = _router([plain, vision])
    assert router.route("chat", "s-caps", ("vision",)).name == "vision"


def test_sensitive_content_never_goes_to_free_cloud() -> None:
    local = FakeClient("local", caps=("chat",))
    cloud = FakeClient("free-cloud", kind=CLOUD, caps=("chat",), weight=99.0)
    router = _router([local, cloud])
    assert router.route("chat", "s-priv", sensitive=True).name == "local"
    assert router.route("chat", "s-normal").name == "free-cloud"  # 非敏感可用云


def test_fallback_records_event_and_keeps_conversation() -> None:
    broken = FakeClient("broken", fail=True, weight=99.0)
    healthy = FakeClient("healthy")
    events: list[dict] = []
    router = ModelRouter(_config(True), clients=[broken, healthy],
                         on_fallback=events.append)
    result = router.complete("你好", intent="chat", session_id="s-fb")
    assert result["ok"] is True and result["fallback"] is True
    assert result["text"] == "healthy:ok"
    assert events and events[0]["model"] == "broken" and events[0]["error"] == "RuntimeError"


def test_all_providers_down_returns_degraded_text() -> None:
    router = _router([FakeClient("a", fail=True), FakeClient("b", fail=True)])
    result = router.complete("你好", intent="chat", session_id="s-down")
    assert result["ok"] is False and "暂时无法" in result["text"]


def test_config_change_triggers_reload() -> None:
    """C8/R9：走订阅回调热重载，正在进行的 session 靠 sticky 继续用旧模型。"""
    router = ModelRouter(_config(False))
    assert len(router._pool) == 1
    router._config.model_router.enabled = True
    router._config.model_router.providers = [
        {"name": "cloud-a", "kind": "cloud", "endpoint": "http://example.invalid",
         "model": "m", "caps": ["chat"], "weight": 1.0},
    ]
    router._on_config_change({"model_router": {"enabled": True}})
    assert any(item.name == "cloud-a" for item in router._pool)
    assert any(item.kind == LOCAL for item in router._pool)


def test_weighted_score_tracks_success_rate() -> None:
    flaky = FakeClient("flaky")
    stable = FakeClient("stable")
    for _ in range(8):
        flaky.record(False, 900.0)
    router = _router([flaky, stable])
    # 两者其它条件相同，失败多的 flaky 分数应更低
    assert router.route("chat", "s-weight").name == "stable"
