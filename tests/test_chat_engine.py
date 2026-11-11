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


class FakeToolkit:
    def __init__(self) -> None:
        self.recalls: list[dict] = []
        self.saves: list[dict] = []

    def recall_for_agent(self, agent: Any, args: dict) -> dict:
        self.recalls.append(dict(args))
        return {"hits": [{"id": "n1", "content": "记忆片段"}],
                "injection": "【人格】大肥鱼在此" if not args.get("deep") else "【深度】事件骨架"}

    def save_for_agent(self, agent: Any, args: dict) -> dict:
        self.saves.append(dict(args))
        return {"applied": True}


class FakeRouter:
    def complete(self, prompt: str, **kwargs: Any) -> dict:
        user_lines = [line[3:] for line in prompt.splitlines() if line.startswith("用户：")]
        return {"ok": True, "text": user_lines[-1] if user_lines else "?",
                "model": "fake", "kind": "local", "fallback": False}


@pytest.fixture()
def engine_factory(tmp_path):
    built: list[tuple[ChatEngine, WriteGate, SessionRegistry, FakeToolkit]] = []

    def build(*, token: str = "test-token", deep_async: bool = True):
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
        toolkit = FakeToolkit()
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
async def test_worth_remembering_goes_through_write_gate(engine_factory) -> None:
    engine, toolkit = engine_factory()
    async with connect(f"ws://127.0.0.1:{engine._port}") as ws:
        await _hello(ws)
        import json

        await ws.send(json.dumps({"type": "message", "text": "记住这个约定：聊天要带人格"}))
        await _frames_until(ws, "reply")
        for _ in range(20):
            if toolkit.saves:
                break
            await asyncio.sleep(0.05)
    assert toolkit.saves and "记住这个约定" in toolkit.saves[0]["content"]
    assert toolkit.saves[0]["cognition"]["source"] == "user_chat"
