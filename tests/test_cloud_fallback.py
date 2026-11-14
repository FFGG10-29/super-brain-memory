"""云端优先 + 失败回落本地（2026-11-12 用户裁决）+ 三模型真实状态报告。

- Fallback Embedder/Reranker/LLM：云端优先，EngineUnavailable 回落本地（懒加载），
  三态熔断 CLOSED→OPEN→HALF_OPEN，云端恢复后半探针自动切回。
- _describe_model：穿透 Fallback 报告【运行时真实生效】实现，不报配置期望值/占位。
- 共享 OAuth vault：discover_accounts 扫 wbmod vault，与桌面端凭证并存。
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from brain.cloud_models import (
    CloudReranker, FallbackEmbedder, FallbackLLM, FallbackReranker, _Circuit,
)
from core.errors import EngineUnavailable
from core.interfaces import Embedding, ScoredId
from engine.local_llm import DisabledLLM
from models.neuron import PAD  # noqa: F401  若测试间共享类型


# ── 三态熔断器 ──────────────────────────────────────────────────────────
def test_circuit_closed_to_open() -> None:
    c = _Circuit(threshold=2, cooldown=60.0)
    assert c.should_try_cloud() is True       # CLOSED
    c.fail()
    assert c.should_try_cloud() is True       # 未达阈值仍试
    c.fail()
    assert c.should_try_cloud() is False      # 达阈值→OPEN
    assert c.state == "open"


def test_circuit_half_open_recovers() -> None:
    c = _Circuit(threshold=1, cooldown=0.0)
    c.fail()
    assert c.state == "half_open"             # cooldown=0 立即半开
    assert c.should_try_cloud() is True
    c.ok()
    assert c.state == "closed"                # 探针成功自动切回


def test_circuit_half_open_reopens_on_fail() -> None:
    c = _Circuit(threshold=1, cooldown=60.0)
    c.fail()
    assert c.state == "open"
    time.sleep(0.01)
    c._open_until = time.monotonic() - 1      # 模拟熔断到期
    assert c.state == "half_open"
    c.fail()
    assert c.state == "open"                  # 探针失败重新熔断


# ── Fallback 行为 ───────────────────────────────────────────────────────
class FakeCloudEmbedder:
    dense_dim = 8
    degraded = False

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def embed_documents(self, texts):
        self.calls += 1
        if self.fail:
            raise EngineUnavailable("cloud down")
        return [Embedding(dense=[1.0] * 8) for _ in texts]


class FakeLocalEmbedder:
    dense_dim = 8
    degraded = False

    def __init__(self) -> None:
        self.calls = 0

    def embed_documents(self, texts):
        self.calls += 1
        return [Embedding(dense=[2.0] * 8) for _ in texts]


def test_fallback_embedder_uses_cloud_when_ok() -> None:
    cloud, local_holder = FakeCloudEmbedder(), {}
    def factory():
        local_holder["v"] = FakeLocalEmbedder()
        return local_holder["v"]
    fb = FallbackEmbedder(cloud, factory)
    out = fb.embed_documents(["x"])
    assert out[0].dense[0] == 1.0
    assert fb.using_local is False
    assert local_holder == {}, "云端可用时绝不预热本地模型"


def test_fallback_embedder_falls_back_and_lazy_loads() -> None:
    cloud = FakeCloudEmbedder(fail=True)
    local_holder = {}
    def factory():
        local_holder["v"] = FakeLocalEmbedder()
        return local_holder["v"]
    fb = FallbackEmbedder(cloud, factory, circuit_threshold=1)
    assert local_holder == {}
    out = fb.embed_documents(["x"])
    assert out[0].dense[0] == 2.0, "云端失败应回落本地"
    assert fb.using_local is True
    assert "v" in local_holder, "回落时才懒加载本地"
    # OPEN 期不再试云端（避免每条白等超时）
    before = cloud.calls
    fb.embed_documents(["y"])
    assert cloud.calls == before


def test_fallback_embedder_recovers_via_half_open() -> None:
    cloud = FakeCloudEmbedder()
    local = FakeLocalEmbedder()
    fb = FallbackEmbedder(cloud, lambda: local, circuit_threshold=1, circuit_cooldown=0.0)
    cloud.fail = True
    fb.embed_documents(["x"])            # 失败→OPEN
    assert fb.using_local is True
    cloud.fail = False
    time.sleep(0.01)
    fb._circuit._open_until = time.monotonic() - 1  # 熔断到期
    out = fb.embed_documents(["y"])      # HALF_OPEN 探针成功
    assert out[0].dense[0] == 1.0
    assert fb.using_local is False, "云端恢复后自动切回"


class FakeCloudReranker:
    degraded = False
    def __init__(self) -> None:
        self.calls = 0
    def rerank(self, query, candidates, *, top_k=None):
        self.calls += 1
        return [ScoredId(id=cid, score=0.9, channel="cloud") for cid, _ in candidates]


class FakeLocalReranker:
    degraded = False
    def rerank(self, query, candidates, *, top_k=None):
        return [ScoredId(id=cid, score=0.5, channel="local") for cid, _ in candidates]


def test_fallback_reranker_falls_back() -> None:
    cloud = FakeCloudReranker()
    def boom_rerank(q, c, *, top_k=None):
        raise EngineUnavailable("down")
    cloud.rerank = boom_rerank  # type: ignore[assignment]
    fb = FallbackReranker(cloud, FakeLocalReranker, circuit_threshold=1)
    out = fb.rerank("q", [("a", "A")])
    assert out[0].channel == "local"
    assert fb.using_local is True


def test_fallback_llm_falls_back_to_disabled() -> None:
    class BadCloud:
        degraded = False
        def complete_json(self, *a, **kw):
            raise EngineUnavailable("down")
        def complete_text(self, *a, **kw):
            raise EngineUnavailable("down")
    fb = FallbackLLM(BadCloud(), DisabledLLM, circuit_threshold=1)
    # DisabledLLM.complete_json 抛 EngineUnavailable（占位），验证回落路径被调用而非崩溃
    with pytest.raises(EngineUnavailable):
        fb.complete_json("sys", "usr", required_keys=("x",))
    assert fb.using_local is True


# ── 真实状态报告（穿透 Fallback）────────────────────────────────────────
def _mk_fallback(*, using_local: bool, circuit: str):
    """构造真实类名的 Fallback 实例（_describe_model 按 type().__name__ 判断）。"""
    cloud = SimpleNamespace(_model="BAAI/bge-m3", degraded=False)

    class FallbackEmbedder:  # 类名必须与生产一致
        def __init__(self) -> None:
            self._cloud = cloud
            self._local = SimpleNamespace() if using_local else None
            self.using_local = using_local
            self.circuit_state = circuit
            self.degraded = using_local

    return FallbackEmbedder()


def test_describe_model_fallback_active_cloud() -> None:
    from v3.rpc import RpcHandler
    inst = _mk_fallback(using_local=False, circuit="closed")
    d = RpcHandler._describe_model(inst, configured="cloud", local_label="BgeM3Embedder")
    assert d["active"] == "cloud" and d["model"] == "BAAI/bge-m3"
    assert "云端可用" in d["detail"]


def test_describe_model_fallback_fell_back() -> None:
    from v3.rpc import RpcHandler
    inst = _mk_fallback(using_local=True, circuit="open")
    d = RpcHandler._describe_model(inst, configured="cloud", local_label="BgeM3Embedder")
    assert d["active"] == "local" and d["configured"] == "cloud"
    assert "回落本地" in d["detail"]


def test_describe_model_plain() -> None:
    from v3.rpc import RpcHandler
    from engine.reranker import IdentityReranker
    d = RpcHandler._describe_model(IdentityReranker(), configured="local")
    assert d["active"] == "local" and d["kind"] == "IdentityReranker"
    assert "实际=配置" in d["detail"]


def test_gpu_memory_no_fabrication(monkeypatch) -> None:
    """GPU 探测失败必须返回 null，绝不硬编码 95%。"""
    import subprocess
    from v3.rpc import RpcHandler
    def boom(*a, **kw):
        raise FileNotFoundError("no nvidia-smi")
    monkeypatch.setattr(subprocess, "run", boom)
    g = RpcHandler._gpu_memory()
    assert g["available"] is False
    assert g["used_pct"] is None and g["used_mb"] is None


# ── 共享 OAuth vault 扫描（隔离环境下）──────────────────────────────────
def test_vault_scan_respects_env(monkeypatch, tmp_path) -> None:
    import json
    from gateway.account_pool import store
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "oauth-acct.info").write_text(json.dumps({
        "version": 1, "source": "oauth-browser", "accountId": "oauth-acct",
        "auth": {"accessToken": "tok-oauth", "refreshToken": "", "expiresAt": 0, "domain": "www.codebuddy.cn"},
        "account": {"uid": "u-oauth", "nickname": "扫码号"},
    }), encoding="utf-8")
    from gateway.account_pool.store import account_id_of
    monkeypatch.setenv("XDPOOL_VAULT_DIRS", str(vault))
    monkeypatch.setenv("XDPOOL_ACCOUNTS_FILE", str(tmp_path / "none.json"))
    monkeypatch.setenv("WORKBUDDY_AUTH_FILE", str(tmp_path / "none.info"))
    accs = store.discover_accounts()
    ids = [c.account_id() for c in accs]
    # account_id 是 uid 的哈希（不是 vault 文件名）；校验哈希命中 + 凭证真实可读
    assert account_id_of("", "u-oauth", "") in ids
    hit = next(c for c in accs if c.nickname == "扫码号")
    assert hit.access_token == "tok-oauth"
    assert hit.region == "cn"


def test_vault_scan_disabled_when_env_empty(monkeypatch, tmp_path) -> None:
    from gateway.account_pool import store
    monkeypatch.setenv("XDPOOL_VAULT_DIRS", "")
    monkeypatch.setenv("XDPOOL_ACCOUNTS_FILE", str(tmp_path / "none.json"))
    monkeypatch.setenv("WORKBUDDY_AUTH_FILE", str(tmp_path / "none.info"))
    assert store.discover_accounts() == []
