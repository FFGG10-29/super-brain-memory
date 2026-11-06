"""v3.notify —— 闹钟（只负责"踹一脚"，不是可靠性来源）。

三层：① 同进程 threading.Event ② 跨进程 UDP 唤醒（固定端口）③ Honker NOTIFY（可选加速）。
无论哪层丢失，Worker 的「启动扫描 + drain 到空 + 1s 兜底轮询」都保证最终收敛。
"""
from __future__ import annotations

import os
import socket
import threading
from pathlib import Path
from typing import Any

__all__ = ["V3Alarm"]


class V3Alarm:
    def __init__(self, *, port: int | None = None, channel: str = "v3_events",
                 honker_db_path: str | None = None, peer_file: Path | None = None) -> None:
        self.channel = channel
        self._peer_file = Path(peer_file) if peer_file else None
        self._local = threading.Event()
        self._honker: Any = None
        if honker_db_path:
            try:
                import honker  # noqa: PLC0415

                self._honker = honker.open(honker_db_path)
            except Exception:  # noqa: BLE001  Honker 不可用直接退化
                self._honker = None
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        requested = int(port or os.environ.get("V3_ALARM_PORT", "0"))
        try:
            self._sock.bind(("127.0.0.1", requested))
        except OSError:
            # 端口被占用（例如同机多个进程）时退到随机端口，靠 1s 兜底轮询收敛
            self._sock.bind(("127.0.0.1", 0))
        self._sock.settimeout(1.0)
        self.port = int(self._sock.getsockname()[1])

    def ring(self, event_id: str = "") -> None:
        self._local.set()
        target = self.port
        if self._peer_file is not None and self._peer_file.exists():
            try:
                target = int(self._peer_file.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                pass
        try:
            self._sock.sendto(event_id.encode("utf-8")[:256], ("127.0.0.1", target))
        except OSError:
            pass
        if self._honker is not None:
            try:
                with self._honker.transaction() as tx:
                    tx.notify(self.channel, event_id)
            except Exception:  # noqa: BLE001
                self._honker = None

    def wait(self, *, timeout: float = 1.0) -> bool:
        """返回 True=被唤醒；False=兜底轮询超时（调用方照样扫 pending）。"""
        if self._local.wait(timeout=0.02):
            self._local.clear()
            return True
        try:
            self._sock.recvfrom(256)
            return True
        except (socket.timeout, OSError):
            return False

    def close(self) -> None:
        try:
            self._honker.close()
        except Exception:  # noqa: BLE001
            pass
        self._sock.close()
