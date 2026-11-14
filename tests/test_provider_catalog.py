"""供应商注册表裁剪 + 免密 lane 删除 + 免费模型类别标注（2026-11-12）。

锁定三件事：
1. 77 家供应商砍到 5 家白名单（硅基流动/OpenRouter/火山/BytePlus/自定义）。
2. 11 个免密免费模型彻底删除：fetch_free_models 恒空、FALLBACK_FREE_IDS 空、
   历史 catalog.json 缓存里的 free.models 被读取层屏蔽。
3. 硅基流动 15 个 / OpenRouter 17 个模型每个都有 category。
"""
from __future__ import annotations

import json

import pytest

from gateway.account_pool import catalog

ALLOWED = {"siliconflow", "open_router", "volcengine-ark", "byteplus", "custom_openai"}
CATEGORIES = {"embedding", "rerank", "llm", "llm-code", "llm-vision",
              "asr", "asr-diarization", "translation", "tts", "router"}


def _raw_registry() -> list[dict]:
    path = catalog.Path(catalog.__file__).with_name("providers_registry.json")
    return json.loads(path.read_text(encoding="utf-8"))


# ── 1. 供应商白名单 ──────────────────────────────────────────────────────────
def test_registry_only_five_providers():
    ids = {x["id"] for x in catalog.provider_registry()}
    assert ids == ALLOWED, f"供应商应严格为 5 家白名单，实际={ids}"


def test_removed_providers_absent():
    ids = {x["id"] for x in catalog.provider_registry()}
    for gone in ("openai_api", "anthropic", "deepseek", "groq", "minimax",
                 "kimi", "xai", "glm", "ollama_cloud", "opencode"):
        assert gone not in ids, f"{gone} 应从注册表删除"


# ── 2. 硅基流动 15 个 + 类别 ─────────────────────────────────────────────────
def test_siliconflow_has_15_models_with_category():
    sf = next(x for x in _raw_registry() if x["id"] == "siliconflow")
    models = sf["models"]
    assert len(models) == 15, f"硅基流动应恰好 15 个，实际 {len(models)}"
    for m in models:
        assert m.get("id"), "模型必须有 id"
        assert m.get("category") in CATEGORIES, f"{m.get('id')} 类别缺失/非法: {m.get('category')}"


def test_siliconflow_expected_ids():
    sf = next(x for x in _raw_registry() if x["id"] == "siliconflow")
    ids = {m["id"] for m in sf["models"]}
    for expected in ("BAAI/bge-m3", "BAAI/bge-reranker-v2-m3",
                     "tencent/Hunyuan-MT-7B", "FunAudioLLM/SenseVoiceSmall",
                     "XingChenAGI/Xing4.0-29B"):
        assert expected in ids


# ── 3. OpenRouter 免费模型 + 类别 ────────────────────────────────────────────
def test_openrouter_models_have_category_and_source():
    op = next(x for x in _raw_registry() if x["id"] == "open_router")
    models = op["models"]
    # 2026-11-12 实测裁剪：3 个 chat:free 上游下架、3 个特殊模态（embed/rerank/tts）
    # OpenRouter 无对应端点，全部删除；只留实测能通 chat/completions 的 11 个。
    assert len(models) >= 10, f"OpenRouter 实测可用模型不应少于 10，实际 {len(models)}"
    for m in models:
        assert m.get("category") in CATEGORIES, f"{m.get('id')} 类别非法: {m.get('category')}"
        assert m.get("source") in ("api", "html"), f"{m.get('id')} 缺来源标注"


def test_every_provider_has_chat_model_and_category():
    """每家供应商（除自定义口）模型必须都有 category 且至少一个可对话模型，
    否则用户填了 key 却没有能保存的模型（火山/BytePlus 曾缺 category 被误禁选）。"""
    chat = {"llm", "llm-code", "llm-vision", "router"}
    for p in _raw_registry():
        models = p.get("models") or []
        if not models:
            continue  # custom_openai 是自定义口，0 模型
        for m in models:
            assert m.get("category") in CATEGORIES, \
                f"{p['id']}/{m.get('id')} 缺合法 category: {m.get('category')}"
        assert any(m["category"] in chat for m in models), \
            f"{p['id']} 没有任何可对话模型，填 key 将无法保存"


def test_openrouter_no_untestable_modalities():
    """2026-11-12 实测裁决：OpenRouter 只有 chat/completions 端点，
    embed/rerank/tts 类模型在该家永远调不通，注册表不得收录。"""
    op = next(x for x in _raw_registry() if x["id"] == "open_router")
    bad = [m["id"] for m in op["models"] if m.get("category") in ("embedding", "rerank", "tts", "asr")]
    assert not bad, f"OpenRouter 收录了无端点的模态模型: {bad}"


# ── 3b. 域名 / 协议（填 key 就能用的前提）────────────────────────────────
def test_siliconflow_uses_cn_domain():
    sf = next(x for x in _raw_registry() if x["id"] == "siliconflow")
    assert sf["baseUrl"].startswith("https://api.siliconflow.cn/"), \
        f"硅基流动国内 key 必须用 .cn 域名，实际 {sf['baseUrl']}"
    assert "siliconflow.com" not in sf["baseUrl"]


def test_openrouter_uses_official_domain():
    op = next(x for x in _raw_registry() if x["id"] == "open_router")
    assert op["baseUrl"].rstrip("/") == "https://openrouter.ai/api/v1"


def test_provider_set_auto_enables_router():
    """填 key 保存后必须自动打开 model_router.enabled，否则假成功。

    用 monkeypatch 隔离 config/secret 落盘，只验证 enabled 被置 True。
    """
    from types import SimpleNamespace
    from v3.rpc_ext import RpcExtension

    calls: dict[str, object] = {}

    class FakeRouter:
        def _providers_overlay(self): return []
        def save_providers_overlay(self, p): calls["overlay"] = p
        def reload(self): calls["reloaded"] = True

    fake_router = FakeRouter()
    mr = SimpleNamespace(enabled=False, providers=[])
    daemon = SimpleNamespace(_config=SimpleNamespace(model_router=mr))
    handler = RpcExtension.__new__(RpcExtension)
    handler._daemon = daemon
    handler._router = lambda: fake_router
    handler._secret_set = lambda k, v: calls.setdefault("secret", (k, v))

    import core.user_config as uc
    orig_save, orig_apply = uc.save_overrides, uc.apply_overrides
    uc.save_overrides = lambda p: p
    uc.apply_overrides = lambda cfg, merged: setattr(mr, "enabled", True) or ["model_router.enabled"]
    try:
        out = RpcExtension._router_provider_set(
            handler, name="siliconflow", endpoint="https://api.siliconflow.cn/v1",
            model="Qwen/Qwen3-8B", api_key="sk-test")
    finally:
        uc.save_overrides, uc.apply_overrides = orig_save, orig_apply

    assert out["ok"] is True
    assert out["router_enabled"] is True
    assert mr.enabled is True
    assert calls.get("secret") == ("model_router.siliconflow", "sk-test")
    assert calls.get("reloaded") is True


# ── 4. 免密 lane 彻底删除 ────────────────────────────────────────────────────
def test_fetch_free_models_always_empty():
    out = catalog.fetch_free_models()
    assert out["models"] == []
    assert out.get("disabled") is True


def test_fallback_free_ids_cleared():
    assert catalog.FALLBACK_FREE_IDS == ()


def test_cached_free_models_shielded(tmp_path, monkeypatch):
    """历史 catalog.json 里残留的 free.models 必须在读取层被清空。"""
    fake = tmp_path / "catalog.json"
    fake.write_text(json.dumps({
        "ok": True,
        "wb": {"models": []},
        "free": {"models": [{"id": "mimo-v2.6-flash-free"}, {"id": "nemotron-3.5-lightning-free"}],
                 "live": True},
        "saved_at_ms": 9999999999999,
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(catalog, "catalog_path", lambda: fake)
    data = catalog.load_cached_catalog()
    assert data is not None
    assert data["free"]["models"] == [], "历史缓存的 11 个免密模型必须被屏蔽"
    assert data["free"].get("disabled") is True


# ── 5. 持久化（保存按钮有用 / 重启恢复 / key 不重复填）────────────────────
def test_config_persists_embed_cloud(tmp_path, monkeypatch):
    """config.set 写盘后，load_overrides 能读回（下次启动 apply_overrides 恢复）。"""
    import core.user_config as uc
    monkeypatch.setenv("SUPER_BRAIN_CONFIG_DIR", str(tmp_path))
    patch = {"model": {"embed_backend": "cloud",
                       "embed_base_url": "https://api.siliconflow.cn/v1",
                       "embed_model": "BAAI/bge-m3"}}
    merged = uc.save_overrides(patch)
    loaded = uc.load_overrides()
    assert loaded["model"]["embed_backend"] == "cloud"
    assert loaded["model"]["embed_base_url"] == "https://api.siliconflow.cn/v1"
    assert loaded["model"]["embed_model"] == "BAAI/bge-m3"
    assert merged["model"]["embed_backend"] == "cloud"


def test_load_cloud_api_key_falls_back_to_provider_key(tmp_path, monkeypatch):
    """号池页填的 model_router.siliconflow key 应被云端 embedding/rerank 回读到，
    避免同一家 key 在两处重复填。"""
    from types import SimpleNamespace
    from brain.cloud_models import load_cloud_api_key

    store = {"model_router.siliconflow": "sk-from-pool-page"}
    class FakeBackend:
        def get(self, account): return store.get(account, "")
    monkeypatch.setattr("core.desktop_features.default_secret_store", lambda: FakeBackend())
    cfg = SimpleNamespace(model=SimpleNamespace(
        embed_api_key_env="EMBED_API_KEY",
        embed_base_url="https://api.siliconflow.cn/v1"))
    assert load_cloud_api_key(cfg, "embed") == "sk-from-pool-page"


def test_load_cloud_api_key_prefers_channel_specific(tmp_path, monkeypatch):
    """通道专用 key（embed_api_key）优先于号池供应商 key。"""
    from types import SimpleNamespace
    from brain.cloud_models import load_cloud_api_key

    store = {"embed_api_key": "sk-dedicated", "model_router.siliconflow": "sk-pool"}
    class FakeBackend:
        def get(self, account): return store.get(account, "")
    monkeypatch.setattr("core.desktop_features.default_secret_store", lambda: FakeBackend())
    cfg = SimpleNamespace(model=SimpleNamespace(
        embed_api_key_env="EMBED_API_KEY",
        embed_base_url="https://api.siliconflow.cn/v1"))
    assert load_cloud_api_key(cfg, "embed") == "sk-dedicated"


def test_saved_overlay_siliconflow_uses_cn(monkeypatch):
    """已落盘的 model_providers.json 里硅基流动 endpoint 必须是 .cn（旧 .com 残留要清）。"""
    import os
    p = os.path.join(os.environ["APPDATA"], "super-brain", "model_providers.json")
    if not os.path.isfile(p):
        pytest.skip("本机无 model_providers.json overlay")
    rows = json.load(open(p, encoding="utf-8"))
    sf = next((r for r in rows if r.get("name") == "siliconflow"), None)
    if sf is None:
        pytest.skip("本机未保存硅基流动 overlay")
    assert "siliconflow.cn" in sf.get("endpoint", ""), \
        f"overlay 残留旧域名会覆盖注册表的 .cn：{sf.get('endpoint')}"
