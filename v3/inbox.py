"""v3.inbox —— 唯一真队列（SQLite WAL）+ 租约状态机。

契约：pending → processing(worker_id, lease_until) → done | failed
- claim 用 BEGIN IMMEDIATE + UPDATE ... RETURNING 原子批量抢占（多 Worker 不重复消费）；
- 完成用 CAS（worker_id + status），租约过期后旧 Worker 不能覆盖新进度；
- lease 超时由任意 Worker 顺带原子回收，不依赖守护进程；
- 幂等键是全局 event_id，本地自增 id 只作行号。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

__all__ = ["Inbox"]


class Inbox:
    def __init__(self, db_path: Path, *, busy_timeout_ms: int = 5000) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = db_path
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")   # 可靠性由 journal 兜底，在线不额外刷盘
        self._conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inbox (
                row_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id     TEXT NOT NULL UNIQUE,
                content      TEXT NOT NULL,
                agent_id     TEXT NOT NULL DEFAULT 'default',
                namespace    TEXT NOT NULL DEFAULT 'private',
                status       TEXT NOT NULL DEFAULT 'pending',
                worker_id    TEXT,
                lease_until  REAL,
                attempts     INTEGER NOT NULL DEFAULT 0,
                error        TEXT,
                payload      TEXT NOT NULL DEFAULT '{}',
                created_ms   INTEGER NOT NULL,
                updated_ms   INTEGER NOT NULL
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_inbox_status ON inbox(status)")

    def enqueue(self, event: dict[str, Any]) -> bool:
        """返回 True 表示首次入队；重复 event_id 幂等跳过。"""
        now = int(time.time() * 1000)
        cursor = self._conn.execute(
            """INSERT OR IGNORE INTO inbox
               (event_id, content, agent_id, namespace, status, payload, created_ms, updated_ms)
               VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)""",
            (
                str(event.get("event_id") or event.get("id") or ""),
                str(event.get("content", "")),
                str(event.get("agent_id", "default")),
                str(event.get("namespace", "private")),
                json.dumps(event, ensure_ascii=False, default=str),
                now, now,
            ),
        )
        return cursor.rowcount > 0

    def recycle_expired(self, now: float | None = None) -> int:
        cursor = self._conn.execute(
            """UPDATE inbox SET status='pending', worker_id=NULL, lease_until=NULL, updated_ms=?
               WHERE status='processing' AND lease_until IS NOT NULL AND lease_until < ?""",
            (int(time.time() * 1000), now if now is not None else time.time()),
        )
        return cursor.rowcount

    def claim(self, worker_id: str, *, limit: int = 20, lease_s: float = 60.0) -> list[dict[str, Any]]:
        self.recycle_expired()
        lease_until = time.time() + lease_s
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self._conn.execute(
                """UPDATE inbox
                   SET status='processing', worker_id=?, lease_until=?, attempts=attempts+1,
                       updated_ms=?, error=NULL
                   WHERE row_id IN (SELECT row_id FROM inbox WHERE status='pending'
                                    ORDER BY row_id LIMIT ?)
                   RETURNING row_id, event_id, content, agent_id, namespace, attempts, payload""",
                (worker_id, lease_until, int(time.time() * 1000), limit),
            ).fetchall()
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return [dict(row) for row in rows]

    def heartbeat(self, event_id: str, worker_id: str, *, lease_s: float = 60.0) -> bool:
        cursor = self._conn.execute(
            """UPDATE inbox SET lease_until=?, updated_ms=?
               WHERE event_id=? AND worker_id=? AND status='processing'""",
            (time.time() + lease_s, int(time.time() * 1000), event_id, worker_id),
        )
        return cursor.rowcount > 0

    def finish(self, event_id: str, worker_id: str, *, ok: bool, error: str = "") -> bool:
        """CAS 完成：租约被别人抢走时返回 False，调用方必须丢弃本次结果。"""
        cursor = self._conn.execute(
            """UPDATE inbox SET status=?, error=?, worker_id=NULL, lease_until=NULL, updated_ms=?
               WHERE event_id=? AND worker_id=? AND status='processing'""",
            ("done" if ok else "failed", error[:500], int(time.time() * 1000), event_id, worker_id),
        )
        return cursor.rowcount > 0

    def recent_done(self, *, after_row_id: int = 0) -> list[dict[str, Any]]:
        """进程分离模式下由 Daemon 轮询，把已完成事件广播给前端。"""
        rows = self._conn.execute(
            """SELECT row_id, event_id, content, payload FROM inbox
               WHERE status IN ('done','failed') AND row_id > ? ORDER BY row_id LIMIT 200""",
            (after_row_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def counts(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT status, count(*) AS n FROM inbox GROUP BY status").fetchall()
        return {row["status"]: row["n"] for row in rows}

    def peek(self, status: str = "pending", *, limit: int = 10) -> list[dict[str, Any]]:
        """队列明细（提炼进度卡可视化）：来源 agent / 空间 / 字数 / 内容预览。"""
        rows = self._conn.execute(
            """SELECT row_id, agent_id, namespace, content, created_ms, attempts
               FROM inbox WHERE status=? ORDER BY row_id LIMIT ?""",
            (status, int(limit)),
        ).fetchall()
        return [{
            "row_id": int(r["row_id"]),
            "agent_id": str(r["agent_id"]),
            "namespace": str(r["namespace"]),
            "chars": len(str(r["content"] or "")),
            "preview": str(r["content"] or "")[:80],
            "created_ms": int(r["created_ms"]),
            "attempts": int(r["attempts"]),
        } for r in rows]

    def close(self) -> None:
        self._conn.close()
