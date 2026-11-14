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
import json as _json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from brain.thalamus import classify_intent
from brain.pad_lexicon import estimate_pad
from brain.write_gate import WriteGate, WriteRequest
from chat.event_bus import EventBus
from chat.protocol import decode, encode
from chat.session_registry import SessionRegistry
from config import BrainConfig
from core.runtime_paths import data_dir

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
        # ★ 聊天记忆缓冲：不再逐条判定"值不值得记"（那曾让日常聊天几乎全丢）。
        #   消息进缓冲，最长 5 分钟 / 满 8 条触发一次批量写入（一进多出管线，
        #   丘脑准入负责去重/拒低价值）；显式指令（"记住"等）立即 flush。
        self._mem_buffer: list[tuple[str, str]] = []  # (原文, intent)
        self._mem_last_flush: float = 0.0
        self._mem_last_message_ts: float = 0.0   # 用户最后一条消息时刻（静默门控）
        self._mem_buffer_since: float = 0.0      # 缓冲里最老一条的入队时刻（兜底强制存）
        self._mem_flushing: bool = False         # 冲刷互斥（flush 是重 IO，不重入）
        self._mem_status: dict[str, Any] = {     # 缓冲实时快照（缓存页卡片数据源）
            "buffer_len": 0, "buffer_since": 0.0, "last_message_ts": 0.0,
            "last_flush_ts": 0.0, "flushing": False,
        }
        # 会话级模型选择（message 帧可选 model 字段；空 = 跟随路由）
        self._session_models: dict[str, str] = {}
        # AI 空间写入口（headless 注入：AI 自己写日记/发说说）
        self.ai_space: Any | None = None
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
        self._wal_replay()  # 上次崩溃/重启丢失的冲刷批次先找回，绝不蒸发
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
        # max_size：聊天支持发图（base64 data URL），默认 1MiB 必炸，放宽到 16MiB
        async with serve(self._handler, self._host, self._port,
                         max_size=16 * 1024 * 1024) as server:
            self.running = True
            self.last_error = ""
            while not self._stopped.is_set():
                await asyncio.sleep(0.2)
                # ★ 聊天记忆缓冲的静默门控轮询：满批 + 用户静默 ≥3 分钟（一轮对话
                #   已告一段落）才冲刷，不把正在聊的高潮强行截断；flush 是重 IO
                #   （LLM 提纯 + 一进多出写闸），放线程执行不阻塞事件循环。
                if self._flush_due():
                    task = asyncio.create_task(
                        asyncio.to_thread(self._flush_memory_buffer))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
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
                    # 多模态：images 为 data URL / http(s) URL 列表（最多 4 张）。
                    raw_images = frame.get("images")
                    images: list[str] = []
                    if isinstance(raw_images, list):
                        for item in raw_images:
                            src = str(item or "").strip()
                            if src.startswith(("data:image/", "http://", "https://")):
                                images.append(src)
                        images = images[:4]
                    if text.strip() or images:
                        model = str(frame.get("model") or "").strip()
                        if model:
                            self._session_models[session_id] = model
                        elif "model" in frame:
                            self._session_models.pop(session_id, None)
                        await queue.put((text, device_id, images))
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
                text, device_id, images = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await self._handle_message(session_id, text, device_id, images=images)
            except Exception as exc:  # noqa: BLE001  单条消息失败不影响后续消息
                await self._push(session_id, "error", {"message": f"{type(exc).__name__}: {exc}"})
            finally:
                queue.task_done()

    async def _handle_message(self, session_id: str, text: str, device_id: str,
                              *, images: list[str] | None = None) -> None:
        cfg = self._config.chat
        images = [str(x) for x in (images or []) if str(x).strip()]
        # 纯图片无文字：正文用占位（prompt 与记忆都落到"发了图"），图片本体走多模态通道
        display_text = text.strip() or ("[图片]" if images else "")
        if not display_text:
            return
        # W5 / C7：每轮喂入当前情绪（规则词典，<1ms），供前额叶 w6 分段门控
        if self._pad_tracker is not None:
            self._pad_tracker.observe(display_text)
        intent = classify_intent(display_text) or ("chat" if images else "")
        # Q20：模型路由与召回深度共用这一个 intent；纯图无文字归 chat
        if not intent:
            intent = "chat"
        sensitive = self._is_sensitive(display_text)
        channel = cfg.persona_channel
        self._persist_message(session_id, "user", display_text)

        # AI 自我表达：聊天里"写日记/发说说" → 直接进 AI 空间（不经模型）
        if self.ai_space is not None and (display_text.startswith(("写日记", "记日记"))
                                          or display_text.startswith(("发说说", "发条说说"))):
            reply = await asyncio.to_thread(self._ai_self_post, display_text)
            self._history[session_id].append(f"用户：{display_text}")
            self._history[session_id].append(f"助手：{reply}")
            await self._push(session_id, "typing", {"state": False})
            await self._push(session_id, "reply", {
                "text": reply, "intent": "chat", "model": "ai-space",
                "provider_kind": "local", "fallback": False,
            })
            self._persist_message(session_id, "assistant", reply)
            return

        # W8 / P6.1：媒体意图先回文本 + typing，后台异步生成，不阻塞对话流
        if intent == "media":
            await self._push(session_id, "typing",
                             {"state": True, "stage": "media", "intent": intent})
            await self._push(session_id, "reply", {
                "text": "好的，正在生成…", "intent": intent, "model": "",
                "provider_kind": "local", "fallback": False,
            })
            self._persist_message(session_id, "assistant", "好的，正在生成…")
            task = asyncio.create_task(self._media_task(session_id, display_text))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return

        # ① 六通道类人召回（2026-11-12 用户裁决：聊天全面用超级大脑精髓，不再走
        #    fast 字符 2-gram 投影）。prefrontal 一次跑齐：密集+稀疏+全文三路 →
        #    W5 激活扩散 → P4.2 叙事骨架整体注入 → E4 PAD 门控 → E2 置信度/知识缺口
        #    → reranker 交叉编码精排；channel=bot 注入 L0 大肥鱼人格层。
        #    性能由缓存兜底：C11 召回结果 / C12 embedding / C18 向量索引段命中即复用，
        #    精排模型首次加载后常驻，稳态单轮约 40~100ms。
        await self._push(session_id, "typing", {"state": True,
                                               "stage": "recall", "intent": intent})
        recall = await asyncio.to_thread(
            self._toolkit.recall_for_agent, self._agent,
            {"query": display_text, "channel": channel, "limit": 3, "deep": True},
        )

        # ② 组 prompt：人格注入 + 最近 N 轮会话缓冲（不逐条落长期记忆）
        history = list(self._history.setdefault(
            session_id, deque(maxlen=int(cfg.max_history_turns) * 2)))
        injection = str(recall.get("injection") or "") if isinstance(recall, dict) else ""
        prompt = self._build_chat_prompt(injection, history, display_text, intent, recall)

        # ③ 模型路由（进程内函数，不是 RPC）
        # C16：把本轮召回片段 id 传给输出缓存，使 memory_updated 能精确失效缓存的回复
        memory_ids = [str(hit.get("id")) for hit in (recall.get("hits") or [])
                      if isinstance(recall, dict) and isinstance(hit, dict) and hit.get("id")]
        result = await asyncio.to_thread(
            self._router.complete, prompt,
            intent=intent, session_id=session_id, sensitive=sensitive,
            memory_ids=memory_ids or None,
            model=self._session_models.get(session_id, ""),
            images=images or None,
        )
        reply = str(result.get("text") or "")
        self._history[session_id].append(f"用户：{display_text}")
        self._history[session_id].append(f"助手：{reply}")
        self._sessions.touch(session_id, device_id)
        await self._push(session_id, "typing", {"state": False})
        await self._push(session_id, "reply", {
            "text": reply, "intent": intent, "model": result.get("model", ""),
            # 不能用 "kind"：encode(kind=...) 的形参会撞名
            "provider_kind": result.get("kind", ""),
            "fallback": bool(result.get("fallback")),
        })
        self._persist_message(session_id, "assistant", reply)

        # ④' C05 情节缓冲快照（Q30 裁决：合并进 _history 载体）：每轮把
        #     {turn_count, 召回片段 id, persona_hash} 增量写入 episodic ns；
        #     快照 stale 后下一轮在此重建（召回每轮都做，快照随轮覆盖）。
        self._record_snapshot(session_id, recall)

        # ④ 深度归因结果从 8769 异步推回（不依赖 8767，不阻塞回复）
        # 2026-11-12：主召回已全面六通道，不再二次跑同一 deep 查询——
        # 直接把本轮主召回的 hits/injection/knowledge_gap 推给前端归因视图。
        if cfg.deep_async and intent == "deep_recall":
            task = asyncio.create_task(self._push_deep_attribution(session_id, recall, channel))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        # ⑤ 聊天记忆：缓冲批量写，最长 5 分钟一次；显式指令立即写。
        #    _remember 内部走云端抽取+WriteGate（重 IO），放线程不阻塞回复。
        task = asyncio.create_task(asyncio.to_thread(
            self._chat_memory_and_feedback, display_text, intent))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # 显式反馈词表：夸（正）/ 骂嫌弃（负）——命中即记一次人格反馈，
    # 同向连续 3 次且跨 ≥2 会话自动生成人格提案（待确认应用）。
    _PRAISE_WORDS = ("乖", "真棒", "好棒", "可爱", "喜欢你", "爱你", "真聪明", "厉害", "夸", "做得好")
    _BLAME_WORDS = ("讨厌", "笨", "蠢", "爬", "滚", "闭嘴", "烦人", "废物", "别这样")

    def _chat_memory_and_feedback(self, text: str, intent: str) -> None:
        content = str(text or "")
        # ① 人格反馈：所有对话必触发（主人硬要求）。
        #    valence 分层：显式夸/骂词强信号 → PAD 词典情绪弱信号 → 中性也记录。
        #    同向连续 3 次且跨 ≥2 会话自动生成人格提案，待确认应用。
        try:
            from core.persona import get_persona_layer
            hit_praise = any(w in content for w in self._PRAISE_WORDS)
            hit_blame = any(w in content for w in self._BLAME_WORDS)
            if hit_praise and hit_blame:
                valence, signal = 0.0, "又夸又骂（抵消）"
            elif hit_praise:
                valence, signal = 0.8, "夸奖"
            elif hit_blame:
                valence, signal = -0.8, "嫌弃"
            else:
                # 隐式通道：PAD 情绪词典 p 维（愉悦度）作弱信号，中性也记录累积
                p, _a, _d = estimate_pad(content)
                valence, signal = max(-1.0, min(1.0, p * 0.5)), "对话情绪(PAD)"
            get_persona_layer().feedback(
                signal=signal, valence=valence, target="sassiness",
                note=content[:100], session_id="", actor="bot")
        except Exception:  # noqa: BLE001  反馈失败不影响记忆与聊天
            pass
        # ② 记忆缓冲批量写
        self._queue_memory(text, intent)

    # ── Prompt 组装（人格优先，检索约束按意图分级）───────────────────────
    @staticmethod
    def _soften_injection(injection: str) -> str:
        """闲聊时剥离"未命中按未知/禁止编造/停止发散"这类检索硬约束。

        这些约束对事实问答是对的，但对情感陪伴是灾难——模型被锤得只敢回"未知"，
        人格剧本被压在下面发挥不出来（QQ 通道从未有这类约束，所以鲜活）。
        """
        if not injection:
            return ""
        kept: list[str] = []
        for line in injection.splitlines():
            if "【执行约束】" in line or "未命中的细节按未知处理" in line:
                continue
            if "据此收敛作答" in line:
                continue
            kept.append(line)
        return "\n".join(kept).strip()

    def _build_chat_prompt(self, injection: str, history: list[str], text: str,
                           intent: str, recall: dict) -> str:
        """大肥鱼聊天 prompt：完整人格剧本置顶当角色，记忆按意图软/硬分级。

        - 闲聊/emotion：人格剧本置顶；检索约束剥离，记忆降级为"想起之前…"的背景；
          不留"未知/禁止编造"指令（这是此前机械的根因）。
        - 事实/deep_recall/code：保留检索约束防编造，但人格剧本仍在最前当风格层。
        """
        personal_chat = intent in ("chat", "emotion")
        # ① 完整行为人格剧本置顶（与 QQ 通道同源：超级大脑档案 cccf1cb8fbad）
        try:
            from core.persona import get_persona_layer

            layer = get_persona_layer()
            persona = layer.compose_text(full=True)
            # 上限只作硬顶：设 N 则按语境回 1~N 条，绝不把它当成"每轮发满 N 条"。
            try:
                cap = int((layer.get().get("params") or {}).get("max_lines_per_reply", 6))
            except Exception:  # noqa: BLE001  参数异常不阻断聊天
                cap = 6
            cap = max(1, min(6, cap))
        except Exception:  # noqa: BLE001  人格层异常不阻断聊天
            persona = "你是大肥鱼，嘴硬心软、爱撒娇斗嘴，说话短碎口语化。"
            cap = 6
        parts = [persona]
        # ② 记忆注入：闲聊软化（去检索硬约束），事实查询保留
        memory_block = self._soften_injection(injection) if personal_chat else injection
        if memory_block:
            if personal_chat:
                parts.append("【记忆背景】下面是从记忆里自然想起的片段，像回忆一样带出，"
                             "别念档案、别逐字复述；想不起来就顺着聊，不要说不知道或未知。\n"
                             + memory_block)
            else:
                parts.append(memory_block)
        # ③ 最近会话缓冲
        parts.append("【最近对话】\n" + "\n".join(history) if history else "【最近对话】无")
        # ④ 知识缺口：闲聊不追加"坦承不知道"（否则又变机械），事实查询才追加
        gap = recall.get("knowledge_gap") if isinstance(recall, dict) else None
        if (not personal_chat) and isinstance(gap, dict) and gap.get("gap"):
            parts.append("（记忆库中没有与该问题直接相关的记录：如实说明不确定，不要编造具体细节。）")
        parts.append(f"用户：{text}")
        if personal_chat:
            parts.append(
                f"以大肥鱼的口吻回复：短、碎、口语化。条数由这轮语境决定，本轮最多 {cap} 条，"
                f"1~{cap} 条都算正常；{cap} 只是硬上限不是目标，别每轮都发满 {cap} 条，"
                "也别把一句话硬拆成几行凑数。")
        else:
            parts.append("请回复：")
        return "\n\n".join(part for part in parts if part)

    # ── AI 自我表达（写日记/发说说，落 AI 空间 kv）────────────────────────
    def _ai_self_post(self, text: str) -> str:
        if self.ai_space is None:
            return "AI 空间还没接好，稍后再试。"
        try:
            if text.startswith(("发说说", "发条说说")):
                body = text.split("说说", 1)[-1].lstrip("：:，, ").strip()
                image = ""
                if not body:
                    return "说说内容是空的，说点什么再发。"
                result = self.ai_space.write_moment(text=body, image=image)
                return "说说已发到我的空间。" if result.get("ok") \
                    else f"发说说失败：{result.get('error', '未知原因')}"
            body = text.split("日记", 1)[-1].lstrip("：:，, ").strip()
            if not body:
                return "想让我写点什么呢？说一句「写日记：今天…」试试。"
            result = self.ai_space.write_diary(text=body)
            return "写好了，收进我的日记本了。" if result.get("ok") \
                else f"写日记失败：{result.get('error', '未知原因')}"
        except Exception as exc:  # noqa: BLE001  自我表达失败不影响聊天
            return f"AI 空间写入失败：{type(exc).__name__}: {exc}"

    # ── 聊天记录持久化（sessions.db messages 表，v2）─────────────────────
    def _persist_message(self, session_id: str, role: str, text: str) -> None:
        try:
            self._sessions.append_message(session_id, role, text)
        except Exception as exc:  # noqa: BLE001  落库失败不阻断对话
            self.last_error = f"chat persist failed: {type(exc).__name__}: {exc}"

    def history(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """跨会话/重启的持久化记录（chat.history RPC 数据源）。"""
        try:
            return self._sessions.history(session_id, limit=limit)
        except Exception:  # noqa: BLE001  读失败按空历史展示
            return []

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
        """兼容保留：显式请求一次 deep 召回（主链路已 deep，此路径仅外部主动调用时用）。"""
        await self._push(session_id, "typing", {"state": True, "stage": "deep"})
        deep = await asyncio.to_thread(
            self._toolkit.recall_for_agent, self._agent,
            {"query": query, "channel": channel, "deep": True, "limit": 5},
        )
        await self._push_deep_attribution(session_id, deep, channel)

    async def _push_deep_attribution(self, session_id: str, recall: Any, channel: str) -> None:
        """把已完成的六通道召回结果推给前端归因视图（不重复跑检索）。

        主召回现已是六通道（激活扩散/叙事骨架/PAD 门控/置信度/精排），
        deep_recall 意图直接复用本轮结果，省掉第二次 embedding + 精排。
        """
        await self._push(session_id, "typing", {"state": True, "stage": "deep"})
        payload = recall if isinstance(recall, dict) else {}
        await self._push(session_id, "deep_recall_ready", {
            "hits": payload.get("hits", []),
            "injection": payload.get("injection", ""),
            # E2 元认知：知识缺口标记（六通道模式由 prefrontal.last_knowledge_gap 产出）
            "knowledge_gap": payload.get("knowledge_gap"),
            "recall_mode": payload.get("recall_mode", "six-channel"),
            "narratives": payload.get("narratives", []),
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
        """敏感词判定（路由是否允许上云的硬门）。

        v4.1-C 全开：结果写 ``safety`` ns（键=sha256(lower(text))）。
        词表是硬编码常量、判定确定性，缓存安全；未装配/异常回退原逻辑。
        """
        content = str(text or "").lower()
        try:
            from core.cache_manager import get_cache_manager

            mgr = get_cache_manager()
        except Exception:  # noqa: BLE001
            mgr = None
        key = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if mgr is not None:
            try:
                hit = mgr.get("safety", key)
                if isinstance(hit, bool):
                    return hit
            except Exception:  # noqa: BLE001
                pass
        verdict = any(hint in content for hint in _SENSITIVE_HINTS)
        if mgr is not None:
            try:
                mgr.set("safety", key, verdict)
            except Exception:  # noqa: BLE001
                pass
        return verdict

    # 聊天记忆缓冲门控（2026-11-12 用户裁决）：满 8 条 **且** 用户静默 ≥3 分钟
    # 才触发 flush（切出来的叙事块是一轮完整对话，不把正在聊的高潮强行截断）；
    # 外加 30 分钟兜底：缓冲压了半小时无人问津也强制存一次，绝不无限滞留。
    _MEM_FLUSH_BATCH = 8
    _MEM_FLUSH_SILENCE_SEC = 180
    _MEM_FLUSH_MAX_IDLE_SEC = 1800

    def _queue_memory(self, text: str, intent: str) -> None:
        """入缓冲；flush 由引擎主循环按"满批+静默"门控触发；显式"记住"立即写。

        注意：本方法只在**新消息到达**时执行，"用户静默"在这里永远判定不出来
        （新消息本身就是打破静默），所以静默触发必须由 _serve 主循环轮询。
        """
        content = str(text or "").strip()
        if not content:
            return
        now = time.time()
        self._mem_last_message_ts = now
        if not self._mem_buffer:
            self._mem_buffer_since = now
        if self._worth_remembering(content, intent):
            try:
                self._remember(content, intent)
            except Exception:  # noqa: BLE001  写失败不中断聊天
                pass
            self._mem_last_flush = now
            return
        self._mem_buffer.append((content, intent))
        # 缓冲状态快照（缓存页「聊天记忆缓冲」卡片实时数据源）
        self._mem_status.update({
            "buffer_len": len(self._mem_buffer),
            "buffer_since": self._mem_buffer_since,
            "last_message_ts": self._mem_last_message_ts,
        })

    def _flush_due(self) -> bool:
        """门控：满 8 条且静默 ≥3 分钟（一轮完整对话已结束）；或缓冲压了 ≥30 分钟兜底。"""
        if not self._mem_buffer or self._mem_flushing:
            return False
        now = time.time()
        full_and_silent = (
            len(self._mem_buffer) >= self._MEM_FLUSH_BATCH
            and now - self._mem_last_message_ts >= self._MEM_FLUSH_SILENCE_SEC
        )
        idle_overflow = now - self._mem_buffer_since >= self._MEM_FLUSH_MAX_IDLE_SEC
        return full_and_silent or idle_overflow

    def _flush_memory_buffer(self) -> None:
        """冲刷缓冲：先 WAL 落盘防丢 → 叙事提纯当抽取器输入 → 一进多出解剖入池。

        ★ WAL（2026-11-12 架构裁决）：冲刷前把整批原文写进 pending_ingest.ndjson，
          六引擎确认写入成功后才删除该行；进程在 LLM 调用/写库途中崩溃，重启时
          `_wal_replay` 自动把未确认批次塞回缓冲立即重试——聊天记忆绝不因崩溃蒸发。
        互斥：`_mem_flushing` 保证同一时刻只有一次冲刷（并发抽取会造成图谱重复
        实体/边）；冲刷期间新消息照常入缓冲，由下一轮门控再触发。
        失败兜底链：云端路由挂 → 本地已裁撤 → 规则拆分（确定性，至少出 1 条碎片）；
        整批彻底失败 → 原文留项目档案 + WAL 保留，重启/夜间重试。
        """
        if self._mem_flushing or not self._mem_buffer:
            return
        self._mem_flushing = True
        wal_seq = 0
        try:
            batch, self._mem_buffer = self._mem_buffer, []
            self._mem_buffer_since = 0.0
            self._mem_last_flush = time.time()
            # 缓冲状态快照（缓存页卡片）
            self._mem_status.update({
                "buffer_len": 0, "flushing": True,
                "last_flush_ts": self._mem_last_flush,
            })
            wal_seq = self._wal_append(batch)
            # ★ 叙事是"输入源"，不是"存储终态"（2026-11-12 架构裁决）：把这轮完整
            #   对话提纯成一段第一人称叙事，交给 RouterExtractor 一进多出解剖成
            #   fact/profile/emotion/scenario + 超边入池；叙事本身作为 scenario 的
            #   narrative 属性随组落库（LMDB 旁路索引），绝不整段直接当记忆正文。
            narrative = self._narrative_summary(batch)
            if narrative:
                try:
                    if self._remember(narrative, "consolidation", narrative_text=narrative):
                        self._wal_remove(wal_seq)
                        return
                except Exception:  # noqa: BLE001  叙事链失败回退逐条
                    pass
            # 回退：按条目逐条各自一进多出（各自判定梯队、各自丘脑准入）
            failed: list[tuple[str, str]] = []
            for item_text, item_intent in batch:
                try:
                    if not self._remember(item_text, item_intent):
                        failed.append((item_text, item_intent))
                except Exception:  # noqa: BLE001  单条失败滚回缓冲，下轮随批重试
                    failed.append((item_text, item_intent))
            if failed:
                self._mem_buffer = failed + self._mem_buffer
                if not self._mem_buffer_since:
                    self._mem_buffer_since = time.time()
            else:
                self._wal_remove(wal_seq)  # 全部落库：WAL 行才允许删除
        finally:
            self._mem_flushing = False
            self._mem_status.update({
                "flushing": False, "buffer_len": len(self._mem_buffer),
                "buffer_since": self._mem_buffer_since,
            })

    def mem_status(self) -> dict[str, Any]:
        """聊天记忆缓冲实时状态（缓存页「聊天记忆缓冲」卡片）。"""
        return {
            "buffer_len": len(self._mem_buffer),
            "buffer_since": self._mem_buffer_since,
            "last_message_ts": getattr(self, "_mem_last_message_ts", 0.0),
            "last_flush_ts": getattr(self, "_mem_last_flush", 0.0),
            "flushing": bool(self._mem_flushing),
            "batch": self._MEM_FLUSH_BATCH,
            "silence_sec": self._MEM_FLUSH_SILENCE_SEC,
            "max_idle_sec": self._MEM_FLUSH_MAX_IDLE_SEC,
        }

    # ── 冲刷 WAL（pending_ingest.ndjson：冲刷前落盘，落库成功才删行）──────
    @staticmethod
    def _wal_path() -> Path:
        return data_dir() / "pending_ingest.ndjson"

    @classmethod
    def _wal_append(cls, batch: list[tuple[str, str]]) -> int:
        seq = time.time_ns()
        try:
            line = _json.dumps({"seq": seq, "ts": int(time.time() * 1000),
                                "batch": [[t, i] for t, i in batch]},
                               ensure_ascii=False)
            with cls._wal_path().open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            return 0  # WAL 写失败不阻断冲刷（降级为内存-only，行为与旧版一致）
        return seq

    @classmethod
    def _wal_remove(cls, seq: int) -> None:
        if not seq:
            return
        path = cls._wal_path()
        try:
            if not path.exists():
                return
            lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
                     if ln.strip()
                     and (not ln.lstrip().startswith("{")
                          or _json.loads(ln).get("seq") != seq)]
            if lines:
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            else:
                path.unlink()
        except (OSError, ValueError):
            pass  # 清理失败不影响主流程；重启重放按幂等写入兜底

    def _wal_replay(self) -> None:
        """启动重放：上次未确认的冲刷批次塞回缓冲，idle_overflow 门控立即重冲刷。"""
        path = self._wal_path()
        if not path.exists():
            return
        restored = 0
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = _json.loads(line)
                    for text, intent in (row.get("batch") or []):
                        self._mem_buffer.append((str(text), str(intent)))
                        restored += 1
                except ValueError:
                    continue  # 半行（崩溃时写一半）直接丢弃
        except OSError:
            return
        if restored:
            # buffer_since=0 → idle_overflow 立即满足，主循环启动后马上重冲刷
            self._mem_buffer_since = 0.0
            self.last_error = f"WAL 重放：{restored} 条上次未确认的聊天记忆待重冲刷"
        try:
            path.unlink()  # 内容已进内存缓冲，下次冲刷会重新落 WAL
        except OSError:
            pass

    def _narrative_summary(self, batch: list[tuple[str, str]]) -> str:
        """把这轮对话交给 LLM 提纯成连贯的第一人称叙事（给抽取器的输入材料）。

        产物不是记忆，是 RouterExtractor 的"高纯度原料"：叙事里必须保留称呼、
        事实、情绪起伏与往来互动，供一进多出解剖成碎片；失败返回空串走回退。
        """
        if not batch:
            return ""
        lines = "\n".join(f"- [intent={it}] {tx}" for tx, it in batch)
        prompt = (
            "以下是我（AI）与用户最近一轮对话的原始记录。请把它提纯成一段连贯的"
            "**第一人称叙事**，作为后续记忆抽取的输入材料：\n"
            "1. 用「我」指代 AI、用「你/用户」指代对方，讲「我们之间发生了什么」；\n"
            "2. 必须保留：称呼（谁怎么叫谁）、事实、名字、数字、因果、情绪起伏、"
            "双方的往来互动；去掉寒暄重复，绝不编造；\n"
            "3. 只输出叙事正文，不要标题、列表或解释，300 字以内。\n\n对话记录：\n" + lines)
        try:
            from gateway.model_router import get_model_router
            raw = get_model_router().complete_task("consolidation", prompt)
            text = str(raw or "").strip()
            if text.startswith("```"):
                text = text.strip("`").lstrip("json").strip()
            return text[:2000] if len(text) >= 20 else ""
        except Exception:  # noqa: BLE001  远程失败 → 调用方回退逐条
            return ""

    @staticmethod
    def _worth_remembering(text: str, intent: str) -> bool:
        content = str(text or "")
        return ("记住" in content or "记一下" in content or "这条很重要" in content
                or intent == "code")

    # Bot 通道记忆归属的真实空间 id（topic:f844c49c0e58 =「AI工具链与WBMod」下的 Bot 会话空间）。
    # 旧版硬编码 topic:"chat" 不是合法空间 id → 落 topic:default，第二/第三层治理丢失。
    _BOT_TOPIC = "f844c49c0e58"

    def _remember(self, text: str, intent: str, *, narrative_text: str = "") -> bool:
        """一进多出：一条输入 → 一组结构化记忆（profile/emotion/fact/scenario）+ 事件超边。

        链路：RouterExtractor.extract_multi（task_routes["extract"] 远程大模型优先 →
        本地 0.6B → 规则拆分兜底）→ ExtractionBundle → MCPToolkit.save_bundle_for_agent
        → Hippocampus.remember_bundle（逐条丘脑准入 + 六引擎投影 + 一张事件骨架超边）。
        ``narrative_text``：本批对话提纯出的第一人称叙事，作为 scenario 的 narrative
        属性随组落库（LMDB 旁路索引），不是记忆正文。返回是否成功入池（或合规不拆）。
        整条经 WriteGate 串行。
        """
        extractor = getattr(self._toolkit, "extractor", None)
        if extractor is None:
            try:
                from brain.router_extractor import RouterExtractor

                extractor = RouterExtractor(None, self._config)  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001
                extractor = None

        def write_bundle() -> dict[str, Any]:
            bundle = None
            if extractor is not None:
                try:
                    bundle = extractor.extract_multi(text, agent_id=self._agent_id())
                except Exception:  # noqa: BLE001
                    bundle = None
            # ★ 铁律：一进多出要么整组入池，要么不进记忆（绝不降级单条 scenario）。
            #   不合规/失败的内容落到项目档案「待重试」，等修复后按新架构重新 ingest。
            if bundle is not None and getattr(bundle, "items", None):
                if narrative_text:
                    import dataclasses as _dc

                    bundle = _dc.replace(bundle, narrative=narrative_text)
                saver = getattr(self._toolkit, "save_bundle_for_agent", None)
                if callable(saver):
                    result = saver(self._agent, bundle, namespace=f"topic:{self._BOT_TOPIC}")
                    if isinstance(result, dict) and result.get(
                            "handled", result.get("applied", 0) > 0):
                        return result   # 含事务性/寒暄/信息不足等"合规不拆"分支
            self._defer_to_archive(text, intent)
            return {"applied": 0, "deferred": True, "reason": "一进多出失败，已留项目档案待重试"}

        try:
            outcome = self._write_gate.submit(
                WriteRequest(fn=write_bundle, event_type="ingest")).result(timeout=45)
            if isinstance(outcome, dict) and outcome.get(
                    "handled", outcome.get("applied", 0) > 0):
                return True
            return False
        except Exception as exc:  # noqa: BLE001  写记忆失败不中断聊天，但要留待重试
            self.last_error = f"remember failed: {type(exc).__name__}: {exc}"
            try:
                self._defer_to_archive(text, intent)
            except Exception:  # noqa: BLE001
                pass
            return False

    # 待重试机制已废除（2026-11-12 用户裁决：系统文档存都不准存）——失败只落日志，
    # 不再写 [待重试 项目档案（旧实现与蒸馏触发器构成死循环泵）。
    _RETRY_DOC_TITLE = "[待重试·一进多出] 未入池的对话原文"

    def _defer_to_archive(self, text: str, intent: str) -> None:
        """一进多出失败：只记结构化日志与 ndjson（不落档案、不进六引擎）。

        聊天链路的重试由 flush WAL（pending_ingest.ndjson）兜底，无需档案中转。
        """
        import json as _json

        try:
            from core.structured_log import log_event

            log_event("chat_ingest_deferred", {"intent": intent,
                                               "chars": len(str(text or "")),
                                               "preview": str(text or "")[:200]})
            data_dir().mkdir(parents=True, exist_ok=True)
            with (data_dir() / "ingest_deferred.ndjson").open("a", encoding="utf-8") as fh:
                fh.write(_json.dumps({
                    "ts": int(time.time() * 1000), "intent": intent,
                    "content": str(text or "")[:4000],
                }, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001  日志失败也不阻断对话
            self.last_error = f"defer_to_archive failed: {type(exc).__name__}: {exc}"

    def _agent_id(self) -> str:
        """ChatEngine 注册的 Agent id（供抽取器标注来源）。"""
        return str(getattr(self._agent, "agent_id", "") or "chat-bot")

    @property
    def status(self) -> dict[str, Any]:
        return {
            "running": self.running, "host": self._host, "port": self._port,
            "sessions": len(self._sessions.all_sessions()),
            "queues": {key: value.qsize() for key, value in self._queues.items()},
            "last_error": self.last_error,
        }
