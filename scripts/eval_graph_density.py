"""P4.1 前置评估：上线激活扩散前先看 Kùzu 图密度。

用法：.venv\\Scripts\\python.exe scripts\\eval_graph_density.py
平均度数 < 2 时提示先跑 `graph.backfill`（W4 补边），扩散首发降 1 跳。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brain.hyperedge_backfill import graph_density  # noqa: E402
from config import get_config  # noqa: E402
from main import build_storage  # noqa: E402


def main() -> int:
    config = get_config()
    storage = build_storage(config)
    try:
        density = graph_density(storage.graph, config)
        print(json.dumps(density.as_dict(), ensure_ascii=False, indent=2))
        if density.sparse:
            print("[WARN] 图平均度数低于阈值：扩散会大面积为空，"
                  "请先调用 graph.backfill 补边（W4），扩散首发降 1 跳。")
        return 0
    finally:
        close = getattr(storage, "close", None)
        if callable(close):
            close()


if __name__ == "__main__":
    raise SystemExit(main())
