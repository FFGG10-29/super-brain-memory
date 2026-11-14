"""C19 幂等读工具结果缓存 + E6 深度验收（agent_brief 三段 / perspective 前置 / persona mtime）。

覆盖：
- C19：白名单读工具二次调用零 dispatch（fake dispatch 计数）+ 响应带 tool_cache_hit
- C19：写类工具绝不缓存；写成功后整个 tool_read ns 被清空
- C19：recall_memory 不进白名单（召回有 access_log/审计副作用）
- E6：agent_brief 三段（协作要点 + 它写过 topic×n + 最近一次协作时间），TTL 300s
- E6：perspective 匹配当前 agent 的条目前置（稳定排序）+ perspective_matched
- E6：persona 外部手改 persona.json 按 mtime 自动失效重读（tmp_path 写两次）

全部 fake / 夹具零网络、tmp_path 隔离。
"""
from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace
from typing import Any

import pytest

from brain.prefrontal import RecallHit
from config import BrainConfig
from core import cache_manager as cache_manager_mod
from core import persona as persona_mod
from core.cache_manager import CacheManager
from core.persona import PersonaLayer
from gateway.mcp_server import MCPToolkit, _TOOL_READ_WHITELIST
from models.neuron import Neuron

AGENT_ID = "workbuddy"


# ── 测试替身 ─────────────────────────────────────────────────────────────
class FakeRegistry:
    @staticmethod
    def verify(token: str):
        return SimpleNamespace(agent_id=AGENT_ID)

    @staticmethod
    def namespace_allowed(agent: Any, namespace: str) -> bool:
        return True

    @staticmethod
    def permission_allowed(agent: Any, perm: str) -> bool:
        return True

    @staticmethod
    def audit(*_: Any, **__: Any) -> None:
        pass


class FakeHippocampus:
    def __init__(self) -> None:
        self.cognitions: list[dict] = []

    def remember(self, content: str, *, agent_id: str = "", namespace: str = "",
                 cognition: dict | None = None, **_: Any):
        self.cognitions.append({"content": content, "agent_id": agent_id,
                                "namespace": namespace, "cognition": cognition})
        return SimpleNamespace(
            applied=True, superseded=[], merge=None,
            admission=SimpleNamespace(reason="new", duplicate_of=""),
            neuron=Neuron(agent_id=agent_id, content=content, namespace="topic:default"))

    def remember_bundle(self, bundle, *, agent_id: str = "", namespace: str = ""):
        self.cognitions.append({"content": "|".join(i.content for i in bundle.items),
                                "agent_id": agent_id, "namespace": namespace, "bundle": True})
        return {"applied": len(bundle.items), "total": len(bundle.items),
                "neuron_ids": ["n-mock"], "handled": True,
                "complexity": getattr(bundle, "complexity", "")}

    def _defer_orphan(self, content, agent_id, cognition=None):
        self.cognitions.append({"content": content, "deferred": True, "agent_id": agent_id})


class FakeRelational:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[dict] = []

    def query(self, kind: str, **kwargs: Any) -> list[dict]:
        self.calls.append(kwargs)
        return list(self.rows)


class FakePrefrontal:
    def __init__(self, hits: list[RecallHit] | None = None) -> None:
        self.hits = hits or []
        self.last_knowledge_gap: dict | None = None
        self.last_narratives: list = []
        self.last_degraded = False

    def recall(self, query: str, *, agent_id: str = "", namespaces=(), limit: int = 5):
        return self.hits

    def inject(self, hits, *, agent_id: str) -> str:
        return "【记忆注入】测试注入" if hits else ""

    def timeline_query(self, *, since_ms=None, until_ms=None, agent_id="", limit=20):
        return []


def _hit(agent_id: str, content: str, *, perspective: str | None = None) -> RecallHit:
    hit = RecallHit(
        neuron=Neuron(agent_id=agent_id, content=content, namespace="topic:default"),
        score=0.9, channels=("dense",))
    if perspective is not None:
        # 已修文档代码：RecallHit 是 frozen dataclass，普通赋值抛 FrozenInstanceError，
        # 文档注释「普通 dataclass 可附属性」与实际不符；改用 object.__setattr__ 注入。
        object.__setattr__(hit, "perspective", perspective)
    return hit


@pytest.fixture()
def mgr(monkeypatch):
    manager = CacheManager(config=BrainConfig(_env_file=None))
    monkeypatch.setattr(cache_manager_mod, "_MANAGER", manager)
    return manager  # monkeypatch 结束自动还原进程级单例


def _toolkit(prefrontal=None, relational=None, hippocampus=None) -> MCPToolkit:
    return MCPToolkit(
        registry=FakeRegistry(), hippocampus=hippocampus or FakeHippocampus(),
        prefrontal=prefrontal or FakePrefrontal(),
        relational=relational or FakeRelational(),
        config=BrainConfig(_env_file=None))


def _call(toolkit: MCPToolkit, tool: str, args: dict) -> dict:
    return toolkit.call(tool, args, headers={"Authorization": "Bearer fake"})


def _count_dispatch(toolkit: MCPToolkit) -> list[str]:
    calls: list[str] = []
    original = toolkit._dispatch

    def spy(tool: str, args: dict, agent):
        calls.append(tool)
        return original(tool, args, agent)

    toolkit._dispatch = spy
    return calls


# ── C19.1 幂等读缓存 ─────────────────────────────────────────────────────
def test_read_tool_second_call_zero_dispatch_and_hit_flag(mgr) -> None:
    toolkit = _toolkit()
    calls = _count_dispatch(toolkit)
    first = _call(toolkit, "health_check", {})
    assert "tool_cache_hit" not in first
    second = _call(toolkit, "health_check", {})
    assert second.get("tool_cache_hit") is True
    assert calls == ["health_check"]              # 第二次零 dispatch
    assert mgr.size("tool_read") == 1


def test_read_cache_key_distinguishes_args(mgr) -> None:
    toolkit = _toolkit()
    calls = _count_dispatch(toolkit)
    _call(toolkit, "timeline_query", {"limit": 5})
    _call(toolkit, "timeline_query", {"limit": 10})
    assert calls == ["timeline_query", "timeline_query"]   # 参数不同 → 不同键
    _call(toolkit, "timeline_query", {"limit": 10})
    assert len(calls) == 2                                 # 同参第二次命中


def test_error_result_not_cached(mgr) -> None:
    toolkit = _toolkit()
    calls = _count_dispatch(toolkit)
    _call(toolkit, "no_such_tool", {})
    _call(toolkit, "no_such_tool", {})
    assert len(calls) == 2
    assert mgr.size("tool_read") == 0


# ── C19.2 写类工具绝不缓存 + 写后清空读缓存 ─────────────────────────────
def test_write_tool_never_cached(mgr) -> None:
    toolkit = _toolkit()
    calls = _count_dispatch(toolkit)
    for _ in range(2):
        _call(toolkit, "save_memory", {"content": "x"})
    assert calls == ["save_memory", "save_memory"]
    assert mgr.size("tool_read") == 0


def test_write_tool_clears_read_cache(mgr) -> None:
    toolkit = _toolkit()
    calls = _count_dispatch(toolkit)
    _call(toolkit, "health_check", {})
    assert mgr.size("tool_read") == 1
    _call(toolkit, "save_memory", {"content": "新记忆"})
    assert mgr.size("tool_read") == 0            # 写成功 → 整 ns 清空（显式例外）
    _call(toolkit, "health_check", {})
    assert calls.count("health_check") == 2      # 读缓存被清 → 重新 dispatch
    assert mgr.size("tool_read") == 1


def test_recall_memory_not_whitelisted(mgr) -> None:
    toolkit = _toolkit(FakePrefrontal([_hit("agent-x", "既有记忆")]), FakeRelational())
    calls = _count_dispatch(toolkit)
    _call(toolkit, "recall_memory", {"query": "q"})
    _call(toolkit, "recall_memory", {"query": "q"})
    assert calls == ["recall_memory", "recall_memory"]   # 审计副作用 → 绝不缓存
    assert mgr.size("tool_read") == 0


def test_write_marker_classification_and_whitelist_disjoint() -> None:
    assert MCPToolkit._is_write_tool("save_memory")
    assert MCPToolkit._is_write_tool("core_memory_set")
    assert MCPToolkit._is_write_tool("memory_forget")
    assert MCPToolkit._is_write_tool("memory_sensitive")
    assert not MCPToolkit._is_write_tool("memory_list")
    assert not set(filter(MCPToolkit._is_write_tool, _TOOL_READ_WHITELIST))


# ── E6.1 agent_brief 三段 ────────────────────────────────────────────────
def _fact_row(agent_id: str, topic: str, *, age_ms: int = 60_000) -> dict:
    return {"agent_id": agent_id, "memory_type": "fact",
            "content": f"关于{topic}的事实", "namespace": f"topic:{topic}",
            "created_at_ms": int(time.time() * 1000) - age_ms}


def test_agent_brief_three_sections() -> None:
    rows = [
        {"agent_id": AGENT_ID, "content": "协作要点一"},
        {"agent_id": AGENT_ID, "content": "协作要点二"},
        {"agent_id": AGENT_ID, "content": "协作要点三"},
        {"agent_id": AGENT_ID, "content": "协作要点四（截断）"},
        _fact_row(AGENT_ID, "proj"), _fact_row(AGENT_ID, "proj"),
        _fact_row(AGENT_ID, "health"),
        _fact_row("someone-else", "secret"),
    ]
    relational = FakeRelational(rows)
    toolkit = _toolkit(FakePrefrontal([_hit("agent-x", "既有记忆")]), relational)
    result = _call(toolkit, "recall_memory", {"query": "q"})
    injection = result["injection"]
    assert injection.startswith("【与该 Agent 的既往协作要点】")
    assert "协作要点一" in injection and "协作要点三" in injection
    assert "协作要点四" not in injection          # 仍只取前 3 条
    assert "它写过：proj×2、health×1" in injection  # 聚合 top3，频次降序
    assert "最近一次协作时间：1 分钟前" in injection
    assert "secret" not in injection              # 其它 agent 的主题不计入


def test_agent_brief_single_query_per_brief_and_ttl_cache() -> None:
    relational = FakeRelational([{"agent_id": AGENT_ID, "content": "协作要点"}])
    toolkit = _toolkit(FakePrefrontal(), relational)
    _call(toolkit, "recall_memory", {"query": "q"})
    _call(toolkit, "recall_memory", {"query": "q"})
    assert len(relational.calls) == 1             # TTL 内第二次走 brief 缓存
    toolkit._agent_brief_cache[AGENT_ID] = (time.time() - 301, "旧 brief")
    _call(toolkit, "recall_memory", {"query": "q"})
    assert len(relational.calls) == 2             # 过期重建，仍只打一次 relational


# ── E6.2 perspective 前置 ────────────────────────────────────────────────
def test_perspective_rows_front_loaded_and_counted() -> None:
    hits = [
        _hit("agent-a", "无视角甲"),
        _hit("agent-b", "视角乙", perspective=f"{AGENT_ID} 认为：先做 A"),
        _hit("agent-c", "无视角丙"),
        _hit("agent-d", "视角丁", perspective="codebuddy 认为别的"),
    ]
    toolkit = _toolkit(FakePrefrontal(hits), FakeRelational())
    result = _call(toolkit, "recall_memory", {"query": "q"})
    assert [row["content"] for row in result["hits"]] == [
        "视角乙", "无视角甲", "无视角丙", "视角丁"]   # 匹配者前置 + 组内稳定序
    assert result["perspective_matched"] == 1


def test_perspective_absent_zero_matched_no_reorder() -> None:
    toolkit = _toolkit(FakePrefrontal([_hit("a", "x"), _hit("b", "y")]), FakeRelational())
    result = _call(toolkit, "recall_memory", {"query": "q"})
    assert result["perspective_matched"] == 0
    assert [row["content"] for row in result["hits"]] == ["x", "y"]


# ── E6.3 persona 外部手改 mtime 自动失效 ─────────────────────────────────
@pytest.fixture()
def persona_mgr(monkeypatch):
    manager = CacheManager(config=BrainConfig(_env_file=None))
    monkeypatch.setattr(persona_mod, "get_cache_manager", lambda: manager)
    return manager


def test_persona_external_edit_invalidates_via_mtime(tmp_path, persona_mgr) -> None:
    layer = PersonaLayer(root=tmp_path / "context")
    assert "斗嘴0.6" in layer.compose_text()
    assert persona_mgr.size("persona") == 1
    data = json.loads(layer._file.read_text(encoding="utf-8"))   # 模拟外部手改（绕过 _write）
    data["params"]["sassiness"] = 0.95
    layer._file.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    assert "斗嘴0.95" in layer.compose_text()    # mtime 漂移 → 自动失效重读
    assert persona_mgr.size("persona") == 1      # 重读后回填新快照


def test_persona_external_touch_revalidates_mtime(tmp_path, persona_mgr) -> None:
    """仅 mtime 变化（内容不变）也失效——校验的是 mtime，不是内容指纹。"""
    layer = PersonaLayer(root=tmp_path / "context")
    layer.compose_text()
    old = persona_mgr.get("persona", "persona_state")
    assert isinstance(old, dict) and "mtime" in old
    time.sleep(0.02)
    os.utime(layer._file, None)                  # 外部 touch
    layer.compose_text()
    new = persona_mgr.get("persona", "persona_state")
    assert new["mtime"] > old["mtime"]           # 旧快照作废 → 重读回填新 mtime
