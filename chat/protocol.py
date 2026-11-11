"""chat.protocol —— 8769 纯消息流的帧格式（客户端只说话，不写 method/params）。"""
from __future__ import annotations

import json
from typing import Any

__all__ = ["decode", "encode"]

CLIENT_FRAMES = frozenset({"hello", "message", "ping"})
SERVER_FRAMES = frozenset({
    "hello_ack", "typing", "reply", "deep_recall_ready", "media_ready", "error", "pong",
})


def encode(kind: str, **fields: Any) -> str:
    """统一帧：{"type": kind, "ts_ms": ..., ...fields}。"""
    import time

    # 帧类型由第一个位置参数唯一决定，业务字段里若误带 type/kind 直接丢弃，避免撞名
    fields.pop("type", None)
    fields.pop("kind", None)
    return json.dumps({"type": kind, "ts_ms": int(time.time() * 1000), **fields},
                      ensure_ascii=False, default=str)


def decode(raw: str | bytes) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {"type": "error", "message": "invalid json"}
    return data if isinstance(data, dict) else {"type": "error", "message": "frame must be object"}
