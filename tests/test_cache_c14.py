"""C14 persona 缓存验收：读走快照、写后精确失效、mgr 缺失回退。

只用 tmp_path 根，不触真实数据根；不调 get()/baseline_text()（它们会拉全局
ContextLayers 单例）。走 compose_text / status / set_params / rollback 链路。
"""
from __future__ import annotations

import json

import pytest

from config import BrainConfig
from core import persona as persona_mod
from core.cache_manager import CacheManager
from core.persona import PersonaLayer


@pytest.fixture()
def mgr() -> CacheManager:
    return CacheManager(config=BrainConfig(_env_file=None))


@pytest.fixture()
def layer(tmp_path, monkeypatch, mgr) -> PersonaLayer:
    monkeypatch.setattr(persona_mod, "get_cache_manager", lambda: mgr)
    return PersonaLayer(root=tmp_path / "context")


def test_write_invalidates_and_reread_gets_new_value(layer, mgr) -> None:
    first = layer.compose_text()
    assert mgr.size("persona") == 1                      # 首读回填快照
    result = layer.set_params({"sassiness": 0.85})
    assert result["ok"] is True
    assert mgr.size("persona") == 0                      # 写后精确失效
    second = layer.compose_text()                        # miss → 重新读盘拿新值
    assert "斗嘴0.85" in second and "斗嘴0.85" not in first
    assert mgr.size("persona") == 1                      # 再读又回填


def test_unchanged_read_serves_from_cache(layer, mgr) -> None:
    layer.compose_text()
    layer._file.unlink()                                 # 拆掉磁盘源，只可能命中缓存
    assert layer.compose_text() == layer.compose_text()
    assert mgr.size("persona") == 1                      # ttl=0 快照仍在
    assert layer._read()["revision"] >= 1


def test_rollback_invalidates_cache(layer, mgr) -> None:
    assert layer.set_params({"sassiness": 0.9})["ok"]
    assert layer.set_params({"sassiness": 0.2})["ok"]    # v2
    assert mgr.size("persona") == 0
    layer.compose_text()                                 # 回填当前 0.2
    result = layer.rollback(1)
    assert result["ok"] is True and result["params"]["sassiness"] == 0.6  # v1 存变更前快照
    assert mgr.size("persona") == 0                       # 回滚后快照失效
    assert "斗嘴0.6" in layer.compose_text()              # 再读拿到回滚值


def test_apply_proposal_invalidates_cache(layer, mgr, monkeypatch) -> None:
    # ★ 阻尼门槛（2026-11-12）：同向 streak 必须跨 ≥24h——把时钟推到三天后逐条喂
    clock = {"ms": persona_mod._now_ms()}
    monkeypatch.setattr(persona_mod, "_now_ms", lambda: clock["ms"])
    for _ in range(3):
        clock["ms"] += 24 * 3600 * 1000 + 60_000  # 每天一条：满足 24h 跨度
        layer.feedback(signal="夸奖", valence=0.8, target="sassiness",
                       session_id=f"s-{_}", note="t")
    proposals = [p for p in layer._read().get("proposals", []) if p["status"] == "pending"]
    assert proposals, "同向跨会话且跨 24h 的反馈应生成提案"
    layer.compose_text()
    result = layer.apply_proposal(proposals[-1]["id"])
    assert result["ok"] is True
    assert mgr.size("persona") == 0                      # 提案应用后失效
    assert f"斗嘴{result['params']['sassiness']}" in layer.compose_text()


def test_damping_blocks_same_day_streak(layer, monkeypatch) -> None:
    """★ 阻尼验收：同天连发 3 次夸奖不生成提案（时间跨度不足 24h），人设不瞬变。"""
    clock = {"ms": persona_mod._now_ms()}
    monkeypatch.setattr(persona_mod, "_now_ms", lambda: clock["ms"])
    for _ in range(3):
        clock["ms"] += 60_000  # 三分钟内连发
        layer.feedback(signal="夸奖", valence=0.8, target="sassiness",
                       session_id=f"s-{_}", note="t")
    proposals = [p for p in layer._read().get("proposals", []) if p["status"] == "pending"]
    assert not proposals, "同天 3 连夸必须被 24h 跨度门槛挡住（防一时情绪冲垮人设）"


def test_no_manager_falls_back_to_disk(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(persona_mod, "get_cache_manager", lambda: None)
    layer = PersonaLayer(root=tmp_path / "context")
    result = layer.set_params({"clinginess": 0.7})
    assert result["ok"] is True                          # 回退语义：功能完整
    on_disk = json.loads(layer._file.read_text(encoding="utf-8"))
    assert on_disk["params"]["clinginess"] == 0.7


def test_cache_disabled_via_config(tmp_path, monkeypatch) -> None:
    disabled = CacheManager(config=BrainConfig(
        _env_file=None, cache=__import__("config").CacheConfig(enabled=False)))
    monkeypatch.setattr(persona_mod, "get_cache_manager", lambda: disabled)
    layer = PersonaLayer(root=tmp_path / "context")
    layer.compose_text()
    assert disabled.size("persona") == 0                 # 总开关关 → get 恒 miss
    layer._file.unlink()
    assert "【可调参数】" in layer.compose_text()        # 仍走盘路径
