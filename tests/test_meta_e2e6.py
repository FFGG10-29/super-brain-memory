"""E2 呈现层 + E6 社会认知（最小可行）验收。

覆盖：
- E2 knowledge_gap 透传（six-channel 才透传，防御式 getattr，fast 路径恒 None）
- E2 来源追溯：hit.source / hit.trust（前缀匹配 config.recall.source_trust，缺省 0.8）
  与 injection 末尾的「记忆来源可信度」行
- E6 Agent 心智模型：_agent_brief 拼装 + 进程内缓存（TTL 300s）
- E6 save_memory cognition.perspective 透传（hippocampus 侧落盘差距见模块注释）
- ChatEngine gap 消费：prompt 末尾坦承指令追加 + deep_recall_ready 带 knowledge_gap

全部 fake / 夹具零网络。
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

from brain.prefrontal import RecallHit
from brain.write_gate import WriteGate
from chat.engine import ChatEngine
from chat.session_registry import SessionRegistry
from config import BrainConfig, ChatConfig
from gateway.mcp_server import MCPToolkit
from models.neuron import Neuron

GAP_INSTRUCTION = ("（记忆库中没有与该问题直接相关的记录："
                   "请坦承你不确定，并询问对方补充信息，不要编造。）")
TRUST_NOTE = "记忆来源可信度：user=1.0 agent=0.9 document=0.8"


# ── 测试替身 ─────────────────────────────────────────────────────────────
def _hit(agent_id: str, content: str) -> RecallHit:
    """构造六通道召回片段（真实 RecallHit / Neuron，confidence 由计算层并行接线）。"""
    return RecallHit(
        neuron=Neuron(agent_id=agent_id, content=content, namespace="topic:default"),
        score=0.9, channels=("dense",),
    )


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


class FakeRelational:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[dict] = []

    def query(self, kind: str, **kwargs: Any) -> list[dict]:
        self.calls.append(kwargs)
        return list(self.rows)


class FakeHippocampus:
    def __init__(self) -> None:
        self.cognitions: list[dict] = []

    def remember(self, content: str, *, agent_id: str = "", namespace: str = "",
                 cognition: dict | None = None, **_: Any):
        self.cognitions.append({
            "content": content, "agent_id": agent_id,
            "namespace": namespace, "cognition": cognition,
        })
        return SimpleNamespace(
            applied=True, superseded=[], merge=None,
            admission=SimpleNamespace(reason="new", duplicate_of=""),
            neuron=Neuron(agent_id=agent_id, content=content, namespace="topic:default"),
        )


class FakeRegistry:
    @staticmethod
    def verify(token: str):
        return SimpleNamespace(agent_id="workbuddy")

    @staticmethod
    def namespace_allowed(agent: Any, namespace: str) -> bool:
        return True

    @staticmethod
    def permission_allowed(agent: Any, perm: str) -> bool:
        return True

    @staticmethod
    def audit(*_: Any, **__: Any) -> None:
        pass


def _toolkit(prefrontal, relational, *, hippocampus=None, rpc_fallback=None) -> MCPToolkit:
    return MCPToolkit(
        registry=FakeRegistry(), hippocampus=hippocampus or FakeHippocampus(),
        prefrontal=prefrontal, relational=relational,
        config=BrainConfig(_env_file=None), rpc_fallback=rpc_fallback,
    )


def _call(toolkit: MCPToolkit, tool: str, args: dict) -> dict:
    return toolkit.call(tool, args, headers={"Authorization": "Bearer fake"})


# ── E2.1 knowledge_gap 透传 ─────────────────────────────────────────────
def test_recall_transmits_knowledge_gap_six_channel() -> None:
    pf = FakePrefrontal([_hit("agent-workbuddy", "既有记忆")])
    pf.last_knowledge_gap = {"gap": True, "query": "q", "note": "无直接命中"}
    result = _call(_toolkit(pf, FakeRelational()), "recall_memory", {"query": "q"})
    assert result["recall_mode"] == "six-channel"
    assert result["knowledge_gap"] == {"gap": True, "query": "q", "note": "无直接命中"}


def test_recall_knowledge_gap_is_none_when_prefrontal_not_wired() -> None:
    """防御式消费：prefrontal 未挂 last_knowledge_gap（E2 计算层未就绪）时取 None。"""
    pf = FakePrefrontal([_hit("agent-workbuddy", "既有记忆")])
    del pf.last_knowledge_gap  # 模拟旧版前额叶
    result = _call(_toolkit(pf, FakeRelational()), "recall_memory", {"query": "q"})
    assert result["knowledge_gap"] is None


def test_recall_fast_path_knowledge_gap_is_none() -> None:
    """fast 投影路径不透传 knowledge_gap（与 narratives 同款：仅 six-channel）。"""
    pf = FakePrefrontal()
    pf.last_knowledge_gap = {"gap": True, "query": "q", "note": "n"}

    def fake_rpc(method: str, args: dict) -> dict:
        assert method == "recall"
        return {"hits": [{"id": "n1", "content": "投影记忆", "score": 0.5}]}

    result = _call(
        _toolkit(pf, FakeRelational(), rpc_fallback=fake_rpc),
        "recall_memory", {"query": "q"},
    )
    assert result["recall_mode"] == "projection"
    assert result["knowledge_gap"] is None


# ── E2.2 来源追溯 ────────────────────────────────────────────────────────
def test_six_channel_hits_carry_source_and_trust() -> None:
    pf = FakePrefrontal([
        _hit("agent-workbuddy", "来自 Agent 的记忆"),   # 前缀 agent → 0.9
        _hit("user-direct", "来自用户口述的记忆"),       # 前缀 user → 1.0
    ])
    result = _call(_toolkit(pf, FakeRelational()), "recall_memory", {"query": "q"})
    by_source = {hit["source"]: hit["trust"] for hit in result["hits"]}
    assert by_source == {"agent-workbuddy": 0.9, "user-direct": 1.0}


def test_fast_hits_without_agent_id_default_trust() -> None:
    """fast 投影行缺 agent_id 时 source 为空串、trust 取缺省 0.8。"""

    def fake_rpc(method: str, args: dict) -> dict:
        return {"hits": [{"id": "n1", "content": "投影记忆", "score": 0.5}]}

    result = _call(
        _toolkit(FakePrefrontal(), FakeRelational(), rpc_fallback=fake_rpc),
        "recall_memory", {"query": "q"},
    )
    assert result["hits"][0]["source"] == ""
    assert result["hits"][0]["trust"] == 0.8


def test_injection_ends_with_trust_note_from_config() -> None:
    pf = FakePrefrontal([_hit("agent-workbuddy", "既有记忆")])
    result = _call(_toolkit(pf, FakeRelational()), "recall_memory", {"query": "q"})
    assert result["injection"].rstrip().endswith(TRUST_NOTE)

    # 值来自 config：改表后注入行同步变化
    cfg = BrainConfig(_env_file=None)
    cfg.recall.source_trust = {"user": 1.0}
    toolkit = MCPToolkit(
        registry=FakeRegistry(), hippocampus=FakeHippocampus(),
        prefrontal=pf, relational=FakeRelational(), config=cfg,
    )
    result2 = _call(toolkit, "recall_memory", {"query": "q"})
    assert result2["injection"].rstrip().endswith("记忆来源可信度：user=1.0")


def test_no_hits_injection_still_has_trust_note() -> None:
    """无六通道命中时 compose 运行时投影仍可能让 injection 非空，此时照样追加可信度行。"""
    result = _call(_toolkit(FakePrefrontal(), FakeRelational()),
                   "recall_memory", {"query": "q"})
    assert result["injection"].rstrip().endswith(TRUST_NOTE)


# ── E6.1 Agent 心智模型 ──────────────────────────────────────────────────
def _profile_rows() -> list[dict]:
    return [
        {"agent_id": "workbuddy", "content": "协作要点一：排期对齐"},
        {"agent_id": "codebuddy", "content": "别人的画像，不该出现"},
        {"agent_id": "workbuddy", "content": "协作要点二：命名用中文"},
        {"agent_id": "workbuddy", "content": "协作要点三"},
        {"agent_id": "workbuddy", "content": "协作要点四（第 4 条，截断）"},
    ]


def test_agent_brief_prepended_and_capped_at_three() -> None:
    relational = FakeRelational(_profile_rows())
    pf = FakePrefrontal([_hit("agent-workbuddy", "既有记忆")])
    result = _call(_toolkit(pf, relational), "recall_memory", {"query": "q"})
    injection = result["injection"]
    assert injection.startswith("【与该 Agent 的既往协作要点】")
    assert "协作要点一：排期对齐" in injection
    assert "协作要点二：命名用中文" in injection
    assert "协作要点三" in injection
    assert "协作要点四" not in injection          # 只取前 3 条
    assert "别人的画像" not in injection          # 按 agent_id 过滤


def test_agent_brief_cached_with_ttl() -> None:
    relational = FakeRelational(_profile_rows())
    toolkit = _toolkit(FakePrefrontal(), relational)
    _call(toolkit, "recall_memory", {"query": "q"})
    _call(toolkit, "recall_memory", {"query": "q"})
    assert len(relational.calls) == 1            # 命中进程内缓存，不再查库

    # TTL 过期后重建
    toolkit._agent_brief_cache["workbuddy"] = (time.time() - 301, "旧 brief")
    result = _call(toolkit, "recall_memory", {"query": "q"})
    assert len(relational.calls) == 2
    assert result["injection"].startswith("【与该 Agent 的既往协作要点】")


def test_agent_brief_empty_when_no_matching_profile() -> None:
    relational = FakeRelational([{"agent_id": "codebuddy", "content": "别人的画像"}])
    result = _call(_toolkit(FakePrefrontal([_hit("agent-workbuddy", "x")]), relational),
                   "recall_memory", {"query": "q"})
    assert "与该 Agent 的既往协作要点" not in result["injection"]


# ── E6.2 perspective 透传 ────────────────────────────────────────────────
def test_save_memory_passes_perspective_through() -> None:
    """cognition 整体透传给 hippocampus 不丢字段；响应回显 perspective 供观察。

    落点差距：hippocampus 旁路 meta 白名单暂不含 perspective 键，落盘需其侧增补。
    """
    hippocampus = FakeHippocampus()
    toolkit = _toolkit(FakePrefrontal(), FakeRelational(), hippocampus=hippocampus)
    resp = _call(toolkit, "save_memory", {
        "content": "Agent A 认为排期应为周五",
        "cognition": {"layer": "fact", "perspective": "Agent A 认为排期应为周五"},
    })
    assert resp["applied"] is True
    assert resp["perspective"] == "Agent A 认为排期应为周五"
    sent = hippocampus.cognitions[0]["cognition"]
    assert sent["perspective"] == "Agent A 认为排期应为周五"


def test_save_memory_without_perspective_echoes_none() -> None:
    resp = _call(_toolkit(FakePrefrontal(), FakeRelational()),
                 "save_memory", {"content": "普通记忆"})
    assert resp["perspective"] is None


# ── E2.3 ChatEngine gap 消费 ─────────────────────────────────────────────
class GapToolkit:
    def __init__(self) -> None:
        self.gap = True

    def recall_for_agent(self, agent: Any, args: dict) -> dict:
        result: dict[str, Any] = {
            "hits": [{"id": "n1", "content": "记忆片段"}], "injection": "注入文本"}
        if self.gap:
            result["knowledge_gap"] = {"gap": True, "query": str(args.get("query", "")),
                                       "note": "无直接命中"}
        return result

    def save_for_agent(self, agent: Any, args: dict) -> dict:
        return {"applied": True}


class PromptCapturingRouter:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt: str, **_: Any) -> dict:
        self.prompts.append(prompt)
        return {"ok": True, "text": "好的", "model": "fake", "kind": "local",
                "fallback": False}


def _engine(tmp_path, toolkit, router) -> tuple[ChatEngine, WriteGate, SessionRegistry]:
    cfg = BrainConfig(_env_file=None, chat=ChatConfig(enabled=False, deep_async=False))
    gate = WriteGate()
    sessions = SessionRegistry(tmp_path / "sessions.db")
    engine = ChatEngine(
        config=cfg, toolkit=toolkit, router=router, write_gate=gate,
        agent=object(), token="t", sessions=sessions,
    )
    return engine, gate, sessions


def test_chat_engine_appends_gap_instruction_to_prompt_end(tmp_path) -> None:
    toolkit, router = GapToolkit(), PromptCapturingRouter()
    engine, gate, sessions = _engine(tmp_path, toolkit, router)
    asyncio.run(engine._handle_message("s1", "你好", "d1"))
    assert router.prompts and router.prompts[0].endswith(GAP_INSTRUCTION)

    # 无 gap（fast 路径未带字段）时不追加
    toolkit.gap = False
    asyncio.run(engine._handle_message("s1", "你好", "d1"))
    assert not router.prompts[1].endswith(GAP_INSTRUCTION)
    gate.shutdown(timeout=5)
    sessions.close()


def test_deep_recall_ready_carries_knowledge_gap(tmp_path) -> None:
    toolkit, router = GapToolkit(), PromptCapturingRouter()
    engine, gate, sessions = _engine(tmp_path, toolkit, router)
    seen: list[dict] = []
    engine._bus.subscribe("s1", seen.append)
    asyncio.run(engine._deep_recall("s1", "三个月前那件事", "bot"))
    event = next(item for item in seen if item.get("type") == "deep_recall_ready")
    assert event["knowledge_gap"] == {"gap": True, "query": "三个月前那件事",
                                      "note": "无直接命中"}
    gate.shutdown(timeout=5)
    sessions.close()
