"""chat.engine —— 8769 纯消息流引擎（v4.1 W3 / P2）。

- C9：独立线程 + 独立 asyncio loop，不复用 8767 的 WebSocketHub。
- P2.2：异步事件（typing / deep_recall_ready / media_ready）只从 8769 推，不依赖 8767。
- P2.3：session_id 由服务端分配；多设备接入同一 session 时回复 fan-out；状态在 sessions.db。
- P2.4：每个 session 一个 asyncio.Queue，消息严格按到达顺序串行处理。
- P2.5 / A2 / B4：复用 chat 专用令牌（不复用 Bridge 令牌），hmac 常量时间比较。
- P2.6 / A5 / Q19：召回走 `MCPToolkit.recall_for_agent(..., channel="bot")` 注入 L0 人格层。
- P2.7：聊天消息不逐条进长期记忆；只有「值得记住」的才经 WriteGate 写入。
- Q20：intent 由 `brain.thalamus.classify_intent()` 统一产出，同时驱动模型路由与召回深度。
"""
from __future__ import annotations

import asyncio
import hmac
import threading
from collections import deque
from typing import Any

from brain.thalamus import classify_intent
from brain.write_gate import WriteGate, WriteRequest
from chat.event_bus import EventBus
from chat.protocol import decode, encode
from chat.session_registry import SessionRegistry
from config import BrainConfig

__all__ = ["ChatEngine"]

_SENSITIVE_HINTS = ("密码", "密钥", "token", "身份证", "隐私", "私密")


class ChatEngine:
    def __init__(
        self,
        *,
        config: BrainConfig,
        toolkit: Any,
        router: Any,
        write_gate: WriteGate,
        agent: Any,
        token: str,
        sessions: SessionRegistry | None = None,
        bus: EventBus | None = None,
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        self._config = config
        self._toolkit = toolkit
        self._router = router
        self._write_gate = write_gate
        self._agent = agent
        self._token = str(token)
        self._sessions = sessions or SessionRegistry()
        self._bus = bus or EventBus()
        self._host = host or config.chat.host
        self._port = int(port if port is not None else config.chat.port)
        self._queues: dict[str, asyncio.Queue] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._history: dict[str, deque] = {}
        self._tasks: set[asyncio.Task] = set()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.running = False
        self.last_error = ""

    # ── 生命周期（独立线程 + 独立 loop）──────────────────────────────────
    def start(self) -> None:
        if self.running:
            return
        self._stopped.clear()
        self._thread = threading.Thread(target=self._run_loop, name="chat-engine", daemon=True)
        self._thread.start()

    def _run_loop(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception as exc:  # noqa: BLE001  线程异常要留痕，不静默
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.running = False

    async def _serve(self) -> None:
        from websockets.asyncio.server import serve

        self._loop = asyncio.get_running_loop()
        async with serve(self._handler, self._host, self._port) as server:
            self.running = True
            self.last_error = ""
            while not self._stopped.is_set():
                await asyncio.sleep(0.2)
            server.close()
            await server.wait_closed()
        self.running = False

    def stop(self, timeout: float = 3.0) -> None:
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self.running = False

    def rebind(self, write_gate: WriteGate) -> None:
        """W1 换核后替换闸门，避免残留写入打到旧引擎。"""
        self._write_gate = write_gate

    # ── 连接处理 ─────────────────────────────────────────────────────────
    async def _handler(self, ws: Any) -> None:
        session_id = ""
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            hello = decode(raw)
            if hello.get("type") != "hello" or not self._authorized(hello.get("token", "")):
                await ws.send(encode("error", message="unauthorized: chat 专用令牌无效"))
                await ws.close()
                return
            supplied = str(hello.get("session_id") or "")
            session_id = supplied if self._sessions.restore(supplied) else self._sessions.create()
            device_id = str(hello.get("device_id") or "device")
            self._sessions.touch(session_id, device_id)
            self._sessions.add_connection(session_id, ws)
            queue = self._queues.setdefault(session_id, asyncio.Queue())
            if session_id not in self._workers or self._workers[session_id].done():
                self._workers[session_id] = asyncio.create_task(
                    self._session_worker(session_id, queue)
                )
            await ws.send(encode("hello_ack", session_id=session_id,
                                 devices=self._sessions.devices(session_id)))
            async for message in ws:
                frame = decode(message)
                kind = frame.get("type")
                if kind == "ping":
                    await ws.send(encode("pong"))
                    continue
                if kind == "message":
                    text = str(frame.get("text") or "")
                    if text.strip():
                        await queue.put((text, device_id))
                else:
                    await ws.send(encode("error", message="unknown frame type"))
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass
        except Exception as exc:  # noqa: BLE001  单连接异常不拖垮服务
            self.last_error = f"{type(exc).__name__}: {exc}"
        finally:
            if session_id:
                self._sessions.remove_connection(session_id, ws)

    def _authorized(self, supplied: str) -> bool:
        return bool(supplied) and hmac.compare_digest(str(supplied), self._token)

    # ── 每 session 串行消费（P2.4）──────────────────────────────────────
    async def _session_worker(self, session_id: str, queue: asyncio.Queue) -> None:
        while not self._stopped.is_set():
            try:
                text, device_id = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await self._handle_message(session_id, text, device_id)
            except Exception as exc:  # noqa: BLE001  单条消息失败不影响后续消息
                await self._push(session_id, "error", {"message": f"{type(exc).__name__}: {exc}"})
            finally:
                queue.task_done()

    async def _handle_message(self, session_id: str, text: str, device_id: str) -> None:
        cfg = self._config.chat
        intent = classify_intent(text)  # Q20：模型路由与召回深度共用这一个 intent
        sensitive = self._is_sensitive(text)
        channel = cfg.persona_channel

        # ① 先给毫秒级 fast_recall（channel=bot 注入 L0 大肥鱼人格层）
        recall = await asyncio.to_thread(
            self._toolkit.recall_for_agent, self._agent,
            {"query": text, "channel": channel, "limit": 3},
        )
        await self._push(session_id, "typing", {"state": True,
                                               "stage": "recall", "intent": intent})

        # ② 组 prompt：人格注入 + 最近 N 轮会话缓冲（不逐条落长期记忆）
        history = list(self._history.setdefault(
            session_id, deque(maxlen=int(cfg.max_history_turns) * 2)))
        injection = str(recall.get("injection") or "") if isinstance(recall, dict) else ""
        parts = [injection, "【最近对话】", *history, f"用户：{text}", "请回复："]
        prompt = "\n".join(part for part in parts if part)

        # ③ 模型路由（进程内函数，不是 RPC）
        result = await asyncio.to_thread(
            self._router.complete, prompt,
            intent=intent, session_id=session_id, sensitive=sensitive,
        )
        reply = str(result.get("text") or "")
        self._history[session_id].append(f"用户：{text}")
        self._history[session_id].append(f"助手：{reply}")
        self._sessions.touch(session_id, device_id)
        await self._push(session_id, "typing", {"state": False})
        await self._push(session_id, "reply", {
            "text": reply, "intent": intent, "model": result.get("model", ""),
            # 不能用 "kind"：encode(kind=...) 的形参会撞名
            "provider_kind": result.get("kind", ""),
            "fallback": bool(result.get("fallback")),
        })

        # ④ deep 结果从 8769 异步推回（不依赖 8767，不阻塞回复）
        if cfg.deep_async and intent == "deep_recall":
            task = asyncio.create_task(self._deep_recall(session_id, text, channel))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        # ⑤ 只有「值得记住」的消息才写长期记忆，且必须经统一 WriteGate
        if self._worth_remembering(text, intent):
            await asyncio.to_thread(self._remember, text, intent)

    async def _push(self, session_id: str, kind: str, fields: dict[str, Any]) -> None:
        payload = encode(kind, session_id=session_id, **fields)
        for connection in self._sessions.connections(session_id):
            try:
                await connection.send(payload)
            except Exception:  # noqa: BLE001  断线连接直接摘除
                self._sessions.remove_connection(session_id, connection)
        self._bus.publish(session_id, {"type": kind, "session_id": session_id, **fields})

    async def _deep_recall(self, session_id: str, query: str, channel: str) -> None:
        await self._push(session_id, "typing", {"state": True, "stage": "deep"})
        deep = await asyncio.to_thread(
            self._toolkit.recall_for_agent, self._agent,
            {"query": query, "channel": channel, "deep": True, "limit": 5},
        )
        await self._push(session_id, "deep_recall_ready", {
            "hits": deep.get("hits", []) if isinstance(deep, dict) else [],
            "injection": deep.get("injection", "") if isinstance(deep, dict) else "",
        })
        await self._push(session_id, "typing", {"state": False})

    # ── 写入 / 判定 ─────────────────────────────────────────────────────
    @staticmethod
    def _is_sensitive(text: str) -> bool:
        content = str(text or "").lower()
        return any(hint in content for hint in _SENSITIVE_HINTS)

    @staticmethod
    def _worth_remembering(text: str, intent: str) -> bool:
        content = str(text or "")
        return ("记住" in content or "记一下" in content or "这条很重要" in content
                or intent == "code")

    def _remember(self, text: str, intent: str) -> None:
        """经 WriteGate 串行写入；cognition 让 P1 分流决定事务性/认知性路径。"""
        cognition = {"memory_type": "scenario", "source": "user_chat",
                     "topic": "chat", "intent": intent}
        try:
            self._write_gate.submit(WriteRequest(
                fn=lambda: self._toolkit.save_for_agent(
                    self._agent, {"content": text, "cognition": cognition}),
                event_type="ingest",
            )).result(timeout=30)
        except Exception as exc:  # noqa: BLE001  写记忆失败不中断聊天
            self.last_error = f"remember failed: {type(exc).__name__}: {exc}"

    @property
    def status(self) -> dict[str, Any]:
        return {
            "running": self.running, "host": self._host, "port": self._port,
            "sessions": len(self._sessions.all_sessions()),
            "queues": {key: value.qsize() for key, value in self._queues.items()},
            "last_error": self.last_error,
        }
