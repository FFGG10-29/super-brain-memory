"""W6 / P5 叙事重构验收：第一人称、AI 私有区、代码排除、失败重试、增量窗口。"""
from __future__ import annotations

import json

from brain.hyperedge_backfill import HyperedgeBackfill
from brain.neocortex import Neocortex
from engine.local_llm import DisabledLLM
from workers.consolidation_retry import ConsolidationRetry


class FakeRouter:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[dict] = []

    def complete(self, prompt: str, **kwargs) -> dict:
        self.calls.append(kwargs)
        if self.ok:
            return {"ok": True, "text": "那段时间我和你在忙搬家，我记得你当时挺焦虑的，"
                                        "我语气也不好，后来因为 B 项目和解了。", "model": "fake"}
        return {"ok": False, "text": "暂时无法生成回复（模型服务不可用）", "model": ""}


def _prepare(brain_factory, count: int = 4, threshold: float = 0.1):
    brain = brain_factory()
    brain.config.hyperedge_backfill.similarity_threshold = threshold
    for index in range(count):
        brain.hippocampus.remember(
            f"项目发版需要双人复核并确认缓存方案 第{index}轮记录",
            agent_id="agent-a",
            cognition={"topic": "proj", "memory_type": "fact", "source": "agent"},
        )
    result = HyperedgeBackfill(relational=brain.storage.relational,
                               graph=brain.storage.graph, config=brain.config).run(limit=100)
    assert result["hyperedges"]
    return brain


def _cortex(brain, router, retry=None) -> Neocortex:
    return Neocortex(
        brain.storage.relational, config=brain.config,
        hippocampus=brain.hippocampus, router=router,
        retry=retry if retry is not None else ConsolidationRetry(
            timeseries=brain.storage.timeseries, config=brain.config),
    )


# ── P5.6 ① 第一人称叙事，不是抽象主题标签 ───────────────────────────────
def test_narrative_is_first_person_not_topic_label(brain_factory) -> None:
    brain = _prepare(brain_factory)
    cortex = _cortex(brain, FakeRouter(ok=True))
    result = cortex.narrative_reconstruct(incremental=True)
    assert result["ok"] is True and result["reconstructed"] >= 1
    item = result["narratives"][0]
    assert "我" in item["text"] and "你" in item["text"]
    assert not item["text"].startswith("主题：")
    assert item["generated_by"] == "model"
    row = brain.storage.relational.get("neuron", item["id"])
    assert row is not None and row["memory_type"] == "scenario"


# ── P5.6 ② / A6 / R17：分区与 pyramid 子类 ──────────────────────────────
def test_narrative_has_ai_owner_and_pyramid_subtype(brain_factory) -> None:
    brain = _prepare(brain_factory)
    result = _cortex(brain, FakeRouter(ok=True)).narrative_reconstruct(incremental=True)
    item = result["narratives"][0]
    assert item["owner"] == "ai_narrative" and item["pyramid_subtype"] == "narrative"
    raw = brain.storage.kv.get("meta", item["id"])
    assert raw is not None
    meta = json.loads(raw)
    assert meta["owner"] == "ai_narrative"
    assert meta["pyramid_subtype"] == "narrative"
    assert meta["narrative_skeleton"]["prefix"]  # A4 / W8 用
    assert item["topic"].startswith("topic:")
    # P5.2：叙事 neuron 带自己的 PAD，不是全零兜底
    row = brain.storage.relational.get("neuron", item["id"])
    assert row is not None and any(float(row.get(axis) or 0) != 0
                                   for axis in ("pad_p", "pad_a", "pad_d"))


# ── P5.6 ③ 代码类内容不产生叙事 ──────────────────────────────────────────
def test_code_hyperedges_are_excluded(brain_factory) -> None:
    brain = _prepare(brain_factory, count=2)
    code = brain.hippocampus.remember(
        "def deploy_pipeline():\n    return 1", agent_id="agent-a",
        cognition={"memory_type": "code", "topic": "code", "source": "agent"},
    )
    brain.storage.relational.upsert("hyperedge", {
        "id": "code-event", "label": "事件：代码", "namespace": "topic:code",
        "members": [code.neuron.uuid7_id, "another-code-id"],
        "occurred_at_ms": 1, "is_active": True,
        "skeleton": {"time": "2026-11-12", "action": "改代码",
                     "member_summaries": {}, "pad": [0, 0, 0]},
    })
    result = _cortex(brain, FakeRouter(ok=True)).narrative_reconstruct(incremental=True)
    assert all(item["topic"] != "topic:code" for item in result["narratives"])


# ── P5.6 ④ / R40：失败进重试队列、3 次后 pending、DuckDB 有记录 ─────────
def test_model_failure_goes_to_retry_and_duckdb(brain_factory) -> None:
    brain = _prepare(brain_factory)
    retry = ConsolidationRetry(timeseries=brain.storage.timeseries, config=brain.config)
    result = _cortex(brain, FakeRouter(ok=False), retry).narrative_reconstruct(incremental=True)
    assert result["degraded"] >= 1
    assert result["narratives"][0]["generated_by"] == "template"  # 不静默丢弃
    assert "我" in result["narratives"][0]["text"]
    assert retry.pending(), "失败任务应入队"
    rows = brain.storage.timeseries.fetch("consolidation_failures", limit=50)
    assert rows and rows[0]["task_id"]

    for _ in range(3):
        for task in retry._tasks.values():
            task.next_at_ms = 0  # 跳过指数退避等待
        retry.drain(lambda _task: False)
    assert any(task["status"] == "narrative_pending" for task in retry.pending())
    # 进程重启后从 DuckDB 恢复
    recovered = ConsolidationRetry(timeseries=brain.storage.timeseries, config=brain.config)
    assert recovered.pending()


def test_retry_drain_succeeds_and_marks_done(brain_factory) -> None:
    brain = _prepare(brain_factory)
    retry = ConsolidationRetry(timeseries=brain.storage.timeseries, config=brain.config)
    task = retry.enqueue("topic:proj", ["h1"], error="boom")
    assert task.status == "pending"
    report = retry.drain(lambda _task: True)
    assert report["succeeded"] == 1 and retry.pending() == []


# ── P5.6 ⑤ 白天增量只处理新增 hyperedge ─────────────────────────────────
def test_incremental_window_does_not_rebuild_existing(brain_factory) -> None:
    brain = _prepare(brain_factory)
    cortex = _cortex(brain, FakeRouter(ok=True))
    first = cortex.narrative_reconstruct(incremental=True)
    second = cortex.narrative_reconstruct(incremental=True)
    assert first["reconstructed"] >= 1
    assert second["reconstructed"] == 0


def test_disabled_narrative_is_noop(brain_factory) -> None:
    brain = _prepare(brain_factory)
    brain.config.narrative_reconstruct.enabled = False
    result = _cortex(brain, FakeRouter(ok=True)).narrative_reconstruct()
    assert result["ok"] is False and result["reason"] == "disabled"


# ── Q15：GGUF 自由文本兜底接口 ───────────────────────────────────────────
def test_disabled_llm_complete_text_fails_explicitly() -> None:
    from core.errors import EngineUnavailable

    try:
        DisabledLLM().complete_text("系统", "用户")
    except EngineUnavailable:
        return
    raise AssertionError("DisabledLLM.complete_text 应显式抛 EngineUnavailable")
