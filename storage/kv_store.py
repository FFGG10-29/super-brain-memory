"""storage.kv_store —— LMDB 画像槽位 / 精确键直读。

只存单键确定性数据：profile 槽位、偏好、去重精确键、限流计数器。
不做范围扫描优化、不做聚合（那是 SQLite / DuckDB 的活）。

并发模型：LMDB 官方模型是"一个进程一个 Environment"，事务（txn）天然线程独立。
因此这里共享 env、每线程每操作独立 txn —— 与架构"每引擎 thread-local 连接"的目的
（禁止跨线程共享连接对象）一致，且避免同路径多 env 的文件锁开销。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import lmdb

from core.errors import StorageError
from core.interfaces import HealthStatus, KVRepository

__all__ = ["LmdbKVStore"]


class LmdbKVStore(KVRepository):
    """LMDB 实现。bucket 即命名的子库（named database）。"""

    name = "kv:lmdb"

    def __init__(self, path: str | Path, *, map_size_mb: int = 256, max_buckets: int = 64) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._map_size = int(map_size_mb) * 1024 * 1024
        self._env = lmdb.open(
            str(self._path),
            map_size=self._map_size,
            max_dbs=int(max_buckets),
            subdir=True,
            sync=False,
        )
        self._buckets: dict[str, int] = {}
        self._bucket("default")

    def _bucket(self, bucket: str) -> int:
        cached = self._buckets.get(bucket)
        if cached is not None:
            return cached
        handle = self._env.open_db(bucket.encode("utf-8"))
        self._buckets[bucket] = handle
        return handle

    # ── 基本读写 ──────────────────────────────────────────────────────────
    def put(self, bucket: str, key: str, value: bytes) -> None:
        if not isinstance(value, (bytes, bytearray)):
            raise StorageError("LMDB 只接受 bytes；JSON 请用 put_json()")
        with self._env.begin(write=True, db=self._bucket(bucket)) as txn:
            txn.put(key.encode("utf-8"), bytes(value))

    def get(self, bucket: str, key: str) -> bytes | None:
        with self._env.begin(db=self._bucket(bucket)) as txn:
            raw = txn.get(key.encode("utf-8"))
        return None if raw is None else bytes(raw)

    def delete(self, bucket: str, key: str) -> None:
        with self._env.begin(write=True, db=self._bucket(bucket)) as txn:
            txn.delete(key.encode("utf-8"))

    def scan_prefix(self, bucket: str, prefix: str, *, limit: int = 100) -> list[tuple[str, bytes]]:
        needle = prefix.encode("utf-8")
        out: list[tuple[str, bytes]] = []
        with self._env.begin(db=self._bucket(bucket)) as txn:
            cursor = txn.cursor()
            if cursor.set_range(needle):
                for raw_key, raw_value in cursor:
                    if not raw_key.startswith(needle):
                        break
                    out.append((raw_key.decode("utf-8"), bytes(raw_value)))
                    if len(out) >= limit:
                        break
        return out

    # ── 计数器（丘脑按 Agent 限流 / 访问次数）────────────────────────────
    def incr(self, bucket: str, key: str, *, ttl_ms: int | None = None) -> int:
        """返回自增后的值；ttl 过期或从未设置时从 1 重新计数。"""
        now = int(time.time() * 1000)
        with self._env.begin(write=True, db=self._bucket(bucket)) as txn:
            raw = txn.get(key.encode("utf-8"))
            current = 0
            expired = True
            if raw is not None:
                try:
                    data = json.loads(raw.decode("utf-8"))
                    current = int(data.get("n", 0))
                    expired = ttl_ms is not None and now >= int(data.get("exp", 0))
                except (ValueError, TypeError):
                    current, expired = 0, True
            value = 1 if expired or current == 0 else current + 1
            payload = {"n": value, "exp": (now + int(ttl_ms)) if ttl_ms else 0}
            txn.put(key.encode("utf-8"), json.dumps(payload).encode("utf-8"))
        return value

    # ── 健康检查 ──────────────────────────────────────────────────────────
    def health(self) -> HealthStatus:
        started = time.perf_counter()
        try:
            stats = self._env.stat()
            info = self._env.info()
            latency = (time.perf_counter() - started) * 1000
            return HealthStatus(
                engine=self.name,
                ok=True,
                latency_ms=latency,
                version=str(info.get("map_size", "")) if isinstance(info, dict) else "",
                detail=f"buckets={len(self._buckets)}",
                extra={
                    "path": str(self._path),
                    "entries": int(stats.get("entries", 0)),
                    "map_size_mb": self._map_size // (1024 * 1024),
                },
            )
        except Exception as exc:  # noqa: BLE001
            return HealthStatus(
                engine=self.name,
                ok=False,
                latency_ms=(time.perf_counter() - started) * 1000,
                detail=f"{type(exc).__name__}: {exc}",
            )

    def close(self) -> None:
        try:
            self._env.close()
        except Exception:  # noqa: BLE001
            pass
