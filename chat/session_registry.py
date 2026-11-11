"""chat.session_registry —— 会话状态持久化（C10 / R21）。

独立 `chat/sessions.db`，**不改主库 schema**；建库时同步建 `schema_version` 表，
低版本走迁移函数链，高版本拒绝启动并提示升级客户端。
内存里维护 `session_id -> Set[connection]` 供多设备 fan-out。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from core.ids import new_uuid7
from core.runtime_paths import user_dir

__all__ = ["SCHEMA_VERSION", "SessionRegistry"]

SCHEMA_VERSION = 1


def _now() -> int:
    return int(time.time() * 1000)


class SessionRegistry:
    def __init__(self, db_path: str | Path | None = None) -> None:
        path = Path(db_path) if db_path else user_dir() / "chat" / "sessions.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._connections: dict[str, set[Any]] = {}
        self._ensure_schema()

    # ── schema 版本与迁移链（R21）─────────────────────────────────────────
    def _ensure_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version "
                "(version INTEGER PRIMARY KEY, applied_at_ms INTEGER NOT NULL)"
            )
            row = self._conn.execute(
                "SELECT MAX(version) AS v FROM schema_version"
            ).fetchone()
            current = int(row["v"]) if row and row["v"] is not None else 0
            if current == 0:
                self._create_v1()
                self._conn.execute(
                    "INSERT INTO schema_version(version, applied_at_ms) VALUES (?, ?)",
                    (SCHEMA_VERSION, _now()),
                )
                return
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"chat/sessions.db 版本 {current} 高于客户端 {SCHEMA_VERSION}，请升级客户端"
                )
            chain = {1: self._migrate_v1_to_v2}  # W9 加字段时在此续链
            while current < SCHEMA_VERSION:
                migrate = chain.get(current)
                if migrate is None:
                    raise RuntimeError(f"缺少 v{current} → v{current + 1} 的迁移函数")
                migrate()
                current += 1
                self._conn.execute(
                    "INSERT INTO schema_version(version, applied_at_ms) VALUES (?, ?)",
                    (current, _now()),
                )

    def _create_v1(self) -> None:
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "session_id TEXT PRIMARY KEY, created_at_ms INTEGER NOT NULL, "
            "last_active_at_ms INTEGER NOT NULL, device_list TEXT NOT NULL DEFAULT '[]')"
        )

    def _migrate_v1_to_v2(self) -> None:
        """占位：v2 加字段时在这里 ALTER TABLE，不删库重建。"""
        return None

    # ── 会话 CRUD ─────────────────────────────────────────────────────────
    def create(self) -> str:
        session_id = new_uuid7()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO sessions(session_id, created_at_ms, last_active_at_ms, device_list) "
                "VALUES (?, ?, ?, ?)", (session_id, _now(), _now(), "[]"),
            )
        return session_id

    def exists(self, session_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        return row is not None

    def restore(self, session_id: str) -> bool:
        """断线重连 / 多设备接入：会话存在则恢复，不存在由调用方决定新建。"""
        return bool(session_id) and self.exists(session_id)

    def all_sessions(self) -> list[str]:
        with self._lock:
            return [str(row["session_id"]) for row in self._conn.execute(
                "SELECT session_id FROM sessions ORDER BY created_at_ms"
            )]

    def touch(self, session_id: str, device_id: str = "") -> None:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT device_list FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            devices: list[str] = []
            if row and row["device_list"]:
                try:
                    devices = [str(x) for x in json.loads(row["device_list"])]
                except (ValueError, TypeError):
                    devices = []
            if device_id and device_id not in devices:
                devices.append(device_id)
            self._conn.execute(
                "UPDATE sessions SET last_active_at_ms=?, device_list=? WHERE session_id=?",
                (_now(), json.dumps(devices, ensure_ascii=False), session_id),
            )

    def devices(self, session_id: str) -> list[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT device_list FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        if not row or not row["device_list"]:
            return []
        try:
            return [str(x) for x in json.loads(row["device_list"])]
        except (ValueError, TypeError):
            return []

    # ── 在线连接 fan-out（内存，重启后可重连恢复）────────────────────────
    def add_connection(self, session_id: str, connection: Any) -> None:
        with self._lock:
            self._connections.setdefault(session_id, set()).add(connection)

    def remove_connection(self, session_id: str, connection: Any) -> None:
        with self._lock:
            connections = self._connections.get(session_id)
            if connections is not None:
                connections.discard(connection)
                if not connections:
                    self._connections.pop(session_id, None)

    def connections(self, session_id: str) -> list[Any]:
        with self._lock:
            return list(self._connections.get(session_id, set()))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
