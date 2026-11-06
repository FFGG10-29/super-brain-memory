"""scripts/migrate_v2_to_v3.py —— 把 v2 存量记忆无损转成 v3 事件（待显卡机数据到位后运行）。

    .venv\\Scripts\\python.exe scripts/migrate_v2_to_v3.py --legacy-db data/relational.db --root data/v3

- 旧库**只读**打开，绝不原地改；含 is_active=0 的封存记录一并迁移（双时态不丢）；
- event_id = 原 uuid7 id，保留 tx_from / valid_from_ms / PAD / tier / pyramid；
- 写入新 journal（group commit + fsync）→ inbox.enqueue 幂等 → 启动 v3 后 Worker 重放投影；
- 派生库（vector.lance / graph.kuzu / kv.lmdb / timeseries.db）不搬文件，由事件重放重建。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from v3.inbox import Inbox  # noqa: E402
from v3.journal import V3Journal  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="v2 → v3 记忆迁移")
    parser.add_argument("--legacy-db", default="data/relational.db")
    parser.add_argument("--root", default="data/v3")
    parser.add_argument("--device-id", default="migrated")
    args = parser.parse_args()

    legacy = Path(args.legacy_db)
    if not legacy.is_absolute():
        legacy = (ROOT / legacy).resolve()
    root = Path(args.root)
    if not root.is_absolute():
        root = (ROOT / root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if not legacy.exists():
        print(f"旧库不存在：{legacy}")
        return 1

    con = sqlite3.connect(f"file:{legacy.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    table = "neurons" if "neurons" in tables else "neuron"
    if table not in tables:
        print("旧库里找不到 neurons 表")
        return 1
    rows = [dict(row) for row in con.execute(f"SELECT * FROM {table} ORDER BY tx_from")]
    con.close()
    print(f"旧库记忆 {len(rows)} 条（含封存 {sum(1 for r in rows if not r.get('is_active', 1))} 条）")

    journal = V3Journal(root / "event-log", device_id=args.device_id)
    inbox = Inbox(root / "v3.db")
    migrated = 0
    for row in rows:
        try:
            payload = json.loads(str(row.get("payload", "{}")))
        except (json.JSONDecodeError, TypeError):
            payload = {}
        content = str(payload.get("content", ""))
        if not content:
            continue
        event = {
            "event_id": str(row.get("id")),
            "type": "ingest",
            "hlc": str(row.get("tx_from") or ""),
            "ts_ms": int(row.get("valid_from_ms") or 0),
            "agent_id": str(row.get("agent_id") or "migrated"),
            "namespace": str(row.get("namespace") or "private"),
            "content": content,
            "legacy_neuron": row,
        }
        journal.append_raw(event)
        if inbox.enqueue(event):
            migrated += 1
    print(f"已生成 {migrated} 条 v3 事件 → {journal.path}")
    print("下一步：启动 v3（Worker 会自动重放并重建六库投影），然后校验新旧 count 一致。")
    inbox.close()
    journal.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
