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
import hashlib
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
        invalidation_bus: Any | None = None,
        host: str | None = None,
        port: int | None = None,
        pad_tracker: Any | None = None,
        media_service: Any | None = None,
        event_sink: Any | None = None,
    ) -> None:
        self._config = config
        self._toolkit = toolkit
        self._router = router
        self._write_gate = write_gate
        self._agent = agent
        self._token = str(token)
        self._sessions = sessions or SessionRegistry()
        self._bus = bus or EventBus()
        # C05：失效总线只保留注入参数（headless 的 cache_bus，session 语义的
        # self._bus 不混用——Q33 裁决）；subscribe 接线由主线程完成。
        self._invalidation_bus = invalidation_bus
        self._snapshots: dict[str, dict] = {}
        self._cache = self._resolve_cache()
        self._pad_tracker = pad_tracker
        self._media_service = media_service
        self._event_sink = event_sink
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
        # W5 / C7：每轮喂入当前情绪（规则词典，<1ms），供前额叶 w6 分段门控
        if self._pad_tracker is not None:
            self._pad_tracker.observe(text)
        intent = classify_intent(text)  # Q20：模型路由与召回深度共用这一个 intent
        sensitive = self._is_sensitive(text)
        channel = cfg.persona_channel

        # W8 / P6.1：媒体意图先回文本 + typing，后台异步生成，不阻塞对话流
        if intent == "media":
            await self._push(session_id, "typing",
                             {"state": True, "stage": "media", "intent": intent})
            await self._push(session_id, "reply", {
                "text": "好的，正在生成…", "intent": intent, "model": "",
                "provider_kind": "local", "fallback": False,
            })
            task = asyncio.create_task(self._media_task(session_id, text))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return

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
        # C16：把本轮召回片段 id 传给输出缓存，使 memory_updated 能精确失效缓存的回复
        memory_ids = [str(hit.get("id")) for hit in (recall.get("hits") or [])
                      if isinstance(recall, dict) and isinstance(hit, dict) and hit.get("id")]
        result = await asyncio.to_thread(
            self._router.complete, prompt,
            intent=intent, session_id=session_id, sensitive=sensitive,
            memory_ids=memory_ids or None,
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

        # ④' C05 情节缓冲快照（Q30 裁决：合并进 _history 载体）：每轮把
        #     {turn_count, 召回片段 id, persona_hash} 增量写入 episodic ns；
        #     快照 stale 后下一轮在此重建（召回每轮都做，快照随轮覆盖）。
        self._record_snapshot(session_id, recall)

        # ④ deep 结果从 8769 异步推回（不依赖 8767，不阻塞回复）
        if cfg.deep_async and intent == "deep_recall":
            task = asyncio.create_task(self._deep_recall(session_id, text, channel))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        # ⑤ 只有「值得记住」的消息才写长期记忆，且必须经统一 WriteGate
        if self._worth_remembering(text, intent):
            await asyncio.to_thread(self._remember, text, intent)

    # ── C05：情节缓冲快照（episodic ns）──────────────────────────────────
    @staticmethod
    def _resolve_cache() -> Any:
        """统一约定入口；未装配（测试/独立脚本）返回 None，回退无缓存路径。"""
        try:
            from core.cache_manager import get_cache_manager
            return get_cache_manager()
        except Exception:  # noqa: BLE001  缓存模块异常不拖垮消息流引擎
            return None

    @staticmethod
    def _recall_hit_ids(recall: Any) -> list[str]:
        """从 recall_for_agent 结果的 hits 提取片段 id（失效精确判定的比对集）。"""
        if not isinstance(recall, dict):
            return []
        hits = recall.get("hits")
        if not isinstance(hits, list):
            return []
        return [str(row["id"]) for row in hits
                if isinstance(row, dict) and row.get("id")]

    def _persona_fingerprint(self) -> str:
        """persona_hash：只随 channel / 上下文层版本 / 人格参数版本变化。

        指纹不可用（单例未就绪等）时退化为仅 channel，绝不阻断对话。
        """
        parts = [str(getattr(self._config.chat, "persona_channel", ""))]
        try:
            from core.context_layers import get_context_layers
            status = get_context_layers().status()
            parts.append(f"layers:{status.get('revision', 0)}")
            from core.persona import get_persona_layer
            persona = get_persona_layer().status()
            parts.append(
                f"persona:{persona.get('versions', 0)}:{persona.get('params_count', 0)}")
        except Exception:  # noqa: BLE001  指纹降级不阻断对话
            pass
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]

    def _record_snapshot(self, session_id: str, recall: Any) -> None:
        """每轮处理完把快照增量写入 episodic ns（键=session_id，值 JSON 可序列化）。"""
        previous = self._snapshots.get(session_id) or {}
        snapshot = {
            "turn_count": int(previous.get("turn_count", 0)) + 1,
            "recalled_memory_ids": self._recall_hit_ids(recall),
            "persona_hash": self._persona_fingerprint(),
            "stale": False,  # 下一轮重建即清位：召回每轮都做，快照随轮覆盖
        }
        self._snapshots[session_id] = snapshot
        cache = self._cache
        if cache is None:
            return  # 未装配缓存：仅保留进程内载体，回退原路径
        try:
            cache.set("episodic", session_id, snapshot)
        except Exception as exc:  # noqa: BLE001  缓存失败不中断对话
            self.last_error = f"snapshot cache failed: {type(exc).__name__}: {exc}"

    def _on_memory_updated(self, event: dict[str, Any]) -> None:
        """C05 失效判定：memory_updated 的 affected_ids 命中本轮召回片段 id 时
        标记快照 stale（下一轮重建并重新召回）；其余快照一律不动（失效精确不清空）。

        注意：不要把本方法挂到 self._bus（session 语义）；应由主线程把它订阅到
        headless 的 cache_bus（self._invalidation_bus 仅保留注入参数）。
        """
        if not isinstance(event, dict) or event.get("type") != "memory_updated":
            return
        affected = {str(item) for item in (event.get("affected_ids") or []) if item}
        if not affected:
            return
        for snapshot in self._snapshots.values():
            ids = snapshot.get("recalled_memory_ids") or []
            if affected.intersection(str(item) for item in ids):
                snapshot["stale"] = True

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

    # ── W8 / P6：媒体异步任务 ────────────────────────────────────────────
    async def _media_task(self, session_id: str, text: str) -> None:
        result: dict[str, Any] | None = None
        try:
            if self._media_service is not None:
                result = await asyncio.to_thread(
                    self._media_service.generate, "image", {"prompt": text},
                    session_id=session_id,
                )
        except Exception as exc:  # noqa: BLE001  媒体失败只降级，不影响对话
            self.last_error = f"media failed: {type(exc).__name__}: {exc}"
        # P6.4：只记 meta 事件，媒体本体不进六引擎（worker 的 media 分支不产生 neuron）
        if self._event_sink is not None:
            try:
                await asyncio.to_thread(
                    lambda: self._event_sink(
                        f"媒体请求：{text[:60]}", agent_id="chat",
                        namespace="topic:chat", event_type="media_interaction",
                    )
                )
            except Exception:  # noqa: BLE001  审计失败不阻断媒体回复
                pass
        await self._push(session_id, "media_ready", {
            "ok": result is not None,
            "message": "媒体已生成" if result else "暂时无法生成图片",
            "path": (result or {}).get("path", ""), "media_type": "image",
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
