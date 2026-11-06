"""POC 1：Honker Windows 可用性 + API 签名探测。保留为验收脚本。"""
from __future__ import annotations

import inspect
import tempfile
import threading
import time
from pathlib import Path

import honker

print("honker:", honker.__file__)
for name in ("listen", "transaction", "queue", "stream", "outbox", "run_workers", "query"):
    obj = getattr(honker.Database, name, None)
    print(f"Database.{name}:", inspect.signature(obj) if obj else "missing")
print("Queue methods:", [n for n in dir(honker.Queue) if not n.startswith("_")][:25])
print("Transaction methods:", [n for n in dir(honker.Database.transaction(honker.Database.__new__(honker.Database))) if not n.startswith("_")] if False else "skip")

path = Path(tempfile.mkdtemp(prefix="honker_poc_")) / "poc.db"
db = honker.open(str(path))
print("open ok:", type(db).__name__)
try:
    info = honker.extension_info(db) if hasattr(honker, "extension_info") else None
    print("extension info:", str(info)[:200])
except Exception as exc:  # noqa: BLE001
    print("extension_info:", type(exc).__name__, exc)

# listener：按真实签名尝试
received: list[object] = []
ready = threading.Event()


def listener() -> None:
    try:
        sub = honker.open(str(path))
        sig = inspect.signature(sub.listen)
        print("listener sig:", sig)
        ready.set()
        listener_obj = sub.listen("v3_events")
        print("listener obj:", type(listener_obj).__name__,
              [n for n in dir(listener_obj) if not n.startswith("_")][:20])
        if hasattr(listener_obj, "wait"):
            received.append(listener_obj.wait(timeout=3))
        elif hasattr(listener_obj, "__next__"):
            received.append(next(listener_obj))
    except Exception as exc:  # noqa: BLE001
        received.append(f"ERR: {type(exc).__name__}: {exc}")


thread = threading.Thread(target=listener, daemon=True)
thread.start()
ready.wait(timeout=2)
time.sleep(0.2)
try:
    with db.transaction() as tx:
        print("tx methods:", [n for n in dir(tx) if not n.startswith("_")][:25])
        tx.notify("v3_events", "hello")
    print("notify ok")
except Exception as exc:  # noqa: BLE001
    print("notify failed:", type(exc).__name__, exc)
thread.join(timeout=4)
print("received:", received)
db.close()
