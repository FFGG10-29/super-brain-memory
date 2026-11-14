"""W3 / P2 ChatEngine 验收：意图统一、会话库版本、鉴权、串行顺序、fan-out、deep 异步。"""
from __future__ import annotations

import asyncio
import socket
import time
from typing import Any

import pytest
from websockets.asyncio.client import connect

from brain.thalamus import classify_intent
from brain.write_gate import WriteGate
from chat.engine import ChatEngine
from chat.event_bus import EventBus
from chat.protocol import decode
from chat.session_registry import SCHEMA_VERSION, SessionRegistry
from config import BrainConfig, ChatConfig


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeExtractor:
    """产出一组结构化记忆（一进多出）。"""
    def extract_multi(self, content: str, *, agent_id: str = ""):
        from brain.router_extractor import (ExtractionBundle, ExtractionItem)
        from models.neuron import PAD
        return ExtractionBundle(
            items=(
                ExtractionItem(memory_type="profile", content=f"画像：{content}",
                               pad=PAD(p=0.3, a=0.1, d=0.2), confidence=0.9),
                ExtractionItem(memory_type="emotion", content=f"情绪：{content}",
                               pad=PAD(p=0.8, a=0.6, d=0.2), confidence=0.85),
                ExtractionItem(memory_type="scenario", content=content,
                               pad=PAD(p=0.2, a=0.1, d=0.1), confidence=0.9),
            ),
            event_label=f"事件：{content[:12]}", event_members=("阿鱼", "肥鱼"),
            source="llm",
        )


class FakeToolkit:
    def __init__(self, *, bundle_ok: bool = True) -> None:
        self.recalls: list[dict] = []
        self.saves: list[dict] = []
        self.bundles: list[Any] = []
        self.bundle_ok = bundle_ok
        self.extractor = FakeExtractor()

    def recall_for_agent(self, agent: Any, args: dict) -> dict:
        self.recalls.append(dict(args))
        return {"hits": [{"id": "n1", "content": "记忆片段"}],
                "injection": "【人格】大肥鱼在此" if not args.get("deep") else "【深度】事件骨架"}

    def save_for_agent(self, agent: Any, args: dict) -> dict:
        self.saves.append(dict(args))
        return {"applied": True}

    def save_bundle_for_agent(self, agent: Any, bundle: Any, **_: Any) -> dict:
        self.bundles.append(bundle)
        return {"applied": 3 if self.bundle_ok else 0,
                "total": 3, "neuron_ids": ["n1", "n2", "n3"]}


class FakeRouter:
    def complete(self, prompt: str, **kwargs: Any) -> dict:
        user_lines = [line[3:] for line in prompt.splitlines() if line.startswith("用户：")]
        return {"ok": True, "text": user_lines[-1] if user_lines else "?",
                "model": "fake", "kind": "local", "fallback": False}


@pytest.fixture()
def engine_factory(tmp_path):
    built: list[tuple[ChatEngine, WriteGate, SessionRegistry, FakeToolkit]] = []

    def build(*, token: str = "test-token", deep_async: bool = True, bundle_ok: bool = True):
        index = len(built)
        port = _free_port()
        cfg = BrainConfig(
            _env_file=None,
            chat=ChatConfig(enabled=True, host="127.0.0.1", port=port,
                            persona_channel="bot", deep_async=deep_async,
                            max_history_turns=5),
        )
        gate = WriteGate()
        sessions = SessionRegistry(tmp_path / f"sessions-{index}.db")
        toolkit = FakeToolkit(bundle_ok=bundle_ok)
        engine = ChatEngine(
            config=cfg, toolkit=toolkit, router=FakeRouter(), write_gate=gate,
            agent=object(), token=token, sessions=sessions,
        )
        built.append((engine, gate, sessions, toolkit))
        engine.start()
        for _ in range(60):
            if engine.running:
                break
            time.sleep(0.05)
        assert engine.running, f"ChatEngine 未启动：{engine.last_error}"
        return engine, toolkit

    yield build
    for engine, gate, sessions, _ in built:
        engine.stop()
        gate.shutdown(timeout=5)
        sessions.close()


async def _frames_until(ws, target: str, max_frames: int = 25) -> list[dict]:
    frames: list[dict] = []
    for _ in range(max_frames):
        frames.append(decode(await asyncio.wait_for(ws.recv(), timeout=8)))
        if frames[-1].get("type") == target:
            return frames
    raise AssertionError(f"未收到 {target}，已收：{[f.get('type') for f in frames]}")


async def _hello(ws, *, token: str = "test-token", session_id: str = "", device: str = "d1") -> dict:
    await ws.send(__import__("json").dumps(
        {"type": "hello", "token": token, "session_id": session_id, "device_id": device}))
    return decode(await asyncio.wait_for(ws.recv(), timeout=8))


# ── Q20：统一 intent ──────────────────────────────────────────────────────
def test_classify_intent_is_shared_by_router_and_recall() -> None:
    assert classify_intent("三个月前我们聊过的那件事") == "deep_recall"
    assert classify_intent("def deploy(): pass") == "code"
    assert classify_intent("我有点难过") == "emotion"
    assert classify_intent("发张猫的图") == "media"
    assert classify_intent("今天天气不错") == "chat"


# ── C10 / R21：独立会话库与版本号 ────────────────────────────────────────
def test_session_registry_schema_version_and_restore(tmp_path) -> None:
    path = tmp_path / "chat" / "sessions.db"
    registry = SessionRegistry(path)
    version = registry._conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
    assert int(version) == SCHEMA_VERSION
    session_id = registry.create()
    assert registry.restore(session_id) is True
    assert registry.restore("not-exist") is False
    registry.touch(session_id, "phone")
    registry.touch(session_id, "phone")
    registry.touch(session_id, "tablet")
    assert registry.devices(session_id) == ["phone", "tablet"]
    assert session_id in registry.all_sessions()
    registry.close()

    reopened = SessionRegistry(path)  # 迁移链/重开不能报错
    assert reopened.restore(session_id) is True
    reopened.close()


def test_session_registry_rejects_newer_schema(tmp_path) -> None:
    path = tmp_path / "newer.db"
    registry = SessionRegistry(path)
    with registry._conn:
        registry._conn.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION + 5,))
    registry.close()
    with pytest.raises(RuntimeError, match="请升级客户端"):
        SessionRegistry(path)


def test_event_bus_publishes_to_subscribers() -> None:
    bus = EventBus()
    seen: list[dict] = []
    callback = seen.append
    bus.subscribe("s1", callback)
    assert bus.publish("s1", {"type": "typing"}) == 1
    bus.unsubscribe("s1", callback)
    assert bus.publish("s1", {"type": "typing"}) == 0
    assert seen == [{"type": "typing"}]


# ── 8769 端到端 ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_unauthorized_hello_is_rejected(engine_factory) -> None:
    engine, _ = engine_factory()
    async with connect(f"ws://127.0.0.1:{engine._port}") as ws:
        frame = await _hello(ws, token="wrong-token")
    assert frame["type"] == "error" and "unauthorized" in frame["message"]


@pytest.mark.asyncio
async def test_chat_flow_has_persona_and_typing(engine_factory) -> None:
    engine, toolkit = engine_factory()
    async with connect(f"ws://127.0.0.1:{engine._port}") as ws:
        ack = await _hello(ws)
        assert ack["type"] == "hello_ack" and ack["session_id"]
        await ws.send(__import__("json").dumps({"type": "message", "text": "你好呀"}))
        frames = await _frames_until(ws, "reply")
    kinds = [item["type"] for item in frames]
    assert "typing" in kinds and kinds[-1] == "reply"
    assert frames[-1]["text"] == "你好呀"
    assert toolkit.recalls and toolkit.recalls[0]["channel"] == "bot"  # A5/Q19 人格注入


@pytest.mark.asyncio
async def test_messages_are_serial_and_fan_out_to_two_devices(engine_factory) -> None:
    engine, _ = engine_factory()
    async with connect(f"ws://127.0.0.1:{engine._port}") as first:
        ack = await _hello(first, device="phone")
        session_id = ack["session_id"]
        async with connect(f"ws://127.0.0.1:{engine._port}") as second:
            second_ack = await _hello(second, session_id=session_id, device="tablet")
            assert second_ack["session_id"] == session_id
            import json

            for index in range(3):
                await first.send(json.dumps({"type": "message", "text": f"消息{index}"}))
            async def collect_replies(ws) -> list[str]:
                replies: list[str] = []
                for _ in range(30):
                    frame = decode(await asyncio.wait_for(ws.recv(), timeout=10))
                    if frame["type"] == "reply":
                        replies.append(frame["text"])
                        if len(replies) == 3:
                            return replies
                return replies

            first_replies = await collect_replies(first)
            second_replies = await collect_replies(second)
            assert first_replies == ["消息0", "消息1", "消息2"]  # P2.4 严格按序
            assert second_replies == first_replies                  # P2.3 fan-out


@pytest.mark.asyncio
async def test_deep_recall_ready_is_pushed_from_8769(engine_factory) -> None:
    engine, toolkit = engine_factory()
    async with connect(f"ws://127.0.0.1:{engine._port}") as ws:
        await _hello(ws)
        import json

        await ws.send(json.dumps({"type": "message", "text": "三个月前那件事"}))
        frames = await _frames_until(ws, "deep_recall_ready")
    assert any(item["type"] == "reply" for item in frames)
    deep = frames[-1]
    assert deep["hits"] and "事件骨架" in deep["injection"]
    assert any(call.get("deep") for call in toolkit.recalls)


@pytest.mark.asyncio
async def test_worth_remembering_is_one_to_many_bundle(engine_factory) -> None:
    """一进多出铁律：一条对话拆成 profile+emotion+scenario 一组，走 bundle 通道。"""
    engine, toolkit = engine_factory()
    async with connect(f"ws://127.0.0.1:{engine._port}") as ws:
        await _hello(ws)
        import json

        await ws.send(json.dumps({"type": "message", "text": "记住这个约定：聊天要带人格"}))
        await _frames_until(ws, "reply")
        for _ in range(20):
            if toolkit.bundles:
                break
            await asyncio.sleep(0.05)
    assert toolkit.bundles, "必须走一进多出 bundle 通道"
    bundle = toolkit.bundles[0]
    assert {i.memory_type for i in bundle.items} >= {"profile", "emotion", "scenario"}
    assert all(any(getattr(i.pad, ax, 0) != 0 for ax in ("p", "a", "d")) for i in bundle.items)
    assert bundle.event_label and "肥鱼" in bundle.event_members
    assert not toolkit.saves, "铁律：bundle 成功时绝不走单条降级"


async def test_worth_remembering_failure_defers_never_falls_back(engine_factory, monkeypatch) -> None:
    """铁律：一进多出失败 → 留项目档案待重试，绝不降级单条写记忆。"""
    engine, toolkit = engine_factory(bundle_ok=False)
    deferred: list[str] = []
    monkeypatch.setattr(engine, "_defer_to_archive",
                        lambda text, intent: deferred.append(text))
    async with connect(f"ws://127.0.0.1:{engine._port}") as ws:
        await _hello(ws)
        import json

        await ws.send(json.dumps({"type": "message", "text": "记住：这条一进多出要失败"}))
        await _frames_until(ws, "reply")
        for _ in range(20):
            if deferred:
                break
            await asyncio.sleep(0.05)
    assert deferred and any("一进多出要失败" in t for t in deferred), "失败原文必须留项目档案待重试"
    assert not toolkit.saves, "铁律：绝不降级单条 save_for_agent"
    assert len(toolkit.bundles) == 1, "仍走了一次 bundle 尝试，只是没落库"


async def test_flush_narrative_is_extractor_input_not_storage(engine_factory) -> None:
    """架构裁决（2026-11-12）：flush 提纯出的叙事是 RouterExtractor 的**输入材料**，
    经一进多出解剖成碎片入池；叙事本身作为 scenario 的 narrative 属性随组落库，
    绝不整段直接当记忆正文。门控 = 满 8 条且用户静默 ≥3 分钟（静默由主循环轮询判定）。"""
    engine, toolkit = engine_factory()
    narrative = "今天中午阿鱼喊我想不想他，我回了想你宝儿，还发了表情包，两边都甜甜的。"
    engine._narrative_summary = lambda batch: narrative  # 提纯通道固定产出
    for i in range(8):
        engine._queue_memory(f"消息{i}：阿鱼说想我", "chat")
    assert len(engine._mem_buffer) == 8
    assert not engine._flush_due(), "刚填满但用户还在说话（未静默）→ 不得截断高潮"
    # 用户静默 4 分钟 → 引擎主循环自动冲刷（不手动调，锁真实触发路径）
    engine._mem_last_message_ts = time.time() - 240
    for _ in range(60):
        if not engine._mem_buffer and toolkit.bundles:
            break
        await asyncio.sleep(0.05)
    assert not engine._mem_buffer, "冲刷后缓冲必须清空"
    assert toolkit.bundles, "叙事必须经一进多出解剖入池，绝不成段直存"
    bundle = toolkit.bundles[-1]
    assert getattr(bundle, "narrative", "") == narrative, "叙事必须作为属性随组落库"
    assert any(narrative[:20] in item.content for item in bundle.items), \
        "碎片由叙事解剖而来（抽取器输入=叙事）"


async def test_flush_falls_back_to_per_item_when_narrative_chain_fails(
        engine_factory, monkeypatch) -> None:
    """提纯失败（空叙事）→ 回退逐条各自一进多出；bundle 失败 → 原文留档待重试。"""
    engine, toolkit = engine_factory(bundle_ok=False)
    monkeypatch.setattr(engine, "_narrative_summary", lambda batch: "")
    deferred: list[str] = []
    monkeypatch.setattr(engine, "_defer_to_archive",
                        lambda text, intent: deferred.append(text))
    for i in range(8):
        engine._queue_memory(f"逐条回退消息{i}", "chat")
    engine._mem_last_message_ts = time.time() - 240
    for _ in range(60):
        if deferred:
            break
        await asyncio.sleep(0.05)
    assert deferred, "叙事链失败必须回退逐条处理"
    assert len(deferred) == 8, "每条原文各走一进多出，失败各留档（一进多出按条判定）"


@pytest.mark.asyncio
async def test_hello_restores_session_and_replays_history(engine_factory) -> None:
    """关页面重进：前端回传持久化的 session_id → restore 同一会话 → 历史可取回。"""
    import json
    engine, _ = engine_factory()
    # 第一次连接：空 session_id → 后端 create 新会话，发一条消息落库
    async with connect(f"ws://127.0.0.1:{engine._port}") as first:
        ack = await _hello(first)  # session_id 默认空串
        sid = ack["session_id"]
        assert sid
        await first.send(json.dumps({"type": "message", "text": "这条要留存"}))
        await _frames_until(first, "reply")
    # 模拟关页面重进：带旧 session_id 重连
    async with connect(f"ws://127.0.0.1:{engine._port}") as second:
        ack2 = await _hello(second, session_id=sid)
        assert ack2["session_id"] == sid, "后端必须 restore 同一会话 id"
    # 历史从持久化库取回（chat.history RPC 的数据源）
    history = engine.history(sid, limit=200)
    roles_texts = [(m["role"], m["text"]) for m in history]
    assert any(role == "user" and text == "这条要留存" for role, text in roles_texts), (
        f"重进后应能取回上次消息，实际={roles_texts}")
