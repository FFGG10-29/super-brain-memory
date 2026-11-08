"""core.wal_maintenance —— SQLite WAL 收缩（长时挂机保护）。

WAL 模式下频繁写入会让 `-wal` 文件持续增长；即便 `wal_autocheckpoint`
已经把页合并回主库，文件本身也不会自动变短。定期执行
`PRAGMA wal_checkpoint(TRUNCATE)` 把文件截回 0，避免挂机一个月后
出现几百 MB 甚至 GB 级的 -wal。

用**独立短连接**执行，不碰业务层的 thread-local 连接池，也不需要在
RelationalDB 里加锁；SQLite 自己处理并发，遇到活动读事务会返回 busy=1，
下次循环再试即可，不影响数据。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

__all__ = ["checkpoint_wal", "checkpoint_data_dir"]


def checkpoint_wal(path: str | Path, *, truncate: bool = True, timeout: float = 5.0) -> dict[str, Any]:
    """对单个 SQLite 库执行 WAL checkpoint，返回结果三元组的可读字典。"""
    db = Path(path)
    result: dict[str, Any] = {"path": str(db), "exists": db.exists(), "ok": False}
    if not db.exists():
        return result
    mode = "TRUNCATE" if truncate else "PASSIVE"
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db), timeout=timeout)
        row = conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
        # row = (busy, log_pages, checkpointed_pages)；busy=0 表示完整完成
        result.update({
            "ok": bool(row and row[0] == 0),
            "busy": int(row[0]) if row else -1,
            "wal_pages": int(row[1]) if row and len(row) > 1 else 0,
            "checkpointed_pages": int(row[2]) if row and len(row) > 2 else 0,
        })
    except (sqlite3.Error, OSError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
    return result


def checkpoint_data_dir(main_db: str | Path, *, extra: tuple[str | Path, ...] = ()) -> list[dict[str, Any]]:
    """主库 + 显式给出的附属库（v3 inbox 等）一起收缩。"""
    targets: list[str | Path] = [main_db, *extra]
    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    for target in targets:
        key = str(Path(target)).lower()
        if key in seen or not Path(target).exists():
            continue
        seen.add(key)
        results.append(checkpoint_wal(target))
    return results
