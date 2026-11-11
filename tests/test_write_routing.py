"""W2 / P1 写入侧分流验收。

覆盖：场景一（10 轮代码 → 1 条 active）、场景二（纠正 3 次可追溯）、
低置信度门控、逻辑分类不改枚举、崩溃注入单向安全、v3 journal seq 定序、
media_interaction 不产生 neuron。
"""
from __future__ import annotations

import json

import pytest

from brain.memory_browser import MemoryBrowser
from brain.memory_editor import MemoryEditor
from brain.write_gate import WriteGate
from brain.write_router import LOGICAL_CATEGORIES, route_write
from core.interfaces import MemoryType
from v3.journal import V3Journal
from v3.worker import V3Worker


def _cognition(**kwargs):
    return {"memory_type": "code", "topic": "proj", "source": "agent", **kwargs}


def _code(revision: int) -> str:
    # 签名稳定（函数名），正文每轮不同 —— 正是「同一段代码改 10 轮」的形态
    return f"def deploy_pipeline():\n    # revision {revision}\n    return {revision}"


# ── Q13 / R1：entity_id 稳定规则 ──────────────────────────────────────────
def test_entity_id_is_stable_across_revisions(brain_config) -> None:
    first = route_write(content=_code(1), cognition=_cognition(),
                        namespace="topic:proj", config=brain_config)
    tenth = route_write(content=_code(10), cognition=_cognition(),
                        namespace="topic:proj", config=brain_config)
    assert first["path"] == tenth["path"] == "overwrite"
    assert first["entity_id"] == tenth["entity_id"]
    assert first["entity_fuzzy"] is False


def test_agent_supplied_entity_id_wins(brain_config) -> None:
    decision = route_write(content=_code(1), cognition=_cognition(entity_id="fixed-id"),
                           namespace="topic:proj", config=brain_config)
    assert decision["entity_id"] == "fixed-id"


# ── C6：服务端重算置信度，低置信回退 fact ─────────────────────────────────
def test_low_confidence_falls_back_to_ingest(brain_config) -> None:
    decision = route_write(
        content="这是一段没有任何函数签名的普通说明文字",
        cognition={"memory_type": "code", "confidence": 0.99, "source": "agent"},
        namespace="topic:proj", config=brain_config,
    )
    assert decision["path"] == "ingest"
    assert decision["routing_fallback"] is True
    assert decision["confidence"] == 0.6 < 0.8
    assert decision["agent_confidence"] == 0.99  # 自报值只作参考
    assert decision["entity_id"] == ""


# ── C5：逻辑分类走 payload category，不改五类枚举 ─────────────────────────
def test_logical_categories_keep_memory_type_enum(brain_config) -> None:
    for category in sorted(LOGICAL_CATEGORIES):
        decision = route_write(
            content="config:\n    retry = 3\n",
            cognition={"memory_type": "fact", "category": category, "source": "agent"},
            namespace="topic:proj", config=brain_config,
        )
        assert decision["path"] == "overwrite"
        assert decision["category"] == category
    assert set(MemoryType.__args__) == {"fact", "code", "scenario", "emotion", "profile"}


# ── P1.5 验收①：10 轮代码只留 1 条 active ─────────────────────────────────
def test_ten_rounds_of_code_leave_one_active(brain_factory) -> None:
    brain = brain_factory()
    browser = MemoryBrowser(brain.storage.relational)
    last_id = ""
    for revision in range(10):
        result = brain.hippocampus.remember(
            _code(revision), agent_id="agent-a", cognition=_cognition(),
        )
        if result.applied:
            last_id = result.neuron.uuid7_id
    active = [row for row in browser.list(limit=100) if row["is_active"]]
    assert len(active) == 1, "事务性覆盖后只能有 1 条 active 记忆"
    assert last_id and active[0]["id"] == last_id
    decision = route_write(content=_code(0), cognition=_cognition(),
                           namespace="topic:proj", config=brain.config)
    seq = int(brain.storage.kv.get("entity_seq", decision["entity_id"]) or b"0")
    assert 1 <= seq <= 10
    assert brain.storage.kv.get("entity", decision["entity_id"]) == last_id.encode()


# ── P1.5 验收②：认知性纠正 3 次，审计原值保留 ────────────────────────────
def test_cognitive_correction_is_traceable(brain_factory) -> None:
    brain = brain_factory(with_journal=True)
    first = brain.hippocampus.remember("项目发版需要双人复核", agent_id="agent-a")
    editor = MemoryEditor(app=brain)
    current = first.neuron.uuid7_id
    for index in range(3):
        result = editor.correct(
            current, changes={"content": f"项目发版需要双人复核（第 {index + 1} 次修订）"},
            reason="验收纠正",
        )
        assert result["ok"] is True
        assert len(result["event_ids"]) == 2  # ingest + supersede
        current = result["id"]
    browser = MemoryBrowser(brain.storage.relational)
    active = [row for row in browser.list(limit=100) if row["is_active"]]
    assert len(active) == 1 and active[0]["id"] == current
    old = brain.storage.relational.get("neuron", first.neuron.uuid7_id)
    assert old is not None and old.get("is_active") == 0  # 原值留存，不物理删除


# ── P1.5 验收④：崩溃注入，单向安全顺序不留双 active ──────────────────────
def test_crash_after_deactivate_leaves_no_dirty_active(brain_factory, monkeypatch) -> None:
    brain = brain_factory()
    brain.hippocampus.remember(_code(0), agent_id="agent-a", cognition=_cognition())

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated crash after upsert")

    monkeypatch.setattr(brain.hippocampus, "dispatch_outbox", boom)
    with pytest.raises(RuntimeError, match="simulated crash"):
        brain.hippocampus.remember(_code(1), agent_id="agent-a", cognition=_cognition())
    active = [row for row in MemoryBrowser(brain.storage.relational).list(limit=100)
              if row["is_active"]]
    assert len(active) <= 1, "旧记录已封存，不能出现两条 active"


# ── A3：重放时同一 entity 只有最大 seq 的 overwrite 生效 ───────────────────
class _FakeInbox:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def enqueue(self, event: dict) -> bool:
        self.events.append(event)
        return True


def test_reconcile_keeps_only_latest_overwrite(tmp_path) -> None:
    journal = V3Journal(tmp_path, device_id="test")
    try:
        for seq in (1, 2, 3):
            journal.append(
                _code(seq), agent_id="a", event_type="overwrite",
                extra={"entity_id": "e1", "seq": seq,
                       "payload": {"content": _code(seq), "cognition": {}}},
            )
        journal.append("一条普通认知性记忆内容", agent_id="a")
        inbox = _FakeInbox()
        journal.reconcile(inbox)
        overwrites = [item for item in inbox.events if item.get("type") == "overwrite"]
        assert len(overwrites) == 1 and overwrites[0]["seq"] == 3
        assert any(item.get("type") == "ingest" for item in inbox.events)
    finally:
        journal.close()


# ── R15：media_interaction 只记 meta，不产生 neuron ────────────────────────
class _FakeAlarm:
    def wait(self, timeout=None):
        return False

    def ring(self, *_args) -> None:
        return None


class _FinishInbox:
    def __init__(self) -> None:
        self.finished: list[tuple[str, bool]] = []

    def finish(self, event_id, worker_id, *, ok: bool, error: str = "") -> bool:
        self.finished.append((event_id, ok))
        return True


def test_media_interaction_creates_no_neuron(brain_factory) -> None:
    brain = brain_factory()
    gate = WriteGate()
    inbox = _FinishInbox()
    try:
        worker = V3Worker(inbox, _FakeAlarm(), brain, write_gate=gate)
        event = {"type": "media_interaction", "event_id": "evt-1",
                 "payload": {"content": "cat.png", "agent_id": "a"}}
        worker._process({
            "event_id": "evt-1", "content": "cat.png", "agent_id": "a",
            "namespace": "topic:default", "payload": json.dumps(event, ensure_ascii=False),
        })
        assert inbox.finished == [("evt-1", True)]
        assert MemoryBrowser(brain.storage.relational).list(limit=10) == []
    finally:
        gate.shutdown(timeout=5)
