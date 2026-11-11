"""v3.daemon —— 在线受理面：HTTP + WebSocket，进程内**没有**模型、向量、图谱引擎。

POST /v1/events  → journal(group commit + fsync) → inbox(pending) → 闹钟 → 202 accepted
GET  /v1/memories /v1/graph /v1/health /  → 只读 SQLite 投影（event_id 增量由 WS 推）
WS   /ws → 广播 node_created / node_rejected / superseded / pulse

两种模式：
- thread（本机验证）：Daemon 内起 Worker 线程，Worker 独占六引擎；
- process（正式形态）：ProcessSupervisor 拉起 v3.worker_main 独立进程，Daemon 只读。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from config import BrainConfig, get_config
from main import build_app
from v3.inbox import Inbox
from v3.journal import V3Journal
from v3.notify import V3Alarm
from v3.rpc import RpcHandler
from v3.supervisor import ProcessSupervisor, SpawnSpec
from v3.worker import V3Worker

__all__ = ["V3Daemon", "WebSocketHub"]

_WEB_DIR = Path(__file__).resolve().parent / "web"


class WebSocketHub:
    """线程安全的 WS 广播：后台 asyncio loop，Worker 用 broadcast 推增量。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 8767, *, rpc: Any | None = None) -> None:
        self.host, self.port = host, port
        self.rpc = rpc
        self._loop = asyncio.new_event_loop()
        self._clients: set[Any] = set()
        self._thread = threading.Thread(target=self._run, name="v3-ws", daemon=True)
        self._started = threading.Event()
        self._async_stop: asyncio.Event | None = None
        self._server: Any = None

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self) -> None:
        import websockets  # noqa: PLC0415

        async def handler(websocket: Any) -> None:
            self._clients.add(websocket)
            try:
                async for raw in websocket:
                    try:
                        message = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(message, dict):
                        continue
                    kind = str(message.get("type", ""))
                    if kind == "rpc" and self.rpc is not None:
                        # 同步业务逻辑放到线程池，避免阻塞 WS 事件循环
                        result = await asyncio.to_thread(self.rpc.dispatch, message)
                        await websocket.send(json.dumps(
                            {"type": "rpc_result", "id": message.get("id"), **result},
                            ensure_ascii=False, default=str,
                        ))
                    elif kind == "pulse":
                        self.broadcast({"type": "pulse", **{
                            k: v for k, v in message.items() if k != "type"
                        }})
            finally:
                self._clients.discard(websocket)

        self._async_stop = asyncio.Event()
        async with websockets.serve(handler, self.host, self.port) as server:
            self._server = server
            self._started.set()
            await self._async_stop.wait()

    def start(self, *, wait_s: float = 2.0) -> None:
        self._thread.start()
        self._started.wait(timeout=wait_s)

    def broadcast(self, payload: dict[str, Any]) -> None:
        if not self._loop.is_running() or not self._clients:
            return
        message = json.dumps(payload, ensure_ascii=False, default=str)

        async def send_all() -> None:
            for client in list(self._clients):
                try:
                    await client.send(message)
                except Exception:  # noqa: BLE001
                    self._clients.discard(client)

        asyncio.run_coroutine_threadsafe(send_all(), self._loop)

    def stop(self) -> None:
        """优雅关闭：让 serve 的 async with 正常退出，避免 loop 关闭后残留协程告警。"""
        if self._async_stop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._async_stop.set)
        if self._thread.is_alive():
            self._thread.join(timeout=3.0)
        if not self._loop.is_closed():
            self._loop.close()


class V3Daemon:
    def __init__(
        self,
        root: Path,
        *,
        config: BrainConfig | None = None,
        http_port: int = 8768,
        ws_port: int = 8767,
        alarm_port: int | None = None,
        use_llm: bool = True,
        worker_mode: str = "thread",
        auto_start: bool = False,
        app: Any | None = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._config = config or get_config()
        self.worker_mode = worker_mode
        self._projection_db = Path(self._config.storage.sqlite_path)
        self.journal = V3Journal(self.root / "event-log", device_id=self._config.sync.device_id)
        self.inbox = Inbox(self.root / "v3.db")
        # Honker 只作可选加速闹钟；默认关闭，避免扩展事务初始化进入在线受理路径
        self.alarm = V3Alarm(port=alarm_port, peer_file=self.root / "worker.port")
        self.hub = WebSocketHub(port=ws_port)
        self.rpc = RpcHandler(self)
        self.hub.rpc = self.rpc
        # 无头后端可注入外部 build_app()：MCP 网关与 v3 Worker 共用同一份六引擎，
        # 保证 Kùzu 单写者 + 同一 SQLite 主库（数据目录天然统一）。
        self.app = app
        self.worker: V3Worker | None = None
        self.supervisor: ProcessSupervisor | None = None
        self._use_llm = use_llm
        self._http_port = http_port
        self._http: ThreadingHTTPServer | None = None
        self._http_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        if auto_start:
            self.start()

    # ── 在线受理（不等待任何计算）────────────────────────────────────────
    def handle_event(self, content: str, *, agent_id: str = "default",
                     namespace: str = "topic:default") -> dict[str, Any]:
        content = str(content).strip()
        if len(content) < 4:
            return {"accepted": False, "error": "content too short"}
        started = time.perf_counter()
        # ★ 新写入默认带空间标记：非 topic: 前缀一律归默认空间，
        #   journal 记录与最终落库 namespace 保持一致，避免审计错位。
        ns = str(namespace or "topic:default")
        if not ns.startswith("topic:"):
            ns = "topic:default"
        event = self.journal.append(content, agent_id=agent_id, namespace=ns)
        self.inbox.enqueue(event)
        self.alarm.ring(str(event["event_id"]))
        cost_ms = (time.perf_counter() - started) * 1000
        return {"accepted": True, "event_id": event["event_id"], "ack_ms": round(cost_ms, 2)}

    # ── 生命周期 ──────────────────────────────────────────────────────────
    def start(self, *, serve_http: bool = True, serve_ws: bool = True) -> None:
        restored = self.journal.reconcile(self.inbox)
        if restored:
            self.hub.broadcast({"type": "reconciled", "count": restored})
        if self.worker_mode == "thread":
            self.app = self.app or build_app(self._config)
            self.worker = V3Worker(self.inbox, self.alarm, self.app, hub=self.hub,
                                   use_llm=self._use_llm)
            self._worker_thread = threading.Thread(target=self.worker.run, name="v3-worker", daemon=True)
            self._worker_thread.start()
        else:
            self.supervisor = ProcessSupervisor()
            self.supervisor.register(SpawnSpec(
                name="model_worker",
                args=["-m", "v3.worker_main", "--root", str(self.root),
                      "--alarm-port", str(self.alarm.port)],
                cwd=str(Path(__file__).resolve().parent.parent),
            ))
            self.supervisor.start()
            self._poll_thread = threading.Thread(target=self._done_poll_loop, name="v3-done-poll", daemon=True)
            self._poll_thread.start()
        if serve_ws:
            self.hub.start()
        if serve_http:
            self._start_http()

    def _start_http(self) -> None:
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                return None

            def _send(self, status: int, payload: Any, *, content_type: str = "application/json") -> None:
                body = payload if isinstance(payload, bytes) else json.dumps(
                    payload, ensure_ascii=False, default=str
                ).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                length = int(self.headers.get("Content-Length", "0"))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    self._send(400, {"error": "invalid json"})
                    return
                if path == "/v1/events":
                    result = daemon.handle_event(
                        str(body.get("content", "")),
                        agent_id=str(body.get("agent_id", "default")),
                        namespace=str(body.get("namespace", "topic:default")),
                    )
                    self._send(202 if result.get("accepted") else 400, result)
                elif path == "/v1/config":
                    self._send(200, daemon.save_client_config(dict(body)))
                elif path == "/v1/rpc":
                    # 通用 RPC 兜底：WS 未连上时，Flutter 与任意 HTTP 客户端
                    # 都能调到 v3 的全部方法（含分层上下文 / 桌面能力 / 模型状态）。
                    from v3.rpc import RpcHandler

                    method = str(body.get("method", ""))
                    args = body.get("args") or {}
                    if not isinstance(args, dict):
                        self._send(400, {"error": "args must be an object"})
                        return
                    reply = RpcHandler(daemon).dispatch({"method": method, "args": args})
                    if not reply.get("ok"):
                        self._send(400, {"error": str(reply.get("error") or "RPC failed")})
                        return
                    result = reply.get("result")
                    if isinstance(result, dict):
                        self._send(200, result)
                    elif isinstance(result, list):
                        self._send(200, {"items": result, "result": result})
                    else:
                        self._send(200, {"result": result})
                else:
                    self._send(404, {"error": "not found"})

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path
                if path in ("/", "/index.html"):
                    self._send(200, (_WEB_DIR / "index.html").read_bytes(), content_type="text/html; charset=utf-8")
                elif path in ("/nebula.js", "/app.js"):
                    self._send(200, (_WEB_DIR / "nebula.js").read_bytes(), content_type="text/javascript; charset=utf-8")
                elif path == "/v1/health":
                    self._send(200, daemon.health())
                elif path == "/v1/memories":
                    limit = int(parse_qs(parsed.query).get("limit", ["100"])[0])
                    self._send(200, {"memories": daemon.memories(limit=limit)})
                elif path == "/v1/graph":
                    self._send(200, daemon.graph())
                elif path == "/v1/recall":
                    query = parse_qs(parsed.query).get("q", [""])[0]
                    limit = int(parse_qs(parsed.query).get("limit", ["8"])[0])
                    self._send(200, daemon.recall(query, limit=limit))
                elif path == "/v1/config":
                    self._send(200, daemon.client_config())
                else:
                    self._send(404, {"error": "not found"})

        self._http = ThreadingHTTPServer(("127.0.0.1", self._http_port), Handler)
        self._http_thread = threading.Thread(target=self._http.serve_forever, name="v3-http", daemon=True)
        self._http_thread.start()

    def _done_poll_loop(self) -> None:
        """进程分离模式：Worker 独立进程无法直接碰 Daemon 的 WS hub，由这里轮询 inbox
        的完成状态并广播增量（低频 0.3s，通知丢了也收敛）。"""
        after = 0
        while not self._stop_event.wait(0.3):
            for row in self.inbox.recent_done(after_row_id=after):
                after = max(after, int(row["row_id"]))
                try:
                    payload = json.loads(str(row.get("payload") or "{}"))
                except json.JSONDecodeError:
                    payload = {}
                self.hub.broadcast({
                    "type": "node_created",
                    "event_id": row["event_id"],
                    "id": payload.get("legacy_neuron", {}).get("id", row["event_id"]),
                    "content": row["content"],
                    "memory_type": payload.get("memory_type", "fact"),
                    "tier": payload.get("tier", "buffer"),
                    "pad": payload.get("pad", (0, 0, 0)),
                    "merge": payload.get("merge", "add"),
                })

    # ── 只读视图（直读 SQLite 投影，不开六引擎）─────────────────────────
    def _rows(self, *, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        if not self._projection_db.exists():
            return []
        try:
            con = sqlite3.connect(f"file:{self._projection_db.as_posix()}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            if limit and limit > 0:
                cur = con.execute(
                    "SELECT * FROM neurons WHERE is_active=1 LIMIT ? OFFSET ?",
                    (int(limit), int(offset)),
                )
            else:
                cur = con.execute("SELECT * FROM neurons WHERE is_active=1")
            rows = cur.fetchall()
            con.close()
        except sqlite3.Error:
            return []
        return [dict(row) for row in rows]

    def active_count(self) -> int:
        """全量渲染的总节点数（只读投影，不加载模型）。"""
        if not self._projection_db.exists():
            return 0
        try:
            con = sqlite3.connect(f"file:{self._projection_db.as_posix()}?mode=ro", uri=True)
            total = con.execute("SELECT COUNT(*) FROM neurons WHERE is_active=1").fetchone()[0]
            con.close()
            return int(total)
        except sqlite3.Error:
            return 0

    def graph_chunk(self, *, offset: int = 0, limit: int = 500) -> dict[str, Any]:
        """全量渲染的分批节点：每批最多 500 个，零边计算。

        默认 graph() 只取 200 条并做 O(n²) 2-gram 相似度连边；3000+ 节点时
        那一步会卡死。全量模式改走这里，边与细节由前端按 LOD 决定是否渲染。
        """
        rows = self._rows(limit=limit, offset=offset)
        nodes = [
            {
                "id": str(row.get("id")),
                "content": self._content(row),
                "memory_type": row.get("memory_type", "fact"),
                "tier": row.get("tier", "buffer"),
                "pad": (row.get("pad_p", 0.0), row.get("pad_a", 0.0), row.get("pad_d", 0.0)),
                "is_emotion": row.get("memory_type") == "emotion",
                "confidence": row.get("confidence", 0.5),
                "namespace": str(row.get("namespace") or "private"),
            }
            for row in rows
        ]
        return {
            "source": "projection-chunk",
            "nodes": nodes,
            "total": self.active_count(),
            "offset": offset,
            "limit": limit,
        }

    @staticmethod
    def _content(row: dict[str, Any]) -> str:
        try:
            return str(json.loads(str(row.get("payload", "{}"))).get("content", ""))
        except (json.JSONDecodeError, TypeError):
            return ""

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "worker_mode": self.worker_mode,
            "inbox": self.inbox.counts(),
            "journal_last": self.journal.last_event_id(),
            "journal_events": len(self.journal.iter_events()),
            "worker": (
                {"id": self.worker.worker_id, "processed": self.worker.processed,
                 "failed": self.worker.failed}
                if self.worker else {"mode": "separate-process"}
            ),
            "http_port": self._http_port,
            "ws_port": self.hub.port,
            "alarm_port": self.alarm.port,
        }

    def memories(self, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        return [
            {
                "id": row.get("id"),
                "content": self._content(row),
                "memory_type": row.get("memory_type"),
                "tier": row.get("tier"),
                "pad": (row.get("pad_p"), row.get("pad_a"), row.get("pad_d")),
                "is_active": row.get("is_active"),
            }
            for row in self._rows(limit=limit, offset=offset)
        ]

    def recall(self, query: str, *, limit: int = 8,
               namespaces: tuple[str, ...] | list[str] = ()) -> dict[str, Any]:
        """轻量召回：字符 2-gram 余弦（只读投影，不加载模型）。Flutter 用结果发射光脉冲。

        ★ A 包：namespaces 非空时按 topic 分区下推过滤，2-gram 只计算候选类，
        扫描/打分开销按类数成比例下降（7 类下约 7 倍）。空元组 = Bot 全量可读。
        """
        ns = {str(x) for x in namespaces}
        # 按类召回时取全量再先过滤：旧版固定 LIMIT 500（无 ORDER BY，实际是最早 500 行），
        # 新写入的记忆永远进不了候选。先按 namespace 收窄，2-gram 只算同类，开销反而更低。
        rows = self._rows(limit=0 if ns else 500)
        if ns:
            rows = [r for r in rows if str(r.get("namespace") or "private") in ns]
        vector = _ngram(query)
        scored = []
        for row in rows:
            content = self._content(row)
            score = _cosine(vector, _ngram(content))
            if score > 0:
                scored.append({
                    "id": str(row.get("id")), "content": content,
                    "score": round(score, 4), "channels": ["fts"],
                    "memory_type": row.get("memory_type", "fact"),
                })
        scored.sort(key=lambda item: item["score"], reverse=True)
        return {"query": query, "hits": scored[:limit], "degraded": True}

    DEFAULT_CLIENT_CONFIG: dict[str, Any] = {
        "nebula": {
            "cluster_radius": 330.0, "galaxy_thickness": 42.0,
            "spin_speed": 0.00035, "focal_length": 900.0,
            "star_size": 3.2, "bloom": True, "auto_rotate": True,
        }
    }

    def _client_config_path(self) -> Path:
        return self.root / "nebula_prefs.json"

    def client_config(self) -> dict[str, Any]:
        data = json.loads(json.dumps(self.DEFAULT_CLIENT_CONFIG))
        path = self._client_config_path()
        if path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                for key, value in saved.items():
                    if isinstance(value, dict) and isinstance(data.get(key), dict):
                        data[key].update(value)
                    else:
                        data[key] = value
            except (json.JSONDecodeError, OSError):
                pass
        data["server"] = {
            "http_port": self._http_port, "ws_port": self.hub.port,
            "worker_mode": self.worker_mode, "alarm_port": self.alarm.port,
        }
        return data

    def save_client_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.client_config()
        for key, value in patch.items():
            if key == "server":
                continue
            if isinstance(value, dict) and isinstance(current.get(key), dict):
                current[key].update(value)
            else:
                current[key] = value
        current.pop("server", None)
        self._client_config_path().write_text(
            json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {"ok": True, "config": self.client_config()}

    def graph(self) -> dict[str, Any]:
        """只读投影：字符 2-gram 余弦≥阈值画突触线（不加载 embedder）。"""
        rows = self._rows(limit=200)
        nodes = [
            {
                "id": str(row.get("id")),
                "label": self._content(row)[:18],
                "content": self._content(row),
                "memory_type": row.get("memory_type", "fact"),
                "tier": row.get("tier", "buffer"),
                "pad": (row.get("pad_p", 0.0), row.get("pad_a", 0.0), row.get("pad_d", 0.0)),
                "is_emotion": row.get("memory_type") == "emotion" or abs(float(row.get("pad_a") or 0)) > 0.6,
                "confidence": row.get("confidence", 0.5),
                "namespace": str(row.get("namespace") or "private"),
            }
            for row in rows
        ]
        vectors = [_ngram(node["content"]) for node in nodes]
        edges = []
        for i, first in enumerate(nodes):
            for j in range(i + 1, len(nodes)):
                score = _cosine(vectors[i], vectors[j])
                if score >= self._config.activation.similarity_threshold:
                    edges.append({"source": first["id"], "target": nodes[j]["id"],
                                  "weight": round(score, 4), "kind": "semantic"})
        return {"source": "char-2gram", "degraded": True, "nodes": nodes, "edges": edges}

    def stop(self) -> None:
        self._stop_event.set()
        if self.worker is not None:
            self.worker.stop()
        if self.supervisor is not None:
            self.supervisor.stop()
        if self._http is not None:
            self._http.shutdown()
            self._http.server_close()
        self.hub.stop()
        self.inbox.close()
        self.journal.close()
        self.alarm.close()
        if self.app is not None:
            self.app.close()


def _ngram(text: str) -> dict[str, float]:
    cleaned = "".join(str(text).split())
    vector: dict[str, float] = {}
    for index in range(max(1, len(cleaned) - 1)):
        key = cleaned[index : index + 2]
        vector[key] = vector.get(key, 0.0) + 1.0
    return vector


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    a, b = (left, right) if len(left) <= len(right) else (right, left)
    dot = sum(value * b.get(key, 0.0) for key, value in a.items())
    norm_a = sum(value * value for value in a.values()) ** 0.5
    norm_b = sum(value * value for value in b.values()) ** 0.5
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0
