"""W8 验收：P7 叙事投影缓存 + P6 媒体异步旁路。"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from brain.write_gate import WriteGate
from chat.engine import ChatEngine
from chat.event_bus import EventBus
from chat.session_registry import SessionRegistry
from config import BrainConfig, ChatConfig, MediaServiceConfig
from core.fast_recall import (
    _PROJECTIONS, build_projection, fast_recall_projected, get_projection,
    invalidate, narrative_ids_hash,
)
from gateway.media_service import MediaService


def _narrative(brain, text: str, *, skeleton: str) -> str:
    result = brain.hippocampus.remember(
        text, agent_id="consolidator",
        cognition={
            "memory_type": "scenario", "topic": "proj", "source": "ai_narrative",
            "owner": "ai_narrative", "pyramid": "topic",
            "pyramid_subtype": "narrative",
            "narrative_skeleton": {"prefix": skeleton, "entities": ["n1", "n2"]},
        },
    )
    return result.neuron.uuid7_id


# ── A4 / P7：骨架索引、全文注入、签名失效 ────────────────────────────────
def test_projection_indexes_only_skeleton_but_injects_full_text(brain_factory) -> None:
    brain = brain_factory()
    full_text = "那段时间我和你在忙搬家，中间因为项目进度有过一次冲突，之后冷战了几天，最后和解了。"
    _narrative(brain, full_text, skeleton="那段时间我和你在忙搬家，中间因为项目进度有过一次冲突")
    rows = build_projection("topic:proj", relational=brain.storage.relational,
                            kv=brain.storage.kv)
    assert rows and rows[0]["is_narrative"] is True
    assert rows[0]["index_text"].startswith("那段时间我和你在忙搬家")
    assert rows[0]["content"] == full_text and len(rows[0]["content"]) > len(rows[0]["index_text"])
    hit = fast_recall_projected("topic:proj", "搬家", relational=brain.storage.relational,
                                kv=brain.storage.kv)
    assert hit["narrative_hits"] >= 1
    assert hit["hits"][0]["content"] == full_text  # 命中骨架后注入完整叙事


def test_projection_cache_invalidates_on_new_narrative(brain_factory) -> None:
    brain = brain_factory()
    topic = "topic:proj"
    invalidate(topic)
    first_hash = narrative_ids_hash(topic, kv=brain.storage.kv)
    _narrative(brain, "第一段我和你的故事。", skeleton="第一段故事骨架")
    second_hash = narrative_ids_hash(topic, kv=brain.storage.kv)
    assert first_hash != second_hash  # R26：叙事新增 → 签名变化
    first = get_projection(topic, relational=brain.storage.relational, kv=brain.storage.kv)
    again = get_projection(topic, relational=brain.storage.relational, kv=brain.storage.kv)
    assert first is again  # 同签名命中缓存
    invalidate(topic)
    assert topic not in _PROJECTIONS
    rebuilt = get_projection(topic, relational=brain.storage.relational, kv=brain.storage.kv)
    assert rebuilt is not first and rebuilt.narrative_ids_hash == second_hash


def test_projection_degrades_without_narrative(brain_factory) -> None:
    """A4 / P7.2：新用户/新 topic 没有叙事时只有 2-gram 关键词匹配，是可接受的退化。"""
    brain = brain_factory()
    brain.hippocampus.remember("项目发版需要双人复核", agent_id="a",
                               cognition={"topic": "proj", "memory_type": "fact"})
    result = fast_recall_projected("topic:proj", "发版", relational=brain.storage.relational,
                                   kv=brain.storage.kv)
    assert result["degraded"] is True and result["recall_mode"] == "projection"
    assert result["narrative_hits"] == 0


# ── B3 / P6：媒体优雅降级与清理 ──────────────────────────────────────────
def test_media_service_returns_none_when_comfyui_unreachable(tmp_path) -> None:
    cfg = BrainConfig(_env_file=None, media_service=MediaServiceConfig(
        comfyui_url="http://127.0.0.1:1", request_timeout_sec=1))
    service = MediaService(cfg, root=tmp_path / "media")
    assert service.generate("image", {"prompt": "猫"}, session_id="s1") is None


def test_media_cleanup_expires_and_evicts(tmp_path) -> None:
    root = tmp_path / "media"
    session = root / "old-session"
    session.mkdir(parents=True)
    stale = session / "stale.png"
    stale.write_bytes(b"x" * 16)
    old = time.time() - 10000
    import os

    os.utime(stale, (old, old))
    cfg = BrainConfig(_env_file=None, media_service=MediaServiceConfig(session_ttl_sec=60))
    report = MediaService(cfg, root=root).cleanup()
    assert report["expired"] == 1 and not stale.exists()


# ── 8769 媒体异步链路 ────────────────────────────────────────────────────
class FakeMedia:
    def __init__(self, result=None) -> None:
        self.result = result
        self.calls: list[dict] = []

    def generate(self, media_type, params=None, *, session_id="default"):
        self.calls.append({"type": media_type, "session_id": session_id})
        return self.result


@pytest.mark.asyncio
async def test_media_task_pushes_media_ready_and_meta_event(tmp_path) -> None:
    cfg = BrainConfig(_env_file=None, chat=ChatConfig(enabled=True, host="127.0.0.1",
                                                      port=18799))
    bus = EventBus()
    seen: list[dict] = []
    bus.subscribe("s1", seen.append)
    meta_events: list[dict] = []
    gate = WriteGate()
    sessions = SessionRegistry(tmp_path / "s.db")
    engine = ChatEngine(
        config=cfg, toolkit=object(), router=object(), write_gate=gate,
        agent=object(), token="t", sessions=sessions, bus=bus,
        media_service=FakeMedia(None),
        event_sink=lambda content, **kw: meta_events.append({"content": content, **kw}),
    )
    try:
        await engine._media_task("s1", "发张猫的图")
        kinds = [item["type"] for item in seen]
        assert "media_ready" in kinds and "typing" in kinds
        ready = next(item for item in seen if item["type"] == "media_ready")
        assert ready["ok"] is False and "暂时无法" in ready["message"]
        assert meta_events and meta_events[0]["event_type"] == "media_interaction"
    finally:
        gate.shutdown(timeout=5)
        sessions.close()


@pytest.mark.asyncio
async def test_media_intent_replies_before_background_generation(tmp_path) -> None:
    """P6.1：先回文本，媒体就绪后从 8769 推（不阻塞对话流）。"""
    cfg = BrainConfig(_env_file=None, chat=ChatConfig(enabled=True, host="127.0.0.1",
                                                      port=18798))
    bus = EventBus()
    seen: list[dict] = []
    bus.subscribe("s1", seen.append)
    gate = WriteGate()
    sessions = SessionRegistry(tmp_path / "s.db")
    fake = FakeMedia({"path": "C:/fake/cat.png", "media_type": "image", "session_id": "s1"})

    class Toolkit:
        def recall_for_agent(self, agent, args):
            return {"hits": [], "injection": ""}

        def save_for_agent(self, agent, args):
            raise AssertionError("媒体意图不应写长期记忆")

    engine = ChatEngine(
        config=cfg, toolkit=Toolkit(), router=object(), write_gate=gate,
        agent=object(), token="t", sessions=sessions, bus=bus,
        media_service=fake, event_sink=None,
    )
    try:
        await engine._handle_message("s1", "发张猫的图", "d1")
        for _ in range(20):
            if any(item["type"] == "media_ready" for item in seen):
                break
            await asyncio.sleep(0.02)
        kinds = [item["type"] for item in seen]
        assert kinds.index("reply") < kinds.index("media_ready")
        assert any(item["type"] == "media_ready" and item["ok"] is True for item in seen)
        assert fake.calls  # 生成确实被异步调用
    finally:
        gate.shutdown(timeout=5)
        sessions.close()
