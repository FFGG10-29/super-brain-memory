"""sync —— 同步平面：GitHub 私有库事件总线（v2.1）。

目录布局（私库内）：journal/devices/{device_id}/{yyyymmdd}.seq.ndjson
每设备只写自己目录 → Git 合并退化为纯新增，结构性免冲突。
"""

from __future__ import annotations
