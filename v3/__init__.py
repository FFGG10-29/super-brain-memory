"""v3 —— 事件驱动记忆内核（与 v2 并行，可回退）。

在线：journal(group commit + fsync) → inbox(pending) → 200 accepted
后台：Worker claim(lease/CAS/幂等) → 认知全链路 → 六库投影 → 广播
"""
from __future__ import annotations

__all__ = ["Inbox", "V3Alarm", "V3Journal", "V3Worker", "V3Daemon"]
